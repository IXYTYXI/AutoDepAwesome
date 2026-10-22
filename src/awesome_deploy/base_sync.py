"""Read-only platform projection to a dedicated Feishu Base.

The SQLite platform remains authoritative. This process never reads secret tables,
raw logs, or error details; delivery failures cannot affect deployment transactions.
"""

import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

import httpx

from .config import Settings
from .feishu import OPEN_ID, _read_secret


BASE_ORIGIN = 'https://open.feishu.cn'
TOKEN_URL = 'https://accounts.feishu.cn/oauth/v3/token'
MAX_RESPONSE = 262144


class BaseSyncError(Exception):
    pass


def _safe_repository(value):
    if not isinstance(value, str):
        return ''
    try:
        parts = urlsplit(value)
        if parts.scheme != 'https' or not parts.hostname or parts.port is not None:
            return ''
        return urlunsplit(('https', parts.hostname.lower(), parts.path, '', ''))
    except ValueError:
        return ''


def _ms(value):
    return int(float(value) * 1000)


def _name(db, user_id):
    if not user_id:
        return ''
    row = db.execute('SELECT name FROM auth_users WHERE id=?', (user_id,)).fetchone()
    return str(row['name'])[:100] if row else ''


def _person(db, user_id):
    if not user_id:
        return []
    row = db.execute('SELECT open_id FROM auth_users WHERE id=?', (user_id,)).fetchone()
    open_id = row['open_id'] if row else ''
    return [{'id': open_id}] if isinstance(open_id, str) and OPEN_ID.fullmatch(open_id) else []


def _job_input(row):
    try:
        value = json.loads(row['input'])
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _job_status(value):
    return {'queued': '排队', 'running': '执行中', 'succeeded': '成功',
            'failed': '失败', 'recovery_required': '待恢复'}.get(value, '待恢复')


def project_app(db, row, *, base_domain=Settings().base_domain):
    app_id = row['id']
    latest = db.execute('SELECT * FROM jobs WHERE app_id=? ORDER BY created_at DESC,id DESC LIMIT 1',
                        (app_id,)).fetchone()
    offline = db.execute('SELECT status FROM offline_operations WHERE app_id=? '
                         'ORDER BY updated_at DESC,id DESC LIMIT 1', (app_id,)).fetchone()
    owner = db.execute('SELECT owner_id FROM app_access WHERE app_id=?', (app_id,)).fetchone()
    last_input = _job_input(latest) if latest else {}
    domain = row['subdomain'] or last_input.get('subdomain') or ''
    active = None
    if row['active_release_id']:
        active = db.execute('SELECT status,snapshot FROM jobs WHERE id=? AND app_id=?',
                            (row['active_release_id'], app_id)).fetchone()
    visit_url = visit_paths = source_dir = ''
    if active and active['status'] == 'succeeded':
        if isinstance(domain, str) and re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,5}[a-z0-9])?', domain):
            visit_url = f'https://{domain}.{base_domain}/'
        try:
            snapshot = json.loads(active['snapshot'])
            manifest = snapshot.get('manifest') if isinstance(snapshot, dict) else None
            routes = manifest.get('routes') if isinstance(manifest, dict) else None
            if isinstance(routes, list):
                paths = [route.get('path') for route in routes if isinstance(route, dict)]
                visit_paths = ' · '.join(dict.fromkeys(path for path in paths if isinstance(path, str)
                                                  and re.fullmatch(r'/[A-Za-z0-9/_.~-]*', path)))
            source = Path(snapshot['source_dir'])
            if (source.is_absolute() and source.parent.name == 'sources'
                    and source.name == row['active_release_id'] and not source.is_symlink()
                    and not source.parent.is_symlink() and source.is_dir()):
                source_dir = str(source)
        except (TypeError, ValueError, KeyError, OSError):
            pass
    if offline and offline['status'] in ('queued', 'running', 'recovery_required'):
        status = {'queued': '排队', 'running': '执行中', 'recovery_required': '待恢复'}[offline['status']]
    elif row['active_release_id']:
        status = '在线'
    elif offline and offline['status'] == 'succeeded':
        status = '已下线'
    elif latest:
        status = _job_status(latest['status'])
        if status == '成功':
            status = '已下线'
    else:
        status = '待部署'
    return {'应用 ID': app_id, '应用名称': str(domain or app_id[:8]),
            '仓库地址': _safe_repository(row['repository']), '当前域名': str(domain),
            '状态': status, '负责人': _person(db, owner['owner_id'] if owner else None),
            '访问地址': visit_url, '访问路径': visit_paths, '服务器部署路径': source_dir,
            '当前部署 ID': str(row['active_release_id'] or ''),
            '创建时间': _ms(row['created_at']), '更新时间': _ms(row['updated_at'])}


