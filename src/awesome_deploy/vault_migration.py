"""Explicit, resumable legacy migration; no implicit key generation or fallback.

Only values read from verified legacy descriptors enter process memory. Durable
plans contain ownership, versions and filesystem identity, never value digests.
"""

import base64
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hmac
import json
import os
import re
import sqlite3
import stat
import time
import uuid

from .config import validate_vault_settings
from .errors import DeployError
from .secret_vault import SecretVault
from .secrets_store import SecretStore
from . import vault_format
from .vault_runtime import VaultRuntime

_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_UUID = re.compile(r"[0-9a-f]{32}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_BACKUP_FIELDS = {"backup_reference", "kek_recovery_reference", "responsible", "verified_at", "retention_until"}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _conflict():
    return DeployError("VAULT_MIGRATION_CONFLICT", "迁移清单、归属或原始文件不一致，请保留现场并人工核对。")


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise _conflict()
        value[key] = item
    return value


def load_record(path):
    """Bounded public operator metadata, not a secret input interface."""
    try:
        fd = os.open(path, _FLAGS)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= 4 * 1024 * 1024:
                raise _conflict()
            raw = os.read(fd, 4 * 1024 * 1024 + 1)
        finally:
            os.close(fd)
        return json.loads(raw, object_pairs_hook=_unique)
    except (OSError, ValueError, TypeError, RecursionError):
        raise _conflict() from None


def validate_backup(value):
    if (not isinstance(value, dict) or set(value) != _BACKUP_FIELDS
            or any(not isinstance(text, str) or not 1 <= len(text) <= 512
                   or not all(char.isprintable() for char in text) or not text.strip() for text in value.values())):
        raise DeployError("VAULT_RECOVERY_RECORD_REQUIRED", "请登记备份、独立根密钥恢复位置、责任人与保留期限。")
    try:
        verified = datetime.fromisoformat(value["verified_at"])
        retained = datetime.fromisoformat(value["retention_until"])
        if verified.tzinfo is None or retained.tzinfo is None or retained <= verified:
            raise ValueError
    except ValueError:
        raise DeployError("VAULT_RECOVERY_RECORD_REQUIRED", "恢复记录须提供带时区的核验时间和更晚的保留截止时间。") from None
    return value


@contextmanager
def worker_lock(settings):
    """Same ordering as deployment: worker lock before any SQLite write."""
    store = SecretStore(settings)
    try:
        with store._open_state_directory(create=True) as directory:
            fd = os.open("worker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         0o600, dir_fd=directory)
            try:
                info = os.fstat(fd)
                current = os.stat("worker.lock", dir_fd=directory, follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                        or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
                    raise _conflict()
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Older workers created this non-secret lock with the process
                # umask. Normalize the verified inode, without replacing it.
                os.fchmod(fd, 0o600)
                yield
            finally:
                os.close(fd)
    except BlockingIOError:
        raise DeployError("SECRET_CONFIGURATION_BUSY", "部署执行器正在工作，请完成当前操作后再维护密钥库。") from None
    except (OSError, sqlite3.Error):
        raise _conflict() from None


class VaultMigration:
    def __init__(self, settings):
        validate_vault_settings(settings)
        if settings.secret_backend != "encrypted" or not settings.feishu_tenant_key or not settings.feishu_app_id:
            raise DeployError("INVALID_CONFIG", "迁移目标必须显式指定企业身份、加密后端与独立根密钥位置。")
        self.settings = settings

    def _store(self):
        from .store import Store
        return Store(self.settings, allow_vault_maintenance=True)

    @staticmethod
    def ensure_schema(db):
        SecretVault.ensure_schema(db)
        vault_format.ensure_schema(db)
        VaultRuntime.ensure_schema(db)
        db.execute("""CREATE TABLE IF NOT EXISTS vault_migrations (
            migration_id TEXT PRIMARY KEY, manifest TEXT NOT NULL, inventory TEXT NOT NULL,
            actor_id TEXT NOT NULL, created_at REAL NOT NULL)""")

    @staticmethod
    def _idle(db):
        for table in ("jobs", "offline_operations"):
            if db.execute(f"SELECT 1 FROM {table} WHERE status IN ('queued','running','recovery_required') LIMIT 1").fetchone():
                raise DeployError("VAULT_MIGRATION_BUSY", "仍有部署、下线或恢复任务未完成，不能迁移密钥库。")
        for table in ("vault_revocations",):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                if db.execute(f"SELECT 1 FROM {table} WHERE status <> 'succeeded' LIMIT 1").fetchone():
                    raise DeployError("VAULT_MIGRATION_BUSY", "仍有密钥吊销清理未确认完成，不能迁移。")

    def _manifest(self, manifest):
        if (not isinstance(manifest, dict)
                or set(manifest) != {"format", "migration_id", "tenant_id", "feishu_app_id", "backup", "applications"}
                or type(manifest["format"]) is not int or manifest["format"] != 1
                or not isinstance(manifest["migration_id"], str) or not _UUID.fullmatch(manifest["migration_id"])
                or manifest["tenant_id"] != self.settings.feishu_tenant_key
                or manifest["feishu_app_id"] != self.settings.feishu_app_id
                or not isinstance(manifest["applications"], list)):
            raise _conflict()
        validate_backup(manifest["backup"])
        ids, scopes, jobs = set(), set(), set()
        for app in manifest["applications"]:
            if (not isinstance(app, dict) or set(app) != {"app_id", "data_generation", "scopes", "job_ids"}
                    or any(not isinstance(app[k], str) or not _IDENTIFIER.fullmatch(app[k]) for k in ("app_id", "data_generation"))
                    or app["app_id"] in ids or not isinstance(app["scopes"], list) or not isinstance(app["job_ids"], list)):
                raise _conflict()
            ids.add(app["app_id"])
            names = set()
            for job in app["job_ids"]:
                if not isinstance(job, str) or not _IDENTIFIER.fullmatch(job) or job in jobs:
                    raise _conflict()
                jobs.add(job)
            for scope in app["scopes"]:
                if (not isinstance(scope, dict) or set(scope) != {"repository", "subdomain", "secrets"}
                        or not isinstance(scope["secrets"], list)):
                    raise _conflict()
                try:
                    identity = SecretStore._scope_name(scope["repository"], scope["subdomain"])
                except (TypeError, ValueError):
                    raise _conflict() from None
                if identity in scopes:
                    raise _conflict()
                scopes.add(identity)
                for secret in scope["secrets"]:
                    if (not isinstance(secret, dict) or set(secret) != {"name", "kind"}
                            or not isinstance(secret["name"], str) or not _NAME.fullmatch(secret["name"])
                            or secret["name"] in names
                            or secret["kind"] not in {"runtime_secret", "platform_managed_master"}):
                        raise _conflict()
                    names.add(secret["name"])
        return manifest

    def _legacy(self, *, quarantine=None):
        legacy = SecretStore(self.settings)
        if quarantine is not None:
            legacy.root = self.settings.state_dir / quarantine
        return legacy

    @staticmethod
    def _read_json(descriptor, filename, limit):
        fd = os.open(filename, _FLAGS, dir_fd=descriptor)
        try:
            info = os.fstat(fd)
            SecretStore._check_private(info, directory=False)
            if info.st_nlink != 1 or not 1 <= info.st_size <= limit:
                raise _conflict()
            raw = os.read(fd, limit + 1)
            if len(raw) != info.st_size:
                raise _conflict()
            return json.loads(raw, object_pairs_hook=_unique)
        finally:
            os.close(fd)

    def _values(self, legacy, scope, name):
        """Yield one plaintext version at a time; callers never persist this iterator."""
        try:
            with legacy._open_secret_directory(scope["repository"], scope["subdomain"], name, create=False) as descriptor:
                pointer = self._read_json(descriptor, "current.json", 512)
                if (not isinstance(pointer, dict) or set(pointer) != {"version"}
                        or not isinstance(pointer["version"], str) or not _UUID.fullmatch(pointer["version"])):
                    raise _conflict()
                files = set(os.listdir(descriptor))
                versions = sorted(filename[:-5] for filename in files if re.fullmatch(r"[0-9a-f]{32}\.json", filename))
                if files != {"current.json", *(version + ".json" for version in versions)} or pointer["version"] not in versions:
                    raise _conflict()
                for version in versions:
                    payload = self._read_json(descriptor, version + ".json", 131072)
                    if not isinstance(payload, dict) or set(payload) != {"version", "value"} or payload["version"] != version:
                        raise _conflict()
                    SecretStore.validate_value(payload["value"])
                    yield version, payload["value"], version == pointer["version"]
        except (OSError, ValueError, TypeError, RecursionError):
            raise _conflict() from None

    def _inventory(self, manifest, legacy):
        plans = []
        expected_scopes = set()
        for app in manifest["applications"]:
            for scope in app["scopes"]:
                expected_scopes.add(SecretStore._scope_name(scope["repository"], scope["subdomain"]))
                plan = legacy.inventory_scope(scope["repository"], scope["subdomain"])
                if set(plan["items"]) != {secret["name"] for secret in scope["secrets"]}:
                    raise _conflict()
                # Fully validate all retained values before creating a KEK or format marker.
                for secret in scope["secrets"]:
                    for _ in self._values(legacy, scope, secret["name"]):
                        pass
                plans.append(plan)
        try:
            with legacy._open_state_directory() as state:
                try:
                    root = os.open(legacy.root.name, _FLAGS | os.O_DIRECTORY, dir_fd=state)
                except FileNotFoundError:
                    actual = set()
                else:
                    try:
                        legacy._check_private(os.fstat(root), directory=True)
                        actual = set(os.listdir(root))
                    finally:
                        os.close(root)
        except OSError:
            raise _conflict() from None
        present = {SecretStore._scope_name(plan["repository"], plan["subdomain"]) for plan in plans if plan["scope"] is not None}
        if actual != present or actual - expected_scopes:
            raise _conflict()
        return plans

    def _coverage(self, db, manifest, inventory):
        self._idle(db)
        apps = {app["app_id"]: app for app in manifest["applications"]}
        if set(apps) != {row[0] for row in db.execute("SELECT id FROM apps")}:
            raise _conflict()
        scopes = {(scope["repository"], scope["subdomain"]): app["app_id"]
                  for app in apps.values() for scope in app["scopes"]}
        actual = {(row["repository"], row["subdomain"]): row["app_id"] for row in db.execute("SELECT * FROM app_secret_scopes")}
        if actual != scopes:
            raise _conflict()
        job_targets = {job: app for app in apps.values() for job in app["job_ids"]}
        by_scope = {(plan["repository"], plan["subdomain"]): plan for plan in inventory}
        required_jobs = set()
        for row in db.execute("SELECT * FROM jobs"):
            payload, execution = json.loads(row["input"]), json.loads(row["execution"])
            if execution.get("data_deleted") is True and execution.get("data_reset_operation"):
                if row["id"] in job_targets:
                    raise _conflict()
                continue
            snapshot = json.loads(row["snapshot"]) if row["snapshot"] else {}
            frozen = snapshot.get("secret_versions", execution.get("secret_versions", {}))
            if VaultRuntime.has_retained_inputs(row["status"], snapshot, execution):
                required_jobs.add(row["id"])
                app = job_targets.get(row["id"])
                if app is None or any(owner is not None and owner != app["app_id"]
                                      for owner in (row["app_id"], payload.get("appId"))):
                    raise _conflict()
                identity = payload.get("repository"), payload.get("subdomain")
                if scopes.get(identity) != app["app_id"] or snapshot.get("repository") != identity[0]:
                    raise _conflict()
                services = snapshot.get("manifest", {}).get("services")
                if (snapshot.get("manifest", {}).get("app", {}).get("subdomain") != identity[1]
                        or not isinstance(services, dict) or not isinstance(frozen, dict) or set(frozen) - services.keys()):
                    raise _conflict()
                scoped = next(scope for scope in app["scopes"] if (scope["repository"], scope["subdomain"]) == identity)
                kinds = {secret["name"]: secret["kind"] for secret in scoped["secrets"]}
                for service, config in services.items():
                    refs, versions = config.get("secretRefs", {}), frozen.get(service, {})
                    if not isinstance(refs, dict) or not isinstance(versions, dict) or set(versions) != set(refs.values()):
                        raise _conflict()
                    for env, name in refs.items():
                        files = by_scope[identity]["items"].get(name, {}).get("files", {})
                        if (name not in kinds or versions[name] + ".json" not in files
                                or env == "MASTER_KEY" and kinds[name] != "platform_managed_master"):
                            raise _conflict()
        if required_jobs != set(job_targets):
            raise _conflict()

    def _provision_keyring(self, db, *, create):
        path = self.settings.secret_kek_file
        if path.exists() or path.is_symlink():
            return SecretVault(self.settings)
        if not create or db.execute("SELECT 1 FROM vault_app_keys LIMIT 1").fetchone():
            raise DeployError("VAULT_KEY_UNAVAILABLE", "根密钥缺失；不能自动生成替代密钥。")
        # Only the final credential directory may be created. Its parent is an
        # explicit operator choice, and every existing ancestor is opened nofollow.
        absolute = path.absolute()
        descriptor = os.open(absolute.anchor, _FLAGS | os.O_DIRECTORY)
        try:
            for index, part in enumerate(absolute.parts[1:-1]):
                if part in {".", ".."}:
                    raise _conflict()
                try:
                    child = os.open(part, _FLAGS | os.O_DIRECTORY, dir_fd=descriptor)
                except FileNotFoundError:
                    if index != len(absolute.parts[1:-1]) - 1:
                        raise _conflict() from None
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                    child = os.open(part, _FLAGS | os.O_DIRECTORY, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            SecretStore._check_private(os.fstat(descriptor), directory=True)
            key_id = uuid.uuid4().hex
            payload = {"format": 1, "active": key_id, "keys": {key_id: base64.b64encode(os.urandom(32)).decode()}}
            fd = os.open(absolute.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=descriptor)
            with os.fdopen(fd, "w") as stream:
                stream.write(_json(payload))
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return SecretVault(self.settings)

    @staticmethod
    def _sentinel(migration_id):
        return ("AWESOME_ENCRYPTED_VAULT_V1\n" + migration_id + "\nLegacy secret writes are disabled.\n").encode()

    def _seal(self, manifest, inventory, checkpoint):
        legacy = self._legacy()
        quarantine_name = "secrets-quarantine-" + manifest["migration_id"]
        quarantine = self._legacy(quarantine=quarantine_name)
        with legacy._open_state_directory() as state:
            try:
                original = os.stat("secrets", dir_fd=state, follow_symlinks=False)
            except FileNotFoundError:
                original = None
            try:
                archived = os.stat(quarantine_name, dir_fd=state, follow_symlinks=False)
            except FileNotFoundError:
                archived = None
            already_sealed_empty = archived is None and original is not None and stat.S_ISREG(original.st_mode) \
                and not any(plan["scope"] is not None for plan in inventory)
            if archived is None and not already_sealed_empty:
                if original is not None and not stat.S_ISDIR(original.st_mode):
                    raise _conflict()
                if self._inventory(manifest, legacy) != inventory:
                    raise _conflict()
                if original is not None:
                    os.rename("secrets", quarantine_name, src_dir_fd=state, dst_dir_fd=state)
                    os.fsync(state)
                checkpoint("quarantined")
            elif archived is not None and self._inventory(manifest, quarantine) != inventory:
                raise _conflict()
            sentinel = self._sentinel(manifest["migration_id"])
            temporary = ".vault-seal-" + manifest["migration_id"]
            # Recover only this migration's private public-text staging file.
            # A linked staging file means publication succeeded before unlink.
            try:
                staged = os.stat(temporary, dir_fd=state, follow_symlinks=False)
            except FileNotFoundError:
                staged = None
            if staged is not None:
                legacy._check_private(staged, directory=False)
                fd = os.open(temporary, _FLAGS, dir_fd=state)
                try:
                    text = os.read(fd, 1024)
                    if staged.st_nlink not in {1, 2} or not sentinel.startswith(text):
                        raise _conflict()
                    if staged.st_nlink == 2:
                        published = os.stat("secrets", dir_fd=state, follow_symlinks=False)
                        if (staged.st_dev, staged.st_ino) != (published.st_dev, published.st_ino) or text != sentinel:
                            raise _conflict()
                finally:
                    os.close(fd)
                os.unlink(temporary, dir_fd=state)
                os.fsync(state)
            try:
                fd = os.open("secrets", _FLAGS, dir_fd=state)
            except FileNotFoundError:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=state)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(sentinel)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, "secrets", src_dir_fd=state, dst_dir_fd=state, follow_symlinks=False)
                os.fsync(state)
                os.unlink(temporary, dir_fd=state)
            else:
                try:
                    info = os.fstat(fd)
                    legacy._check_private(info, directory=False)
                    if info.st_nlink != 1 or os.read(fd, 1024) != sentinel:
                        raise _conflict()
                finally:
                    os.close(fd)
            os.fsync(state)
            checkpoint("sealed")

    def _import(self, db, vault, manifest, legacy, actor_id):
        for app in manifest["applications"]:
            vault.register_app(db, app["app_id"], manifest["tenant_id"], app["data_generation"], actor_id=actor_id)
            for scope in app["scopes"]:
                for secret in scope["secrets"]:
                    for version, value, current in self._values(legacy, scope, secret["name"]):
                        vault.import_version(db, app["app_id"], secret["name"], value, kind=secret["kind"],
                                             version=version, make_current=current, actor_id=actor_id)
                    for version, value, _ in self._values(legacy, scope, secret["name"]):
                        resolved = vault.resolve(db, app["app_id"], {"VERIFY": secret["name"]}, {secret["name"]: version})["VERIFY"]
                        if not hmac.compare_digest(value.encode(), resolved.encode()):
                            raise _conflict()
            for job_id in app["job_ids"]:
                db.execute("INSERT INTO vault_legacy_jobs VALUES (?,?,?)",
                           (job_id, app["app_id"], app["data_generation"]))

    def _verify_import(self, db, vault, manifest, legacy):
        expected_jobs = set()
        for app in manifest["applications"]:
            metadata = {item["name"]: item for item in vault.metadata(db, app["app_id"])}
            declared = {secret["name"] for scope in app["scopes"] for secret in scope["secrets"]}
            if set(metadata) != declared:
                raise _conflict()
            for scope in app["scopes"]:
                for secret in scope["secrets"]:
                    expected_versions = set()
                    item = metadata[secret["name"]]
                    if item["kind"] != secret["kind"] or item["data_generation"] != app["data_generation"]:
                        raise _conflict()
                    for version, value, current in self._values(legacy, scope, secret["name"]):
                        expected_versions.add(version)
                        resolved = vault.resolve(db, app["app_id"], {"VERIFY": secret["name"]}, {secret["name"]: version})["VERIFY"]
                        if not hmac.compare_digest(value.encode(), resolved.encode()) or current and item["current_version"] != version:
                            raise _conflict()
                    if expected_versions != {record["version"] for record in item["versions"]}:
                        raise _conflict()
            expected_jobs.update((job, app["app_id"], app["data_generation"]) for job in app["job_ids"])
        if expected_jobs != {tuple(row) for row in db.execute("SELECT job_id,app_id,data_generation FROM vault_legacy_jobs")}:
            raise _conflict()

    def migrate(self, manifest, *, actor_id, create_keyring=False, checkpoint=None):
        try:
            return self._migrate(manifest, actor_id=actor_id, create_keyring=create_keyring, checkpoint=checkpoint)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise _conflict() from None

    @staticmethod
    def _authenticate_existing(db, vault):
        """Completed replay validates today's vault, not yesterday's currents."""
        apps = list(db.execute("SELECT app_id,state,data_generation FROM vault_apps"))
        known = {app["app_id"] for app in apps}
        for table in ("vault_app_keys", "vault_secrets", "vault_secret_versions"):
            if {row[0] for row in db.execute(f"SELECT DISTINCT app_id FROM {table}")} - known:
                raise _conflict()
        for app in apps:
            if app["state"] == "active":
                vault.metadata(db, app["app_id"])
            else:
                tombstone = db.execute("SELECT plan FROM vault_tombstones WHERE app_id=? AND data_generation=?",
                                       (app["app_id"], app["data_generation"])).fetchone()
                if tombstone is None:
                    raise _conflict()
                vault.verify_deletion(db, json.loads(tombstone["plan"]))

    def _migrate(self, manifest, *, actor_id, create_keyring=False, checkpoint=None):
        manifest = self._manifest(manifest)
        checkpoint = checkpoint or (lambda phase: None)
        with worker_lock(self.settings):
            store = self._store()
            with store._connection() as db:
                self.ensure_schema(db)
            with store._connection(write=True) as db:
                existing = vault_format.state(db)
                record = db.execute("SELECT * FROM vault_migrations WHERE migration_id=?", (manifest["migration_id"],)).fetchone()
                if existing:
                    if (record is None or record["manifest"] != _json(manifest)
                            or existing["migration_id"] != manifest["migration_id"] or existing["format"] != 1
                            or existing["phase"] not in {"maintenance", "imported", "sealing", "active"}
                            or existing["tenant_id"] != self.settings.feishu_tenant_key
                            or existing["feishu_app_id"] != self.settings.feishu_app_id):
                        raise _conflict()
                    inventory = json.loads(record["inventory"])
                else:
                    inventory = self._inventory(manifest, self._legacy())
                    self._coverage(db, manifest, inventory)
                    self._provision_keyring(db, create=create_keyring)
                    db.execute("INSERT INTO vault_migrations VALUES (?,?,?,?,?)",
                               (manifest["migration_id"], _json(manifest), _json(inventory), actor_id, time.time()))
                    vault_format.set_format(db, self.settings, phase="maintenance", migration_id=manifest["migration_id"])
                    SecretVault._audit(db, actor_id, "migration_started", detail={"migration_id": manifest["migration_id"]})
                    existing = {"phase": "maintenance"}
            checkpoint(existing["phase"])
            vault = SecretVault(self.settings)
            if existing["phase"] == "active":
                with store._connection() as db:
                    db.execute("BEGIN")
                    vault_format.require_ready(self.settings, db)
                    self._authenticate_existing(db, vault)
            if existing["phase"] == "maintenance":
                with store._connection(write=True) as db:
                    if self._inventory(manifest, self._legacy()) != inventory:
                        raise _conflict()
                    self._coverage(db, manifest, inventory)
                    self._import(db, vault, manifest, self._legacy(), actor_id)
                    self._verify_import(db, vault, manifest, self._legacy())
                    vault_format.set_format(db, self.settings, phase="imported", migration_id=manifest["migration_id"])
                existing["phase"] = "imported"
                checkpoint("imported")
            if existing["phase"] == "imported":
                with store._connection(write=True) as db:
                    self._coverage(db, manifest, inventory)
                    self._verify_import(db, vault, manifest, self._legacy())
                    vault_format.set_format(db, self.settings, phase="sealing", migration_id=manifest["migration_id"])
                existing["phase"] = "sealing"
                checkpoint("sealing")
            if existing["phase"] == "sealing":
                self._seal(manifest, inventory, checkpoint)
                with store._connection(write=True) as db:
                    self._coverage(db, manifest, inventory)
                    self._verify_import(db, vault, manifest, self._legacy(
                        quarantine="secrets-quarantine-" + manifest["migration_id"]))
                    vault_format.set_format(db, self.settings, phase="active", migration_id=manifest["migration_id"])
                    SecretVault._audit(db, actor_id, "migration_activated", detail={"migration_id": manifest["migration_id"]})
            return {"phase": "active", "migration_id": manifest["migration_id"],
                    "applications": len(manifest["applications"]),
                    "quarantine": "secrets-quarantine-" + manifest["migration_id"],
                    "backup_copies_deleted": False}

    def provision(self, manifest=None, *, actor_id):
        """Create only a KEK, so its independent backup can precede encryption."""
        try:
            return self._provision(manifest, actor_id=actor_id)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise _conflict() from None

    def _provision(self, manifest=None, *, actor_id):
        if manifest is not None:
            manifest = self._manifest(manifest)
        with worker_lock(self.settings):
            store = self._store()
            with store._connection() as db:
                self.ensure_schema(db)
            with store._connection(write=True) as db:
                if (vault_format.state(db) is not None
                        or db.execute("SELECT 1 FROM vault_app_keys LIMIT 1").fetchone()):
                    raise DeployError("VAULT_KEY_UNAVAILABLE", "已有加密格式或加密数据，禁止初始化替代根密钥。")
                if manifest is not None:
                    inventory = self._inventory(manifest, self._legacy())
                    self._coverage(db, manifest, inventory)
                elif (any(db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                          for table in ("apps", "jobs", "offline_operations", "app_secret_scopes"))
                      or self._inventory({"applications": []}, self._legacy())):
                    raise DeployError("VAULT_MIGRATION_REQUIRED", "已有状态必须先提供完整迁移清单进行预检。")
                existed = self.settings.secret_kek_file.exists() or self.settings.secret_kek_file.is_symlink()
                vault = self._provision_keyring(db, create=True)
                SecretVault._audit(db, actor_id, "kek_provision_checked" if existed else "kek_provisioned",
                                   detail={"kek_id": vault.keyring.active})
                return {"key_id": vault.keyring.active, "created": not existed,
                        "next_step": "请先制作并核验独立 KEK 副本，再执行 vault-init 或 vault-migrate。"}

    def initialize(self, backup, *, actor_id, create_keyring=False):
        try:
            return self._initialize(backup, actor_id=actor_id, create_keyring=create_keyring)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise _conflict() from None

    def _initialize(self, backup, *, actor_id, create_keyring=False):
        validate_backup(backup)
        with worker_lock(self.settings):
            store = self._store()
            with store._connection() as db:
                self.ensure_schema(db)
            with store._connection(write=True) as db:
                existing = vault_format.state(db)
                if existing is not None:
                    row = db.execute("SELECT * FROM vault_migrations WHERE migration_id=?", (existing["migration_id"],)).fetchone()
                    if row is None:
                        raise _conflict()
                    manifest, inventory = json.loads(row["manifest"]), json.loads(row["inventory"])
                    self._manifest(manifest)
                    if (manifest["applications"] or manifest["backup"] != backup or existing["format"] != 1
                            or existing["phase"] not in {"sealing", "active"}):
                        raise _conflict()
                    vault = SecretVault(self.settings)
                    if existing["phase"] == "active":
                        vault_format.require_ready(self.settings, db)
                        self._authenticate_existing(db, vault)
                        return {"phase": "active", "migration_id": manifest["migration_id"], "applications": 0}
                else:
                    if any(db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                           for table in ("apps", "jobs", "offline_operations", "vault_app_keys", "vault_tombstones", "app_secret_scopes")):
                        raise DeployError("VAULT_MIGRATION_REQUIRED", "已有应用、历史任务或密钥记录，必须使用显式迁移清单。")
                    manifest = {"format": 1, "migration_id": uuid.uuid4().hex, "tenant_id": self.settings.feishu_tenant_key,
                                "feishu_app_id": self.settings.feishu_app_id, "backup": backup, "applications": []}
                    inventory = self._inventory(manifest, self._legacy())
                    self._provision_keyring(db, create=create_keyring)
                    db.execute("INSERT INTO vault_migrations VALUES (?,?,?,?,?)",
                               (manifest["migration_id"], _json(manifest), _json(inventory), actor_id, time.time()))
                    vault_format.set_format(db, self.settings, phase="sealing", migration_id=manifest["migration_id"])
                    SecretVault._audit(db, actor_id, "vault_initialized", detail={"recovery_record": backup})
            self._seal(manifest, inventory, lambda phase: None)
            with store._connection(write=True) as db:
                vault_format.set_format(db, self.settings, phase="active", migration_id=manifest["migration_id"])
            return {"phase": "active", "migration_id": manifest["migration_id"], "applications": 0}
