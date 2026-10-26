import sqlite3
import time

import pytest

from test_team_integration import call, payload, person
import test_team_integration as team_fixtures


sessions = team_fixtures.sessions
team = team_fixtures.team


PATH = '/api/v1/integrations/apps/search'


def search(team, session_id, **fields):
    client, _, credential, _ = team
    return client.post(PATH, json={'sessionId': session_id, **fields},
                       headers={'Authorization': 'Bearer ' + credential['token']})


def create_project(team, sessions, name, *, open_id='ou_applicant', repository=None):
    client, app, credential, _ = team
    deployment = payload(name)
    if repository is not None:
        deployment['repository'] = repository
    response = call(client, credential['token'], {
        'deployment': deployment, 'sessionId': sessions.issue(person(open_id)),
    }, 'create-' + name)
    assert response.status_code == 202, response.text
    with app.state.identity.connection() as db:
        owner_id = db.execute('SELECT id FROM auth_users WHERE open_id=?', (open_id,)).fetchone()[0]
    return response.json()['appId'], app.state.identity.authorize_user(owner_id)


def test_search_uses_applicant_permissions_before_filter_and_pagination(team, sessions):
    _, app, credential, _ = team
    owner = app.state.identity.get_user(credential['ownerUserId'])
    hidden, _ = create_project(team, sessions, 'hidden', open_id=owner['open_id'])
    own, actor = create_project(team, sessions, 'own')
    viewed, other = create_project(team, sessions, 'viewed', open_id='ou_other')
    maintained, _ = create_project(team, sessions, 'maint', open_id='ou_other')
    app.state.access.set_member(other, viewed, actor['id'], 'viewer')
    app.state.access.set_member(other, maintained, actor['id'], 'maintainer')
    sid = sessions.issue(person())

    response = search(team, sid, limit=1)
    assert response.status_code == 200, response.text
    first = response.json()
    assert first['total'] == 2
    assert first['limit'] == 1 and first['offset'] == 0 and first['nextOffset'] == 1
    assert first['items'][0] == {
        'appId': own, 'projectName': 'own', 'repository': payload()['repository'],
        'subdomain': None, 'lastSubdomain': 'own', 'domainBound': False,
        'ownerId': actor['id'], 'ownerName': actor['name'],
    }
    second = search(team, sid, limit=1, offset=1).json()
    assert [item['appId'] for item in second['items']] == [maintained]
    assert second['total'] == 2 and second['nextOffset'] is None
    assert search(team, sid, query=hidden).json()['total'] == 0
    assert search(team, sid, query='viewed').json()['items'] == []
    assert search(team, sid, offset=2).json() == {
        'items': [], 'total': 2, 'limit': 20, 'offset': 2, 'nextOffset': None,
    }


def test_search_matches_name_previous_name_repository_and_app_id(team, sessions):
    _, app, _, _ = team
    app_id, _ = create_project(team, sessions, 'alpha', repository='https://github.com/org/Alpha.git')
    unbound, actor = create_project(team, sessions, 'oldname')
    with app.state.store._connection() as db:
        db.execute('UPDATE apps SET subdomain=? WHERE id=?', ('alpha', app_id))
        db.execute('UPDATE apps SET subdomain=NULL WHERE id=?', (unbound,))
    sid = sessions.issue(person())
    for query in ('  ALPHA  ', 'ORG/ALPHA.GIT', app_id[:12].upper()):
        result = search(team, sid, query=query).json()
        assert [item['appId'] for item in result['items']] == [app_id]
    previous = search(team, sid, query='OLDNAME').json()['items']
    assert previous == [{
        'appId': unbound, 'projectName': 'oldname', 'repository': payload()['repository'],
        'subdomain': None, 'lastSubdomain': 'oldname', 'domainBound': False,
        'ownerId': actor['id'], 'ownerName': actor['name'],
    }]
    assert search(team, sid, query='  ').json()['total'] == 2
    assert search(team, sid, query='no-such-project').json()['total'] == 0


def test_search_does_not_create_deployment_or_return_internal_application_fields(team, sessions):
    _, app, _, _ = team
    create_project(team, sessions, 'demo')
    def counts():
        with sqlite3.connect(app.state.store.settings.database_path) as db:
            return tuple(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                         for table in ('apps', 'jobs', 'operation_auth'))
    before = counts()
    sid = sessions.issue(person())
    result = search(team, sid).json()
    assert result['total'] == 1
    assert counts() == before
    assert set(result['items'][0]) == {
        'appId', 'projectName', 'repository', 'subdomain', 'lastSubdomain', 'domainBound',
        'ownerId', 'ownerName',
    }


def test_search_empty_results_and_global_deploy_restriction(team, sessions):
    _, app, _, _ = team
    sid = sessions.issue(person())
    assert search(team, sid).json() == {
        'items': [], 'total': 0, 'limit': 20, 'offset': 0, 'nextOffset': None,
    }
    with app.state.identity.connection() as db:
        actor_id = db.execute("SELECT id FROM auth_users WHERE open_id='ou_applicant'").fetchone()[0]
    app.state.identity.set_user(actor_id, can_deploy=False)
    assert search(team, sid).status_code == 403


@pytest.mark.parametrize('change', [{'enabled': False}, {'can_deploy': False}])
def test_search_respects_local_permission_revocation(team, sessions, change):
    _, app, _, _ = team
    _, actor = create_project(team, sessions, 'demo')
    sid = sessions.issue(person())
    assert search(team, sid).json()['total'] == 1
    app.state.identity.set_user(actor['id'], **change)
    assert search(team, sid).status_code == 403


def test_search_requires_live_session_and_does_not_accept_receipt(team, sessions):
    client, _, credential, _ = team
    sid = sessions.issue(person(), ttl=1)
    receipt = sessions.admit(sid, 'search', payload())
    sessions.clock = lambda: time.time() + 2
    assert search(team, sid).status_code == 401
    assert client.post(PATH, json={'receipt': receipt}, headers={
        'Authorization': 'Bearer ' + credential['token'],
    }).status_code == 422


def test_search_redis_unavailability_fails_closed(team, sessions):
    sid = sessions.issue(person())
    def offline(_key):
        raise ConnectionError('Redis unavailable')
    sessions.redis.get = offline
    response = search(team, sid)
    assert response.status_code == 503
    assert 'items' not in response.json()


def test_search_rejects_missing_credentials_ordinary_source_and_self_managed_tokens(team, sessions):
    client, _, credential, credentials = team
    sid = sessions.issue(person())
    assert client.post(PATH, json={'sessionId': sid}).status_code == 401
    ordinary = credentials.issue(credential['ownerUserId'], 'ordinary', kind='source', allow_any_app=True)
    personal = credentials.issue(credential['ownerUserId'], 'personal', kind='person',
                                 allow_create=True, self_managed=True)
    for issued in (ordinary, personal):
        response = client.post(PATH, json={'sessionId': sid},
                               headers={'Authorization': 'Bearer ' + issued['token']})
        assert response.status_code == 403


@pytest.mark.parametrize('fields', [
    {'query': 123}, {'query': None}, {'query': 'x' * 201}, {'query': []},
    {'limit': 0}, {'limit': 21}, {'limit': '1'}, {'limit': True}, {'limit': 1.5},
    {'offset': -1}, {'offset': 1000001}, {'offset': '1'}, {'offset': False},
    {'unknown': 'ignored'}, {'sessionId': None},
])
def test_search_rejects_invalid_filters(team, sessions, fields):
    sid = sessions.issue(person())
    assert search(team, sid, **fields).status_code == 422
