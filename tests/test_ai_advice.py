import asyncio
from dataclasses import replace
import json
import os

import httpx
import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError


CONTEXT = {"kind": "job", "status": "failed", "error_code": "BUILD_FAILED"}
SECRET = "secret-api-value"


@pytest.fixture
def configured(tmp_path):
    key = tmp_path / "ai-key"
    key.write_text(SECRET + "\n")
    key.chmod(0o600)
    return Settings(
        ai_base_url="https://api.example/v1/", ai_model="example-model", ai_api_key_file=key
    )


def service(settings, handler=None, **kwargs):
    from awesome_deploy.ai_advice import AdviceService

    return AdviceService(settings, transport=httpx.MockTransport(handler or success), **kwargs)


def success(request):
    return httpx.Response(200, json={"choices": [{"message": {"content": "请检查构建入口；具体原因尚未确认。"}}]})


def run_error(instance, code, context=CONTEXT):
    with pytest.raises(DeployError) as caught:
        asyncio.run(instance.analyze(context))
    assert caught.value.code == code
    assert SECRET not in json.dumps(caught.value.as_dict())
    assert "api.example" not in json.dumps(caught.value.as_dict())


def test_disabled_never_calls_provider():
    def unexpected(request):
        pytest.fail("disabled advice must not make a request")

    instance = service(Settings(), unexpected)
    assert instance.configured is False
    run_error(instance, "AI_NOT_CONFIGURED")


def test_configured_does_not_read_key(configured):
    configured.ai_api_key_file.unlink()
    assert service(configured).configured is True


def test_chat_request_is_explicit_bounded_and_data_only(configured):
    requests = []

    def handle(request):
        requests.append(request)
        assert str(request.url) == "https://api.example/v1/chat/completions"
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer " + SECRET
        body = json.loads(request.content)
        assert body["model"] == configured.ai_model
        assert body["max_tokens"] <= 1200
        assert "reasoning_effort" not in body
        assert "max_completion_tokens" not in body
        assert "tools" not in body and "functions" not in body
        assert body["messages"][0]["role"] == "system"
        assert "中文" in body["messages"][0]["content"]
        assert "数据" in body["messages"][0]["content"]
        assert json.loads(body["messages"][1]["content"]) == CONTEXT
        return success(request)

    instance = service(configured, handle)
    result = asyncio.run(instance.analyze(CONTEXT))
    assert result == {"text": "请检查构建入口；具体原因尚未确认。", "model": "example-model", "cached": False}
    assert len(requests) == 1


def test_reasoning_model_uses_explicit_effort_and_completion_budget(configured):
    configured = replace(configured, ai_model="gpt-6-astra", ai_reasoning_effort="ultra")
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == "gpt-6-astra"
        assert body["reasoning_effort"] == "ultra"
        assert body["max_completion_tokens"] == 8192
        assert "max_tokens" not in body
        return success(request)

    result = asyncio.run(service(configured, handle).analyze(CONTEXT))
    assert result["model"] == "gpt-6-astra"
    assert len(requests) == 1


def test_long_reasoning_deadline_keeps_connection_timeout_short(configured):
    from awesome_deploy import ai_advice

    assert ai_advice.REQUEST_TIMEOUT == 240.0

    def handle(request):
        assert request.extensions["timeout"]["connect"] == 10.0
        assert request.extensions["timeout"]["read"] == 240.0
        return success(request)

    assert asyncio.run(service(configured, handle).analyze(CONTEXT))["text"]


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o700, 0o000])
def test_key_rejects_unprotected_or_non_secret_modes(configured, mode):
    configured.ai_api_key_file.chmod(mode)
    run_error(service(configured), "AI_KEY_UNAVAILABLE")


def test_read_only_key_is_supported(configured):
    configured.ai_api_key_file.chmod(0o400)
    assert asyncio.run(service(configured).analyze(CONTEXT))["text"]


