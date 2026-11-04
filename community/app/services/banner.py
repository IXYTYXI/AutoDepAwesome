"""The newest created record in the configured view is the homepage banner."""
import hashlib
from datetime import UTC, datetime

from app.schemas.banner import Banner, BannerSnapshot
from app.services.cached_snapshot import CachedSnapshotService
from app.services.private_media import PrivateImageCache
from app.services.officers import text_value
from app.services.cases import safe_link

BANNER_FIELDS = ['标题', '描述', '封面', '链接']


class BannerService(CachedSnapshotService, PrivateImageCache):
    snapshot_schema = BannerSnapshot
    image_size = (2400, 1600)

    def __init__(self, store, source, settings):
        self.store, self.source, self.settings = store, source, settings
        identity = '|'.join([settings.feishu_app_id, settings.feishu_bitable_app_token.get_secret_value(),
                             settings.banner_table_id, settings.banner_view_id])
        self.key = 'banner:v2:' + hashlib.sha256(identity.encode()).hexdigest()[:24]
        self.root = settings.media_root / 'banner'
        self.cache_seconds = settings.banner_cache_seconds
        self.stale_seconds = settings.banner_stale_seconds

    def normalize(self, records):
        result = BannerSnapshot(updated_at=datetime.now(UTC))
        media = {}
        # Fetch all pages before selecting; neither view order nor edit time defines newest.
        for record in records:
            timestamp = record.get('created_time')
            if type(timestamp) is not int or timestamp < 0:
                raise ValueError('Missing or invalid Banner creation time')
        for record in sorted(records, key=lambda row: row['created_time'], reverse=True)[:1]:
            fields = record['fields']
            title = text_value(fields.get('标题'))
            image = None
            attachments = fields.get('封面') or []
            if isinstance(attachments, list) and attachments:
                attachment = attachments[0]
                token = attachment.get('file_token') if isinstance(attachment, dict) else None
                if isinstance(token, str) and token:
                    digest = hashlib.sha256((self.key + '|' + token).encode()).hexdigest()
                    media[digest] = {'file_token': token, 'url': attachment.get('url') or ''}
                    image = '/api/v1/banner/media/' + digest
            result.item = Banner(id=record['record_id'], title=title,
                                 summary=text_value(fields.get('描述')), image=image,
                                 url=safe_link(fields.get('链接')))
            break
        return result, media
