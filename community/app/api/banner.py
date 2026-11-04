from fastapi import APIRouter, Request
from starlette.responses import FileResponse

from app.api.dependencies import CurrentUser, check_source_tenant
from app.api.error_docs import error_responses
from app.schemas.banner import BannerSnapshot
from app.schemas.common import Envelope, success

router = APIRouter(prefix='/api/v1/banner', tags=['首页 Banner'], responses=error_responses())



@router.get('', response_model=Envelope[BannerSnapshot])
async def current(request: Request, user: CurrentUser):
    check_source_tenant(request, user, "banner")
    return success(request, await request.app.state.banner.read())


@router.get('/media/{image_id}', response_class=FileResponse,
            responses={200: {'content': {'image/webp': {'schema': {'type': 'string', 'format': 'binary'}}}}})
async def cover(image_id: str, request: Request, user: CurrentUser):
    check_source_tenant(request, user, "banner")
    return FileResponse(await request.app.state.banner.image(image_id), media_type='image/webp')
