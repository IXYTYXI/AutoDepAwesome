import copy

import pytest

from test_gateway import request, setup_state


def gateway(tmp_path):
    module = setup_state(tmp_path)
    module.probe_deactivated = lambda *args, **kwargs: None
    return module


def quarantine_request(release="old"):
    return request("quarantine", release)


def test_quarantine_current_durably_blocks_upstream_before_success(tmp_path):
    module = gateway(tmp_path)
    seen = []

    def probe(req, token, **kwargs):
        state = module.read_state("demo")
        assert state["quarantines"]["old"]["completed"] is False
        assert state["current"]["release"] == "old"
        assert state["pending"] is None
        assert "proxy_pass" not in module.config_path("demo").read_text()
        assert kwargs["master"] == (1, 1)
        seen.append(token)

    module.probe_deactivated = probe
    result = module.quarantine(quarantine_request())
    assert result == {"quarantined": True, "release": "old", "domain_quarantined": True,
                      "displaced_release": None}
    state = module.read_state("demo")
    assert state["current"] is state["pending"] is None
    assert state["quarantines"]["old"]["completed"] is True
    assert state["deactivation"]["completed"] is True
    assert seen == [state["quarantines"]["old"]["token"]]


@pytest.mark.parametrize("crash_point", ["reload", "completed_state", "probe"])
def test_quarantine_crash_reuses_intent_and_requires_worker_drain_again(tmp_path, crash_point):
    module = gateway(tmp_path)
    original_save = module.save_state
    if crash_point == "reload":
        module.run = lambda args, **kw: (_ for _ in ()).throw(SystemExit()) if "reload" in args else ""
    elif crash_point == "completed_state":
        def crash_save(name, state):
            if state.get("quarantines", {}).get("old", {}).get("completed"):
                raise SystemExit()
            original_save(name, state)
        module.save_state = crash_save
    else:
        module.probe_deactivated = lambda *a, **kw: (_ for _ in ()).throw(SystemExit())
    with pytest.raises(SystemExit):
        module.quarantine(quarantine_request())
    state = module.read_state("demo")
    token = state["quarantines"]["old"]["token"]
    assert state["quarantines"]["old"]["completed"] is False
    for operation in (module.publish, module.rollback, module.verify, module.ensure_https, module.deactivate):
        with pytest.raises(ValueError):
            operation(request(operation.__name__, "old"))
    with pytest.raises(ValueError):
        module.publish({**request(), "previous_release": None})
    module.save_state = original_save
    module.run = lambda *a, **kw: ""
    probes = []
    module.probe_deactivated = lambda req, value, **kw: probes.append(value)
    assert module.quarantine(quarantine_request())["quarantined"] is True
    assert probes == [token]


@pytest.mark.parametrize("crash_point", ["before_replace", "after_reload"])
def test_pending_candidate_quarantine_never_restores_displaced_old_release(tmp_path, crash_point):
    module = gateway(tmp_path)
    original_apply, original_save = module.apply_config, module.save_state
    if crash_point == "before_replace":
        module.apply_config = lambda *a, **kw: (_ for _ in ()).throw(SystemExit())
    else:
        def crash_save(name, state):
            if state["current"]["release"] == "new":
                raise SystemExit()
            original_save(name, state)
        module.save_state = crash_save
    with pytest.raises(SystemExit):
        module.publish(request())
    module.apply_config, module.save_state = original_apply, original_save
    result = module.quarantine(quarantine_request("new"))
    assert result["displaced_release"] == "old"
    assert result["domain_quarantined"] is True
    assert module.read_state("demo")["current"] is None
    assert "proxy_pass" not in module.config_path("demo").read_text()
    with pytest.raises(ValueError):
        module.rollback(request("rollback"))


def test_completed_quarantine_permits_new_release_and_offline_rollback_not_old_upstream(tmp_path):
    module = gateway(tmp_path)
    module.quarantine(quarantine_request())
    req = {**request(), "previous_release": None}
    module.publish(req)
    state, text = module.read_state("demo"), module.config_path("demo").read_text()
    module.probe_deactivated = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not offline new release"))
    assert module.quarantine(quarantine_request()) == {
        "quarantined": True, "release": "old", "domain_quarantined": False, "displaced_release": None}
    assert module.read_state("demo") == state
    assert module.config_path("demo").read_text() == text
    for operation in (module.publish, module.rollback, module.verify, module.ensure_https, module.deactivate):
        with pytest.raises(ValueError):
            operation(request(operation.__name__, "old"))
    module.probe_deactivated = lambda *a, **kw: None
    module.rollback({**req, "op": "rollback"})
    assert module.read_state("demo")["current"] is None
    assert "proxy_pass" not in module.config_path("demo").read_text()


