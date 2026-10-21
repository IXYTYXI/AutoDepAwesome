import json

import pytest

from awesome_deploy.errors import DeployError


def test_config_relative_paths_and_unknown_fields(tmp_path):
    from awesome_deploy.cli import load_settings

    f = tmp_path / "config.json"
    f.write_text(
        json.dumps({"state_dir": "state", "listen_port": 8088, "public_url": "http://127.0.0.1:8088"})
    )
    settings = load_settings(f)
    assert settings.state_dir == tmp_path / "state"
    assert settings.auth_file == tmp_path / "state" / "admin.json"
    f.write_text('{"typo": true}')
    with pytest.raises(DeployError):
        load_settings(f)


def test_application_memory_limit_defaults_and_can_be_configured(tmp_path):
    from awesome_deploy.cli import load_settings

    path = tmp_path / "config.json"
    path.write_text("{}")
    settings = load_settings(path)
    assert settings.max_app_memory_mib == 1024
    assert settings.max_memory_mib == 6144
    path.write_text('{"max_app_memory_mib": 768}')
    assert load_settings(path).max_app_memory_mib == 768


@pytest.mark.parametrize("value", [True, False, 0, -1, 512.5, "1024", None])
def test_invalid_application_memory_limit_rejected(tmp_path, value):
    from awesome_deploy.cli import load_settings

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"max_app_memory_mib": value}))
    with pytest.raises(DeployError) as caught:
        load_settings(path)
    assert caught.value.code == "INVALID_CONFIG"


@pytest.mark.parametrize(
    "config",
    [
        {"listen_port": 0},
        {"listen_port": 8088.5},
        {"port_start": True},
        {"port_end": 30000.5},
        {"base_domain": "bad;nginx"},
        {"build_network": "host"},
        {"public_url": "http://example.com"},
        {"public_url": "https://"},
        {"public_url": "https://example.com:invalid"},
        {"max_memory_mib": -1},
        {"max_memory_mib": 512.5},
        {"max_cpu": float("nan")},
        {"max_cpu": float("inf")},
        {"docker_host": None},
    ],
)
def test_invalid_host_configuration_rejected(tmp_path, config):
    from awesome_deploy.cli import load_settings

    f = tmp_path / "config.json"
    f.write_text(json.dumps(config))
    with pytest.raises(DeployError):
        load_settings(f)


def test_rootlesskit_socket_path_is_resolved_relative_to_config(tmp_path):
    from awesome_deploy.cli import load_settings

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"rootlesskit_socket": "runtime/api.sock"}))
    assert load_settings(path).rootlesskit_socket == tmp_path / "runtime" / "api.sock"


def test_rootlesskit_socket_rejects_non_path_values(tmp_path):
    from awesome_deploy.cli import load_settings

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"rootlesskit_socket": 123}))
    with pytest.raises(DeployError):
        load_settings(path)


def test_ai_configuration_defaults_to_disabled(tmp_path):
    from awesome_deploy.cli import load_settings

    path = tmp_path / "config.json"
    path.write_text("{}")
    settings = load_settings(path)
    assert settings.ai_base_url is None
    assert settings.ai_model == ""
    assert settings.ai_api_key_file is None
    assert settings.ai_reasoning_effort is None


def test_ai_configuration_resolves_key_path_without_following_symlinks(tmp_path):
    from awesome_deploy.cli import load_settings

    target = tmp_path / "actual-key"
    target.write_text("unused-in-config-validation")
    alias = tmp_path / "key-link"
    alias.symlink_to(target)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "ai_base_url": "https://api.example/v1/",
        "ai_model": "example-model",
        "ai_api_key_file": "key-link",
    }))
    settings = load_settings(path)
    assert settings.ai_base_url == "https://api.example/v1/"
    assert settings.ai_model == "example-model"
    assert settings.ai_api_key_file == alias


@pytest.mark.parametrize("patch", [
    {"ai_base_url": None},
    {"ai_model": ""},
    {"ai_api_key_file": None},
    {"ai_base_url": "http://api.example/v1"},
    {"ai_base_url": "https://user:secret@api.example/v1"},
    {"ai_base_url": "https://api.example/v1?secret=value"},
    {"ai_base_url": "https://api.example/v1?"},
    {"ai_base_url": "https://api.example/v1#"},
    {"ai_base_url": "https://api.example/v1/../other"},
    {"ai_base_url": "https://api.example/v1/./other"},
    {"ai_base_url": "https://api.example/v1/%2e%2e/other"},
    {"ai_base_url": "https://api.example\\other/v1"},
    {"ai_base_url": "https://api.example:bad/v1"},
    {"ai_base_url": "https://api.example:70000/v1"},
    {"ai_base_url": "https://api.example\n/v1"},
    {"ai_base_url": 123},
    {"ai_base_url": "https://"},
    {"ai_model": "model\r\nsecret"},
    {"ai_model": "model\x7f"},
    {"ai_model": " "},
    {"ai_model": "x" * 129},
    {"ai_model": 123},
    {"ai_api_key_file": 123},
])
def test_ai_configuration_rejects_incomplete_or_unsafe_values(tmp_path, patch):
    from awesome_deploy.cli import load_settings

    config = {
        "ai_base_url": "https://api.example/v1",
        "ai_model": "example-model",
        "ai_api_key_file": "key",
        **patch,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(DeployError) as caught:
        load_settings(path)
    assert caught.value.code == "INVALID_CONFIG"
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"])
def test_ai_reasoning_effort_accepts_only_supported_values_when_configured(tmp_path, effort):
    from awesome_deploy.cli import load_settings

    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "ai_base_url": "https://api.example/v1", "ai_model": "gpt-6-astra",
        "ai_api_key_file": "key", "ai_reasoning_effort": effort,
    }))
    assert load_settings(path).ai_reasoning_effort == effort


@pytest.mark.parametrize("effort", ["", "ULTRA", "unbounded", "ultra\n", 123, ["ultra"]])
def test_ai_reasoning_effort_rejects_invalid_values(tmp_path, effort):
    from awesome_deploy.cli import load_settings

    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "ai_base_url": "https://api.example/v1", "ai_model": "gpt-6-astra",
        "ai_api_key_file": "key", "ai_reasoning_effort": effort,
    }))
    with pytest.raises(DeployError) as caught:
        load_settings(path)
    assert caught.value.code == "INVALID_CONFIG"


def test_ai_reasoning_effort_requires_complete_ai_configuration(tmp_path):
    from awesome_deploy.cli import load_settings

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"ai_reasoning_effort": "ultra"}))
    with pytest.raises(DeployError) as caught:
        load_settings(path)
    assert caught.value.code == "INVALID_CONFIG"
