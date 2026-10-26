from importlib.metadata import version

import pytest
from fastapi.testclient import TestClient

from awesome_deploy.auth import initialize_credentials
from awesome_deploy.config import Settings


@pytest.fixture
def client(tmp_path):
    from awesome_deploy.api import create_app

    auth = tmp_path / "admin.json"
    initialize_credentials(auth, "operator", "long-test-password")
    settings = Settings(state_dir=tmp_path, auth_file=auth, public_url="http://testserver")
    with TestClient(create_app(settings)) as c:
        yield c


def headers(key="request-1"):
    return {"Idempotency-Key": key, "Origin": "http://testserver"}


AUTH = ("operator", "long-test-password")
BODY = {"repository": "https://github.com/org/repo.git", "ref": "main", "subdomain": "demo"}


def test_openapi_info_uses_installed_platform_version(client):
    response = client.get("/api/v1/openapi.json", auth=AUTH)

    assert response.status_code == 200
    assert response.json()["info"]["version"] == version("awesome-deploy")


def test_management_endpoints_are_protected(client):
    for url in ["/", "/api/v1/deployments", "/api/v1/apps", "/api/v1/schema",
                "/document", "/document/deployment-guide.txt",
                "/document/content/overview", "/document/content/catalog", "/document/content/intake",
                "/document/content/protocol", "/document/content/protocol-legacy"]:
        assert client.get(url).status_code == 401
    assert client.post("/api/v1/deployments", json=BODY, headers=headers()).status_code == 401


def test_handbook_download_uses_current_limits_and_document_viewer_is_protected(client, monkeypatch):
    monkeypatch.setattr("awesome_deploy.storage_status.quota_capability", lambda settings: {
        "configured": True, "enabled": True,
        "limits": {"app_budget_mib": 37, "global_budget_mib": 4096},
    })
    page = client.get("/document", auth=AUTH)
    assert page.status_code == 200
    assert 'id="manual-content"' in page.text
    assert 'data-doc-id="api"' in page.text
    assert 'data-doc-id="overview"' in page.text
    assert 'data-doc-id="catalog"' in page.text
    assert 'data-doc-id="intake"' in page.text
    assert 'data-doc-id="protocol"' in page.text
    assert 'data-doc-id="protocol-legacy"' in page.text
    assert 'data-status="当前可用文档"' in page.text
    assert 'data-status="待实施规范"' in page.text
    assert 'data-status="历史文档"' in page.text
    assert 'https://guanghe.feishu.cn/docx/DxI1dW2LOoDjssxtaAfcrdOCnNg' in page.text
    assert 'https://guanghe.feishu.cn/docx/VBffdzM2do2H8txSOt1cwLsanig' in page.text
    assert 'data-url="https://guanghe.feishu.cn/docx/DSUwdxpXhot6fQx2YticNCtWnHe"' in page.text
    assert "frame-src" not in page.headers["content-security-policy"]
    for doc_id in ("catalog", "overview", "api", "intake", "protocol", "protocol-legacy", "audit"):
        content = client.get(f"/document/content/{doc_id}", auth=AUTH)
        assert content.status_code == 200
        assert "text/html" in content.headers["content-type"]
        assert "<h1>" in content.text
        assert "<script" not in content.text.lower()
        assert content.headers["cache-control"] == "no-store"
    assert client.get("/document/content/unknown", auth=AUTH).status_code == 404
    assert "技术协议 v2.0" in client.get("/document/content/protocol", auth=AUTH).text
    assert "技术协议 v1.1" in client.get("/document/content/protocol-legacy", auth=AUTH).text
    assert client.get("/assets/manual-api.html", auth=AUTH).status_code == 404
    guide = client.get("/document/deployment-guide.txt", auth=AUTH)
    assert guide.status_code == 200
    assert "attachment" in guide.headers["content-disposition"]
    assert "text/plain" in guide.headers["content-type"]
    assert "37 MiB" in guide.text and "4096 MiB" in guide.text
    assert "{{RESOURCE_LIMITS}}" not in guide.text
    assert client.get("/assets/deployment-guide.txt", auth=AUTH).status_code == 404


