"""Source-authenticated transport for delegated deployments and applicant searches."""

from urllib.parse import urlsplit

import httpx

from .feishu import FeishuUnavailable
from .intake_remote import PlatformClient, PlatformRejected, PlatformUnavailable
from .team_sessions import request_key


class TeamSearchError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__('项目查询未完成。')


class TeamPlatformClient(PlatformClient):
    def __init__(self, origin, token_file, *, host=None, client=None):
        parts = urlsplit(origin)
        if (parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment
                or parts.path not in ('', '/')
                or parts.scheme == 'http' and parts.hostname not in ('127.0.0.1', '::1', 'localhost')):
            raise ValueError('平台地址只允许本机 HTTP 或 HTTPS。')
        if host is not None and (not isinstance(host, str) or not host or any(c in host for c in '/\r\n :')):
            raise ValueError('Host 必须是纯域名。')
        self.origin, self.token_file, self.host = origin.rstrip('/'), token_file, host
        self.client = client or httpx.Client(timeout=httpx.Timeout(10, connect=3),
                                             trust_env=False, follow_redirects=False)
        self.owns_client = client is None

    def _call(self, method, path, **kwargs):
        if self.host:
            kwargs['headers'] = {**kwargs.get('headers', {}), 'Host': self.host}
        return super()._call(method, path, **kwargs)

    def post(self, request_id, body):
        try:
            return self._call('POST', '/api/v1/integrations/deployments', json=body,
                             headers={'Idempotency-Key': request_id, 'Content-Type': 'application/json'})
        except PlatformUnavailable as exc:
            # Local permission denial is definitive; 401 remains retryable for credential maintenance.
            if exc.status_code == 403:
                raise PlatformRejected(403) from None
            raise

    def get(self, job_id):
        request_key(job_id)
        return self._call('GET', '/api/v1/integrations/deployments/' + job_id)

    def search(self, body):
        try:
            return self._call('POST', '/api/v1/integrations/apps/search', json=body,
                             headers={'Content-Type': 'application/json'})
        except PlatformRejected as exc:
            raise TeamSearchError(exc.status_code) from None
        except PlatformUnavailable as exc:
            code = exc.status_code if exc.status_code in (401, 403) else 503
            raise TeamSearchError(code) from None
        except (OSError, ValueError, FeishuUnavailable):
            raise TeamSearchError(503) from None
