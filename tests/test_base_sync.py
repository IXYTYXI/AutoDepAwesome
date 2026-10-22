import json
import sqlite3
import time

from awesome_deploy.base_sync import BaseMirror, project_app, project_job


class FakeBase:
    def __init__(self):
        self.rows = {'app': {}, 'job': {}}
        self.writes = 0
        self.fail = False

    def upsert(self, kind, key, fields, record_id, client_token):
        if self.fail:
            raise RuntimeError('remote unavailable')
        self.writes += 1
        self.rows[kind][key] = dict(fields)
        return f'{kind}-{key}'


def database(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript('''
    CREATE TABLE apps(id TEXT PRIMARY KEY,repository TEXT,subdomain TEXT,active_release_id TEXT,created_at REAL,updated_at REAL);
    CREATE TABLE jobs(id TEXT PRIMARY KEY,app_id TEXT,input TEXT,status TEXT,phase TEXT,snapshot TEXT,execution TEXT,error TEXT,created_at REAL,updated_at REAL);
    CREATE TABLE offline_operations(id TEXT PRIMARY KEY,app_id TEXT,status TEXT,updated_at REAL);
    CREATE TABLE app_access(app_id TEXT PRIMARY KEY,owner_id TEXT);
    CREATE TABLE operation_auth(kind TEXT,id TEXT,user_id TEXT);
    CREATE TABLE auth_users(id TEXT PRIMARY KEY,name TEXT,open_id TEXT);
    ''')
    db.execute("INSERT INTO apps VALUES (?,?,?,?,?,?)", ('a1','https://user:pass@github.com/team/repo.git?token=oops','demo',None,100,100))
    db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)", ('j1','a1',json.dumps({'repository':'https://user:pass@github.com/team/repo.git?token=oops','ref':'main','subdomain':'demo','secretRefs':['VERY_SECRET']}),'failed','build',None,'{}',json.dumps({'message':'VERY_SECRET'}),100,100))
    db.execute("INSERT INTO auth_users VALUES (?,?,?)", ('u1','测试用户','ou_test123'))
    db.execute("INSERT INTO app_access VALUES (?,?)", ('a1','u1'))
    db.execute("INSERT INTO operation_auth VALUES (?,?,?)", ('job','j1','u1'))
    db.commit()
    return db


def test_projection_excludes_secrets_and_raw_errors(tmp_path):
    with database(tmp_path / 'db.sqlite') as db:
        app = project_app(db, db.execute('SELECT * FROM apps').fetchone())
        job = project_job(db, db.execute('SELECT * FROM jobs').fetchone())
    rendered = json.dumps([app,job], ensure_ascii=False)
    assert 'VERY_SECRET' not in rendered
    assert 'pass@' not in rendered
    assert 'token=oops' not in rendered
    assert app['状态'] == '失败'
    assert job['状态'] == '失败'
    assert app['负责人'] == [{'id': 'ou_test123'}]
    assert job['提交人'] == '测试用户'


def test_backfill_retries_and_updates_without_duplicates(tmp_path):
    path = tmp_path / 'db.sqlite'
    database(path).close()
    remote = FakeBase()
    mirror = BaseMirror(path, remote)
    assert mirror.run_once() == {'synced': 2, 'pending': 0}
    assert remote.writes == 2
    assert mirror.run_once() == {'synced': 0, 'pending': 0}
    with sqlite3.connect(path) as db:
        db.execute("UPDATE jobs SET status='succeeded',phase='complete',updated_at=? WHERE id='j1'", (time.time(),))
    remote.fail = True
    assert mirror.run_once()['pending'] == 2
    remote.fail = False
    assert mirror.run_once(force=True) == {'synced': 2, 'pending': 0}
    assert len(remote.rows['job']) == 1
    assert remote.rows['job']['j1']['状态'] == '成功'


