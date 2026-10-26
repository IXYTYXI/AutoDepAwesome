import time

import pytest

from test_team_search import create_project, person, search
import test_team_integration as team_fixtures


sessions = team_fixtures.sessions
team = team_fixtures.team


OWNERS_PATH = '/api/v1/integrations/owners/search'


def owners(team, session_id, **fields):
    client, _, credential, _ = team
    return client.post(OWNERS_PATH, json={'sessionId': session_id, **fields},
                       headers={'Authorization': 'Bearer ' + credential['token']})


def named_user(app, open_id, name):
    return app.state.identity.login_user({'open_id': open_id, 'tenant_key': 'tenant', 'name': name})


def admin_session(team, sessions):
    _, app, _, _ = team
    admin = named_user(app, 'ou_admin', '管理员')
    admin = app.state.identity.set_user(admin['id'], is_admin=True)
    return sessions.issue(person(admin['open_id']))


def test_owner_candidates_are_limited_to_deployable_projects(team, sessions):
    _, app, credential, _ = team
    named_user(app, 'ou_applicant', '自己')
    named_user(app, 'ou_other', '维护项目负责人')
    named_user(app, 'ou_viewed', '仅查看项目负责人')
    named_user(app, 'ou_hidden', '无权限项目负责人')
    named_user(app, 'ou_unused', '没有项目的用户')
    own, actor = create_project(team, sessions, 'own')
    maintained, other = create_project(team, sessions, 'maint', open_id='ou_other')
    create_project(team, sessions, 'maint2', open_id='ou_other')
    viewed, viewer_owner = create_project(team, sessions, 'viewed', open_id='ou_viewed')
    _, hidden_owner = create_project(team, sessions, 'hidden', open_id='ou_hidden')
    app.state.access.set_member(other, maintained, actor['id'], 'maintainer')
    app.state.access.set_member(viewer_owner, viewed, actor['id'], 'viewer')
    sid = sessions.issue(person())

    response = owners(team, sid)
    assert response.status_code == 200, response.text
    expected = sorted([
        {'ownerId': actor['id'], 'displayName': '自己'},
        {'ownerId': other['id'], 'displayName': '维护项目负责人'},
    ], key=lambda item: (item['displayName'].casefold(), item['ownerId']))
    assert response.json() == {'items': expected, 'total': 2, 'limit': 20, 'offset': 0, 'nextOffset': None}
    assert owners(team, sid, query='无权限').json()['total'] == 0
    assert owners(team, sid, query=credential['ownerUserId']).json()['total'] == 0
    assert search(team, sid, ownerId=hidden_owner['id']).json()['total'] == 0
    assert [item['appId'] for item in search(team, sid, ownerId=actor['id']).json()['items']] == [own]
    assert [item['appId'] for item in search(team, sid, ownerId=other['id']).json()['items']] == [maintained]


def test_admin_owners_include_disabled_owners_but_not_unassigned_apps_or_unrelated_users(team, sessions):
    _, app, _, _ = team
    named_user(app, 'ou_applicant', '已停用负责人')
    owned, owner = create_project(team, sessions, 'owned')
    unassigned, old_owner = create_project(team, sessions, 'legacy', open_id='ou_legacy')
    named_user(app, 'ou_unused', '没有项目')
    with app.state.identity.connection() as db:
        db.execute('DELETE FROM app_access WHERE app_id=?', (unassigned,))
        db.execute('DELETE FROM app_members WHERE app_id=?', (unassigned,))
    app.state.identity.set_user(owner['id'], enabled=False)
    sid = admin_session(team, sessions)

    response = owners(team, sid)
    assert response.status_code == 200, response.text
    assert response.json()['items'] == [{'ownerId': owner['id'], 'displayName': '已停用负责人'}]
    apps = search(team, sid).json()
    assert apps['total'] == 2
    by_id = {item['appId']: item for item in apps['items']}
    assert by_id[unassigned]['ownerId'] is None and by_id[unassigned]['ownerName'] is None
    assert by_id[owned]['ownerId'] == owner['id']
    assert search(team, sid, ownerId=old_owner['id']).json()['total'] == 0
    assert search(team, sid, ownerId=owner['id']).json()['total'] == 1


def test_owner_filter_applies_current_ownership_before_query_count_and_pagination(team, sessions):
    _, app, _, _ = team
    alpha, actor = create_project(team, sessions, 'alpha')
    beta, _ = create_project(team, sessions, 'beta')
    gamma, other = create_project(team, sessions, 'gamma', open_id='ou_other')
    app.state.access.set_member(other, gamma, actor['id'], 'maintainer')
    sid = sessions.issue(person())
    result = search(team, sid, ownerId=actor['id'], limit=1).json()
    assert result['total'] == 2 and result['nextOffset'] == 1
    assert result['items'][0]['appId'] == alpha
    assert result['items'][0]['ownerId'] == actor['id']
    assert result['items'][0]['ownerName'] == actor['name']
    second = search(team, sid, ownerId=actor['id'], limit=1, offset=1).json()
    assert [item['appId'] for item in second['items']] == [beta]
    assert second['nextOffset'] is None
    assert search(team, sid, ownerId=actor['id'], query=' GAMMA ').json()['total'] == 0
    assert search(team, sid, ownerId='does-not-exist').json()['total'] == 0

    # App ownership, not the initial/latest deployment applicant, controls filtering.
    app.state.access.transfer_owner(actor, alpha, other['id'])
    assert search(team, sid, ownerId=actor['id']).json()['total'] == 1
    transferred = search(team, sid, ownerId=other['id']).json()
    assert {item['appId'] for item in transferred['items']} == {alpha, gamma}


