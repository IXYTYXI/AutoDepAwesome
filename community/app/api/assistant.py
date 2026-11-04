from uuid import UUID
from fastapi import APIRouter, Request
from app.api.dependencies import CurrentUser, check_source_tenant
from app.core.errors import AppError
from app.core.error_codes import ErrorCode
from app.schemas.assistant import ChatInput, TicketDecision
from app.schemas.common import success

router = APIRouter(prefix='/api/v1/assistant', tags=['社区助手'])


@router.get('/config')
async def config(request: Request, user: CurrentUser):
    settings = request.app.state.settings
    allowed = not settings.assistant_source_tenant or user.identity_source == 'development' or user.tenant_id == settings.assistant_source_tenant
    return success(request, {'enabled': settings.assistant_enabled and allowed})


def service(request, user):
    check_source_tenant(request, user, 'assistant')
    if not request.app.state.settings.assistant_enabled:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE)
    return request.app.state.assistant


@router.post('/session')
async def session(request: Request, user: CurrentUser):
    return success(request, await service(request, user).open(user))


@router.post('/messages')
async def send(body: ChatInput, request: Request, user: CurrentUser):
    return success(request, await service(request, user).send(user, body))


@router.get('/session/{session_id}')
async def poll(session_id: UUID, request: Request, user: CurrentUser):
    return success(request, await service(request, user).poll(user, session_id))


@router.post('/tickets/{draft_id}/decision')
async def decide(draft_id: UUID, body: TicketDecision, request: Request, user: CurrentUser):
    return success(request, await service(request, user).decide(user, body.session_id, draft_id, body.confirmed))