@pytest.mark.parametrize("patch", [
    {"release": "unrecorded"}, {"ports": {"web": 20002}},
    {"routes": [{"path": "/different", "match": "exact", "service": "web", "stripPrefix": False}]},
])
def test_quarantine_rejects_unproven_identity_without_changing_other_release(tmp_path, patch):
    module = gateway(tmp_path)
    before = module.read_state("demo"), module.config_path("demo").read_text()
    with pytest.raises(ValueError):
        module.quarantine({**quarantine_request(), **patch})
    assert (module.read_state("demo"), module.config_path("demo").read_text()) == before


def test_quarantine_unknown_configuration_refuses_before_durable_intent(tmp_path):
    module = gateway(tmp_path)
    module.config_path("demo").write_text("# manually changed\n")
    before = module.read_state("demo")
    with pytest.raises(ValueError):
        module.quarantine(quarantine_request())
    assert module.read_state("demo") == before
    assert module.config_path("demo").read_text() == "# manually changed\n"


def test_quarantine_unknown_never_published_candidate_requires_operator_proof(tmp_path):
    module = gateway(tmp_path)
    module.save_state("demo", {"current": None, "pending": None})
    module.config_path("demo").unlink()
    with pytest.raises(ValueError):
        module.quarantine(quarantine_request())
    assert not module.config_path("demo").exists()


def test_quarantine_replay_rejects_changed_request_and_corrupt_completion(tmp_path):
    module = gateway(tmp_path)
    module.quarantine(quarantine_request())
    with pytest.raises(ValueError):
        module.quarantine({**quarantine_request(), "ports": {"web": 20002}})
    state = module.read_state("demo")
    state["current"] = {"release": "old", "request": request(release="old"), "verified": True}
    module.save_state("demo", state)
    with pytest.raises(ValueError):
        module.quarantine(quarantine_request())


def test_quarantine_failed_journal_has_no_config_side_effect(tmp_path):
    module = gateway(tmp_path)
    text = module.config_path("demo").read_text()
    module.save_state = lambda *a, **kw: (_ for _ in ()).throw(OSError("full"))
    with pytest.raises(OSError):
        module.quarantine(quarantine_request())
    assert module.config_path("demo").read_text() == text


def test_quarantine_blocks_rollback_that_would_restore_previously_quarantined_release(tmp_path):
    module = gateway(tmp_path)
    old = copy.deepcopy(module.read_state("demo")["current"])
    module.quarantine(quarantine_request())
    req = {**request(), "previous_release": None}
    module.publish(req)
    state = module.read_state("demo")
    state["pending"]["previous"] = old
    state["pending"]["previous_config"] = module.render(old["request"], tls=True)
    state["pending"]["previous_deactivation"] = None
    module.save_state("demo", state)
    text = module.config_path("demo").read_text()
    with pytest.raises(ValueError):
        module.rollback(request("rollback"))
    assert module.config_path("demo").read_text() == text


def test_quarantine_an_already_offline_release_still_records_irreversible_block(tmp_path):
    module = gateway(tmp_path)
    module.deactivate(request("deactivate", "old"))
    assert module.quarantine(quarantine_request())["quarantined"] is True
    assert module.read_state("demo")["quarantines"]["old"]["completed"] is True


def test_completed_quarantine_does_not_recreate_missing_owned_configuration(tmp_path):
    module = gateway(tmp_path)
    module.quarantine(quarantine_request())
    module.config_path("demo").unlink()
    with pytest.raises(ValueError):
        module.quarantine(quarantine_request())
    assert not module.config_path("demo").exists()


def test_quarantine_failed_reload_keeps_intent_for_same_release_retry(tmp_path):
    module = gateway(tmp_path)
    before = module.config_path("demo").read_text()
    calls = []

    def run(args, **kwargs):
        if "reload" in args:
            calls.append(args)
            if len(calls) == 1:
                raise RuntimeError("reload failed")
        return ""

    module.run = run
    with pytest.raises(RuntimeError):
        module.quarantine(quarantine_request())
    assert module.config_path("demo").read_text() == before
    assert module.read_state("demo")["quarantines"]["old"]["completed"] is False
    assert module.quarantine(quarantine_request())["quarantined"] is True
