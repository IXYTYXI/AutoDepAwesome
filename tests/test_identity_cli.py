import json
import os

import pytest

from awesome_deploy.cli import load_settings, main
from awesome_deploy.errors import DeployError


@pytest.fixture(autouse=True)
def restore_cli_umask():
    previous = os.umask(0o077)
    os.umask(previous)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"state_dir": str(tmp_path / "state"), "public_url": "https://console.example.com",
                                "auth_mode": "feishu", "feishu_app_id": "cli_platform",
                                "feishu_tenant_key": "tenant", "feishu_app_secret_file": "app-secret"}))
    return path


@pytest.mark.parametrize("patch", [
    {"auth_mode": "mixed"}, {"auth_mode": "basic"}, {"feishu_app_id": ""}, {"feishu_tenant_key": ""},
    {"feishu_app_secret_file": None}, {"public_url": "http://127.0.0.1:8088"},
    {"public_url": "https://console.example.com/path"}, {"public_url": "https://console.example.com?x=y"},
])
def test_incomplete_or_ambiguous_login_config_is_rejected(config, patch):
    config.write_text(json.dumps({**json.loads(config.read_text()), **patch}))
    with pytest.raises(DeployError):
        load_settings(config)


def test_cli_user_grant_and_revoke_invalidates_sessions(config, capsys):
    from awesome_deploy.identity import IdentityStore
    settings = load_settings(config)
    assert settings.feishu_app_secret_file == config.parent / "app-secret"
    identity = IdentityStore(settings)
    actor = identity.login_user({"open_id": "ou_test", "tenant_key": "tenant", "name": "Test"})
    token = identity.create_session(actor["id"])
    assert main(["--config", str(config), "user-set", "--user-id", actor["id"], "--admin", "true"]) == 0
    assert identity.get_user(actor["id"])["is_admin"] == 1
    assert identity.session(token) is None
    main(["--config", str(config), "user-list"])
    output = capsys.readouterr().out
    assert actor["id"] in output and token not in output and "csrf" not in output
    main(["--config", str(config), "auth-revoke-all"])
    identity.provider.close()
