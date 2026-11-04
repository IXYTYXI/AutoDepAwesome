from fastapi import APIRouter, Request
from starlette.responses import FileResponse
from app.api.dependencies import CurrentUser, check_source_tenant
from app.api.error_docs import error_responses
from app.schemas.news import NewsDirectory
from app.schemas.common import Envelope, success

router = APIRouter(prefix='/api/v1/news', tags=['AI 资讯'], responses=error_responses())



@router.get('', response_model=Envelope[NewsDirectory])
async def news(request: Request, user: CurrentUser):
    check_source_tenant(request, user, "news")
    return success(request, await request.app.state.news.read())


@router.get('/media/{image_id}', response_class=FileResponse)
async def media(image_id: str, request: Request, user: CurrentUser):
    check_source_tenant(request, user, "news")
    return FileResponse(await request.app.state.news.image(image_id), media_type='image/webp')
