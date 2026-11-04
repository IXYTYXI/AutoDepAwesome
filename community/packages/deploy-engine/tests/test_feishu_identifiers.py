import json

import httpx
import pytest

from awesome_deploy.config import Settings, validate_identity_settings
from awesome_deploy.feishu import FeishuProvider, FeishuUnavailable
from awesome_deploy.intake_server import load_relay


# These are the platform's configuration boundaries, not a claim about all
# identifiers accepted by Feishu's upstream APIs.
APP_IDS = [
    pytest.param("cli_a", True, id="minimum-suffix"),
    pytest.param("cli_A09z", True, id="ascii-alphanumeric"),
    pytest.param("cli_" + "A" * 80, True, id="maximum-suffix"),
    pytest.param("cli_" + "A" * 81, False, id="suffix-too-long"),
    pytest.param("cli_", False, id="empty-suffix"),
    pytest.param("", False, id="empty"),
    pytest.param("CLI_abc", False, id="wrong-prefix-case"),
    pytest.param("cli_test_app", False, id="extra-underscore"),
    pytest.param("cli_a-b", False, id="hyphen"),
    pytest.param("cli_应用", False, id="non-ascii"),
    pytest.param(" cli_abc", False, id="leading-space"),
    pytest.param("cli_abc\n", False, id="trailing-newline"),
    pytest.param(None, False, id="null"),
    pytest.param(123, False, id="number"),
    pytest.param(True, False, id="boolean"),
    pytest.param([], False, id="list"),
    pytest.param({}, False, id="object"),
]


@pytest.mark.parametrize("app_id,valid", APP_IDS)
def test_identity_configuration_app_id_boundaries(tmp_path, app_id, valid):
    settings = Settings(
        auth_mode="feishu", feishu_app_id=app_id, feishu_tenant_key="tenant",
        feishu_app_secret_file=tmp_path / "secret", public_url="https://deploy.example.com",
    )
    if valid:
        validate_identity_settings(settings)
    else:
        with pytest.raises(ValueError, match="Feishu identity settings"):
            validate_identity_settings(settings)


@pytest.mark.parametrize("app_id,valid", APP_IDS)
def test_provider_app_id_boundaries(app_id, valid):
    settings = Settings(feishu_app_id=app_id)
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no network expected"))) as http:
        if valid:
            provider = FeishuProvider(settings, client=http)
            assert provider.settings.feishu_app_id == app_id
        else:
            with pytest.raises(FeishuUnavailable):
                FeishuProvider(settings, client=http)


@pytest.mark.parametrize("app_id,valid", APP_IDS)
def test_independent_intake_app_id_boundaries(tmp_path, app_id, valid):
    secret = tmp_path / "secret"
    secret.write_text("test-secret-value")
    secret.chmod(0o600)
    config = tmp_path / "intake.json"
    config.write_text(json.dumps({
        "database_path": str(tmp_path / "outbox.sqlite3"),
        "platform_origin": "https://deploy.example.com",
        "platform_token_file": str(secret), "intake_token_file": str(secret),
        "integrity_key_file": str(secret), "feishu_app_secret_file": str(secret),
        "feishu_app_id": app_id, "base_token": "a" * 16, "intake_table_id": "tbl" + "a" * 8,
    }))
    if valid:
        relay, token = load_relay(config)
        try:
            assert token == "test-secret-value"
        finally:
            relay.platform.close()
            relay.base.close()
    else:
        with pytest.raises(ValueError, match="飞书 App ID 无效"):
            load_relay(config)
