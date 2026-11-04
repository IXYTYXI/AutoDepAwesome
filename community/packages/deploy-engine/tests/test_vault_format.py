from dataclasses import replace
import sqlite3

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.vault_format import ensure_schema, require_ready, set_format


def fixture(tmp_path):
    db = sqlite3.connect(':memory:', isolation_level=None)
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    settings = Settings(state_dir=tmp_path, auth_mode='feishu',
                        feishu_app_id='cli_test', feishu_tenant_key='tenant')
    return db, settings


def test_encrypted_format_requires_explicit_initialization(tmp_path):
    db, settings = fixture(tmp_path)
    with pytest.raises(DeployError, match='初始化'):
        require_ready(replace(settings, secret_backend='encrypted'), db)


def test_active_format_never_falls_back_to_plaintext(tmp_path):
    db, settings = fixture(tmp_path)
    db.execute('BEGIN IMMEDIATE')
    set_format(db, settings, phase='active', migration_id='test')
    db.commit()
    with pytest.raises(DeployError) as error:
        require_ready(settings, db)
    assert error.value.code == 'VAULT_FORMAT_DOWNGRADE'
    require_ready(replace(settings, secret_backend='encrypted'), db)


@pytest.mark.parametrize('phase', ['maintenance', 'imported', 'sealing'])
def test_incomplete_migration_blocks_both_formats(tmp_path, phase):
    db, settings = fixture(tmp_path)
    db.execute('BEGIN IMMEDIATE')
    set_format(db, settings, phase=phase, migration_id='test')
    db.commit()
    for backend in ('legacy', 'encrypted'):
        with pytest.raises(DeployError):
            require_ready(replace(settings, secret_backend=backend), db)


def test_changed_tenant_or_feishu_app_is_not_an_implicit_migration(tmp_path):
    db, settings = fixture(tmp_path)
    db.execute('BEGIN IMMEDIATE')
    set_format(db, settings, phase='active', migration_id='test')
    db.commit()
    for changed in ({'feishu_tenant_key':'other'}, {'feishu_app_id':'cli_other'}):
        with pytest.raises(DeployError) as error:
            require_ready(replace(settings, secret_backend='encrypted', **changed), db)
        assert error.value.code == 'VAULT_IDENTITY_CONFLICT'


def test_format_mutation_requires_transaction(tmp_path):
    db, settings = fixture(tmp_path)
    with pytest.raises(DeployError):
        set_format(db, settings, phase='active', migration_id='test')
