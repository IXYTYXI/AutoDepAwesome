"""Explicit vault format and maintenance gate, independent of secret material."""

import time

from .errors import DeployError


def ensure_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS vault_format (
        id INTEGER PRIMARY KEY CHECK(id=1), format INTEGER NOT NULL,
        phase TEXT NOT NULL, tenant_id TEXT NOT NULL, feishu_app_id TEXT NOT NULL,
        migration_id TEXT NOT NULL, updated_at REAL NOT NULL
    )''')


def state(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vault_format'").fetchone():
        return None
    row = db.execute('SELECT * FROM vault_format WHERE id=1').fetchone()
    return dict(row) if row else None


def require_ready(settings, db):
    record = state(db)
    backend = getattr(settings, 'secret_backend', 'legacy')
    if record is None:
        if backend == 'legacy':
            return
        raise DeployError('VAULT_NOT_INITIALIZED', '加密密钥库尚未显式初始化或完成迁移。')
    if record['format'] != 1 or record['phase'] not in {'maintenance', 'imported', 'sealing', 'active'}:
        raise DeployError('VAULT_FORMAT_UNSUPPORTED', '密钥库格式无法识别，请使用兼容版本恢复。')
    if record['phase'] != 'active':
        raise DeployError('VAULT_MAINTENANCE', '密钥库正在维护，暂不接受新的部署或配置变更。')
    if backend != 'encrypted':
        raise DeployError('VAULT_FORMAT_DOWNGRADE', '该状态已启用加密格式，禁止降级读取或写入旧明文格式。')
    if (settings.auth_mode != 'feishu' or record['tenant_id'] != settings.feishu_tenant_key
            or record['feishu_app_id'] != settings.feishu_app_id):
        raise DeployError('VAULT_IDENTITY_CONFLICT', '密钥库所属企业或飞书应用不匹配，必须显式迁移身份。')


def set_format(db, settings, *, phase, migration_id):
    if not db.in_transaction:
        raise DeployError('VAULT_TRANSACTION_REQUIRED', '格式变更必须在受控事务中执行。')
    if phase not in {'maintenance', 'imported', 'sealing', 'active'} or not migration_id:
        raise DeployError('VAULT_FORMAT_UNSUPPORTED', '密钥库迁移阶段无效。')
    existing = state(db)
    if existing and (existing['tenant_id'] != settings.feishu_tenant_key
                     or existing['feishu_app_id'] != settings.feishu_app_id
                     or existing['migration_id'] != migration_id):
        raise DeployError('VAULT_IDENTITY_CONFLICT', '迁移标识与原记录不一致。')
    db.execute('''INSERT INTO vault_format VALUES(1,1,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET phase=excluded.phase, updated_at=excluded.updated_at''',
        (phase, settings.feishu_tenant_key, settings.feishu_app_id, migration_id, time.time()))
