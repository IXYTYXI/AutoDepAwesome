import hashlib
import sqlite3
from dataclasses import replace

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError


class Provider:
    allowed = True
    unavailable = False

    def check_eligible(self, open_id):
        if self.unavailable:
            from awesome_deploy.feishu import FeishuUnavailable
            raise FeishuUnavailable()
        return self.allowed


@pytest.fixture
def identity(tmp_path):
    from awesome_deploy.identity import IdentityStore
    settings = Settings(state_dir=tmp_path, auth_mode="feishu", public_url="https://console.example.com",
                        feishu_app_id="cli_test", feishu_tenant_key="tenant", feishu_app_secret_file=tmp_path / "key")
    now = [1_000_000.0]
    store = IdentityStore(settings, provider=Provider(), clock=lambda: now[0])
    store.test_now = now
    return store


def user(store, open_id="ou_person"):
    return store.login_user({"open_id": open_id, "tenant_key": "tenant", "name": "测试用户"})


def test_login_does_not_make_first_user_admin_and_stable_identity(identity):
    one = user(identity)
    assert one["is_admin"] == 0 and one["can_deploy"] == 1
    assert one["id"] == user(identity)["id"]
    with pytest.raises(DeployError):
        identity.login_user({"open_id": "ou_other", "tenant_key": "other", "name": "x"})


def test_oauth_is_browser_bound_single_use_and_expires(identity):
    attempt = identity.begin_login("/manage")
    assert len(attempt["state"]) >= 43
    assert len(attempt["verifier"]) >= 43
    with pytest.raises(DeployError):
        identity.consume_login(attempt["state"], "wrong")
    result = identity.consume_login(attempt["state"], attempt["browser"])
    assert result["return_path"] == "/manage"
    with pytest.raises(DeployError):
        identity.consume_login(attempt["state"], attempt["browser"])
    other = identity.begin_login("//evil.example")
    assert other["return_path"] == "/"
    identity.test_now[0] += 301
    with pytest.raises(DeployError):
        identity.consume_login(other["state"], other["browser"])


def test_sessions_store_only_hash_and_have_idle_and_absolute_expiry(identity):
    actor = user(identity)
    token = identity.create_session(actor["id"])
    assert identity.session(token)["id"] == actor["id"]
    with sqlite3.connect(identity.settings.database_path) as db:
        values = str(db.execute("SELECT * FROM auth_sessions").fetchall())
        assert token not in values
        assert hashlib.sha256(token.encode()).hexdigest() in values
    identity.test_now[0] += 1801
    assert identity.session(token) is None
    token = identity.create_session(actor["id"])
    for _ in range(16):
        identity.test_now[0] += 1799
        assert identity.session(token)
    identity.test_now[0] += 17
    assert identity.session(token) is None


def test_revocation_is_immediate_and_regrant_does_not_resurrect_session_or_job(identity):
    actor = user(identity)
    token = identity.create_session(actor["id"])
    identity.set_user(actor["id"], enabled=False)
    assert identity.session(token) is None
    with pytest.raises(DeployError):
        identity.authorize_user(actor["id"], expected_revision=actor["revision"])
    identity.set_user(actor["id"], enabled=True)
    assert identity.session(token) is None
    with pytest.raises(DeployError):
        identity.authorize_user(actor["id"], expected_revision=actor["revision"])


def test_directory_and_visibility_revocation_invalidates_sessions(identity):
    actor = user(identity)
    token = identity.create_session(actor["id"])
    identity.provider.allowed = False
    identity.test_now[0] += 301
    with pytest.raises(DeployError):
        identity.authorize_user(actor["id"])
    assert identity.session(token) is None
    identity.provider.allowed = True
    assert user(identity)["revision"] > actor["revision"]
    with pytest.raises(DeployError):
        identity.authorize_user(actor["id"], expected_revision=actor["revision"])


def test_unknown_eligibility_fails_closed_without_permanent_disable(identity):
    actor = user(identity)
    identity.provider.unavailable = True
    identity.test_now[0] += 301
    with pytest.raises(DeployError) as error:
        identity.authorize_user(actor["id"])
    assert error.value.code == "IDENTITY_UNAVAILABLE"
    assert identity.get_user(actor["id"])["enabled"] == 1


@pytest.mark.parametrize("force", [False, True])
def test_login_mode_skips_remote_eligibility_after_ttl_and_when_forced(identity, force):
    identity.settings = replace(identity.settings, feishu_eligibility_mode="login")
    actor = user(identity)
    token = identity.create_session(actor["id"])
    identity.test_now[0] += 301
    identity.provider.check_eligible = lambda open_id: pytest.fail("login mode must not poll Feishu")
    assert identity.authorize_user(actor["id"], actor["revision"], force=force)["id"] == actor["id"]
    assert identity.session(token)["id"] == actor["id"]


@pytest.mark.parametrize("field", ["enabled", "eligible", "revision"])
def test_login_mode_still_rejects_local_revocation(identity, field):
    identity.settings = replace(identity.settings, feishu_eligibility_mode="login")
    actor = user(identity)
    token = identity.create_session(actor["id"])
    identity.provider.check_eligible = lambda open_id: pytest.fail("login mode must not poll Feishu")
    with identity.connection(write=True) as db:
        db.execute(f"UPDATE auth_users SET {field}=? WHERE id=?", (2 if field == "revision" else 0, actor["id"]))
    with pytest.raises(DeployError) as error:
        identity.authorize_user(actor["id"], actor["revision"], force=True)
    assert error.value.code == "ACCESS_DENIED"
    assert identity.session(token) is None


def test_logout_and_revoke_all(identity):
    actor = user(identity)
    one, two = identity.create_session(actor["id"]), identity.create_session(actor["id"])
    identity.logout(one)
    assert identity.session(one) is None and identity.session(two)
    identity.revoke_all_sessions()
    assert identity.session(two) is None


def test_reset_invalidates_inflight_callback_and_slow_session_cannot_pass_expiry(identity):
    actor = user(identity)
    attempt = identity.begin_login()
    consumed = identity.consume_login(attempt["state"], attempt["browser"])
    identity.revoke_all_sessions()
    with pytest.raises(DeployError):
        identity.create_session(actor["id"], expected_epoch=consumed["auth_epoch"])
    token = identity.create_session(actor["id"])
    identity.test_now[0] += 301
    def delayed_check(open_id):
        identity.test_now[0] += 1500
        return True
    identity.provider.check_eligible = delayed_check
    assert identity.session(token) is None