def test_key_rejects_symlink(configured, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(configured.ai_api_key_file)
    run_error(service(replace(configured, ai_api_key_file=alias)), "AI_KEY_UNAVAILABLE")


def test_key_rejects_directory(configured, tmp_path):
    run_error(service(replace(configured, ai_api_key_file=tmp_path)), "AI_KEY_UNAVAILABLE")


def test_key_rejects_fifo_without_blocking(configured, tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    run_error(service(replace(configured, ai_api_key_file=fifo)), "AI_KEY_UNAVAILABLE")


def test_key_rejects_another_uid(configured, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: configured.ai_api_key_file.stat().st_uid + 1)
    run_error(service(configured), "AI_KEY_UNAVAILABLE")


@pytest.mark.parametrize("content", [
    b"", b"\n", b"key\r\nX-Injected: yes", b"key\nextra", b"key\x00", b"bad\xff", b"x" * 8193
], ids=["empty", "blank", "header-injection", "multiline", "nul", "non-ascii", "oversized"])
def test_key_rejects_invalid_or_oversized_content(configured, content):
    configured.ai_api_key_file.write_bytes(content)
    run_error(service(configured), "AI_KEY_UNAVAILABLE")


@pytest.mark.parametrize("status,code", [
    (301, "AI_UNAVAILABLE"), (401, "AI_AUTH_FAILED"), (403, "AI_AUTH_FAILED"),
    (429, "AI_RATE_LIMITED"), (400, "AI_UNAVAILABLE"), (500, "AI_UNAVAILABLE"),
])
def test_provider_errors_are_private_and_never_redirect(configured, status, code):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, text=SECRET, headers={"Location": "https://other.example/steal"})

    run_error(service(configured, handle), code)
    assert len(requests) == 1


@pytest.mark.parametrize("failure,code", [
    (httpx.ReadTimeout("private provider URL and secret"), "AI_TIMEOUT"),
    (httpx.ConnectError("private provider URL and secret"), "AI_UNAVAILABLE"),
])
def test_network_errors_are_private(configured, failure, code):
    def fail(request):
        raise failure

    run_error(service(configured, fail), code)


@pytest.mark.parametrize("body", [
    b"not json", b"[]", b"null", b"{}", b'{"choices": []}',
    b'{"choices": [{"message": {"content": null}}]}',
    b'{"choices": [{"message": {"content": []}}]}',
    b'{"choices": [{"message": {"content": " "}}]}',
    json.dumps({"choices": [{"message": {"content": "中" * 4001}}]}).encode(),
], ids=["invalid-json", "array", "null", "missing-choices", "empty-choices", "null-content", "array-content", "blank-content", "oversized-content"])
def test_invalid_or_oversized_advice_is_rejected(configured, body):
    run_error(service(configured, lambda request: httpx.Response(200, content=body)), "AI_RESPONSE_INVALID")


def test_provider_cannot_echo_current_api_key_to_caller_or_cache(configured):
    now = [100.0]
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "供应商意外回显：" + SECRET}}]
            })
        return success(request)

    instance = service(configured, handle, clock=lambda: now[0])
    run_error(instance, "AI_RESPONSE_INVALID")
    now[0] += 20
    result = asyncio.run(instance.analyze(CONTEXT))
    assert result["cached"] is False
    assert SECRET not in result["text"]
    assert len(requests) == 2


def test_advice_allows_ordinary_text_similar_to_key(configured):
    text = "请检查 API 配置。字段名 secret-api 不表示当前密钥。"
    result = asyncio.run(service(configured, lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": text}}]
    })).analyze(CONTEXT))
    assert result["text"] == text


class ChunkStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        for _ in range(4):
            yield b" " * 9000


def test_stream_limit_uses_actual_body_size_without_content_length(configured):
    run_error(service(configured, lambda request: httpx.Response(200, stream=ChunkStream())), "AI_RESPONSE_INVALID")


def test_absolute_deadline_stops_slow_stream(configured, monkeypatch):
    from awesome_deploy import ai_advice

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b" "
                await asyncio.sleep(0.005)

    monkeypatch.setattr(ai_advice, "REQUEST_TIMEOUT", 0.02)
    run_error(service(configured, lambda request: httpx.Response(200, stream=SlowStream())), "AI_TIMEOUT")


def test_cache_ignores_key_order_and_returns_independent_values(configured):
    requests = []

    def handle(request):
        requests.append(request)
        return success(request)

    instance = service(configured, handle)
    first = asyncio.run(instance.analyze(CONTEXT))
    first["text"] = "modified caller value"
    second = asyncio.run(instance.analyze(dict(reversed(list(CONTEXT.items())))))
    assert second["cached"] is True
    assert second["text"] != first["text"]
    assert len(requests) == 1


def test_cache_expires_after_five_minutes(configured):
    now = [100.0]
    instance = service(configured, clock=lambda: now[0])
    asyncio.run(instance.analyze(CONTEXT))
    now[0] += 301
    assert asyncio.run(instance.analyze(CONTEXT))["cached"] is False


