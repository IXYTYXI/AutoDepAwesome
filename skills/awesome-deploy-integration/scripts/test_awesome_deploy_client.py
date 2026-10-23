#!/usr/bin/env python3
"""Offline contract checks: no sockets, production credentials, or third parties."""
from email.message import Message
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import HTTPHandler, ProxyHandler, build_opener
from urllib.response import addinfourl

import awesome_deploy_client as module
from awesome_deploy_client import AwesomeDeployClient, ClientError, NoRedirect, WaitTimeout


TOKEN = "offline-fake-intake-credential"
SID = "a" * 43
IDENTITY = {"appId": "cli_example", "tenantKey": "example-tenant",
            "openId": "ou_example", "name": "测试用户"}
PAYLOAD = {"appId": "app-second", "repository": "https://github.com/example/demo.git",
           "ref": "main", "subdomain": "demo"}


def progress(status="pending"):
    return {"requestId": "test-001", "status": status, "jobId": None,
            "appId": None, "error": None, "deployment": None, "pollAfterSeconds": 5}


class MemoryHTTP(HTTPHandler):
    """Exercise urllib's real redirect/error pipeline without opening a socket."""
    def __init__(self, replies):
        super().__init__()
        self.replies = list(replies)
        self.calls = []

    def http_open(self, request):
        self.calls.append(request)
        if not self.replies:
            raise AssertionError("Unexpected extra request")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, body, extra_headers = reply
        headers = Message()
        headers["Content-Type"] = "application/json"
        for name, value in extra_headers.items():
            headers[name] = value
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        response = addinfourl(io.BytesIO(raw), headers, request.full_url, status)
        response.msg = "simulated"
        return response


class ClientTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"AWESOME_INTAKE_TOKEN": TOKEN}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def client(self, *replies, timeout=4):
        client = AwesomeDeployClient(timeout=timeout)
        transport = MemoryHTTP([(status, body, headers) for status, body, headers in replies])
        client._opener = build_opener(ProxyHandler({}), NoRedirect(), transport)
        return client, transport

    def test_complete_backend_flow_with_explicit_selection_and_pagination(self):
        first = {"appId": "app-first", "repository": PAYLOAD["repository"], "subdomain": "other"}
        second = {"appId": "app-second", "repository": PAYLOAD["repository"], "subdomain": "demo"}
        client, transport = self.client(
            (201, {"sessionId": SID, "expiresIn": 1800}, {}),
            (200, {"items": [first], "total": 2, "limit": 1, "offset": 0, "nextOffset": 1}, {}),
            (200, {"items": [second], "total": 2, "limit": 1, "offset": 1, "nextOffset": None}, {}),
            (202, progress(), {}), (200, progress("succeeded"), {}),
            (200, {"revoked": True}, {}),
        )
        session = client.issue_session(IDENTITY, ttl=1800)
        page = client.search_apps(session["sessionId"], query="demo", limit=1)
        next_page = client.search_apps(SID, query="demo", limit=1, offset=page["nextOffset"])
        selected_app_id = "app-second"  # represents the user's explicit selection
        chosen = next(item for item in next_page["items"] if item["appId"] == selected_app_id)
        client.submit("test-001", {**PAYLOAD, "appId": chosen["appId"]}, SID)
        self.assertEqual(client.get_request("test-001")["status"], "succeeded")
        self.assertTrue(client.revoke_session(SID)["revoked"])
        self.assertEqual([r.selector for r in transport.calls],
                         ["/sessions", "/apps/search", "/apps/search", "/requests", "/requests/test-001", "/sessions/revoke"])
        for request in transport.calls:
            self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
            self.assertEqual(request.timeout, 4)
            self.assertNotIn(SID, request.full_url)
        submission = transport.calls[3]
        self.assertEqual(submission.get_header("Idempotency-key"), "test-001")
        self.assertEqual(json.loads(submission.data)["deployment"]["appId"], "app-second")
        self.assertNotIn("Idempotency-key", transport.calls[1].headers)

    def test_repeat_preserves_id_and_exact_body(self):
        client, transport = self.client((202, progress(), {}), (202, progress(), {}))
        client.submit("test-001", PAYLOAD, SID)
        client.submit("test-001", PAYLOAD, SID)
        self.assertEqual(transport.calls[0].data, transport.calls[1].data)
        self.assertEqual(transport.calls[0].get_header("Idempotency-key"),
                         transport.calls[1].get_header("Idempotency-key"))

    def test_redirects_are_never_followed_or_leaked(self):
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                client, transport = self.client((code, {}, {"Location": "http://127.0.0.1:9999/stolen"}))
                with self.assertRaises(ClientError) as caught:
                    client.get_request("test-001")
                self.assertEqual(caught.exception.status, code)
                self.assertEqual(len(transport.calls), 1)
                self.assertNotIn(TOKEN, str(caught.exception))

    def test_http_errors_are_not_empty_searches_or_automatic_retries(self):
        for status in (401, 403, 409, 422, 503):
            with self.subTest(status=status):
                client, transport = self.client((status, {"detail": TOKEN + SID}, {}))
                with self.assertRaises(ClientError) as caught:
                    client.search_apps(SID)
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(len(transport.calls), 1)
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertNotIn(SID, str(caught.exception))

    def test_network_timeout_does_not_resubmit(self):
        client = AwesomeDeployClient()
        transport = MemoryHTTP([URLError("timed out")])
        client._opener = build_opener(ProxyHandler({}), NoRedirect(), transport)
        with self.assertRaises(ClientError):
            client.submit("test-001", PAYLOAD, SID)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0].get_header("Idempotency-key"), "test-001")

    def test_bounded_polling_retains_last_request(self):
        client, transport = self.client((200, progress(), {}))
        clock = [0.0]
        def advance(seconds):
            clock[0] += seconds
        with patch.object(module.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(module.time, "sleep", side_effect=advance):
            with self.assertRaises(WaitTimeout) as caught:
                client.wait_for_request("test-001", max_seconds=3)
        self.assertEqual(caught.exception.request_id, "test-001")
        self.assertEqual(caught.exception.last_request["status"], "pending")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0].timeout, 3)

    def test_polling_stops_on_every_terminal_status(self):
        for terminal in module.TERMINAL:
            with self.subTest(status=terminal):
                client, transport = self.client((200, progress(terminal), {}))
                with patch.object(module.time, "sleep") as sleep:
                    self.assertEqual(client.wait_for_request("test-001")["status"], terminal)
                    sleep.assert_not_called()
                self.assertEqual(len(transport.calls), 1)

    def test_valid_origin_and_verified_tls_no_proxy_defaults(self):
        for origin in ("http://127.0.0.1:8099", "http://[::1]:8099", "http://localhost:8099", "https://deploy.example"):
            with self.subTest(origin=origin):
                client = AwesomeDeployClient(origin)
                self.assertEqual(client.origin, origin)
                https = next(h for h in client._opener.handlers if isinstance(h, module.HTTPSHandler))
                self.assertTrue(https._context.check_hostname)
                self.assertEqual(https._context.verify_mode, module.ssl.CERT_REQUIRED)
                self.assertFalse(any(getattr(h, "proxies", {}) for h in client._opener.handlers))
        for origin in ("http://example.com", "http://127.0.0.1.evil", "http://10.0.0.1", "https://user:pass@example.com",
                       "https://example.com/path", "https://example.com?q=1", "https://example.com#x",
                       "https://example.com:99999", "https://example.com\n", "ftp://example.com"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                AwesomeDeployClient(origin)

    def test_credential_precedence_defaults_and_unreadable_file(self):
        with patch.dict(os.environ, {"AWESOME_INTAKE_TOKEN_FILE": "/never-read"}), patch.object(Path, "open") as read:
            AwesomeDeployClient()
            read.assert_not_called()
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(Path, "open", autospec=True, return_value=io.StringIO(TOKEN + "\n")) as read:
            client = AwesomeDeployClient()
            self.assertEqual(client.origin, "http://127.0.0.1:8099")
            self.assertEqual(client._token, TOKEN)
            read.assert_called_once_with(Path("/etc/awesome-team/intake-token"), "r", encoding="ascii")
        with patch.dict(os.environ, {"AWESOME_INTAKE_TOKEN_FILE": "/service/token"}, clear=True), \
                patch.object(Path, "open", side_effect=PermissionError("secret path")):
            with self.assertRaises(ClientError) as caught:
                AwesomeDeployClient()
            self.assertIn("AWESOME_INTAKE_TOKEN_FILE", str(caught.exception))
            self.assertNotIn("secret path", str(caught.exception))
        with patch.dict(os.environ, {"AWESOME_INTAKE_TOKEN": "bad\r\ncredential"}), self.assertRaises(ClientError):
            AwesomeDeployClient()

    def test_invalid_inputs_never_reach_transport(self):
        client, transport = self.client()
        invalid_calls = [lambda: client.issue_session({**IDENTITY, "role": "admin"}),
                         lambda: client.issue_session(IDENTITY, ttl=True),
                         lambda: client.search_apps(SID, limit=True),
                         lambda: client.search_apps(SID, offset=-1),
                         lambda: client.search_apps("oauth-token"),
                         lambda: client.get_request("../escape"),
                         lambda: client.submit("test-001", {**PAYLOAD, "owner": "someone"}, SID),
                         lambda: client.submit("test-001", {**PAYLOAD, "subdomain": "Bad_Name"}, SID)]
        for call in invalid_calls:
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(transport.calls, [])

    def test_invalid_server_responses_fail_explicitly(self):
        for body in (b"<html>bad gateway</html>", [], {"requestId": "other", "status": "pending"},
                     {"requestId": "test-001", "status": "unknown"}, b"a" * (module.MAX_RESPONSE + 1)):
            with self.subTest(body_type=type(body).__name__):
                client, _ = self.client((200, body, {}))
                with self.assertRaises(ClientError):
                    client.get_request("test-001")
        client, _ = self.client((200, {"items": [], "total": 1, "limit": 20, "offset": 0, "nextOffset": 0}, {}))
        with self.assertRaises(ClientError):
            client.search_apps(SID)


if __name__ == "__main__":
    unittest.main(verbosity=2)