def project_job(db, row):
    payload = _job_input(row)
    try:
        execution = json.loads(row['execution'])
    except (TypeError, ValueError):
        execution = {}
    actor = db.execute("SELECT user_id FROM operation_auth WHERE kind='job' AND id=?", (row['id'],)).fetchone()
    commit = execution.get('commit') if isinstance(execution, dict) else None
    return {'部署 ID': row['id'], '应用 ID': str(row['app_id'] or ''),
            '仓库地址': _safe_repository(payload.get('repository')),
            '分支或版本': str(payload.get('ref') or '')[:200],
            '固定提交': commit if isinstance(commit, str) and re.fullmatch(r'[0-9a-f]{40}', commit) else '',
            '域名前缀': str(payload.get('subdomain') or '')[:100],
            '状态': _job_status(row['status']), '阶段': str(row['phase'])[:100],
            '提交人': _name(db, actor['user_id'] if actor else None),
            '提交时间': _ms(row['created_at']), '更新时间': _ms(row['updated_at'])}


class BaseRemote:
    """Narrow, bounded Bitable client. Never logs response bodies or credentials."""

    def __init__(self, settings, config, *, client=None, clock=time.monotonic):
        self.settings, self.config, self.clock = settings, config, clock
        self.client = client or httpx.Client(timeout=httpx.Timeout(10, connect=5),
                                              trust_env=False, follow_redirects=False)
        self.owns_client = client is None
        self.token = None
        self.token_until = 0

    def close(self):
        if self.owns_client:
            self.client.close()

    def _request(self, method, url, *, token=None, **kwargs):
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        try:
            with self.client.stream(method, url, headers=headers, follow_redirects=False, **kwargs) as response:
                if response.is_redirect:
                    raise BaseSyncError('飞书接口发生意外跳转')
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE:
                        raise BaseSyncError('飞书响应过大')
                body = json.loads(data)
                if not isinstance(body, dict) or response.status_code // 100 != 2 or body.get('code') != 0:
                    code = body.get('code') if isinstance(body, dict) and type(body.get('code')) is int else 'HTTP_ERROR'
                    raise BaseSyncError(f'飞书接口失败：{code}')
                return body
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise BaseSyncError('飞书接口连接或响应异常') from None

    def _token(self):
        if self.token and self.clock() < self.token_until:
            return self.token
        body = self._request('POST', TOKEN_URL, data={
            'grant_type': 'client_credentials', 'client_id': self.settings.feishu_app_id,
            'client_secret': _read_secret(self.settings.feishu_app_secret_file),
            'scope': 'bitable:app',
        })
        token, ttl = body.get('access_token'), body.get('expires_in')
        if not isinstance(token, str) or not token or type(ttl) is not int or ttl <= 0:
            raise BaseSyncError('飞书访问凭证无效')
        self.token, self.token_until = token, self.clock() + max(1, ttl - 60)
        return token

    def upsert(self, kind, key, fields, record_id, client_token):
        table = self.config['app_table_id'] if kind == 'app' else self.config['deployment_table_id']
        path = f'{BASE_ORIGIN}/open-apis/bitable/v1/apps/{self.config["base_token"]}/tables/{table}/records'
        token = self._token()
        wire_fields = dict(fields)
        repository = wire_fields.pop('仓库地址', '')
        if repository:
            wire_fields['仓库地址'] = {'text': repository, 'link': repository}
        if '访问地址' in wire_fields:
            visit_url = wire_fields['访问地址']
            wire_fields['访问地址'] = {'text': visit_url, 'link': visit_url} if visit_url else None
        if not record_id:
            primary = '应用 ID' if kind == 'app' else '部署 ID'
            found = self._request('POST', path + '/search', token=token, json={
                'filter': {'conjunction': 'and', 'conditions': [
                    {'field_name': primary, 'operator': 'is', 'value': [key]}]}, 'page_size': 2})
            items = (found.get('data') or {}).get('items') or []
            if not isinstance(items, list) or len(items) > 1:
                raise BaseSyncError('飞书表格存在重复业务主键')
            if items:
                record_id = items[0].get('record_id')
        if record_id:
            body = self._request('PUT', path + '/' + record_id + '?user_id_type=open_id',
                                 token=token, json={'fields': wire_fields})
            return (body.get('data') or {}).get('record', {}).get('record_id') or record_id
        body = self._request('POST', path + '?client_token=' + client_token + '&user_id_type=open_id', token=token,
                             json={'fields': wire_fields})
        value = (body.get('data') or {}).get('record', {}).get('record_id')
        if not isinstance(value, str) or not value:
            raise BaseSyncError('飞书未返回记录 ID')
        return value


