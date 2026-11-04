import json

import pytest

from awesome_deploy.cli import load_settings
from awesome_deploy.config import Settings, validate_identity_settings
from awesome_deploy.errors import DeployError
from awesome_deploy.source import validate_repository


@pytest.mark.parametrize("patch", [
    {"state_dir": None}, {"state_dir": ""}, {"state_dir": "\x00"},
    {"auth_file": ""}, {"rootlesskit_socket": ""}, {"shared_sessions_file": ""},
    {"git_askpass": False}, {"git_askpass": ""},
    {"docker_binary": None}, {"docker_binary": ""}, {"docker_binary": "docker\n"},
    {"gateway_command": [""]}, {"gateway_command": ["sudo", "\x00"]},
    {"runtime_user": "root"}, {"runtime_user": "0:0"}, {"runtime_user": None},
    {"builder_name": ""}, {"admin_user": None}, {"admin_user": "user:name"},
    {"git_hosts": [""]}, {"git_hosts": ["https://github.com"]},
    {"reserved_names": [""]}, {"banned_words": [""]},
    {"port_start": 80, "port_end": 81},
    {"base_domain": ".".join(["a" * 63] * 4) + ".com"},
    {"max_cpu": 10 ** 400},
])
def test_invalid_configuration_is_rejected_before_runtime(tmp_path, patch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(patch))
    with pytest.raises(DeployError) as caught:
        load_settings(path)
    assert caught.value.code == "INVALID_CONFIG"


INVALID_ORIGINS = [
    123, None, [], "", "https://example.com\n", "https://exa\tmple.com",
    " https://example.com", "https://@example.com", "https://example.com?",
    "https://example.com#", "https://example.com\\other", "https://example.com:0",
    "https://example.com:65536", "https://example.com:invalid", "https://example.com:",
]


@pytest.mark.parametrize("origin", INVALID_ORIGINS)
def test_invalid_origins_rejected_by_cli(tmp_path, origin):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"public_url": origin}))
    with pytest.raises(DeployError) as caught:
        load_settings(path)
    assert caught.value.code == "INVALID_CONFIG"


@pytest.mark.parametrize("origin", INVALID_ORIGINS)
def test_invalid_origins_rejected_by_direct_identity_configuration(tmp_path, origin):
    settings = Settings(auth_mode="feishu", feishu_app_id="cli_test", feishu_tenant_key="tenant",
                        feishu_app_secret_file=tmp_path / "secret", public_url=origin)
    with pytest.raises(ValueError):
        validate_identity_settings(settings)


def test_git_host_case_matches_repository_normalization(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"git_hosts": ["GitHub.com", "GitLab.Example.com"]}))
    settings = load_settings(path)
    assert validate_repository("https://github.com/team/repo", settings) == "https://github.com/team/repo.git"
    assert validate_repository("https://GITLAB.EXAMPLE.COM/team/repo", settings) == "https://gitlab.example.com/team/repo.git"


def test_optional_paths_and_commands_keep_supported_forms(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"auth_file": None, "rootlesskit_socket": None,
                               "git_askpass": None, "docker_binary": "/opt/docker bin/docker",
                               "runtime_user": "1000"}))
    settings = load_settings(path)
    assert settings.auth_file == tmp_path / ".state/admin.json"
    assert settings.docker_binary == "/opt/docker bin/docker"
    assert settings.runtime_user == "1000"


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8088", "http://localhost:8088", "http://[::1]:8088",
                                    "https://deploy.example:8443/"])
def test_supported_origin_forms(tmp_path, origin):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"public_url": origin, "port_start": 1024, "port_end": 65535}))
    assert load_settings(path).public_url == origin


@pytest.mark.parametrize("tenant", [None, 123, [], "", "space tenant", "a" * 129])
def test_identity_invalid_tenant_is_a_configuration_error(tmp_path, tenant):
    settings = Settings(auth_mode="feishu", feishu_app_id="cli_test", feishu_tenant_key=tenant,
                        feishu_app_secret_file=tmp_path / "secret", public_url="https://example.com")
    with pytest.raises(ValueError):
        validate_identity_settings(settings)


def test_origin_parse_error_does_not_include_supplied_value():
    from awesome_deploy.config_validation import validate_http_origin

    with pytest.raises(ValueError) as caught:
        validate_http_origin("https://SENSITIVE_VALUE:bad")
    assert "SENSITIVE_VALUE" not in str(caught.value)
    assert "bad" not in str(caught.value)
