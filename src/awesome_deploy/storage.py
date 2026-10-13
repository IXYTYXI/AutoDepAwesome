"""Application-owned local volumes. This module never deletes persisted data."""

import hashlib
import json
import re
import uuid

from .errors import DeployError


class ManagedStorage:
    def __init__(self, docker):
        self.docker = docker

    @staticmethod
    def identity(app_id, service, spec):
        if (
            not isinstance(app_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", app_id)
            or not isinstance(service, str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", service)
            or not isinstance(spec, dict)
            or set(spec) != {"name", "mountPath"}
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

    def ensure(self, app_id, service, spec, expected=None, *, create=True, timeout=120):
        name, labels = self.identity(app_id, service, spec)
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

    def validate_mounts(self, job, service, row, *, timeout=120):
        spec = job["snapshot"]["manifest"]["services"][service].get("storage")
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
