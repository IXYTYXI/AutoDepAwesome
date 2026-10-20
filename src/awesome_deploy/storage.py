"""Application-owned local volumes with explicit, identity-checked data removal."""

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from pathlib import PurePosixPath

from .quota_client import QuotaClient

from .errors import DeployError


class ManagedStorage:
    def __init__(self, docker, settings=None, *, quota=None):
        self.docker = docker
        self.backend = getattr(settings, "storage_backend", "local")
        self.quota = quota or QuotaClient(getattr(settings, "storage_command", (
            "sudo", "-n", "/usr/local/libexec/awesome-storage")))

    @staticmethod
    def is_quota(reference):
        return isinstance(reference, dict) and reference.get("backend") == "xfs-project"

    def validate_reference(self, app_id, service, spec, expected):
        self.identity(app_id, service, spec)
        digest = hashlib.sha256(json.dumps({"app_id": app_id, "service": service, "name": spec["name"]},
                                           sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if (not self.is_quota(expected) or set(expected) != {"backend", "storage_id", "generation"}
                or expected["storage_id"] != digest
                or not re.fullmatch(r"[0-9a-f]{32}", str(expected.get("generation", "")))):
            raise DeployError("STORAGE_IDENTITY", "Invalid stable quota storage reference")
        return expected

    def _record(self, app_id, service, spec, result, expected=None, *, allow_pending=False):
        if not isinstance(result, dict):
            raise DeployError("QUOTA_PROTOCOL", "Storage helper returned an invalid record")
        reference = self.validate_reference(app_id, service, spec, result.get("reference"))
        path = result.get("path")
        absent_pending = (allow_pending and path is None and result.get("state") in {"allocating", "deleting", "blocked"}
                          and not result.get("writers"))
        valid_path = (isinstance(path, str) and path.startswith("/")
                      and str(PurePosixPath(path)) == path and ".." not in PurePosixPath(path).parts
                      and not any(char in path for char in ",\\\n\r\x00") and path != "/")
        if (expected is not None and reference != expected
                or result.get("identity") != {"app_id": app_id, "service": service, "name": spec["name"]}
                or not (valid_path or absent_pending)):
            raise DeployError("STORAGE_IDENTITY", "Storage helper record identity or path changed")
        return result

    def _quota_required(self, spec, expected):
        if self.backend != "xfs-project" and (self.is_quota(expected) or "sizeMiB" in spec or "maxInodes" in spec):
            raise DeployError("QUOTA_BACKEND_REQUIRED", "Hard-quota storage requires the XFS backend")

    def inspect(self, app_id, service, spec, expected, *, timeout=120):
        self._quota_required(spec, expected)
        self.validate_reference(app_id, service, spec, expected)
        return self._record(app_id, service, spec,
                            self.quota.call("inspect", expected=expected, timeout=timeout), expected)

    def mount_argument(self, job, service, *, timeout=120):
        spec = job["snapshot"]["manifest"]["services"][service]["storage"]
        expected = job["execution"].get("storage_volumes", {}).get(service)
        self._quota_required(spec, expected)
        if self.is_quota(expected):
            record = self.inspect(job["app_id"], service, spec, expected, timeout=timeout)
            return f"type=bind,source={record['path']},destination=/data,bind-propagation=rprivate"
        name, _ = self.identity(job["app_id"], service, spec)
        if not expected or expected.get("name") != name:
            raise DeployError("STORAGE_IDENTITY", "Container creation requires its persisted volume identity")
        return f"type=volume,source={name},destination=/data"

    @contextmanager
    def guard(self, job, service, row, *, timeout=120):
        expected = job["execution"].get("storage_volumes", {}).get(service)
        if not self.is_quota(expected):
            yield None
            return
        writer = {"container_id": row.get("Id"), "container_name": row.get("Name", "").removeprefix("/"),
                  "app_id": job["app_id"], "release_id": job["id"], "service": service}
        self._quota_required({}, expected)
        with self.quota.session("guard", items=[{"expected": expected, "writer": writer}], timeout=timeout) as session:
            spec = job["snapshot"]["manifest"]["services"][service]["storage"]
            records = session.result.get("records") if isinstance(session.result, dict) else None
            if not isinstance(records, list) or len(records) != 1:
                raise DeployError("QUOTA_PROTOCOL", "Storage guard did not return its record")
            record = self._record(job["app_id"], service, spec, records[0], expected)
            self._validate_quota_mount(row, record["path"])
            yield session

    def retire(self, container_id, *, session=None):
        if self.backend == "xfs-project":
            (session or self.quota).call("retire", container_id=container_id)

    @staticmethod
    def _validate_quota_mount(row, path=None):
        mounts = row.get("Mounts") or []
        binds = [mount for mount in mounts if mount.get("Type") == "bind"]
        if (any(m.get("Type") not in {"tmpfs", "bind"} for m in mounts)
                or any(m.get("Type") == "tmpfs" and m.get("Destination") not in {"/tmp", "/dev/shm"} for m in mounts)
                or len(binds) != 1 or binds[0].get("Destination") != "/data"
                or binds[0].get("RW") is not True
                or path is not None and binds[0].get("Source") != path
                or binds[0].get("Propagation", "rprivate") != "rprivate"):
            raise DeployError("STORAGE_MOUNT_CHANGED", "Container must have its exact writable quota bind mount")

    @staticmethod
    def identity(app_id, service, spec):
        if (
            not isinstance(app_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", app_id)
            or not isinstance(service, str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", service)
            or not isinstance(spec, dict)
            or not {"name", "mountPath"}.issubset(spec)
            or set(spec) - {"name", "mountPath", "sizeMiB", "maxInodes"}
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", str(spec.get("name", "")))
            or spec.get("mountPath") != "/data"
        ):
            raise DeployError("STORAGE_IDENTITY", "Invalid controlled storage identity")
        digest = hashlib.sha256(json.dumps([app_id, service, spec["name"]]).encode()).hexdigest()
        return "awesome-data-" + digest, {
            "awesome.managed": "true",
            "awesome.app": app_id,
            "awesome.service": service,
            "awesome.storage": spec["name"],
        }

    def ensure(self, app_id, service, spec, expected=None, *, create=True, timeout=120, operation_id=None):
        name, labels = self.identity(app_id, service, spec)
        self._quota_required(spec, expected)
        if self.backend == "xfs-project":
            if expected is not None:
                if not self.is_quota(expected):
                    raise DeployError("STORAGE_MIGRATION_REQUIRED", "Legacy volumes require explicit migration")
                self.validate_reference(app_id, service, spec, expected)
            if not create:
                return self.inspect(app_id, service, spec, expected, timeout=timeout)["reference"]
            request = {"identity": {"app_id": app_id, "service": service, "name": spec["name"]},
                       "operation_id": operation_id or uuid.uuid4().hex}
            if expected is not None:
                request["expected"] = expected
            if "sizeMiB" in spec or "maxInodes" in spec:
                if any(type(spec.get(k)) is not int or spec[k] <= 0 for k in ("sizeMiB", "maxInodes")):
                    raise DeployError("QUOTA_LIMIT_INVALID", "Both hard limits must be positive integers")
                request["limits"] = {"size_mib": spec["sizeMiB"], "max_inodes": spec["maxInodes"]}
            result = self.quota.call("ensure", timeout=timeout, **request)
            return self._record(app_id, service, spec, result, expected)["reference"]
        if expected is not None and (
            not isinstance(expected, dict)
            or set(expected) != {"name", "created_at", "instance"}
            or expected.get("name") != name
            or not expected.get("created_at")
            or not re.fullmatch(r"[0-9a-f]{32}", str(expected.get("instance", "")))
        ):
            raise DeployError("STORAGE_IDENTITY", "Persisted volume identity is invalid")
        found = self.docker(
            ["volume", "ls", "--filter", f"name=^{name}$", "--format", "{{.Name}}"], timeout=timeout
        ).split()
        if found and found != [name]:
            raise DeployError("STORAGE_CONFLICT", "Conflicting controlled volume name")
        if not found:
            if expected is not None or not create:
                raise DeployError(
                    "STORAGE_MISSING", "Persisted volume is missing; refusing empty replacement"
                )
            creation_labels = {**labels, "awesome.storage-instance": uuid.uuid4().hex}
            flags = [arg for key, value in creation_labels.items() for arg in ("--label", f"{key}={value}")]
            self.docker(["volume", "create", "--driver", "local", *flags, name], timeout=timeout)
        rows = json.loads(self.docker(["volume", "inspect", name], timeout=timeout))
        if not isinstance(rows, list) or len(rows) != 1:
            raise DeployError("STORAGE_CONFLICT", "Controlled volume inspection failed")
        row = rows[0]
        actual_labels = row.get("Labels") or {}
        instance = actual_labels.get("awesome.storage-instance", "")
        if (
            row.get("Name") != name
            or row.get("Driver") != "local"
            or row.get("Scope") != "local"
            or row.get("Options") not in (None, {})
            or not re.fullmatch(r"[0-9a-f]{32}", str(instance))
            or actual_labels != {**labels, "awesome.storage-instance": instance}
            or not isinstance(row.get("CreatedAt"), str)
            or not row["CreatedAt"]
        ):
            raise DeployError("STORAGE_CONFLICT", "Volume ownership, driver or options do not match")
        record = {"name": name, "created_at": row["CreatedAt"], "instance": instance}
        if expected is not None and record != expected:
            raise DeployError("STORAGE_IDENTITY", "Persisted volume was replaced")
        return record

    def inventory_record(self, app_id, service, spec, expected, *, timeout=120):
        self._quota_required(spec, expected)
        self.validate_reference(app_id, service, spec, expected)
        inventory = self.quota.call("inventory", app_id=app_id, expected=expected, timeout=timeout)
        if not isinstance(inventory, dict) or not isinstance(inventory.get("records"), list):
            raise DeployError("QUOTA_PROTOCOL", "Invalid quota inventory")
        matches = [record for record in inventory["records"]
                   if isinstance(record, dict) and record.get("reference", {}).get("storage_id") == expected["storage_id"]]
        if not matches:
            raise DeployError("QUOTA_REFERENCE_MISSING", "Recorded quota storage is missing from the ledger")
        if len(matches) != 1:
            raise DeployError("STORAGE_IDENTITY", "Ambiguous quota inventory")
        return self._record(app_id, service, spec, matches[0], expected, allow_pending=True)

    def _retire_stopped_writers(self, record, *, timeout):
        for writer in record.get("writers", []):
            identifier = writer.get("container_id", "")
            if (not re.fullmatch(r"[0-9a-f]{64}", str(identifier))
                    or writer.get("app_id") != record["identity"]["app_id"]
                    or writer.get("service") != record["identity"]["service"]
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(writer.get("release_id", "")))
                    or writer.get("container_name") != f"awesome-{writer['release_id']}-{writer['service']}"):
                raise DeployError("STORAGE_IDENTITY", "Invalid registered storage writer")
            found = self.docker(["container", "ls", "-a", "--no-trunc", "--filter", f"id={identifier}", "--format", "{{.ID}}"], timeout=timeout).split()
            if found:
                if found != [identifier]:
                    raise DeployError("STORAGE_IDENTITY", "Ambiguous registered storage writer")
                rows = json.loads(self.docker(["container", "inspect", identifier], timeout=timeout))
                labels = {"awesome.managed": "true", "awesome.app": writer["app_id"], "awesome.job": writer["release_id"]}
                if (not isinstance(rows, list) or len(rows) != 1 or rows[0].get("Id") != identifier
                        or rows[0].get("Name", "").removeprefix("/") != writer["container_name"]
                        or any(rows[0].get("Config", {}).get("Labels", {}).get(k) != v for k, v in labels.items())):
                    raise DeployError("STORAGE_IDENTITY", "Registered storage writer identity changed")
                state = rows[0].get("State", {})
                if state.get("Running") is not False or state.get("Paused") or state.get("Restarting"):
                    raise DeployError("STORAGE_IN_USE", "A registered storage writer is still running")
            self.retire(identifier)

    def verify_legacy(self, app_id, service, spec, expected, *, timeout=120):
        """Read the exact legacy instance for inventory/removal, never admit a writer."""
        self.identity(app_id, service, spec)
        identity_spec = {key: spec[key] for key in ("name", "mountPath")}
        return ManagedStorage(self.docker).ensure(app_id, service, identity_spec, expected,
                                                   create=False, timeout=timeout)

    def _verify_removal(self, app_id, service, spec, expected, *, timeout):
        if self.is_quota(expected):
            record = self.inventory_record(app_id, service, spec, expected, timeout=timeout)
            if record.get("state") == "deleted":
                return False
            self._retire_stopped_writers(record, timeout=timeout)
            references = self.docker(["ps", "-a", "--filter", f"volume={record['path']}", "--format", "{{.ID}}"], timeout=timeout).split() if record["path"] else []
            if references:
                raise DeployError("STORAGE_IN_USE", "A container still references quota storage")
            return True
        name, _ = self.identity(app_id, service, spec)
        if (
            not isinstance(expected, dict)
            or set(expected) != {"name", "created_at", "instance"}
            or expected.get("name") != name
            or not isinstance(expected.get("created_at"), str)
            or not expected["created_at"].strip()
            or not isinstance(expected.get("instance"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", expected["instance"])
        ):
            raise DeployError("STORAGE_IDENTITY", "Persisted volume identity is required for removal")
        try:
            self.verify_legacy(app_id, service, spec, expected, timeout=timeout)
        except DeployError as error:
            if error.code == "STORAGE_MISSING":
                return False
            raise
        references = self.docker(
            ["ps", "-a", "--filter", f"volume={name}", "--format", "{{.ID}}"], timeout=timeout
        ).split()
        if references:
            raise DeployError("STORAGE_IN_USE", "A container still references the controlled volume")
        # Recheck after querying references so a replaced instance is never accepted
        # merely because the original inspection happened earlier in this call.
        self.verify_legacy(app_id, service, spec, expected, timeout=timeout)
        return True

    def verify_removal(self, app_id, service, spec, expected, *, timeout=120) -> None:
        """Check an explicit deletion plan without mutation; missing originals are safe."""
        self._verify_removal(app_id, service, spec, expected, timeout=timeout)

    def remove(self, app_id, service, spec, expected, *, timeout=120) -> None:
        """Remove only the recorded, unused managed volume; callers hold the worker lock."""
        if not self._verify_removal(app_id, service, spec, expected, timeout=timeout):
            return
        if self.is_quota(expected):
            self.quota.call("delete", expected=expected,
                            operation_id=f"delete:{expected['storage_id']}:{expected['generation']}", timeout=timeout)
            return
        name = expected["name"]
        # Docker also rejects references acquired after our check; never force removal.
        self.docker(["volume", "rm", name], timeout=timeout)
        remaining = self.docker(
            ["volume", "ls", "--filter", f"name=^{name}$", "--format", "{{.Name}}"], timeout=timeout
        ).split()
        if remaining:
            raise DeployError("STORAGE_DELETE_FAILED", "Controlled volume removal was not confirmed")

    def validate_mounts(self, job, service, row, *, timeout=120, readiness=True):
        spec = job["snapshot"]["manifest"]["services"][service].get("storage")
        expected = job["execution"].get("storage_volumes", {}).get(service)
        if self.is_quota(expected):
            self.validate_reference(job["app_id"], service, spec, expected)
            path = self.inspect(job["app_id"], service, spec, expected, timeout=timeout)["path"] if readiness else None
            self._validate_quota_mount(row, path)
            return
        mounts = row.get("Mounts") or []
        # Docker reports its controlled /tmp and /dev/shm tmpfs as Type=tmpfs.
        if any(mount.get("Type") not in {"tmpfs", "volume"} for mount in mounts):
            raise DeployError("STORAGE_MOUNT_CHANGED", "Container has an unsupported mount")
        if any(
            mount.get("Type") == "tmpfs" and mount.get("Destination") not in {"/tmp", "/dev/shm"}
            for mount in mounts
        ):
            raise DeployError("STORAGE_MOUNT_CHANGED", "Container has an unexpected temporary mount")
        volumes = [mount for mount in mounts if mount.get("Type") == "volume"]
        if spec is None:
            if volumes:
                raise DeployError("STORAGE_MOUNT_CHANGED", "Stateless container has a persistent mount")
            return
        record = job["execution"].get("storage_volumes", {}).get(service)
        if record is None:
            raise DeployError("STORAGE_IDENTITY", "Missing persisted volume identity")
        if readiness:
            self.ensure(job["app_id"], service, spec, record, create=False, timeout=timeout)
        if (
            len(volumes) != 1
            or volumes[0].get("Name") != record["name"]
            or volumes[0].get("Destination") != "/data"
            or volumes[0].get("RW") is not True
            or volumes[0].get("Driver") != "local"
        ):
            raise DeployError(
                "STORAGE_MOUNT_CHANGED", "Container must have its exact owned writable /data volume"
            )
