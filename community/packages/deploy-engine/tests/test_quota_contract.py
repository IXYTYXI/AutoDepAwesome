import copy
import json
from pathlib import Path
import re

import jsonschema
import pytest
import yaml

from awesome_deploy.cli import load_settings
from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.manifest import load_manifest, schema_document, validate_storage_transition
from test_manifest import valid


def quota_manifest():
    data = valid()
    data["schemaVersion"] = 2
    data["services"]["web"]["storage"] = {
        "name": "data", "mountPath": "/data", "sizeMiB": 2048, "maxInodes": 50000,
    }
    return data


def test_v2_schema_is_explicit_and_v1_stays_strict():
    schema = schema_document(2)
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(quota_manifest(), schema)
    data = quota_manifest()
    data["schemaVersion"] = 1
    assert not jsonschema.Draft202012Validator(schema_document()).is_valid(data)
    assert schema_document()["properties"]["schemaVersion"]["const"] == 1


def test_v2_manifest_normalizes_with_explicit_quota_without_mutating_input():
    data = quota_manifest()
    before = copy.deepcopy(data)
    loaded = load_manifest(yaml.safe_dump(data), Settings(storage_backend="xfs-project"))
    assert loaded["services"]["web"]["storage"] == data["services"]["web"]["storage"]
    assert loaded["services"]["web"]["resources"]["memoryMiB"] == 500
    assert data == before


@pytest.mark.parametrize("field,value", [
    ("sizeMiB", 0), ("sizeMiB", -1), ("sizeMiB", True), ("sizeMiB", 1.5), ("sizeMiB", 2048.0),
    ("sizeMiB", 2**64), ("maxInodes", 0), ("maxInodes", False), ("maxInodes", 2**64),
    ("maxInodes", 50000.0),
    ("mountPath", "/tmp"), ("hostPath", "/etc"), ("projectId", 1),
])
def test_v2_schema_rejects_unlimited_or_unsafe_storage_fields(field, value):
    data = quota_manifest()
    data["services"]["web"]["storage"][field] = value
    with pytest.raises(DeployError):
        load_manifest(yaml.safe_dump(data), Settings(storage_backend="xfs-project"))


@pytest.mark.parametrize("field", ["sizeMiB", "maxInodes"])
def test_v2_requires_both_hard_limits(field):
    data = quota_manifest()
    del data["services"]["web"]["storage"][field]
    with pytest.raises(DeployError):
        load_manifest(yaml.safe_dump(data), Settings(storage_backend="xfs-project"))


def test_disabled_backend_never_accepts_new_quota_storage_as_legacy_volume():
    with pytest.raises(DeployError) as error:
        load_manifest(yaml.safe_dump(quota_manifest()), Settings())
    assert error.value.code == "QUOTA_NOT_ENABLED"


def test_storage_transition_uses_stable_identity_not_quota_declaration():
    old = quota_manifest()
    new = copy.deepcopy(old)
    new["services"]["web"]["storage"]["sizeMiB"] *= 2
    validate_storage_transition(new, old)
    # An old business release may omit quota fields and must use ledger effective values.
    new["schemaVersion"] = 1
    new["services"]["web"]["storage"] = {"name": "data", "mountPath": "/data"}
    validate_storage_transition(new, old)
    new["services"]["web"]["storage"]["name"] = "replacement"
    with pytest.raises(DeployError):
        validate_storage_transition(new, old)


@pytest.mark.parametrize("version", [True, False, 0, 3, "2", None])
def test_unsupported_schema_versions_are_rejected(version):
    data = valid()
    data["schemaVersion"] = version
    with pytest.raises(DeployError):
        load_manifest(yaml.safe_dump(data), Settings())


def test_storage_config_is_opt_in_and_uses_fixed_helper_entry(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}")
    settings = load_settings(path)
    assert settings.storage_backend == "local"
    assert settings.storage_command == ("sudo", "-n", "/usr/local/libexec/awesome-storage")
    path.write_text(json.dumps({"storage_backend": "xfs-project"}))
    assert load_settings(path).storage_backend == "xfs-project"


@pytest.mark.parametrize("config", [
    {"storage_backend": "anything"}, {"storage_backend": True},
    {"storage_command": []}, {"storage_command": "sudo"},
    {"storage_command": ["sudo", "-n", "/tmp/custom-helper"]},
])
def test_storage_config_rejects_unknown_backend_and_arbitrary_privileged_commands(tmp_path, config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(DeployError):
        load_settings(path)


def test_quota_diagnostics_explain_preserved_budget_and_never_echo_helper_details():
    from awesome_deploy.diagnostics import localize_error
    for code in ("QUOTA_PENDING", "QUOTA_DELETE_PENDING", "QUOTA_BUDGET_EXCEEDED"):
        result = localize_error({"code": code, "message": "/private/secret"})
        assert result["title"] != "部署未完成"
        assert "额度" in result["message"] + " ".join(result["steps"])
        assert "/private/secret" not in str(result)
    result = localize_error({"code": "QUOTA_UNAVAILABLE"})
    assert "查询" in result["message"]
    assert localize_error({"code": "INVALID_MANIFEST", "path": "services.secret.storage.maxInodes"})[
        "path"] == "services.*.storage.maxInodes"


def test_manifest_diagnostics_and_ai_rules_preserve_v2_quota_protocol():
    from awesome_deploy.api import advice_context
    from awesome_deploy.diagnostics import localize_error
    error = {"code": "INVALID_MANIFEST", "message": "Manifest field does not satisfy the const rule.",
             "path": "services.web.storage.mountPath"}
    assert "使用 schemaVersion: 1" not in str(localize_error(error))
    rules = " ".join(advice_context({"error": error})["platform_rules"])
    assert "schemaVersion 为 1。" not in rules
    assert "sizeMiB" in rules and "maxInodes" in rules


def test_privileged_helper_error_categories_have_safe_operator_diagnostics():
    from awesome_deploy.diagnostics import ERROR_CODES, localize_error
    directory = Path(__file__).parents[1] / "ops" / "storage_quota"
    codes = set()
    for source in directory.glob("*.py"):
        codes.update(re.findall(r'"(QUOTA_[A-Z_]+)"', source.read_text()))
    assert codes <= ERROR_CODES
    for code in codes:
        assert "/root/private" not in str(localize_error({"code": code, "message": "/root/private"}))
