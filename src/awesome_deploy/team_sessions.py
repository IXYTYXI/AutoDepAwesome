"""Source-isolated Redis sessions and durable, payload-bound admission receipts.

Only the trusted A backend may issue sessions, after its own OAuth verification.
Passwords, Feishu access tokens and role claims never belong in this protocol.
"""
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

from .feishu import _read_secret


class SessionError(ValueError):
    pass


class SessionUnavailable(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def request_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', value):
        raise SessionError('请求 ID 无效。')
    return value


def deployment_body(value):
    if (not isinstance(value, dict) or set(value) - {'repository', 'ref', 'subdomain', 'appId'}
            or not {'repository', 'subdomain'} <= set(value)
            or any(not isinstance(v, str) or not 1 <= len(v) <= 4096 for v in value.values())
            or len(canonical(value)) > 8192):
        raise SessionError('部署请求字段无效。')
    return {'ref': 'main', **value}


class SharedSessions:
    def __init__(self, redis, *, source, key, app_id, tenant_key, clock=time.time):
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}', source) or len(key) < 32:
            raise ValueError('共享会话来源或签名密钥无效。')
        self.redis, self.source, self.key = redis, source, key
        self.app_id, self.tenant_key, self.clock = app_id, tenant_key, clock

    def _sign(self, value):
        return hmac.new(self.key, canonical(value).encode(), hashlib.sha256).hexdigest()

    def _seal(self, value):
        return {**value, 'signature': self._sign(value)}

    def _verify(self, value, kind):
        try:
            unsigned = {k: v for k, v in value.items() if k != 'signature'}
            signature = value['signature']
            if (not isinstance(signature, str) or len(canonical(value)) > 16384
                    or not hmac.compare_digest(signature, self._sign(unsigned))
                    or value['version'] != 1 or value['kind'] != kind
                    or value['source'] != self.source or value['audience'] != 'awesome-deploy'):
                raise ValueError()
            self._identity(value['identity'])
            if type(value['issuedAt']) not in (int, float) or not 0 < value['issuedAt'] <= self.clock() + 30:
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise SessionError('共享身份凭据无效。') from None
        return value

    def _identity(self, identity):
        if (not isinstance(identity, dict) or set(identity) != {'appId', 'tenantKey', 'openId', 'name'}
                or identity['appId'] != self.app_id or identity['tenantKey'] != self.tenant_key
                or not isinstance(identity['openId'], str)
                or not re.fullmatch(r'ou_[A-Za-z0-9_-]{1,128}', identity['openId'])
                or not isinstance(identity['name'], str) or not 1 <= len(identity['name']) <= 256
                or any(ord(c) < 32 for c in identity['name'])):
            raise SessionError('身份必须来自约定的飞书应用和企业，不能传入角色。')

    def _redis_key(self, sid):
        if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', sid):
            raise SessionError('会话标识无效。')
        return f'awesome:sessions:{self.source}:' + hashlib.sha256(sid.encode()).hexdigest()

    def issue(self, identity, *, ttl=28800):
        self._identity(identity)
        if type(ttl) is not int or not 1 <= ttl <= 28800:
            raise SessionError('会话有效期必须在 1 秒至 8 小时之间。')
        sid, now = secrets.token_urlsafe(32), self.clock()
        value = self._seal(dict(version=1, kind='session', source=self.source, audience='awesome-deploy',
                                identity=identity, issuedAt=now, expiresAt=now + ttl))
        try:
            self.redis.set(self._redis_key(sid), canonical(value), ex=ttl)
        except Exception:
            raise SessionUnavailable('共享登录服务暂不可用。') from None
        return sid

    def read(self, sid):
        key = self._redis_key(sid)
        try:
            raw = self.redis.get(key)
        except Exception:
            raise SessionUnavailable('共享登录服务暂不可用。') from None
        try:
            if raw is None or len(raw) > 8192:
                raise SessionError('登录已过期或已退出。')
            value = self._verify(json.loads(raw), 'session')
            if (type(value['expiresAt']) not in (int, float)
                    or not value['issuedAt'] < value['expiresAt'] <= value['issuedAt'] + 28800
                    or value['expiresAt'] <= self.clock()):
                raise SessionError('登录已过期或已退出。')
        except (ValueError, KeyError, TypeError):
            raise SessionError('登录已过期或会话无效。') from None
        return value['identity']

    def logout(self, sid):
        key = self._redis_key(sid)
        try:
            self.redis.delete(key)
        except Exception:
            raise SessionUnavailable('共享登录服务暂不可用，不能确认退出。') from None

    def admit(self, sid, request_id, deployment):
        request_key(request_id)
        deployment = deployment_body(deployment)
        identity = self.read(sid)
        return self._seal(dict(version=1, kind='admission', source=self.source, audience='awesome-deploy',
                               identity=identity, issuedAt=self.clock(), requestId=request_id,
                               deploymentDigest=hashlib.sha256(canonical(deployment).encode()).hexdigest()))

    def verify_receipt(self, receipt, request_id, deployment):
        self._verify(receipt, 'admission')
        if (receipt.get('requestId') != request_key(request_id)
                or receipt.get('deploymentDigest') != hashlib.sha256(
                    canonical(deployment_body(deployment)).encode()).hexdigest()):
            raise SessionError('受理凭据与请求内容不匹配。')
        return receipt['identity']


def load_shared_sessions(path):
    """Operator-controlled registry; URL has no secrets; remote Redis requires TLS."""
    import redis
    values = json.loads(Path(path).read_text())
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError('共享会话配置必须是来源列表。')
    result = {}
    for item in values:
        required = {'source', 'redis_url', 'redis_username', 'redis_password_file', 'signing_key_file',
                    'feishu_app_id', 'tenant_key'}
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError('共享会话配置字段无效。')
        u = urlsplit(item['redis_url'])
        if (u.scheme not in ('redis', 'rediss') or not u.hostname or u.username or u.password
                or u.query or u.fragment or not re.fullmatch(r'/[0-9]{1,2}', u.path)
                or u.scheme == 'redis' and u.hostname not in ('127.0.0.1', '::1', 'localhost')):
            raise ValueError('Redis 仅本机可使用 redis://；远端必须 rediss://，凭证通过文件配置。')
        for field in ('redis_password_file', 'signing_key_file'):
            if not Path(item[field]).is_absolute():
                raise ValueError('密钥文件必须使用绝对路径。')
        client = redis.Redis.from_url(item['redis_url'], username=item['redis_username'],
            password=_read_secret(Path(item['redis_password_file'])), socket_timeout=2,
            socket_connect_timeout=2, decode_responses=True, max_connections=16)
        source = item['source']
        if source in result:
            raise ValueError('共享会话来源重复。')
        result[source] = SharedSessions(client, source=source,
            key=_read_secret(Path(item['signing_key_file'])).encode(),
            app_id=item['feishu_app_id'], tenant_key=item['tenant_key'])
    return result
