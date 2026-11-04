"""Complete declared missing configuration without exposing or rotating existing values."""

from contextlib import contextmanager
import fcntl
import json
import os
import re
import secrets
import sqlite3
import stat

from .errors import DeployError
from .manifest import load_manifest
from .secrets_store import SecretStore
from .source import validate_ref, validate_repository


class DeploymentSecrets:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.secrets = SecretStore(settings)
        self.encrypted = getattr(settings, "secret_backend", "legacy") == "encrypted"

    @contextmanager
    def _configuration_scope(self, job_id, *, write=False, actor=None):
        arguments = {"write": write, **({"actor": actor} if actor else {})}
        if self.encrypted:
            arguments["include_connection"] = True
        with self.store.secret_configuration_scope(job_id, **arguments) as values:
            if self.encrypted:
                yield values
            else:
                yield (*values, None)

    def _vault(self):
        from .secret_vault import SecretVault
        return SecretVault(self.settings)

    def _requirements(self, job):
        """Only a validated, durably pinned source snapshot may supply reference names."""
        try:
            payload, snapshot, execution = job["input"], job["snapshot"], job["execution"]
            repository = validate_repository(payload["repository"], self.settings)
            validate_ref(payload["ref"])
            if repository != payload["repository"] or snapshot["repository"] != repository:
                raise ValueError("repository identity")
            commit = snapshot["commit"]
            if not isinstance(commit, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
                raise ValueError("commit")
            if execution.get("commit") != commit:
                raise ValueError("pin")
            manifest = load_manifest(json.dumps(snapshot["manifest"]), self.settings)
            if manifest["app"]["subdomain"] != payload["subdomain"]:
                raise ValueError("subdomain identity")
            uses = {}
            for service, definition in sorted(manifest["services"].items()):
                for variable, name in sorted(definition["secretRefs"].items()):
                    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
                        raise ValueError("reference name")
                    uses.setdefault(name, []).append({"service": service, "environmentVariable": variable})
            if not uses:
                raise ValueError("references")
            return repository, payload["subdomain"], uses
        except (DeployError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise DeployError("SECRET_CONFIGURATION_NOT_AVAILABLE", "该任务缺少可核验的固定配置快照。") from None

    def _describe(self, job, can_generate, db=None):
        repository, subdomain, uses = self._requirements(job)
        if self.encrypted:
            from .vault_format import require_ready
            require_ready(self.settings, db)
            # No registration or legacy fallback here: missing/corrupt records require migration or recovery.
            metadata = {item["name"]: item for item in self._vault().metadata(db, job["app_id"])}
            items = []
            for name in sorted(uses):
                existing = metadata.get(name)
                declared_master = any(use["environmentVariable"] == "MASTER_KEY" for use in uses[name])
                if existing and declared_master and existing["kind"] != "platform_managed_master":
                    raise DeployError("SECRET_KIND_CONFLICT", "已有运行密钥不能通过清单改为数据主密钥，请专项迁移。")
                managed = existing["kind"] == "platform_managed_master" if existing else declared_master
                state = "missing"
                if existing:
                    current = next((version for version in existing["versions"]
                                    if version["version"] == existing["current_version"]), None)
                    state = "configured" if current and current["status"] == "active" else "unavailable"
                items.append({"name": name, "uses": uses[name], "state": state, "managed": managed})
            return {"jobId": job["id"], "repository": repository, "subdomain": subdomain,
                    "canGenerate": can_generate, "items": items}
        return {
            "jobId": job["id"], "repository": repository, "subdomain": subdomain,
            "canGenerate": can_generate,
            "items": [{"name": name, "uses": uses[name], "state": self.secrets.inspect(repository, subdomain, name),
                       "managed": name == "MASTER_KEY"
                       and all(use["environmentVariable"] == "MASTER_KEY" for use in uses[name])}
                      for name in sorted(uses)],
        }

    def describe(self, job_id, *, actor=None):
        """Read only authorized reference names, pointer metadata and file attributes."""
        try:
            with self._configuration_scope(job_id, actor=actor) as (job, can_generate, db):
                return self._describe(job, can_generate, db)
        except DeployError:
            raise
        except Exception:
            raise DeployError("SECRET_UNAVAILABLE", "无法安全读取配置状态。") from None

    @contextmanager
    def _worker_lock(self):
        fd = None
        try:
            with self.secrets._open_state_directory() as state_fd:
                fd = os.open("worker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=state_fd)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                    raise OSError("unsafe worker lock")
                current = os.stat("worker.lock", dir_fd=state_fd, follow_symlinks=False)
                if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                    raise OSError("worker lock changed")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                yield
        except BlockingIOError:
            raise DeployError("SECRET_CONFIGURATION_BUSY", "部署执行器正在工作，请稍后重试。") from None
        except (OSError, sqlite3.Error):
            raise DeployError("SECRET_UNAVAILABLE", "无法安全保存配置，请刷新状态后重试。") from None
        finally:
            if fd is not None:
                os.close(fd)

    def save(self, job_id, values, *, actor=None):
        try:
            return self._save(job_id, values, actor=actor)
        except DeployError:
            raise
        except Exception:
            # Dependencies must never turn submitted values into an ASGI traceback.
            raise DeployError("SECRET_UNAVAILABLE", "无法安全保存配置，请刷新状态后重试。") from None

    def _save(self, job_id, values, *, actor=None):
        if not isinstance(values, dict) or len(values) > 2048:
            raise DeployError("INVALID_REQUEST", "请提供需要补充的配置。")
        for name, value in values.items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
                raise DeployError("INVALID_REQUEST", "配置引用名称不符合要求。")
            SecretStore.validate_value(value)
        with self._worker_lock():
            with self._configuration_scope(job_id, write=True, actor=actor) as (job, can_generate, db):
                metadata = self._describe(job, can_generate, db)
                states = {item["name"]: item["state"] for item in metadata["items"]}
                if set(values) - states.keys():
                    raise DeployError("INVALID_REQUEST", "只能补充该固定快照声明的配置。")
                managed = {item["name"] for item in metadata["items"] if item["managed"]}
                if managed.intersection(values):
                    raise DeployError("SECRET_MANAGED_INPUT", "平台管理的配置不接受客户端提交值。")
                generate = []
                for name in sorted(managed):
                    if states[name] == "unavailable" or (states[name] == "missing" and not can_generate):
                        raise DeployError("SECRET_UNAVAILABLE", "原有平台密钥需要恢复，不能生成替代值。")
                    if states[name] == "missing":
                        generate.append(name)
                if generate:
                    if "unavailable" in states.values():
                        raise DeployError("SECRET_UNAVAILABLE", "请先恢复不可用的配置，再补充缺少的值。")
                    required = {name for name, state in states.items() if state == "missing" and name not in managed}
                    if required - values.keys():
                        raise DeployError("INVALID_REQUEST", "请先补齐所有需要手动填写的配置。")
                elif not values:
                    if all(state == "configured" for state in states.values()):
                        return metadata
                    raise DeployError("INVALID_REQUEST", "请提供需要补充的配置。")
                # Validate the complete batch before the first filesystem write.
                for name in values:
                    if states[name] == "configured":
                        raise DeployError("SECRET_ALREADY_CONFIGURED", "已有配置不能通过此入口覆盖。")
                    if states[name] != "missing":
                        raise DeployError("SECRET_UNAVAILABLE", "现有配置状态异常，不能自动修复。")
                vault = self._vault() if self.encrypted else None
                for name, value in sorted(values.items()):
                    if vault:
                        vault.create_missing(db, job["app_id"], name, value, actor_id=actor["id"])
                    else:
                        self.secrets.create_missing(metadata["repository"], metadata["subdomain"], name, value)
                # A failed user-value write must not leave a newly generated master key behind.
                # A published key is always reused on retries, including a lost success response.
                for name in generate:
                    if vault:
                        vault.create_missing(db, job["app_id"], name, secrets.token_hex(32),
                            kind="platform_managed_master", actor_id=actor["id"])
                    else:
                        self.secrets.create_missing(metadata["repository"], metadata["subdomain"], name,
                                                    secrets.token_hex(32))
                return self._describe(job, can_generate, db)
