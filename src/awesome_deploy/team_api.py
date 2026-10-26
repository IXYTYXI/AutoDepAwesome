"""Server-to-server delegated deployment routes; no browser login endpoints."""
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from .access import denied
from .team_sessions import SessionError, SessionUnavailable, load_shared_sessions


class TeamSubmission(BaseModel):
    model_config = ConfigDict(extra='forbid')
    deployment: dict
    sessionId: StrictStr | None = None
    receipt: dict | None = None


class TeamOwnerSearch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sessionId: StrictStr
    query: StrictStr = Field(default='', max_length=200)
    limit: StrictInt = Field(default=20, ge=1, le=20)
    offset: StrictInt = Field(default=0, ge=0, le=1_000_000)


class TeamAppSearch(TeamOwnerSearch):
    ownerId: StrictStr | None = Field(default=None, min_length=1, max_length=128)

    @field_validator('ownerId')
    @classmethod
    def nonblank_owner(cls, value):
        if value is not None and not value.strip():
            raise ValueError('ownerId must not be blank')
        return value


def install_team_routes(app, settings, *, automation_access, deployment_input, submission_model, public_job):
    app.state.shared_sessions = load_shared_sessions(settings.shared_sessions_file) if settings.shared_sessions_file else {}
    identity, store = app.state.identity, app.state.store

    def source_access(pair=Depends(automation_access)):
        owner, credential = pair
        source = credential.get('delegation_source')
        if credential['kind'] != 'source' or not source or source not in app.state.shared_sessions:
            raise denied()
        sessions = app.state.shared_sessions[source]
        if sessions.app_id != settings.feishu_app_id or sessions.tenant_key != settings.feishu_tenant_key:
            raise HTTPException(503, '共享身份配置与平台身份命名空间不一致。')
        return owner, credential, sessions

    def applicant(details):
        # Never let imported claims restore a locally disabled/ineligible account or grant admin roles.
        with identity.connection() as db:
            row = db.execute('SELECT * FROM auth_users WHERE app_id=? AND tenant_key=? AND open_id=?',
                (settings.feishu_app_id, settings.feishu_tenant_key, details['openId'])).fetchone()
        if row:
            return identity.authorize_user(row['id'], expected_revision=row['revision'])
        return identity.login_user({'open_id': details['openId'], 'tenant_key': details['tenantKey'],
                                    'name': details['name']})

    def search_actor(session_id, source):
        _, _, sessions = source
        try:
            actor = applicant(sessions.read(session_id))
        except SessionUnavailable:
            raise HTTPException(503, '共享登录服务暂不可用，无法查询项目。') from None
        except SessionError:
            raise HTTPException(401, '共享会话无效或已过期。') from None
        if not actor['can_deploy']:
            raise denied()
        return actor

    def page(items, body):
        end = body.offset + body.limit
        return {'items': items[body.offset:end], 'total': len(items), 'limit': body.limit,
                'offset': body.offset, 'nextOffset': end if end < len(items) else None}

    @app.post('/api/v1/integrations/owners/search')
    def search_owners(body: TeamOwnerSearch, source=Depends(source_access)):
        actor = search_actor(body.sessionId, source)
        owners = app.state.access.deployment_owners(actor)
        unique = {owner['ownerId']: {'ownerId': owner['ownerId'], 'displayName': owner['ownerName']}
                  for owner in owners.values() if owner['ownerId'] is not None}
        query = body.query.strip().casefold()
        matched = [owner for owner in unique.values() if not query or any(
            query in owner[key].casefold() for key in ('ownerId', 'displayName'))]
        matched.sort(key=lambda owner: (owner['displayName'].casefold(), owner['ownerId']))
        return page(matched, body)

    @app.post('/api/v1/integrations/apps/search')
    def search_apps(body: TeamAppSearch, source=Depends(source_access)):
        actor = search_actor(body.sessionId, source)
        owners = app.state.access.deployment_owners(actor)
        query = body.query.strip().casefold()
        visible = []
        for item in store.apps():
            owner = owners.get(item['id'])
            if owner is None or body.ownerId is not None and body.ownerId != owner['ownerId']:
                continue
            if query and not any(query in str(item.get(key) or '').casefold()
                                 for key in ('subdomain', 'lastSubdomain', 'repository', 'id')):
                continue
            visible.append({
                'appId': item['id'], 'projectName': item['subdomain'] or item['lastSubdomain'],
                'repository': item['repository'], 'subdomain': item['subdomain'],
                'lastSubdomain': item['lastSubdomain'], 'domainBound': item['domainBound'],
                **owner,
            })
        return page(visible, body)

    @app.post('/api/v1/integrations/deployments', status_code=202)
    def submit(body: TeamSubmission, request: Request, source=Depends(source_access)):
        _, credential, sessions = source
        if (body.sessionId is None) == (body.receipt is None):
            raise HTTPException(400, 'sessionId 和 receipt 必须且只能提供一种。')
        if len(request.headers.getlist('idempotency-key')) != 1:
            raise HTTPException(400, '必须提供唯一 Idempotency-Key。')
        if not request.headers.get('content-type', '').lower().startswith('application/json'):
            raise HTTPException(415, '请使用 application/json。')
        from pydantic import ValidationError
        try:
            deployment = submission_model.model_validate(body.deployment)
        except ValidationError:
            raise HTTPException(422, '部署请求字段无效。') from None
        # Validate the original payload signature before normalization of repository URLs.
        try:
            if body.receipt is not None:
                details = sessions.verify_receipt(body.receipt, request.headers['idempotency-key'], body.deployment)
            else:
                details = sessions.read(body.sessionId)
        except SessionUnavailable:
            raise HTTPException(503, '共享登录服务暂不可用，未受理新请求。') from None
        except SessionError:
            raise HTTPException(401, '共享会话或受理凭据无效。') from None
        payload, key = deployment_input(deployment, request)
        actor = applicant(details)
        request.state.actor = actor
        return public_job(store.enqueue(payload, key, actor=actor, credential_id=credential['id'], delegated=True))

    @app.get('/api/v1/integrations/deployments/{job_id}')
    def get(job_id: str, source=Depends(source_access)):
        _, credential, _ = source
        job = store.get(job_id)
        with store._connection() as db:
            binding = db.execute('''SELECT c.delegation_source FROM operation_credentials o
                JOIN api_credentials c ON c.id=o.credential_id
                JOIN operation_delegations d ON d.kind=o.kind AND d.id=o.id WHERE o.kind='job' AND o.id=?''', (job_id,)).fetchone()
            if not binding or binding['delegation_source'] != credential['delegation_source']:
                raise denied()
        # A may query only its own source's requests after the applicant has logged out.
        return public_job(job)
