import ast
import copy
import importlib
from pathlib import Path
import re

import pytest


def diagnostics():
    from importlib.util import find_spec
    assert find_spec("awesome_deploy.diagnostics") is not None, "Chinese diagnostics are required"
    return importlib.import_module("awesome_deploy.diagnostics")


def test_historical_manifest_error_remains_ambiguous_and_does_not_mutate_record():
    original = {"code": "INVALID_MANIFEST", "message":
                "Repository must contain an awesome.yaml within its size limit.",
                "path": "awesome.yaml", "suggestion": ""}
    saved = copy.deepcopy(original)
    result = diagnostics().localize_error(original, "source")
    assert original == saved
    assert result is not original
    assert result["code"] == "INVALID_MANIFEST"
    assert "缺失" in result["message"] and "或" in result["message"] and "256 KiB" in result["message"]
    assert result["path"] == "awesome.yaml"
    assert result["steps"] and result["suggestion"] == result["steps"][0]
    assert "MANIFEST_MISSING" != result.get("reason")


@pytest.mark.parametrize("reason,expected,absent", [
    ("MANIFEST_MISSING", "未找到", "超出"),
    ("MANIFEST_TOO_LARGE", "256 KiB", "未找到"),
    ("MANIFEST_ENCODING", "UTF-8", "未找到"),
])
def test_manifest_reasons_are_specific(reason, expected, absent):
    result = diagnostics().localize_error({"code": "INVALID_MANIFEST", "reason": reason,
                                          "message": "untrusted source", "path": "awesome.yaml"})
    assert result["reason"] == reason and result["code"] == "INVALID_MANIFEST"
    assert expected in result["message"] and absent not in result["message"]
    assert "untrusted" not in str(result)


@pytest.mark.parametrize("code", ["COMMAND_FAILED", "GATEWAY_FAILED", "GIT_FAILED", "UNKNOWN_ERROR"])
def test_raw_error_and_unknown_fields_never_escape(code):
    secret = "ghp_do_not_send_this_secret"
    result = diagnostics().localize_error({"code": code, "message": secret, "suggestion": secret,
                                          "path": secret, "reason": secret, "debug": secret}, "build")
    assert secret not in str(result)
    assert result["code"] == code
    assert re.search("[\u4e00-\u9fff]", result["message"])
    assert re.search("[\u4e00-\u9fff]", result["suggestion"])


@pytest.mark.parametrize("path,expected", [
    ("services.my-secret-token.healthcheck.path", "services.*.healthcheck.path"),
    ("services.web.env.SECRET_TOKEN", "services.*.env.*"),
    ("services.web.secretRefs.ghp_secret", "services.*.secretRefs.*"),
    ("services.web.build.args.TOKEN", "services.*.build.args.*"),
    ("app.subdomain", "app.subdomain"), ("routes.0.service", "routes.0.service"),
    ("services.web.password.secret-value", ""), ("/root/.ssh/id_rsa", ""),
    ("services.web." + "x" * 300, ""), ("app.subdomain\nPASSWORD", ""),
])
def test_paths_only_expose_known_schema_structure(path, expected):
    assert diagnostics().localize_error({"code": "INVALID_MANIFEST", "path": path})["path"] == expected


@pytest.mark.parametrize("value", [None, {}, {"code": ["invalid"]}, {"code": "BAD\nTOKEN"}, {"message": []}])
def test_missing_or_malformed_errors_are_safe(value):
    result = diagnostics().localize_error(value)
    if value is None:
        assert result is None
    else:
        assert result["code"] == "DEPLOYMENT_FAILED"
        assert result["steps"]


def test_all_existing_static_codes_have_fixed_chinese_advice():
    module = diagnostics()
    root = Path(__file__).parents[1] / "src/awesome_deploy"
    # Include literal dictionary errors and port-forward helper calls as well as DeployError.
    sources = ("api.py", "auth.py", "cli.py", "manifest.py", "runtime.py", "store.py", "source.py",
               "worker.py", "processes.py", "gateway.py", "port_forward.py", "secrets_store.py")
    codes = set()
    for name in sources:
        tree = ast.parse((root / name).read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in {"DeployError", "_error"} and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                codes.add(node.args[0].value)
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (isinstance(key, ast.Constant) and key.value == "code"
                            and isinstance(value, ast.Constant) and isinstance(value.value, str)):
                        codes.add(value.value)
    assert codes <= module.ERROR_CODES
    for code in codes:
        result = module.localize_error({"code": code, "message": "secret-raw", "suggestion": "secret-raw"})
        assert result["code"] == code
        assert "secret-raw" not in str(result)
        assert re.search("[\u4e00-\u9fff]", result["title"])
        assert re.search("[\u4e00-\u9fff]", result["message"])
        assert result["steps"] and all(re.search("[\u4e00-\u9fff]", step) for step in result["steps"])


@pytest.mark.parametrize("message,expected", [
    ("Manifest field does not satisfy the required rule.", "必填"),
    ("Manifest field does not satisfy the oneOf rule.", "方案"),
    ("Manifest field does not satisfy the type rule.", "类型"),
    ("Service dependency graph contains a cycle.", "循环"),
    ("Manifest is not valid supported YAML.", "YAML"),
])
def test_known_manifest_messages_explain_the_rule(message, expected):
    result = diagnostics().localize_error({"code": "INVALID_MANIFEST", "message": message})
    assert expected in result["message"]


def test_historical_events_are_translated_without_mutation():
    event = {"id": 17, "phase": "source", "message": "Preparing the pinned repository source.", "createdAt": 2}
    saved = dict(event)
    result = diagnostics().localize_event(event)
    assert event == saved and result is not event
    assert result["id"] == 17 and result["phase"] == "source"
    assert "固定" in result["message"] and "代码" in result["message"]
    assert "Preparing" not in result["message"]
    unknown = diagnostics().localize_event({"phase": "weird", "message": "Bearer secret-token"})
    assert "secret-token" not in str(unknown)
    assert re.search("[\u4e00-\u9fff]", unknown["message"])


def test_cleanup_and_rollback_events_keep_their_distinct_outcomes():
    module = diagnostics()
    for phase, message in [
        ("rollback", "Rollback requires manual recovery; all resources remain reserved."),
        ("cleanup", "Cleanup requires manual recovery; resources remain reserved."),
    ]:
        output = module.localize_event({"phase": phase, "message": message})["message"]
        assert "人工" in output and "保留" in output


def test_error_reason_is_optional_and_existing_dictionary_shape_is_preserved():
    from awesome_deploy.errors import DeployError
    assert DeployError("X", "message").as_dict() == {
        "code": "X", "message": "message", "path": "", "suggestion": ""}
    # Assert the feature before using it so missing keyword support is an intentional failure.
    import inspect
    assert "reason" in inspect.signature(DeployError).parameters
    assert DeployError("INVALID_MANIFEST", "missing", reason="MANIFEST_MISSING").as_dict()["reason"] == "MANIFEST_MISSING"
