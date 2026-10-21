"""Frozen, resumable deletion of one application's recorded volumes and secrets.

The worker owns its process lock throughout; the Store gates every affected scope.
Plans contain ownership/file metadata only and never secret values.
"""

import copy
import json
import re

from .errors import DeployError
from .secrets_store import SecretStore
from .storage import ManagedStorage


class OfflineDataDeletion:
    def __init__(self, settings, docker):
        self.settings = settings
        self.encrypted = settings.secret_backend == "encrypted"
        self.secrets = None if self.encrypted else SecretStore(settings)
        self.storage = ManagedStorage(docker, settings)
        self.docker = docker

    @staticmethod
    def _scopes(scopes):
        if not isinstance(scopes, list) or any(
            not isinstance(scope, dict) or set(scope) != {"repository", "subdomain"}
            or not all(isinstance(value, str) and value for value in scope.values())
            for scope in scopes
        ):
            raise DeployError("OFFLINE_DATA_CONFLICT", "Invalid data deletion scope")
        identities = {(scope["repository"], scope["subdomain"]) for scope in scopes}
        if len(identities) != len(scopes):
            raise DeployError("OFFLINE_DATA_CONFLICT", "Duplicate data deletion scope")
        return identities

    def _metadata(self, metadata):
        if (
            not isinstance(metadata, dict)
            or set(metadata) != {"app_id", "job_ids", "scopes", "volumes"}
            or not isinstance(metadata["job_ids"], list)
            or not all(isinstance(value, str) and value for value in metadata["job_ids"])
            or not isinstance(metadata["volumes"], list)
            or any(not isinstance(volume, dict) or set(volume) != {"service", "spec", "expected"}
                   for volume in metadata["volumes"])
        ):
            raise DeployError("OFFLINE_DATA_CONFLICT", "Invalid data deletion metadata")
        return self._scopes(metadata["scopes"])

    def _check_volumes(self, metadata, *, unused):
        app_id = metadata["app_id"]
        if not isinstance(app_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", app_id):
            raise DeployError("OFFLINE_DATA_CONFLICT", "Invalid application data identity")
        expected_names = set()
        for volume in metadata["volumes"]:
            expected = volume["expected"]
            name, _ = self.storage.identity(app_id, volume["service"], volume["spec"])
            if self.storage.is_quota(expected):
                self.storage.validate_reference(app_id, volume["service"], volume["spec"], expected)
                if name in expected_names:
                    raise DeployError("STORAGE_IDENTITY", "Duplicate recorded storage identity")
                expected_names.add(name)
                if unused:
                    self.storage.verify_removal(app_id, **volume)
                else:
                    self.storage.inventory_record(app_id, **volume)
                continue
            if (
                not isinstance(expected, dict)
                or set(expected) != {"name", "created_at", "instance"}
                or expected["name"] != name
                or not isinstance(expected["created_at"], str)
                or not expected["created_at"].strip()
                or not isinstance(expected["instance"], str)
                or not re.fullmatch(r"[0-9a-f]{32}", expected["instance"])
                or name in expected_names
            ):
                raise DeployError("STORAGE_IDENTITY", "Invalid recorded volume identity")
            expected_names.add(name)
            if unused:
                self.storage.verify_removal(app_id, **volume)
            else:
                try:
                    self.storage.verify_legacy(app_id, **volume)
                except DeployError as error:
                    if error.code != "STORAGE_MISSING":
                        raise
        found = self.docker([
            "volume", "ls", "--filter", f"label=awesome.app={app_id}", "--format", "{{.Name}}",
        ], timeout=120).split()
        if set(found) - expected_names:
            raise DeployError("OFFLINE_DATA_CONFLICT", "Unrecorded application volumes require review")
        if self.storage.backend == "xfs-project":
            inventory = self.storage.quota.call("inventory", app_id=app_id)
            recorded = {volume["expected"]["storage_id"] for volume in metadata["volumes"]
                        if self.storage.is_quota(volume["expected"])}
            retained = {record["reference"]["storage_id"] for record in inventory["records"]
                        if record.get("state") != "deleted"}
            if retained - recorded:
                raise DeployError("OFFLINE_DATA_CONFLICT", "Unrecorded retained quota storage requires review")

    def inventory(self, metadata):
        self._metadata(metadata)
        self._check_volumes(metadata, unused=False)
        if self.encrypted:
            from .secret_vault import SecretVault
            from .store import Store
            with Store(self.settings)._connection() as db:
                db.execute("BEGIN")
                operation, _ = self._vault_operation(db, metadata)
                return {"version": 2, "metadata": copy.deepcopy(metadata), "operation_id": operation["id"],
                        "vault": SecretVault(self.settings).inventory(db, metadata["app_id"])}
        return {
            "version": 1,
            "metadata": copy.deepcopy(metadata),
            "secrets": [self.secrets.inventory_scope(**scope) for scope in metadata["scopes"]],
        }

    def delete(self, plan, checkpoint):
        if self.encrypted:
            return self._delete_vault(plan, checkpoint)
        if (
            not isinstance(plan, dict) or set(plan) != {"version", "metadata", "secrets"}
            or type(plan["version"]) is not int or plan["version"] != 1
            or not isinstance(plan["secrets"], list)
        ):
            raise DeployError("OFFLINE_DATA_CONFLICT", "Unsupported data deletion plan")
        metadata = plan["metadata"]
        expected_scopes = self._metadata(metadata)
        actual_scopes = self._scopes([
            {"repository": scope.get("repository"), "subdomain": scope.get("subdomain")}
            if isinstance(scope, dict) else {} for scope in plan["secrets"]
        ])
        if expected_scopes != actual_scopes:
            raise DeployError("OFFLINE_DATA_CONFLICT", "Data deletion plan scope changed")
        # Validate every target before deleting any target. Retries use the same
        # identities and accept only missing originals, never new replacements.
        self._check_volumes(metadata, unused=True)
        for scope in plan["secrets"]:
            self.secrets.verify_deletion(scope)
        checkpoint({"phase": "offline_delete_data", "data_delete_intent": True})
        for volume in metadata["volumes"]:
            self.storage.remove(metadata["app_id"], **volume)
        checkpoint({"volumes_deleted": True, "phase": "offline_delete_secrets"})
        for scope in plan["secrets"]:
            self.secrets.delete_scope(scope)
        # A completed deletion is durable before the Store releases the name and
        # ports and marks historical inputs unavailable for configuration replay.
        checkpoint({"secrets_deleted": True, "data_deleted": True})

    def _vault_operation(self, db, metadata, plan=None):
        """The durable offline gate excludes API writes throughout volume deletion.

        The worker already holds worker.lock, which excludes CLI and deployment
        writes. No SQLite transaction is held across checkpoint callbacks.
        """
        conflict = DeployError("OFFLINE_DATA_CONFLICT", "应用数据销毁记录或授权范围不一致，已停止清理。")
        rows = db.execute("SELECT * FROM offline_operations WHERE app_id=? AND status IN ('running','succeeded')",
                          (metadata["app_id"],)).fetchall()
        matches = []
        for row in rows:
            execution = json.loads(row["execution"])
            if (execution.get("data_policy") == "delete" and execution.get("deletion_metadata") == metadata
                    and (plan is None or row["id"] == plan["operation_id"])):
                matches.append((row, execution))
        if len(matches) != 1:
            raise conflict
        operation, execution = matches[0]
        authorization = db.execute("SELECT * FROM operation_auth WHERE kind='offline' AND id=?",
                                   (operation["id"],)).fetchone()
        if (authorization is None or authorization["app_id"] != metadata["app_id"]
                or authorization["action"] != "delete" or not authorization["user_id"]):
            raise conflict
        if plan is None:
            if operation["status"] != "running":
                raise conflict
        elif (execution.get("deletion_plan") != plan or execution.get("authorization_started") is not True
              or execution.get("cleanup_confirmed") is not True
              or operation["status"] == "succeeded" and execution.get("data_deleted") is not True):
            raise conflict
        if db.execute("SELECT 1 FROM vault_revocations WHERE app_id=? AND status!='succeeded' LIMIT 1",
                      (metadata["app_id"],)).fetchone():
            raise conflict
        return operation, authorization["user_id"]

    def _delete_vault(self, plan, checkpoint):
        if (not isinstance(plan, dict) or set(plan) != {"version", "metadata", "vault", "operation_id"}
                or type(plan["version"]) is not int or plan["version"] != 2
                or not isinstance(plan["operation_id"], str) or not isinstance(plan["vault"], dict)):
            raise DeployError("OFFLINE_DATA_CONFLICT", "Unsupported encrypted data deletion plan")
        metadata = plan["metadata"]
        self._metadata(metadata)
        if plan["vault"].get("app_id") != metadata["app_id"]:
            raise DeployError("OFFLINE_DATA_CONFLICT", "Vault deletion application changed")
        from .secret_vault import SecretVault
        from .store import Store
        store, vault = Store(self.settings), SecretVault(self.settings)
        self._check_volumes(metadata, unused=True)
        with store._connection() as db:
            db.execute("BEGIN")
            self._vault_operation(db, metadata, plan)
            vault.verify_deletion(db, plan["vault"])
        checkpoint({"phase": "offline_delete_data", "data_delete_intent": True})
        for volume in metadata["volumes"]:
            self.storage.remove(metadata["app_id"], **volume)
        checkpoint({"volumes_deleted": True, "phase": "offline_delete_secrets"})
        with store._connection(write=True) as db:
            _, actor_id = self._vault_operation(db, metadata, plan)
            vault.delete(db, plan["vault"], actor_id=actor_id)
        checkpoint({"secrets_deleted": True, "data_deleted": True})
