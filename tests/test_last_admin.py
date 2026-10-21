import pytest

from awesome_deploy.errors import DeployError
from test_identity import identity as identity, user


def administrator(identity, open_id="ou_admin"):
    person = user(identity, open_id)
    return identity.set_user(person["id"], is_admin=True)


def web_disable(identity, actor, target):
    return identity.set_user(target["id"], enabled=False, actor_id=actor["id"],
                             expected_actor_revision=actor["revision"])


@pytest.mark.parametrize("other_state", [None, "disabled", "ineligible", "other_tenant", "other_app"])
def test_web_cannot_disable_last_usable_administrator(identity, other_state):
    admin = administrator(identity)
    token = identity.create_session(admin["id"])
    if other_state:
        other = administrator(identity, "ou_other")
        column, value = {"disabled": ("enabled", 0), "ineligible": ("eligible", 0),
                         "other_tenant": ("tenant_key", "elsewhere"),
                         "other_app": ("app_id", "cli_elsewhere")}[other_state]
        with identity.connection(write=True) as db:
            db.execute(f"UPDATE auth_users SET {column}=? WHERE id=?", (value, other["id"]))
    with identity.connection() as db:
        audit_count = db.execute("SELECT COUNT(*) FROM auth_audit").fetchone()[0]
    with pytest.raises(DeployError) as error:
        web_disable(identity, admin, admin)
    assert error.value.code == "LAST_ADMIN_REQUIRED"
    assert identity.get_user(admin["id"]) == admin
    assert identity.session(token)
    with identity.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM auth_audit").fetchone()[0] == audit_count


def test_web_may_disable_one_admin_but_not_the_remaining_admin(identity):
    first = administrator(identity)
    other = administrator(identity, "ou_other")
    web_disable(identity, first, first)
    with pytest.raises(DeployError) as error:
        web_disable(identity, other, other)
    assert error.value.code == "LAST_ADMIN_REQUIRED"
    assert identity.get_user(other["id"])["enabled"] == 1


def test_cli_can_disable_and_recover_last_admin_and_web_can_revoke_deploy(identity):
    admin = administrator(identity)
    updated = identity.set_user(admin["id"], can_deploy=False, actor_id=admin["id"],
                                expected_actor_revision=admin["revision"])
    assert updated["is_admin"] and not updated["can_deploy"]
    identity.set_user(admin["id"], enabled=False)
    assert not identity.get_user(admin["id"])["enabled"]
    assert identity.set_user(admin["id"], enabled=True)["enabled"]
