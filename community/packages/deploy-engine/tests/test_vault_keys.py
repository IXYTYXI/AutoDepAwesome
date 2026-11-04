"""KEK references stay outside state; malformed roots fail without creation."""

import base64
from dataclasses import replace
import json
import os

import pytest

from awesome_deploy.config import Settings, validate_vault_settings
from awesome_deploy.errors import DeployError
from awesome_deploy.vault_keys import VaultKeyring


@pytest.fixture
def settings(tmp_path):
    credentials = tmp_path / "credentials"
    credentials.mkdir(mode=0o700)
    key = credentials / "root.json"
    key.write_text(json.dumps({"format": 1, "active": "root", "keys": {
        "root": base64.b64encode(b"s" * 32).decode()}}))
    key.chmod(0o600)
    return Settings(state_dir=tmp_path / "state", auth_mode="feishu", secret_backend="encrypted",
                    secret_kek_file=key)


def test_loading_root_does_not_create_state_or_expose_keys_in_repr(settings):
    ring = VaultKeyring(settings)
    assert ring.active == "root"
    assert ring.key("root") == b"s" * 32
    assert "synthetic" not in repr(ring)
    assert not settings.state_dir.exists()


@pytest.mark.parametrize("patch", [
    {"secret_backend": "unknown"}, {"secret_backend": "legacy"},
    {"auth_mode": "basic"}, {"secret_kek_file": None}, {"secret_kek_file": "path"},
])
def test_bad_configuration_rejected_before_io(settings, patch):
    with pytest.raises(ValueError):
        validate_vault_settings(replace(settings, **patch))


def test_default_legacy_settings_remain_valid():
    validate_vault_settings(Settings())


@pytest.mark.parametrize("payload", [
    {}, [], {"format": True, "active": "root", "keys": {}},
    {"format": 2, "active": "root", "keys": {}},
    {"format": 1, "active": "missing", "keys": {"root": "YQ=="}},
    {"format": 1, "active": "root", "keys": {"root": 5}},
    {"format": 1, "active": "root", "keys": {"root": "!"}},
    {"format": 1, "active": "root", "keys": {"root": base64.b64encode(b"x" * 31).decode()}},
    {"format": 1, "active": "root", "keys": {"root": base64.b64encode(b"x" * 32).decode(),
                                             "alias": base64.b64encode(b"x" * 32).decode()}},
])
def test_malformed_keyring_fails_closed(settings, payload):
    settings.secret_kek_file.write_text(json.dumps(payload))
    with pytest.raises(DeployError) as error:
        VaultKeyring(settings)
    assert error.value.code == "VAULT_KEY_UNAVAILABLE"
    assert not settings.state_dir.exists()


def test_duplicate_json_fields_are_rejected(settings):
    raw = settings.secret_kek_file.read_text().replace('"format": 1', '"format": 1, "format": 1')
    settings.secret_kek_file.write_text(raw)
    with pytest.raises(DeployError):
        VaultKeyring(settings)


@pytest.mark.parametrize("kind", ["file_symlink", "directory_symlink", "hardlink", "directory_mode", "fifo"])
def test_unsafe_credential_paths_are_rejected(settings, kind):
    path = settings.secret_kek_file
    if kind == "file_symlink":
        original = path.with_suffix(".original")
        path.rename(original)
        path.symlink_to(original)
    elif kind == "directory_symlink":
        original = path.parent.with_name("original")
        path.parent.rename(original)
        path.parent.symlink_to(original, target_is_directory=True)
    elif kind == "hardlink":
        os.link(path, path.with_suffix(".alias"))
    elif kind == "directory_mode":
        path.parent.chmod(0o755)
    else:
        path.unlink()
        os.mkfifo(path, 0o600)
    with pytest.raises(DeployError):
        VaultKeyring(settings)


def test_keyring_in_state_is_rejected_even_with_external_alias(settings):
    settings.state_dir.mkdir(mode=0o700)
    moved = settings.state_dir / "root.json"
    settings.secret_kek_file.rename(moved)
    settings.secret_kek_file.symlink_to(moved)
    with pytest.raises(ValueError):
        validate_vault_settings(settings)
