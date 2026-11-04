"""Health failures keep useful classifications without retaining probe output."""

import copy
import http.client
import json
from types import SimpleNamespace

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.diagnostics import localize_error
from awesome_deploy.errors import DeployError
from awesome_deploy.runtime import DeploymentBackend


PRIVATE = "private-probe-output-must-not-persist"


@pytest.fixture
def health_case(tmp_path, monkeypatch):
    import awesome_deploy.runtime as runtime

    clock = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(runtime.time, "sleep", sleep)
    job = {
        "id": "healthjob", "app_id": "healthapp",
        "snapshot": {"manifest": {"app": {"subdomain": "demo"}, "services": {"web": {
            "healthcheck": {"type": "http", "path": "/healthz", "timeoutSeconds": 1,
                            "intervalSeconds": 1, "retries": 2, "startPeriodSeconds": 0},
        }}}},
        "execution": {"ports": {"web": 20000}},
    }
    engine = DeploymentBackend(Settings(state_dir=tmp_path))
    row = {"Config": {"Labels": engine._labels(job), "Env": [PRIVATE]},
           "State": {"Running": True, "OOMKilled": False, "Error": PRIVATE}}
    calls = []

    def docker(args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps([row]) if args[1] == "inspect" else ""

    engine._docker = docker
    monkeypatch.setattr(runtime, "http_status", lambda *args, **kwargs: 503)
    return SimpleNamespace(engine=engine, job=job, row=row, calls=calls,
                           clock=clock, runtime=runtime)


def failure(case, reason, deadline=20):
    before = copy.deepcopy(case.job)
    with pytest.raises(DeployError) as raised:
        case.engine._health_service(case.job, "web", deadline)
    error = raised.value
    assert error.code == "HEALTH_FAILED"
    assert error.reason == reason
    assert error.path == "services.web.healthcheck"
    assert PRIVATE not in json.dumps(error.as_dict())
    assert case.job == before
    return error


@pytest.mark.parametrize("oom,reason", [
    (False, "HEALTH_CONTAINER_EXITED"), (True, "HEALTH_OOM_KILLED"),
])
def test_stopped_container_retains_safe_failure_reason(health_case, oom, reason):
    case = health_case
    case.row["State"].update(Running=False, OOMKilled=oom)
    failure(case, reason)
    assert len(case.calls) == 2 and case.clock.sleeps == [1]


def test_unsuccessful_http_status_retains_reason_and_retry_count(health_case):
    failure(health_case, "HEALTH_HTTP_STATUS")
    assert len(health_case.calls) == 2 and health_case.clock.sleeps == [1]


@pytest.mark.parametrize("error,reason", [
    (ConnectionRefusedError(PRIVATE), "HEALTH_CONNECTION_FAILED"),
    (TimeoutError(PRIVATE), "HEALTH_TIMEOUT"),
    (http.client.BadStatusLine(PRIVATE), "HEALTH_CHECK_ERROR"),
])
def test_http_probe_exceptions_are_classified_without_details(health_case, monkeypatch, error, reason):
    def probe(*args, **kwargs):
        raise error

    monkeypatch.setattr(health_case.runtime, "http_status", probe)
    failure(health_case, reason)


@pytest.mark.parametrize("code,reason", [
    ("COMMAND_FAILED", "HEALTH_EXEC_FAILED"),
    ("COMMAND_TIMEOUT", "HEALTH_TIMEOUT"),
    ("COMMAND_UNAVAILABLE", "HEALTH_CHECK_ERROR"),
])
def test_exec_probe_errors_never_keep_command_output(health_case, code, reason):
    case = health_case
    case.job["snapshot"]["manifest"]["services"]["web"]["healthcheck"].update(
        type="exec", command=["/app/healthcheck", PRIVATE])
    inspect = case.engine._docker

    def docker(args, **kwargs):
        if args[1] == "exec":
            raise DeployError(code, PRIVATE, PRIVATE, PRIVATE, reason=PRIVATE)
        return inspect(args, **kwargs)

    case.engine._docker = docker
    failure(case, reason)


@pytest.mark.parametrize("payload", ["not-json " + PRIVATE, "[]", "null", '[{"State": null}]'])
def test_malformed_inspection_is_safe_checker_failure(health_case, payload):
    health_case.engine._docker = lambda *args, **kwargs: payload
    failure(health_case, "HEALTH_CHECK_ERROR")


def test_unowned_container_is_not_classified_as_application_exit(health_case):
    health_case.row["Config"]["Labels"] = {"untrusted": PRIVATE}
    health_case.row["State"].update(Running=False, OOMKilled=True)
    failure(health_case, "HEALTH_CHECK_ERROR")


def test_inspection_command_timeout_is_retained(health_case):
    def docker(*args, **kwargs):
        raise DeployError("COMMAND_TIMEOUT", PRIVATE)

    health_case.engine._docker = docker
    failure(health_case, "HEALTH_TIMEOUT")


def test_grace_and_deadline_keep_existing_attempt_schedule(health_case):
    case = health_case
    case.job["snapshot"]["manifest"]["services"]["web"]["healthcheck"]["startPeriodSeconds"] = 2
    failure(case, "HEALTH_HTTP_STATUS")
    assert len(case.calls) == 4 and case.clock.sleeps == [1, 1, 1]


def test_deadline_stops_attempts_and_retains_last_observation(health_case):
    health_case.job["snapshot"]["manifest"]["services"]["web"]["healthcheck"]["retries"] = 10
    failure(health_case, "HEALTH_HTTP_STATUS", deadline=1.5)
    assert len(health_case.calls) == 2 and health_case.clock.sleeps == [1, 0.5]


def test_expired_deadline_does_not_probe(health_case):
    failure(health_case, "HEALTH_TIMEOUT", deadline=0)
    assert health_case.calls == [] and health_case.clock.sleeps == []


@pytest.mark.parametrize("status", [200, 204, 302, 399])
def test_success_still_returns_immediately(health_case, monkeypatch, status):
    monkeypatch.setattr(health_case.runtime, "http_status", lambda *args, **kwargs: status)
    assert health_case.engine._health_service(health_case.job, "web", 20) is None
    assert len(health_case.calls) == 1 and health_case.clock.sleeps == []


@pytest.mark.parametrize("reason,fragment", [
    ("HEALTH_CONTAINER_EXITED", "退出"), ("HEALTH_OOM_KILLED", "内存"),
    ("HEALTH_HTTP_STATUS", "HTTP"), ("HEALTH_CONNECTION_FAILED", "连接"),
    ("HEALTH_TIMEOUT", "超时"), ("HEALTH_EXEC_FAILED", "命令"),
    ("HEALTH_CHECK_ERROR", "检查器"),
])
def test_health_reasons_have_specific_safe_chinese_advice(reason, fragment):
    original = {"code": "HEALTH_FAILED", "reason": reason, "path": "services.web.healthcheck",
                "message": PRIVATE, "suggestion": PRIVATE, "stdout": PRIVATE, "State": PRIVATE}
    result = localize_error(original)
    assert result["reason"] == reason and fragment in result["message"]
    assert result["path"] == "services.*.healthcheck"
    assert result["steps"] and PRIVATE not in json.dumps(result)
    assert localize_error(result) == result


@pytest.mark.parametrize("reason", [None, PRIVATE, [], "MANIFEST_MISSING"])
def test_historical_and_unknown_health_reasons_remain_generic(reason):
    result = localize_error({"code": "HEALTH_FAILED", "reason": reason, "message": PRIVATE})
    assert "reason" not in result and "限定时间" in result["message"]
    assert PRIVATE not in json.dumps(result)
