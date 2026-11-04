from fastapi import APIRouter, Request
from starlette.responses import FileResponse

from app.api.dependencies import CurrentUser, check_source_tenant
from app.api.error_docs import error_responses
from app.schemas.cases import CaseDirectory
from app.schemas.common import Envelope, success

router = APIRouter(prefix='/api/v1/cases', tags=['AI 案例'], responses=error_responses())



@router.get('', response_model=Envelope[CaseDirectory])
async def directory(request: Request, user: CurrentUser):
    check_source_tenant(request, user, "cases")
    return success(request, await request.app.state.cases.read())


@router.get('/media/{image_id}', response_class=FileResponse,
            responses={200: {'content': {'image/webp': {'schema': {'type': 'string', 'format': 'binary'}}}}})
async def cover(image_id: str, request: Request, user: CurrentUser):
    check_source_tenant(request, user, "cases")
    return FileResponse(await request.app.state.cases.image(image_id), media_type='image/webp')
