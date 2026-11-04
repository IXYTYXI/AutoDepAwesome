from fastapi import APIRouter, Request
from app.api.dependencies import CurrentUser, check_source_tenant
from app.api.error_docs import error_responses
from app.schemas.faq import FAQSnapshot
from app.schemas.common import Envelope, success

router = APIRouter(prefix='/api/v1/faq', tags=['常见问题'], responses=error_responses())


@router.get('', response_model=Envelope[FAQSnapshot])
async def faq(request: Request, user: CurrentUser):
    check_source_tenant(request, user, "faq")
    return success(request, await request.app.state.faq.read())