def test_shared_console_theme_is_available_on_every_page(client):
    from awesome_deploy.api import STATIC

    for name in ("index", "manage", "api-credentials", "document", "members", "login"):
        assert '/assets/theme.css?v=0.10.0' in (STATIC / f"{name}.html").read_text()
    theme = client.get("/assets/theme.css", auth=AUTH)
    assert theme.status_code == 200
    assert "--blue:#2457d6" in theme.text


def test_submit_idempotent_and_status_available(client):
    r = client.post("/api/v1/deployments", json=BODY, headers=headers(), auth=AUTH)
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "queued"
    assert "approved" not in r.text
    again = client.post("/api/v1/deployments", json=BODY, headers=headers(), auth=AUTH)
    assert again.json()["id"] == job["id"]
    changed = client.post("/api/v1/deployments", json={**BODY, "ref": "dev"}, headers=headers(), auth=AUTH)
    assert changed.status_code == 409
    fetched = client.get("/api/v1/deployments/" + job["id"], auth=AUTH)
    assert fetched.status_code == 200
    assert fetched.json()["url"] is None
    assert "snapshot" not in fetched.json()


def test_omitted_ref_defaults_to_main_without_changing_idempotency(client):
    omitted = {key: value for key, value in BODY.items() if key != "ref"}
    submitted = client.post("/api/v1/deployments", json=omitted, headers=headers(), auth=AUTH)
    assert submitted.status_code == 202, submitted.text
    assert submitted.json()["ref"] == "main"
    repeated = client.post("/api/v1/deployments", json=BODY, headers=headers(), auth=AUTH)
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()["id"] == submitted.json()["id"]
    explicit = client.post("/api/v1/deployments", json={**BODY, "ref": "release-v2"},
                           headers=headers("release-v2"), auth=AUTH)
    assert explicit.status_code == 202, explicit.text
    assert explicit.json()["ref"] == "release-v2"
    invalid = client.post("/api/v1/deployments", json={**BODY, "ref": ""},
                          headers=headers("empty-ref"), auth=AUTH)
    assert invalid.status_code == 422


def test_csrf_and_request_limits(client):
    r = client.post(
        "/api/v1/deployments",
        json=BODY,
        headers={"Origin": "https://evil.awesome.guanghexinzhi.cn", "Idempotency-Key": "x"},
        auth=AUTH,
    )
    assert r.status_code == 403
    assert client.post("/api/v1/deployments", json=BODY, auth=AUTH).status_code == 400
    assert (
        client.post(
            "/api/v1/deployments", json={**BODY, "repository": "file:///etc"}, headers=headers(), auth=AUTH
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/deployments", json={**BODY, "subdomain": "fuck"}, headers=headers(), auth=AUTH
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/deployments", json={**BODY, "approved": True}, headers=headers(), auth=AUTH
        ).status_code
        == 422
    )


def test_schema_and_readiness_do_not_fake_live_deployment(client):
    schema = client.get("/api/v1/schema", auth=AUTH)
    assert schema.status_code == 200
    assert "schemaVersion" in schema.json()["properties"]
    capabilities = client.get("/api/v1/capabilities", auth=AUTH).json()
    assert capabilities["persistentStorage"] is True
    assert capabilities["resourceLimits"]["maxAppMemoryMiB"] == Settings().max_app_memory_mib
    assert capabilities["storage"] == {"driver": "local", "mountPath": "/data", "perService": 1, "updates": "recreate"}
    assert capabilities["approval"] is False
    assert capabilities["serviceKinds"] == ["web"]
    assert client.get("/", auth=AUTH).status_code == 200


