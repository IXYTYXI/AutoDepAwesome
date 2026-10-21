"""Trusted-host CLI for explicit vault maintenance and audited runtime secrets."""

from dataclasses import replace
import getpass
import json
import os
from pathlib import Path

from .errors import DeployError
from .secret_vault import SecretVault
from .vault_migration import VaultMigration, load_record, validate_backup, worker_lock
from .vault_format import require_ready, state
from .vault_runtime import VaultRuntime


def add_arguments(sub):
    for name, description in (
        ("vault-keyring-init", "仅显式创建独立根密钥，随后必须备份；不迁移数据"),
        ("vault-init", "仅为空平台初始化加密密钥库"),
        ("vault-migrate", "按已核对清单迁移全部旧密钥，原文封存而非删除"),
        ("vault-check", "核验加密密钥与历史冻结版本，不输出值"),
        ("vault-rewrap", "使用keyring中的active KEK重新包裹DEK，不改变业务密钥"),
    ):
        parser = sub.add_parser(name, help=description)
        parser.add_argument("--kek-file", type=Path, help="显式独立KEK文件；迁移旧配置时必填")
        if name in {"vault-keyring-init", "vault-migrate"}:
            help_text = ("完整归属与历史清单；仅生成KEK时允许 backup:null 表示恢复核验待完成"
                         if name == "vault-keyring-init" else "含已核验恢复记录的完整迁移清单；backup 不得为空")
            parser.add_argument("--manifest", type=Path, required=name == "vault-migrate", help=help_text)
        if name in {"vault-init", "vault-rewrap"}:
            parser.add_argument("--recovery-record", type=Path, required=True)
        if name == "vault-rewrap":
            parser.add_argument("--reason", required=True)


def _target(settings, key_path=None):
    chosen = key_path or settings.secret_kek_file
    if chosen is None or settings.auth_mode != "feishu":
        raise DeployError("INVALID_CONFIG", "请先配置飞书身份，并显式提供独立KEK文件路径。")
    return replace(settings, secret_backend="encrypted", secret_kek_file=Path(os.path.abspath(chosen)))


def _actor():
    return "cli-uid-" + str(os.getuid())


def _reason(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or not all(c.isprintable() for c in value):
        raise DeployError("INVALID_REQUEST", "请输入简短、非敏感的操作原因。")
    return value


def _store(settings):
    from .store import Store
    return Store(settings)


def _check_history(settings, store, db, vault):
    checked, revoked, live_revoked = 0, 0, []
    runtime = VaultRuntime(settings)
    for row in db.execute("SELECT * FROM jobs ORDER BY id"):
        job = store._job(row)
        snapshot, execution = job.get("snapshot") or {}, job.get("execution") or {}
        if store._data_cleared(job) or not runtime.has_retained_inputs(job["status"], snapshot, execution):
            continue
        binding = runtime._validate_binding(job, db, vault)
        app = vault._app(db, binding["app_id"])
        services = snapshot.get("manifest", {}).get("services")
        frozen = snapshot.get("secret_versions", execution.get("secret_versions"))
        if not isinstance(services, dict) or not isinstance(frozen, dict) or set(frozen) - set(services):
            raise runtime._invalid()
        job_revoked = False
        for service in services:
            refs = runtime._refs(snapshot, service)
            versions = frozen.get(service, {})
            if not isinstance(versions, dict) or set(versions) != set(refs.values()):
                raise runtime._invalid()
            runtime._check_kinds(db, vault, app["app_id"], refs)
            for name, version in versions.items():
                vault._read(db, app, vault._record(db, app, name), version, allow_revoked=True)
                status = db.execute("SELECT status FROM vault_secret_versions WHERE app_id=? AND name=? AND version=?",
                                    (app["app_id"], name, version)).fetchone()[0]
                checked += 1
                if status == "revoked":
                    revoked += 1
                    job_revoked = True
        active = db.execute("SELECT 1 FROM apps WHERE id=? AND active_release_id=?", (app["app_id"], job["id"])).fetchone()
        if job_revoked and active and not execution.get("resources_released"):
            live_revoked.append(job["id"])
    pending = db.execute("SELECT count(*) FROM vault_revocations WHERE status <> 'succeeded'").fetchone()[0]
    return {"frozen_references_checked": checked, "revoked_references": revoked,
            "pending_revocations": pending, "revoked_active_releases": live_revoked}


def command(settings, args):
    target = _target(settings, args.kek_file)
    migration = VaultMigration(target)
    if args.command == "vault-keyring-init":
        result = migration.provision(load_record(args.manifest) if args.manifest else None, actor_id=_actor())
    elif args.command == "vault-init":
        result = migration.initialize(load_record(args.recovery_record), actor_id=_actor())
    elif args.command == "vault-migrate":
        result = migration.migrate(load_record(args.manifest), actor_id=_actor())
    else:
        with worker_lock(target):
            store = _store(target)
            with store._connection(write=args.command == "vault-rewrap") as db:
                if args.command != "vault-rewrap":
                    db.execute("BEGIN")
                require_ready(target, db)
                vault = SecretVault(target)
                if args.command == "vault-rewrap":
                    backup = validate_backup(load_record(args.recovery_record))
                    reason = _reason(args.reason)
                    vault.rewrap(db, actor_id=_actor())
                    vault._audit(db, _actor(), "kek_rotation_recorded", detail={"reason": reason, "recovery_record": backup,
                                                                            "kek_id": vault.keyring.active})
                apps = list(db.execute("SELECT app_id FROM vault_apps WHERE state='active' ORDER BY app_id"))
                versions = sum(len(secret["versions"]) for app in apps for secret in vault.metadata(db, app["app_id"]))
                history = _check_history(target, store, db, vault)
                result = {"phase": state(db)["phase"], "applications": len(apps), "versions": versions,
                          **history, "key_id": vault.keyring.active}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("pending_revocations") or result.get("revoked_active_releases"):
        return 1
    return 0


def set_secret(settings, args):
    if (not args.app_id or args.repository is not None or args.subdomain is not None
            or args.name == "MASTER_KEY"):
        raise DeployError("INVALID_REQUEST", "加密模式须使用app-id；平台主密钥由受控初始化生成，不能通过secret-set填写。")
    if args.expected_current_version is not None:
        reason = _reason(args.reason)
    else:
        reason = _reason(args.reason or "可信服务器 CLI 补录运行配置")
    # Prompt values only at runtime, never accept an argv/env/file secret argument.
    value = getpass.getpass("秘密配置值（输入不回显）：")
    with worker_lock(settings):
        store = _store(settings)
        with store._connection(write=True) as db:
            require_ready(settings, db)
            if not db.execute("SELECT 1 FROM apps WHERE id=?", (args.app_id,)).fetchone():
                raise DeployError("ACCESS_DENIED", "应用目标不存在。")
            vault = SecretVault(settings)
            if args.expected_current_version is None:
                store._check_offline_gate(db, args.app_id, None)
                version = vault.create_missing(db, args.app_id, args.name, value, actor_id=_actor())
            else:
                from .vault_service import VaultService
                VaultService._require_no_offline(db, args.app_id)
                version = vault.rotate(db, args.app_id, args.name, value, args.expected_current_version, actor_id=_actor())
            vault._audit(db, _actor(), "cli_secret_change", args.app_id, args.name, version, {"reason": reason})
    print(json.dumps({"name": args.name, "version": version}, ensure_ascii=False))
    return 0
