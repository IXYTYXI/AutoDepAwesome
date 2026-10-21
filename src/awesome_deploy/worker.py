"""A single local worker with durable checkpoints and conservative recovery."""

from dataclasses import replace
import fcntl
import os
import stat
import time

from .errors import DeployError
from .access import AccessStore, offline_started
from .source import GitSource


class Worker:
    def __init__(self, settings, store, source, backend):
        self.settings = settings
        self.store = store
        self.source = source
        self.backend = backend
        self.access = AccessStore(settings)

    def run_once(self):
        """Process one job, or return False when idle or another worker owns the lock.

        Subprocess adapters must pass ``lock_fd`` via ``pass_fds``. The descriptor
        remains locked if the worker dies while a mutating subprocess is alive.
        There is deliberately no expiring lease or automatic timed takeover.
        """
        self.settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_fd = os.open(self.settings.state_dir / 'worker.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lock_fd, 'a+b') as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise DeployError('UNSAFE_WORKER_LOCK', '部署执行锁的文件身份不安全。')
            os.fchmod(lock.fileno(), 0o600)
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            self.backend.lock_fd = lock.fileno()
            self.source.lock_fd = lock.fileno()
            try:
                from .runtime_environment import EnvironmentFiles
                EnvironmentFiles(self.settings).cleanup()
                if self.settings.secret_backend == 'encrypted':
                    from .vault_service import VaultService
                    vault = VaultService(self.settings, self.store)
                    operation = vault.next_revocation()
                    if operation:
                        self._revoke(vault, operation)
                        return True
                job = self.store.next_job()
                maintenance = False
                urgent_recovery = (
                    job is not None
                    and job["status"] == "running"
                    and any(
                        job["execution"].get(key)
                        for key in ("publish_intent", "failure_intent", "resources_released", "storage_handoff_intent")
                    )
                )
                if not urgent_recovery:
                    offline = self.store.next_offline()
                    if offline:
                        self._offline(offline)
                        return True
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
                try:
                    if not urgent_recovery:
                        self.access.authorize_operation("job", job["id"], force=True)
                    job = self.store.claim(job["id"])
                except DeployError as error:
                    self._fail(job["id"], error)
                    return True
                if job["execution"].get("failure_intent") or job["execution"].get("resources_released"):
                    self._fail(
                        job["id"],
                        DeployError("INTERRUPTED_CLEANUP", "An interrupted candidate cleanup was finalized."),
                        recovered=True,
                    )
                    return True
                if job["execution"].get("storage_handoff_intent"):
                    self._fail(
                        job["id"],
                        DeployError("INTERRUPTED_STORAGE_HANDOFF", "An interrupted storage handoff was rolled back."),
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

    def _revoke(self, vault, operation):
        operation = vault.claim_revocation(operation['id'])
        confirmed = set(operation['execution'].get('confirmed_job_ids', []))
        failed = False
        for job_id in operation['execution']['job_ids']:
            if job_id in confirmed:
                continue
            try:
                job = self.store.get(job_id)
                if job['app_id'] != operation['app_id']:
                    raise DeployError('SECRET_REVOCATION_STATE', '吊销对象身份不匹配。')
                if not job['execution'].get('resources_released'):
                    result = self.backend.revoke_release(job) or {}
                    displaced = result.get('displaced_release')
                    if displaced and displaced not in operation['execution']['job_ids']:
                        raise DeployError('SECRET_REVOCATION_FAILED', '中断的发布涉及其他旧版本，需要人工核对路由与状态。')
                self.store.confirm_secret_revocation_release(operation['id'], job_id)
                confirmed.add(job_id)
                vault.checkpoint_revocation(operation['id'], {'confirmed_job_ids': sorted(confirmed)})
            except Exception:
                # Continue stopping the other fixed targets; never restore an old release.
                failed = True
        if failed:
            vault.fail_revocation(operation['id'], {'code': 'SECRET_REVOCATION_FAILED'})
        else:
            vault.checkpoint_revocation(operation['id'], {'confirmed_job_ids': sorted(confirmed)})
            vault.finish_revocation(operation['id'])

    def _offline(self, operation):
        try:
            if not offline_started(operation):
                self.access.authorize_operation("offline", operation["id"], force=True)
            operation = self.store.claim_offline(operation["id"])
            job = self.store.get(operation["target_release_id"])
            def checkpoint(delta):
                return self.store.checkpoint_offline(operation["id"], delta)

            delete_data = operation["execution"].get("data_policy", "retain") == "delete"
            if delete_data and "deletion_plan" not in operation["execution"]:
                checkpoint({"phase": "offline_delete_preflight"})
                plan = self.backend.inventory_offline_data(operation["execution"]["deletion_metadata"])
                operation = checkpoint({"deletion_plan": plan})
            if delete_data and operation["execution"]["deletion_plan"].get("metadata") != operation["execution"]["deletion_metadata"]:
                raise DeployError("OFFLINE_DATA_CONFLICT", "Deletion plan differs from accepted application scope")
            if not offline_started(operation):
                self.access.authorize_operation("offline", operation["id"], force=True)
                operation = checkpoint({"authorization_started": True})
            # Once runtime cleanup was durably confirmed, a partial data purge
            # must never replay container logic expecting those volumes to exist.
            if not delete_data or not operation["execution"].get("cleanup_confirmed"):
                self.backend.offline(job, checkpoint)
            if delete_data:
                self.backend.delete_offline_data(operation["execution"]["deletion_plan"], checkpoint)
            self.store.finish_offline(operation["id"])
        except Exception as error:
            failure = error.as_dict() if isinstance(error, DeployError) else {
                "code": "OFFLINE_FAILED", "message": "Application offline requires an explicit retry.",
                "path": "", "suggestion": "Verify the gateway and runtime, then retry this offline request.",
            }
            # Never restore online traffic after a durable user request to stop it.
            # Keep reservations and the admission/recovery gate until cleanup is confirmed.
            current = self.store.get_offline(operation["id"])
            if not offline_started(current) and failure["code"] in {
                "ACCESS_DENIED", "IDENTITY_UNAVAILABLE",
            }:
                self.store.cancel_offline(operation["id"], failure)
            else:
                self.store.fail_offline(operation["id"], failure)

    def _recover_active(self, job, required=False):
        try:
            job = self.store.begin_active_recovery(job["id"], self.backend.recovery_boot_id())
        except DeployError as error:
            if error.code in {"APP_OFFLINE_BUSY", 'SECRET_REVOCATION_STATE'} and not required:
                return  # A user requested offline after this recovery scan selected the release.
            if self.settings.secret_backend == 'encrypted' and not required:
                self.store.checkpoint(job['id'], execution={'active_recovery': {
                    'boot_id': self.backend.recovery_boot_id(), 'outcome': 'failed', 'attempts': 1,
                    'next_at': time.time() + 60,
                }})
                self.store.event(job['id'], 'recovery', 'Secret validation blocked automatic recovery; operator review is required.')
                return
            raise
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
            self.access.authorize_operation("job", job_id)
            job = self.store.prepare_context(job_id)
            if not job["snapshot"]:
                job = self._phase(job_id, "source", "Preparing the pinned repository source.")
                source = self.source
                if isinstance(source, GitSource) and not self.access.repository_credentials_allowed(job["app_id"]):
                    source = GitSource(replace(self.settings, git_askpass=None), runner=self.source.runner)
                    source.lock_fd = self.source.lock_fd
                try:
                    snapshot = source.prepare(job, checkpoint)
                finally:
                    if source is not self.source:
                        source.lock_fd = None
                job = self.store.checkpoint(job_id, phase="prepared", snapshot=snapshot)
            if not job["execution"].get("frozen"):
                self.access.authorize_operation("job", job_id)
                job = self._phase(job_id, "freeze", "Freezing deployment inputs.")
                snapshot = self.backend.freeze(job["snapshot"], job, checkpoint)
                job = self.store.checkpoint(
                    job_id, phase="frozen", execution={"frozen": True}, snapshot=snapshot
                )
            job = self.store.reserve(job_id, job["snapshot"])
            self.store.validate_runtime_secrets(job)
            services = set(job["snapshot"]["manifest"]["services"])
            if not services.issubset(job["execution"].get("images", {})):
                job = self._phase(job_id, "build", "Building candidate images.")
                images = self.backend.build(job, checkpoint)
                job = self.store.checkpoint(job_id, execution={"images": images}, phase="built")
            self.store.validate_runtime_secrets(job)
            self.access.authorize_operation("job", job_id, force=True)
            if job["execution"].get("previous_release") and any(
                service.get("storage") for service in job["snapshot"]["manifest"]["services"].values()
            ):
                previous = self.store.active_release(job["app_id"])
                if previous is None or previous["id"] != job["execution"]["previous_release"]:
                    raise DeployError("STORAGE_HANDOFF_STATE", "Previous active release changed before handoff.")
                job = self.store.checkpoint(job_id, execution={
                    "storage_volumes": previous["execution"].get("storage_volumes", {}),
                })
                if callable(getattr(self.backend, "prepare_storage", None)):
                    self.backend.prepare_storage(job, checkpoint)
                    job = self.store.get(job_id)
                self.access.authorize_operation("job", job_id, force=True)
                self.store.validate_runtime_secrets(job)
                job = self.store.checkpoint(job_id, phase="storage_handoff", execution={
                    "storage_handoff_intent": True,
                })
                self.store.event(job_id, "storage_handoff", "Stopping the previous release before attaching persistent data.")
                self.backend.stop_for_storage_handoff(previous)
                job = self.store.checkpoint(job_id, execution={"storage_previous_stopped": True})
            elif callable(getattr(self.backend, "prepare_storage", None)):
                self.backend.prepare_storage(job, checkpoint)
                job = self.store.get(job_id)
            self.access.authorize_operation("job", job_id, force=True)
            self.store.validate_runtime_secrets(job)
            job = self._phase(job_id, "start", "Starting the isolated candidate release.")
            self.backend.start(job, checkpoint)
            job = self._phase(job_id, "health", "Checking candidate health.")
            self.backend.health(job)
            job = self._phase(job_id, "https", "Ensuring HTTPS is available.")
            self.backend.ensure_https(job)
            self.access.authorize_operation("job", job_id, force=True)
            self.store.validate_runtime_secrets(job)
            job = self.store.checkpoint(job_id, phase="publish", execution={"publish_intent": True})
            self.store.event(job_id, "publish", "Publishing the candidate release.")
            self.backend.publish(job)
            job = self._phase(job_id, "verify", "Verifying the public HTTPS route.")
            self.store.validate_runtime_secrets(job)
            if self.settings.secret_backend == 'encrypted':
                from .vault_runtime import VaultRuntime
                VaultRuntime(self.settings).validate_previous(job)
            self.backend.verify(job)
            self.access.authorize_operation("job", job_id)
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
        if job["execution"].get("storage_handoff_intent"):
            # The candidate must relinquish the data before the old release can run.
            # Keep reservations until both cleanup and any route rollback are confirmed.
            try:
                if not job["execution"].get("storage_candidate_cleaned"):
                    self.backend.quiesce_storage_candidate(job)
                    self.backend.cleanup(job)
                    job = self.store.checkpoint(job_id, execution={"storage_candidate_cleaned": True})
                self._recover_previous_for_rollback(job)
            except Exception:
                self.store.fail(job_id, {
                    "code": "STORAGE_RECOVERY_REQUIRED",
                    "message": "Exclusive storage handoff recovery could not be confirmed.",
                    "path": "",
                    "suggestion": "Verify candidate termination and volume ownership before restoring the previous release.",
                }, recovery=True)
                self.store.event(job_id, "storage_handoff", "Storage recovery requires operator attention; automatic active recovery is blocked.")
                return
        if job["execution"].get("publish_intent") and not job["execution"].get("rollback_confirmed"):
            try:
                if not job["execution"].get("storage_handoff_intent"):
                    self._recover_previous_for_rollback(job)
                self.store.event(job_id, "rollback", "Restoring the previous public route.")
                if self.settings.secret_backend == 'encrypted':
                    from .vault_runtime import VaultRuntime
                    VaultRuntime(self.settings).validate_previous(job)
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
                if not job["execution"].get("storage_candidate_cleaned"):
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