def test_ai_platform_rules_describe_persistent_storage_without_unsafe_recovery_advice():
    from awesome_deploy.api import advice_context
    job = {"status": "failed", "phase": "storage_handoff", "error": {"code": "STORAGE_MISSING"}}
    context = advice_context(job)
    rules = " ".join(context["platform_rules"])
    assert "services" in rules and "/data" in rules and "Recreate" in rules
    assert "仅支持无状态" not in rules and "不支持数据库、持久卷" not in rules
    assert "不会" in rules and "数据" in rules


def test_runtime_capabilities_default_to_internal_without_affecting_builds(client):
    capabilities = client.get('/api/v1/capabilities', auth=AUTH).json()
    network = capabilities['runtimeNetwork']
    assert network['mode'] == 'internal' and network['enabled'] is False
    assert network['publicIPv4TcpPorts'] == [] and network['controlledDns'] is False
    assert capabilities['buildNetwork'] == 'none'
    assert capabilities['customEgress'] is False


def test_runtime_capabilities_describe_transport_policy_without_exposing_resolvers(tmp_path):
    from awesome_deploy.api import create_app
    auth = tmp_path / 'admin.json'
    initialize_credentials(auth, *AUTH)
    settings = Settings(state_dir=tmp_path, auth_file=auth, public_url='http://testserver')
    object.__setattr__(settings, 'runtime_network', 'public-https-v1')
    object.__setattr__(settings, 'runtime_dns', ('203.0.113.53',))
    with TestClient(create_app(settings)) as client:
        response = client.get('/api/v1/capabilities', auth=AUTH)
    assert response.status_code == 200
    capabilities = response.json()
    network = capabilities['runtimeNetwork']
    assert network['mode'] == 'public-https-v1' and network['enabled'] is True
    assert network['publicIPv4TcpPorts'] == [443] and network['controlledDns'] is True
    assert network['tlsInspection'] is False and network['domainAllowlist'] is False
    assert network['appliesTo'] == 'new-deployments'
    assert '私网' in network['description'] and 'IPv6' in network['description']
    assert '203.0.113.53' not in response.text
    assert capabilities['buildNetwork'] == 'none' and capabilities['customEgress'] is False


@pytest.mark.parametrize('policy,expected', [
    ({'mode': 'internal'}, 'internal'),
    ({'mode': 'public-https-v1', 'dns': ['203.0.113.53'], 'blocked_ips': ['203.0.113.44'],
      'secret': 'RAW_NETWORK_SECRET'}, 'public-https-v1'),
    ({'mode': 'RAW_NETWORK_SECRET'}, 'unknown'),
    ({'mode': ['RAW_NETWORK_SECRET']}, 'unknown'),
    ('RAW_NETWORK_SECRET', 'unknown'),
    (None, 'unknown'),
])
def test_ai_network_rules_only_describe_the_frozen_mode(policy, expected):
    import copy
    import json
    from awesome_deploy.api import advice_context
    job = {'status': 'failed', 'phase': 'start', 'error': {'code': 'HEALTH_FAILED'},
           'snapshot': {'runtime_network': policy, 'repository': 'private-repository-sentinel'}}
    before = copy.deepcopy(job)
    context = advice_context(job)
    assert context['runtime_network'] == {'mode': expected}
    text = json.dumps(context, ensure_ascii=False)
    assert all(secret not in text for secret in ('203.0.113.53', '203.0.113.44', 'RAW_NETWORK_SECRET',
                                                'private-repository-sentinel'))
    assert job == before
    if expected == 'public-https-v1':
        assert 'IPv4' in text and 'TCP 443' in text and '受控 DNS' in text
        assert '不进行 TLS' in text and '域名白名单' in text
    elif expected == 'internal':
        assert '不能直连' in text
    else:
        assert '无法确认' in text


def test_legacy_ai_context_remains_internal_even_when_new_deployments_allow_egress():
    from awesome_deploy.api import advice_context
    for snapshot in (None, {}, {'manifest': {}}):
        context = advice_context({'snapshot': snapshot, 'error': {'code': 'HEALTH_FAILED'}})
        assert context['runtime_network'] == {'mode': 'internal'}
        assert '旧部署' in ' '.join(context['platform_rules'])