def test_cache_evicts_old_entries_above_32(configured, monkeypatch):
    from awesome_deploy import ai_advice

    # Isolate capacity eviction from the separate five-minute expiry behavior.
    monkeypatch.setattr(ai_advice, "CACHE_TTL", 10000.0)
    now = [0.0]
    instance = service(configured, clock=lambda: now[0])
    for index in range(33):
        now[0] += 20
        asyncio.run(instance.analyze({"id": index}))
    assert asyncio.run(instance.analyze({"id": 1}))["cached"] is True
    now[0] += 20
    assert asyncio.run(instance.analyze({"id": 0}))["cached"] is False
    assert len(instance._cache) <= 32


def test_global_cooldown_blocks_different_contexts_and_failed_requests(configured):
    now = [100.0]
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(429, text=SECRET)

    instance = service(configured, handle, clock=lambda: now[0])
    run_error(instance, "AI_RATE_LIMITED")
    now[0] += 19
    run_error(instance, "AI_RATE_LIMITED", {"id": "different"})
    assert len(calls) == 1
    now[0] += 1
    run_error(instance, "AI_RATE_LIMITED", {"id": "different"})
    assert len(calls) == 2


def test_failed_request_is_cached_for_twenty_seconds_after_completion(configured):
    now = [100.0]
    calls = []

    def fail(request):
        calls.append(request)
        now[0] += 240
        raise httpx.ReadTimeout("private provider details " + SECRET)

    instance = service(configured, fail, clock=lambda: now[0])
    run_error(instance, "AI_TIMEOUT")
    first_failure_at = now[0]
    now[0] += 19
    run_error(instance, "AI_TIMEOUT", dict(reversed(list(CONTEXT.items()))))
    assert len(calls) == 1
    assert now[0] == first_failure_at + 19
    now[0] += 1
    run_error(instance, "AI_TIMEOUT")
    assert len(calls) == 2


def test_failure_cache_is_bounded_and_does_not_reuse_mutable_exceptions(configured, monkeypatch):
    from awesome_deploy import ai_advice

    monkeypatch.setattr(ai_advice, "FAILURE_TTL", 10000.0)
    now = [0.0]
    calls = []

    def fail(request):
        calls.append(request)
        return httpx.Response(503, text=SECRET)

    instance = service(configured, fail, clock=lambda: now[0])
    for index in range(33):
        now[0] += 20
        run_error(instance, "AI_UNAVAILABLE", {"id": index})
    for _ in range(2):
        with pytest.raises(DeployError) as caught:
            asyncio.run(instance.analyze({"id": 1}))
        assert SECRET not in json.dumps(caught.value.as_dict())
        caught.value.message = SECRET
    assert len(calls) == 33
    now[0] += 20
    run_error(instance, "AI_UNAVAILABLE", {"id": 0})
    assert len(calls) == 34
    assert len(instance._failures) <= 32


def test_cancelled_poll_keeps_one_request_and_failed_result_is_cached(configured):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        now = [100.0]
        calls = []

        async def fail(request):
            calls.append(request)
            started.set()
            await release.wait()
            now[0] += 240
            return httpx.Response(503, text=SECRET)

        instance = service(configured, fail, clock=lambda: now[0])
        poll = asyncio.create_task(instance.analyze(CONTEXT))
        await started.wait()
        poll.cancel()
        with pytest.raises(asyncio.CancelledError):
            await poll
        next_poll = asyncio.create_task(instance.analyze(CONTEXT))
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(DeployError) as caught:
            await next_poll
        assert caught.value.code == "AI_UNAVAILABLE"
        with pytest.raises(DeployError) as caught:
            await instance.analyze(CONTEXT)
        assert caught.value.code == "AI_UNAVAILABLE"
        assert len(calls) == 1

    asyncio.run(scenario())


def test_duplicate_requests_share_result_and_other_contexts_fail_fast(configured):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def handle(request):
            requests.append(request)
            started.set()
            await release.wait()
            return success(request)

        instance = service(configured, handle)
        first = asyncio.create_task(instance.analyze(CONTEXT))
        await started.wait()
        duplicate = asyncio.create_task(instance.analyze(CONTEXT))
        await asyncio.sleep(0)
        with pytest.raises(DeployError) as caught:
            await instance.analyze({"id": "other"})
        assert caught.value.code == "AI_BUSY"
        release.set()
        results = await asyncio.gather(first, duplicate)
        assert [result["cached"] for result in results] == [False, True]
        assert len(requests) == 1

    asyncio.run(scenario())
