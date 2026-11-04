import json
import stat

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError


def test_password_hash_and_verification(tmp_path):
    from awesome_deploy.auth import initialize_credentials, verify_credentials

    file = tmp_path / "admin.json"
    initialize_credentials(file, "operator", "long-test-password")
    assert "long-test-password" not in file.read_text()
    assert verify_credentials(file, "operator", "long-test-password")
    assert not verify_credentials(file, "operator", "wrong")
    assert not verify_credentials(file, "someone", "long-test-password")
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    with pytest.raises(DeployError, match="存在"):
        initialize_credentials(file, "operator", "other-long-password")


def test_missing_credentials_fail_closed(tmp_path):
    from awesome_deploy.auth import verify_credentials

    assert not verify_credentials(tmp_path / "missing", "operator", "password")


@pytest.mark.parametrize(
    "username,password",
    [("管理员", "long-test-password"), ("operator", "中文密码足够长但不支持"), ("operator", "x" * 1025)],
)
def test_unusable_basic_credentials_rejected(tmp_path, username, password):
    from awesome_deploy.auth import initialize_credentials

    with pytest.raises(DeployError):
        initialize_credentials(tmp_path / "admin.json", username, password)


def test_secrets_are_scoped_and_not_overwritten(tmp_path):
    from awesome_deploy.secrets_store import SecretStore

    store = SecretStore(Settings(state_dir=tmp_path))
    repo = "https://github.com/org/repo.git"
    store.set(repo, "demo", "api-key", "a-very-secret-value")
    assert store.resolve(repo, "demo", {"API_KEY": "api-key"}) == {"API_KEY": "a-very-secret-value"}
    with pytest.raises(DeployError) as error:
        store.resolve(repo, "other", {"API_KEY": "api-key"})
    assert error.value.code == "SECRET_NOT_FOUND"
    assert "a-very-secret-value" not in str(error.value)
    with pytest.raises(DeployError):
        store.set(repo, "demo", "../escape", "secret")
    for f in (tmp_path / "secrets").rglob("*.json"):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
        assert json.loads(f.read_text())["version"]


def test_secret_snapshot_versions_are_immutable(tmp_path):
    from awesome_deploy.secrets_store import SecretStore

    store = SecretStore(Settings(state_dir=tmp_path))
    repo = "https://github.com/org/repo.git"
    v1 = store.set(repo, "demo", "key", "old-value")
    store.set(repo, "demo", "key", "new-value")
    assert store.resolve(repo, "demo", {"KEY": "key"}, {"key": v1}) == {"KEY": "old-value"}
    assert store.versions(repo, "demo", {"KEY": "key"})["key"] != v1
