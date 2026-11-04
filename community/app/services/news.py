"""Read the curated Feishu table through one authenticated shared snapshot."""
import asyncio
import hashlib
from datetime import UTC, datetime
from app.services.cached_snapshot import CachedSnapshotService
from app.services.private_media import PrivateImageCache
from app.services.officers import text_value
from app.services.cases import safe_link
from app.schemas.news import NewsItem, NewsDirectory
from app.core.errors import AppError
from app.core.error_codes import ErrorCode

NEWS_FIELDS = ['标题','AIHOT ID','原标题','摘要','原始来源','分类','分类代码','来源署名','来源署名链接',
               '同步指纹','人工标题','人工摘要','原文链接','AIHOT链接','原文发布时间','发现时间','同步时间',
               '来源评分','排序权重','封面','上游精选','隐藏','首页推荐','同步状态']


def date_value(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, UTC)
    return None


class NewsService(CachedSnapshotService, PrivateImageCache):
    snapshot_schema = NewsDirectory
    image_size = (1600, 1600)

    def __init__(self, store, source, settings):
        self.store, self.source, self.settings = store, source, settings
        identity = '|'.join([settings.feishu_app_id, settings.feishu_bitable_app_token.get_secret_value(),
                             settings.news_table_id, settings.news_view_id])
        self.key = 'news:v1:' + hashlib.sha256(identity.encode()).hexdigest()[:24]
        self.root = settings.media_root / 'news'
        self.cache_seconds, self.stale_seconds = settings.news_cache_seconds, settings.news_stale_seconds

    def normalize(self, records):
        items, media = [], {}
        for record in records:
            f = record['fields']
            if not text_value(f.get('AIHOT ID')) or f.get('隐藏') or f.get('上游精选') is not True:
                continue
            if text_value(f.get('同步状态')) != '已同步':
                continue
            title = text_value(f.get('人工标题')) or text_value(f.get('标题'))
            if not title:
                continue
            image = None
            for attachment in f.get('封面') or []:
                if not isinstance(attachment, dict) or not attachment.get('file_token'):
                    continue
                if attachment.get('type') not in {None, '', 'image/png','image/jpeg','image/webp','image/gif','image/mpo'}:
                    continue
                digest = hashlib.sha256((self.key + '|' + attachment['file_token']).encode()).hexdigest()
                media[digest] = {'file_token':attachment['file_token'], 'url':attachment.get('url') or ''}
                image = '/api/v1/news/media/' + digest
                break
            items.append(NewsItem(id=record['record_id'], title=title,
                summary=text_value(f.get('人工摘要')) or text_value(f.get('摘要')),
                source=text_value(f.get('原始来源')), category=text_value(f.get('分类')) or '其他',
                published_at=date_value(f.get('原文发布时间')), discovered_at=date_value(f.get('发现时间')),
                url=safe_link(f.get('原文链接')), aihot_url=safe_link(f.get('AIHOT链接')),
                image=image, featured=f.get('首页推荐') is True, weight=f.get('排序权重') or 0,
                attribution={'name':text_value(f.get('来源署名')), 'url':safe_link(text_value(f.get('来源署名链接')))}))
        items.sort(key=lambda x:(x.weight, (x.published_at or x.discovered_at or datetime(1970, 1, 1, tzinfo=UTC)).timestamp(), x.id), reverse=True)
        return NewsDirectory(items=items, updated_at=datetime.now(UTC)), media

    async def can_refresh(self):
        # Recheck under the snapshot lease to close the read/write race.
        return not (await self.store.get(self.key + ':sync:lock') or
                    await self.store.get(self.key + ':sync:pending'))

    async def read(self):
        if (await self.store.get(self.key + ':sync:lock') or
                await self.store.get(self.key + ':sync:pending')):
            return self.stale(await self.store.get(self.key + ':snapshot'))
        return await super().read()

    async def refresh(self, *, from_sync=False):
        if from_sync:
            # The writer holds both leases: publish a complete replacement, never an empty cache.
            async with asyncio.timeout(90):
                directory, media = self.normalize(await self.source.records())
            await self.publish(directory, media)
            await self.store.delete(self.key + ':cooldown')
            return directory
        await self.store.delete(self.key + ':fresh')
        await self.store.delete(self.key + ':cooldown')
        result = await super().read()
        if result.stale:
            raise AppError(ErrorCode.UPSTREAM_ERROR)
        return result

