from fastapi import APIRouter, Request
from starlette.responses import FileResponse

from app.api.dependencies import CurrentUser, check_source_tenant
from app.api.error_docs import error_responses
from app.schemas.common import Envelope, success
from app.schemas.officers import OfficerDirectory

router = APIRouter(prefix='/api/v1/officers', tags=['AI 领航团'], responses=error_responses())



@router.get('', response_model=Envelope[OfficerDirectory])
async def directory(request: Request, user: CurrentUser):
    check_source_tenant(request, user, "officers")
    return success(request, await request.app.state.officers.read())


@router.get('/media/{image_id}', response_class=FileResponse,
            responses={200: {'content': {'image/webp': {'schema': {'type': 'string', 'format': 'binary'}}}}})
async def photo(image_id: str, request: Request, user: CurrentUser):
    check_source_tenant(request, user, "officers")
    path = await request.app.state.officers.image(image_id)
    return FileResponse(path, media_type='image/webp')