def test_owner_search_preserves_same_names_and_stable_casefold_paging(team, sessions):
    _, app, _, _ = team
    users = [named_user(app, 'ou_' + str(index), name)
             for index, name in enumerate(['ALICE', 'alice', '同名', '同名'])]
    for index, user in enumerate(users):
        create_project(team, sessions, 'app' + str(index), open_id=user['open_id'])
    sid = admin_session(team, sessions)
    expected = [{'ownerId': user['id'], 'displayName': user['name']}
                for user in sorted(users, key=lambda user: (user['name'].casefold(), user['id']))]
    first = owners(team, sid, limit=2).json()
    second = owners(team, sid, limit=2, offset=2).json()
    assert first['items'] + second['items'] == expected
    assert first['total'] == second['total'] == 4
    assert first['nextOffset'] == 2 and second['nextOffset'] is None
    assert owners(team, sid, offset=4).json()['items'] == []
    assert owners(team, sid, query='  AlIcE ').json()['total'] == 2
    assert owners(team, sid, query='同名').json()['total'] == 2
    assert owners(team, sid, query=users[0]['id'].upper()).json()['items'] == [
        {'ownerId': users[0]['id'], 'displayName': users[0]['name']},
    ]


@pytest.mark.parametrize('change', [{'enabled': False}, {'can_deploy': False}])
def test_owner_search_obeys_current_local_account_permissions(team, sessions, change):
    _, app, _, _ = team
    _, actor = create_project(team, sessions, 'own')
    sid = sessions.issue(person())
    assert owners(team, sid).status_code == 200
    app.state.identity.set_user(actor['id'], **change)
    assert owners(team, sid).status_code == 403


def test_owner_search_uses_current_platform_role_not_session_claims(team, sessions):
    _, app, _, _ = team
    create_project(team, sessions, 'hidden', open_id='ou_hidden')
    actor = named_user(app, 'ou_applicant', '申请人')
    sid = sessions.issue(person())
    result = owners(team, sid)
    assert result.status_code == 200, result.text
    assert result.json()['total'] == 0
    app.state.identity.set_user(actor['id'], is_admin=True)
    assert owners(team, sid).json()['total'] == 1
    app.state.identity.set_user(actor['id'], is_admin=False)
    assert owners(team, sid).json()['total'] == 0


def test_owner_search_requires_source_credential_and_live_shared_session(team, sessions):
    client, _, credential, credentials = team
    sid = sessions.issue(person())
    assert client.post(OWNERS_PATH, json={'sessionId': sid}).status_code == 401
    for kind in ('source', 'person'):
        issued = credentials.issue(credential['ownerUserId'], kind, kind=kind, allow_create=True)
        assert client.post(OWNERS_PATH, json={'sessionId': sid}, headers={
            'Authorization': 'Bearer ' + issued['token'],
        }).status_code == 403
    sessions.logout(sid)
    assert owners(team, sid).status_code == 401
    sid = sessions.issue(person(), ttl=1)
    sessions.clock = lambda: time.time() + 2
    assert owners(team, sid).status_code == 401


def test_owner_search_fails_closed_when_redis_unavailable(team, sessions):
    sid = sessions.issue(person())
    def unavailable(_key):
        raise ConnectionError('Redis unavailable')
    sessions.redis.get = unavailable
    response = owners(team, sid)
    assert response.status_code == 503
    assert 'items' not in response.json()


def test_owner_candidates_and_filter_reflect_membership_revocation(team, sessions):
    _, app, _, _ = team
    app_id, owner = create_project(team, sessions, 'maint', open_id='ou_other')
    actor = named_user(app, 'ou_applicant', '维护者')
    sid = sessions.issue(person())
    app.state.access.set_member(owner, app_id, actor['id'], 'maintainer')
    assert owners(team, sid).json()['total'] == 1
    app.state.access.set_member(owner, app_id, actor['id'], 'viewer')
    assert owners(team, sid).json()['total'] == 0
    assert search(team, sid, ownerId=owner['id']).json()['total'] == 0
    app.state.access.set_member(owner, app_id, actor['id'], 'maintainer')
    app.state.access.remove_member(owner, app_id, actor['id'])
    assert owners(team, sid).json()['total'] == 0
    assert search(team, sid, ownerId=owner['id']).json()['total'] == 0


@pytest.mark.parametrize('attribute', ['app_id', 'tenant_key'])
def test_both_search_routes_reject_shared_identity_namespace_mismatch(team, sessions, attribute):
    sid = sessions.issue(person())
    setattr(sessions, attribute, 'other')
    assert owners(team, sid).status_code == 503
    assert search(team, sid).status_code == 503


@pytest.mark.parametrize('fields', [
    {'query': 123}, {'query': None}, {'query': 'x' * 201},
    {'limit': 0}, {'limit': 21}, {'limit': '1'}, {'limit': True},
    {'offset': -1}, {'offset': 1000001}, {'offset': '1'},
    {'unknown': 'ignored'}, {'sessionId': None}, {'ownerId': 'unexpected'},
])
def test_owner_search_rejects_invalid_request_fields(team, sessions, fields):
    assert owners(team, sessions.issue(person()), **fields).status_code == 422


@pytest.mark.parametrize('owner_id', ['', ' ', '\t\n', 'x' * 129, 1, True, []])
def test_app_search_rejects_invalid_owner_filter(team, sessions, owner_id):
    assert search(team, sessions.issue(person()), ownerId=owner_id).status_code == 422


def test_null_owner_filter_is_backwards_compatible(team, sessions):
    create_project(team, sessions, 'own')
    sid = sessions.issue(person())
    assert search(team, sid, ownerId=None).json() == search(team, sid).json()
