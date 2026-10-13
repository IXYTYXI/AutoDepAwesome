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
