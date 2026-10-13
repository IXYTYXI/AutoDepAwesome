"""A single local worker with durable checkpoints and conservative recovery."""

import fcntl
import time

from .errors import DeployError


class Worker:
    def __init__(self, settings, store, source, backend):
        self.settings = settings
        self.store = store
        self.source = source
        self.backend = backend

    def run_once(self):
        """Process one job, or return False when idle or another worker owns the lock.

        Subprocess adapters must pass ``lock_fd`` via ``pass_fds``. The descriptor
        remains locked if the worker dies while a mutating subprocess is alive.
        There is deliberately no expiring lease or automatic timed takeover.
        """
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.settings.state_dir / "worker.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            self.backend.lock_fd = lock.fileno()
            self.source.lock_fd = lock.fileno()
            try:
                job = self.store.next_job()
                maintenance = False
                urgent_recovery = (
                    job is not None
                    and job["status"] == "running"
                    and any(
                        job["execution"].get(key)
                        for key in ("publish_intent", "failure_intent", "resources_released")
                    )
                )
                if not urgent_recovery:
                    boot_id = self.backend.recovery_boot_id()
                    active = self.store.next_active_recovery(boot_id)
                    if active:
                        fresh_boot = active["execution"].get("active_recovery", {}).get("boot_id") != boot_id
                        self._recover_active(active)
                        maintenance = True
                        # Try every previously active release once after boot before
                        # entering a potentially long candidate build. Failed attempts
                        # also persist the boot ID, so one broken app cannot stall work.
                        if fresh_boot:
                            return True
                    retirement = self.store.next_retirement()
                    if retirement:
                        self._retire(retirement)
                        maintenance = True
                if job is None:
                    return maintenance
                interrupted_publish = job["status"] == "running" and job["execution"].get("publish_intent")
                job = self.store.claim(job["id"])
                if job["execution"].get("failure_intent") or job["execution"].get("resources_released"):
                    self._fail(
                        job["id"],
                        DeployError("INTERRUPTED_CLEANUP", "An interrupted candidate cleanup was finalized."),
                        recovered=True,
                    )
                    return True
                if interrupted_publish:
                    self._fail(
                        job["id"],
                        DeployError("INTERRUPTED_PUBLISH", "An interrupted publication was rolled back."),
                        recovered=True,
                    )
                    return True
                self._execute(job["id"])
                return True
            finally:
                self.backend.lock_fd = None
                self.source.lock_fd = None
                # Closing, rather than LOCK_UN, preserves the flock in inherited child descriptors.

    def _recover_active(self, job, required=False):
        job = self.store.begin_active_recovery(job["id"], self.backend.recovery_boot_id())
        try:
            result = self.backend.recover_active(
                job, lambda delta: self.store.checkpoint(job["id"], execution=delta)
            )
        except Exception:
            self.store.finish_active_recovery(job["id"], success=False)
            self.store.event(
                job["id"],
                "recovery",
                "Active containers could not be recovered; release and routes remain unchanged. Recovery will retry with backoff.",
            )
            if required:
                raise DeployError(
                    "ACTIVE_RECOVERY_FAILED", "Previous active containers could not be recovered."
                ) from None
            return
        restarted = result.get("restarted", [])
        self.store.finish_active_recovery(job["id"], success=True, restarted=restarted)
        if restarted:
            self.store.event(
                job["id"], "recovery", "Stopped containers for the active release were restarted and checked."
            )

    def _recover_previous_for_rollback(self, candidate):
        previous_id = candidate["execution"].get("previous_release")
        previous = self.store.active_release(candidate["app_id"])
        if previous_id is None:
            if previous is not None:
                raise DeployError("ACTIVE_RECOVERY_STATE", "The active release changed before rollback.")
            return
        if previous is None or previous["id"] != previous_id:
            raise DeployError("ACTIVE_RECOVERY_STATE", "The expected previous release is no longer active.")
        state = previous["execution"].get("active_recovery", {})
        if (
            state.get("boot_id") == self.backend.recovery_boot_id()
            and state.get("outcome") in {"checking", "failed"}
            and state.get("next_at", 0) > time.time()
        ):
            raise DeployError(
                "ACTIVE_RECOVERY_BACKOFF", "Previous active recovery is waiting for its retry window."
            )
        self._recover_active(previous, required=True)

    def _phase(self, job_id, phase, message):
        self.store.checkpoint(job_id, phase=phase)
        self.store.event(job_id, phase, message)
        return self.store.get(job_id)

    def _execute(self, job_id):
        def checkpoint(delta):
            return self.store.checkpoint(job_id, execution=delta)

        try:
            job = self.store.prepare_context(job_id)
            if not job["snapshot"]:
                job = self._phase(job_id, "source", "Preparing the pinned repository source.")
                snapshot = self.source.prepare(job, checkpoint)
                job = self.store.checkpoint(job_id, phase="prepared", snapshot=snapshot)
            if not job["execution"].get("frozen"):
                job = self._phase(job_id, "freeze", "Freezing deployment inputs.")
                snapshot = self.backend.freeze(job["snapshot"], job, checkpoint)
                job = self.store.checkpoint(
                    job_id, phase="frozen", execution={"frozen": True}, snapshot=snapshot
                )
            job = self.store.reserve(job_id, job["snapshot"])
            services = set(job["snapshot"]["manifest"]["services"])
            if not services.issubset(job["execution"].get("images", {})):
                job = self._phase(job_id, "build", "Building candidate images.")
                images = self.backend.build(job, checkpoint)
                job = self.store.checkpoint(job_id, execution={"images": images}, phase="built")
            job = self._phase(job_id, "start", "Starting the isolated candidate release.")
            self.backend.start(job, checkpoint)
            job = self._phase(job_id, "health", "Checking candidate health.")
            self.backend.health(job)
            job = self._phase(job_id, "https", "Ensuring HTTPS is available.")
            self.backend.ensure_https(job)
            job = self.store.checkpoint(job_id, phase="publish", execution={"publish_intent": True})
            self.store.event(job_id, "publish", "Publishing the candidate release.")
            self.backend.publish(job)
            job = self._phase(job_id, "verify", "Verifying the public HTTPS route.")
            self.backend.verify(job)
            self.store.finish(job_id)
        except Exception as error:
            self._fail(job_id, error)
            return
        # The active pointer and successful job status have already committed together.
        # Recovery bookkeeping is maintenance: it can never roll back a committed success.
        try:
            self.store.begin_active_recovery(job_id, self.backend.recovery_boot_id())
            self.store.finish_active_recovery(job_id, success=True)
        except Exception:
            pass  # A legacy/missing marker is eligible on the next recovery scan.
        # Retirement failure cannot turn the successful new release into a failed job.
        job = self.store.get(job_id)
        self._retire(job)
        self.store.event(job_id, "complete", "Deployment is active and verified.")

    def _retire(self, job):
        pending = self.store.retirement_pending(job)
        for previous_id in list(pending):
            try:
                previous = self.store.get(previous_id)
                if not previous["execution"].get("resources_released"):
                    self.backend.cleanup(previous)
                    self.store.release(previous_id)
                pending.remove(previous_id)
                self.store.checkpoint(
                    job["id"],
                    execution={
                        "retirement_pending": pending,
                        "retirement_retry_at": 0,
                        "previous_retired": not pending,
                    },
                )
            except Exception:
                # A durable cooldown survives worker restarts and lets run_once()
                # become idle instead of spinning on a failed cleanup command.
                self.store.checkpoint(
                    job["id"],
                    execution={"retirement_pending": pending, "retirement_retry_at": time.time() + 60},
                )
                self.store.event(
                    job["id"],
                    "retire",
                    "Previous release cleanup needs attention; resources remain reserved.",
                )
                return

    @staticmethod
    def _error(error):
        if isinstance(error, DeployError):
            return error.as_dict()
        return {
            "code": "DEPLOYMENT_FAILED",
            "message": "Deployment failed; see the current phase.",
            "path": "",
            "suggestion": "Review the deployment configuration and retry with a new key.",
        }

    def _fail(self, job_id, error, recovered=False):
        job = self.store.get(job_id)
        failure = job["execution"].get("failure_error") or self._error(error)
        job = self.store.checkpoint(job_id, execution={"failure_intent": True, "failure_error": failure})
        if job["execution"].get("publish_intent") and not job["execution"].get("rollback_confirmed"):
            try:
                self._recover_previous_for_rollback(job)
                self.store.event(job_id, "rollback", "Restoring the previous public route.")
                self.backend.rollback(job)
                job = self.store.checkpoint(job_id, execution={"rollback_confirmed": True})
            except Exception:
                self.store.fail(
                    job_id,
                    {
                        "code": "ROLLBACK_FAILED",
                        "message": "Rollback could not be confirmed. Candidate resources were retained.",
                        "path": "",
                        "suggestion": "Recover the gateway before deploying this application again.",
                    },
                    recovery=True,
                )
                self.store.event(
                    job_id, "rollback", "Rollback requires manual recovery; all resources remain reserved."
                )
                return
        if not job["execution"].get("resources_released"):
            try:
                self.backend.cleanup(job)
                self.store.release(job_id)
            except Exception:
                self.store.fail(
                    job_id,
                    {
                        "code": "CLEANUP_FAILED",
                        "message": "Candidate cleanup could not be confirmed.",
                        "path": "",
                        "suggestion": "Confirm resource cleanup before deploying this application again.",
                    },
                    recovery=True,
                )
                self.store.event(
                    job_id, "cleanup", "Cleanup requires manual recovery; resources remain reserved."
                )
                return
        self.store.fail(job_id, failure)
        self.store.event(
            job_id,
            "failed",
            "Interrupted publication recovered." if recovered else "Deployment failed safely.",
        )