def load_base_config(path: Path):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise BaseSyncError('无法读取多维表格同步配置') from None
    if not isinstance(value, dict) or set(value) != {'base_token', 'app_table_id', 'deployment_table_id'}:
        raise BaseSyncError('多维表格同步配置字段错误')
    if not re.fullmatch(r'[A-Za-z0-9]{16,80}', value['base_token']) or any(
            not re.fullmatch(r'tbl[A-Za-z0-9]{8,80}', value[k]) for k in ('app_table_id', 'deployment_table_id')):
        raise BaseSyncError('多维表格同步配置标识错误')
    return value


class BaseMirror:
    def __init__(self, database_path, remote, *, base_domain=Settings().base_domain, clock=time.time):
        self.path, self.remote, self.base_domain, self.clock = database_path, remote, base_domain, clock

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=30000')
        return db

    def _reconcile(self, db):
        db.execute('''CREATE TABLE IF NOT EXISTS base_registry_sync (
            kind TEXT NOT NULL, entity_id TEXT NOT NULL, payload TEXT NOT NULL,
            desired_hash TEXT NOT NULL, synced_hash TEXT, record_id TEXT,
            client_token TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            retry_after REAL NOT NULL DEFAULT 0, last_error TEXT,
            PRIMARY KEY(kind,entity_id))''')
        for kind, table, project in (('app', 'apps', project_app), ('job', 'jobs', project_job)):
            for row in db.execute(f'SELECT * FROM {table}').fetchall():
                fields = project(db, row, base_domain=self.base_domain) if kind == 'app' else project(db, row)
                payload = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                digest = hashlib.sha256(payload.encode()).hexdigest()
                db.execute('''INSERT INTO base_registry_sync(kind,entity_id,payload,desired_hash,client_token)
                    VALUES (?,?,?,?,?) ON CONFLICT(kind,entity_id) DO UPDATE SET
                    payload=excluded.payload,desired_hash=excluded.desired_hash,
                    retry_after=CASE WHEN desired_hash<>excluded.desired_hash THEN 0 ELSE retry_after END,
                    attempts=CASE WHEN desired_hash<>excluded.desired_hash THEN 0 ELSE attempts END''',
                    (kind, row['id'], payload, digest, str(uuid.uuid4())))
        db.commit()

    def run_once(self, *, force=False, limit=1000):
        with self._connect() as db:
            self._reconcile(db)
            rows = db.execute('''SELECT * FROM base_registry_sync WHERE desired_hash IS NOT synced_hash
                AND (retry_after<=? OR ?) ORDER BY kind,entity_id LIMIT ?''',
                (self.clock(), int(force), limit)).fetchall()
            synced = 0
            for row in rows:
                try:
                    record_id = self.remote.upsert(row['kind'], row['entity_id'], json.loads(row['payload']),
                                                   row['record_id'], row['client_token'])
                except Exception:
                    # Preserve only a fixed local error category, never remote payloads or tokens.
                    attempts = row['attempts'] + 1
                    db.execute('''UPDATE base_registry_sync SET attempts=?,retry_after=?,last_error=?
                        WHERE kind=? AND entity_id=? AND desired_hash=?''',
                        (attempts, self.clock() + min(3600, 2 ** min(attempts, 10)),
                         'remote_unavailable', row['kind'], row['entity_id'], row['desired_hash']))
                    db.commit()
                    continue
                db.execute('''UPDATE base_registry_sync SET record_id=?,synced_hash=?,attempts=0,
                    retry_after=0,last_error=NULL WHERE kind=? AND entity_id=? AND desired_hash=?''',
                    (record_id, row['desired_hash'], row['kind'], row['entity_id'], row['desired_hash']))
                db.commit()
                synced += 1
            pending = db.execute('SELECT count(*) FROM base_registry_sync WHERE desired_hash IS NOT synced_hash').fetchone()[0]
            return {'synced': synced, 'pending': pending}

    def status(self):
        with self._connect() as db:
            self._reconcile(db)
            rows = db.execute('''SELECT kind,count(*) total,
                sum(CASE WHEN desired_hash IS NOT synced_hash THEN 1 ELSE 0 END) pending,
                sum(CASE WHEN last_error IS NOT NULL THEN 1 ELSE 0 END) errors
                FROM base_registry_sync GROUP BY kind''').fetchall()
            return {row['kind']: {'total': row['total'], 'pending': row['pending'], 'errors': row['errors']}
                    for row in rows}
