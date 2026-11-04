import hashlib
from datetime import UTC, datetime
from app.schemas.faq import FAQItem, FAQSnapshot
from app.services.cached_snapshot import CachedSnapshotService
from app.services.officers import text_value

FAQ_FIELDS = ['问题', '回复']


class FAQService(CachedSnapshotService):
    snapshot_schema = FAQSnapshot

    def __init__(self, store, source, settings):
        self.store, self.source = store, source
        identity = '|'.join([settings.feishu_app_id, settings.feishu_bitable_app_token.get_secret_value(),
                             settings.faq_table_id, settings.faq_view_id])
        self.key = 'faq:v1:' + hashlib.sha256(identity.encode()).hexdigest()[:24]
        self.cache_seconds, self.stale_seconds = settings.faq_cache_seconds, settings.faq_stale_seconds

    def normalize(self, records):
        items = []
        for row in records:
            question = text_value(row['fields'].get('问题')).strip()
            answer = text_value(row['fields'].get('回复')).strip()
            if question and answer:
                items.append(FAQItem(id=row['record_id'], question=question, answer=answer))
        return FAQSnapshot(items=items, updated_at=datetime.now(UTC)), {}