def test_remote_search_before_create_and_update(monkeypatch):
    import httpx
    from types import SimpleNamespace
    from awesome_deploy.base_sync import BaseRemote

    calls = []
    def handler(request):
        calls.append((request.method, str(request.url), request.content))
        if str(request.url).endswith('/oauth/v3/token'):
            return httpx.Response(200, json={'code': 0, 'access_token': 'token', 'expires_in': 7200})
        if str(request.url).endswith('/search'):
            return httpx.Response(200, json={'code': 0, 'data': {'items': []}})
        if request.method == 'POST':
            return httpx.Response(200, json={'code': 0, 'data': {'record': {'record_id': 'rec1'}}})
        return httpx.Response(200, json={'code': 0, 'data': {'record': {'record_id': 'rec1'}}})
    monkeypatch.setattr('awesome_deploy.base_sync._read_secret', lambda path: 'secret')
    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = SimpleNamespace(feishu_app_id='cli_test', feishu_app_secret_file='/secret')
    remote = BaseRemote(settings, {'base_token': 'base123456789012',
                                    'app_table_id': 'tbl123456789',
                                    'deployment_table_id': 'tbl987654321'}, client=client)
    assert remote.upsert('app', 'a1', {'应用 ID': 'a1', '仓库地址': 'https://github.com/team/repo'}, None, '00000000-0000-4000-8000-000000000000') == 'rec1'
    assert remote.upsert('app', 'a1', {'应用 ID': 'a1', '仓库地址': 'https://github.com/team/repo'}, 'rec1', '00000000-0000-4000-8000-000000000000') == 'rec1'
    assert [method for method, _, _ in calls] == ['POST', 'POST', 'POST', 'PUT']
    assert 'user_id_type=open_id' in calls[2][1] and 'user_id_type=open_id' in calls[3][1]
    sent = json.loads(calls[2][2])['fields']['仓库地址']
    assert sent == {'text': 'https://github.com/team/repo', 'link': 'https://github.com/team/repo'}
    remote.upsert('app', 'a1', {'应用 ID': 'a1', '访问地址': 'https://demo.example.test/'},
                  'rec1', '00000000-0000-4000-8000-000000000000')
    assert json.loads(calls[-1][2])['fields']['访问地址'] == {
        'text': 'https://demo.example.test/', 'link': 'https://demo.example.test/'}
    remote.upsert('app', 'a1', {'应用 ID': 'a1', '访问地址': ''}, 'rec1', '00000000-0000-4000-8000-000000000000')
    assert json.loads(calls[-1][2])['fields']['访问地址'] is None
    assert all('secret' not in url and 'Bearer' not in url for _, url, _ in calls)


def test_owner_transfer_uses_current_login_identity(tmp_path):
    path = tmp_path / 'db.sqlite'
    with database(path) as db:
        db.execute("INSERT INTO auth_users VALUES (?,?,?)", ('u2', '另一个用户', 'ou_other123'))
        db.execute("UPDATE app_access SET owner_id='u2' WHERE app_id='a1'")
    remote = FakeBase()
    mirror = BaseMirror(path, remote)
    assert mirror.run_once() == {'synced': 2, 'pending': 0}
    assert remote.rows['app']['a1']['负责人'] == [{'id': 'ou_other123'}]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE app_access SET owner_id='u1' WHERE app_id='a1'")
    assert mirror.run_once() == {'synced': 1, 'pending': 0}
    assert remote.rows['app']['a1']['负责人'] == [{'id': 'ou_test123'}]


def test_active_app_projects_visit_url_and_server_source_path(tmp_path):
    path = tmp_path / 'db.sqlite'
    with database(path) as db:
        source = tmp_path / 'sources' / 'j1'
        source.mkdir(parents=True)
        db.execute("UPDATE jobs SET status='succeeded',snapshot=? WHERE id='j1'",
                   (json.dumps({'source_dir': str(source)}),))
        db.execute("UPDATE apps SET active_release_id='j1' WHERE id='a1'")
        app = project_app(db, db.execute('SELECT * FROM apps').fetchone(), base_domain='example.test')
        assert app['访问地址'] == 'https://demo.example.test/'
        assert app['服务器部署路径'] == str(source)
        source.rmdir()
        app = project_app(db, db.execute('SELECT * FROM apps').fetchone(), base_domain='example.test')
        assert app['服务器部署路径'] == ''
        db.execute("UPDATE apps SET active_release_id=NULL WHERE id='a1'")
        app = project_app(db, db.execute('SELECT * FROM apps').fetchone(), base_domain='example.test')
        assert app['访问地址'] == ''
        assert app['服务器部署路径'] == ''


def test_base_mirror_uses_configured_public_domain(tmp_path):
    path = tmp_path / 'db.sqlite'
    with database(path) as db:
        db.execute("UPDATE jobs SET status='succeeded' WHERE id='j1'")
        db.execute("UPDATE apps SET active_release_id='j1' WHERE id='a1'")
    remote = FakeBase()
    assert BaseMirror(path, remote, base_domain='example.test').run_once()['pending'] == 0
    assert remote.rows['app']['a1']['访问地址'] == 'https://demo.example.test/'
