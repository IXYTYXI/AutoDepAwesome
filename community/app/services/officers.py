"""Shared AI officer directory and private image cache."""
import hashlib
import re
from datetime import UTC, datetime
from app.schemas.officers import Officer, OfficerDirectory
from app.services.cached_snapshot import CachedSnapshotService
from app.services.private_media import PrivateImageCache

def text_value(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return ' '.join(filter(None, (text_value(item) for item in value)))
    if isinstance(value, dict):
        # Formula/lookup values from the OpenAPI may wrap rich text under value.
        for key in ('text', 'name', 'value'):
            if key in value:
                return text_value(value[key])
    return ''


class OfficerService(CachedSnapshotService, PrivateImageCache):
    snapshot_schema = OfficerDirectory
    def __init__(self, store, source, settings):
        self.store, self.source, self.settings = store, source, settings
        source_key = '|'.join([settings.feishu_app_id, settings.feishu_bitable_app_token.get_secret_value(),
                               settings.officers_table_id, settings.officers_view_id])
        self.key = 'officers:v1:' + hashlib.sha256(source_key.encode()).hexdigest()[:24]
        self.root = settings.media_root / 'officers'
        self.cache_seconds = settings.officers_cache_seconds
        self.stale_seconds = settings.officers_stale_seconds

    def normalize(self, records):
        items, media, ids = [], {}, set()
        for record in records:
            record_id = record['record_id']
            if record_id in ids:
                raise ValueError('Duplicate record')
            ids.add(record_id)
            fields = record['fields']
            people = fields.get('人员') or []
            name = text_value(people)
            if not name:
                raise ValueError('Missing person name; check application field permissions')
            # Source requests user_id_type=open_id. Never guess a recipient by name
            # or choose one person from an ambiguous multi-person cell.
            contact = people[0] if isinstance(people, list) and len(people) == 1 and isinstance(people[0], dict) else {}
            open_id = contact.get('id')
            chat_url = None
            if isinstance(open_id, str) and re.fullmatch(r'ou_[A-Za-z0-9_]{1,128}', open_id):
                chat_url = 'https://applink.feishu.cn/client/chat/open?openId=' + open_id
            attachment_list = fields.get('形象') or []
            image = None
            if isinstance(attachment_list, list) and attachment_list:
                attachment = attachment_list[0]
                token = attachment.get('file_token')
                if isinstance(token, str) and token:
                    digest = hashlib.sha256((self.key + '|' + token).encode()).hexdigest()
                    media[digest] = {'file_token': token, 'url': attachment.get('url', '')}
                    image = '/api/v1/officers/media/' + digest
            tags_raw = fields.get('tag')
            # Preserve source phrases (e.g. "AI 产品经理"), never split on spaces.
            if isinstance(tags_raw, dict):
                tags_raw = tags_raw.get('value', tags_raw)
            tags = [text_value(v) for v in tags_raw] if isinstance(tags_raw, list) else [text_value(tags_raw)]
            items.append(Officer(id=record_id, name=name, title=text_value(fields.get('title')) or 'AI 官',
                                 team=text_value(fields.get('团队')), position=text_value(fields.get('定位')), tags=list(dict.fromkeys(filter(None,tags))),
                                 bio=text_value(fields.get('个性签名')), image=image, chat_url=chat_url))
        # Stable partition: honor title, not team or a guessed employee role.
        items.sort(key=lambda person: person.title != '组织效能团队')
        return OfficerDirectory(items=items, updated_at=datetime.now(UTC)), media

