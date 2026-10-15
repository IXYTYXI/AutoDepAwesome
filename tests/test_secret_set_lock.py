from contextlib import contextmanager
import fcntl
import json
import os

import pytest

from awesome_deploy import cli
from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.secrets_store import SecretStore
from awesome_deploy.store import Store


@pytest.fixture
def setup(tmp_path, monkeypatch):
    previous_umask = os.umask(0o077)
    settings = Settings(state_dir=(tmp_path / "state").resolve())
    settings.state_dir.mkdir(mode=0o700)
    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "private-secret-value")
    yield settings
    os.umask(previous_umask)


def command():
    return ["secret-set", "--repository", "https://github.com/org/repo", "--subdomain", "demo", "--name", "PASSWORD"]


def test_secret_set_worker_lock_is_nonblocking_and_precedes_store_construction(setup, monkeypatch, capsys):
    with (setup.state_dir / "worker.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(Store, "__init__", lambda *args, **kwargs: pytest.fail("DB accessed before worker lock"))
        monkeypatch.setattr(SecretStore, "set", lambda *args: pytest.fail("Secret changed while worker active"))
        assert cli.main(command()) == 1
    output = capsys.readouterr()
    assert json.loads(output.err)["error"]["code"] == "SECRET_CONFIGURATION_BUSY"
    assert "private-secret-value" not in output.out + output.err


def test_secret_set_holds_worker_lock_and_authorized_scope_while_writing(setup, monkeypatch, capsys):
    state = {"scope": False, "written": False}
    @contextmanager
    def scope(store, repository, subdomain):
        assert repository == "https://github.com/org/repo.git" and subdomain == "demo"
        with (setup.state_dir / "worker.lock").open("a+b") as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state["scope"] = True
        try:
            yield
        finally:
            state["scope"] = False
    def write(store, repository, subdomain, name, value):
        assert state["scope"] and value == "private-secret-value"
        state["written"] = True
        return "new-version"
    monkeypatch.setattr(Store, "secret_write_scope", scope, raising=False)
    monkeypatch.setattr(SecretStore, "set", write)
    assert cli.main(command()) == 0
    assert state == {"scope": False, "written": True}
    with (setup.state_dir / "worker.lock").open("a+b") as probe:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    output = capsys.readouterr()
    assert json.loads(output.out) == {"name": "PASSWORD", "version": "new-version"}
    assert "private-secret-value" not in output.out + output.err


def test_secret_set_scope_rejection_never_writes_and_releases_worker_lock(setup, monkeypatch, capsys):
    @contextmanager
    def blocked(*args):
        raise DeployError("SECRET_CONFIGURATION_BUSY", "相关清理尚未完成。")
        yield
    monkeypatch.setattr(Store, "secret_write_scope", blocked, raising=False)
    monkeypatch.setattr(SecretStore, "set", lambda *args: pytest.fail("Unauthorized secret write"))
    assert cli.main(command()) == 1
    with (setup.state_dir / "worker.lock").open("a+b") as probe:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    output = capsys.readouterr()
    assert json.loads(output.err)["error"]["code"] == "SECRET_CONFIGURATION_BUSY"
    assert "private-secret-value" not in output.out + output.err


def test_secret_set_rejects_symlink_worker_lock(setup, monkeypatch, capsys):
    elsewhere = setup.state_dir / "outside"
    elsewhere.write_text("unchanged")
    (setup.state_dir / "worker.lock").symlink_to(elsewhere)
    monkeypatch.setattr(SecretStore, "set", lambda *args: pytest.fail("Unsafe lock allowed secret write"))
    assert cli.main(command()) == 1
    assert elsewhere.read_text() == "unchanged"
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "SECRET_UNAVAILABLE"
