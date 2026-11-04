"""One shared, source-scoped case snapshot. No per-user copies or client Feishu calls."""
import hashlib
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from app.schemas.cases import Case, CaseContact, CaseDirectory
from app.services.officers import text_value
from app.services.cached_snapshot import CachedSnapshotService
from app.services.private_media import PrivateImageCache

CASE_FIELDS = ['作品名', '一句话描述', '一句话简单描述', '网站案例描述', '团队 AI 官', '所属团队', '第一作者', '作品tag', '产物类型',
               '案例来源', '案例编号', '敏感风控', '文档 / 链接', '封面']


def safe_link(value):
    if isinstance(value, dict):
        value = value.get('link') or value.get('url')
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme in {'http', 'https'} and url.hostname and not url.username and not url.password:
            return value
    except ValueError:
        pass
    return None


def officer_contacts(value):
    # Lookup/person fields return a wrapper; only official open IDs become links.
    if isinstance(value, dict):
        value = value.get('value', [])
    if not isinstance(value, list):
        return []
    contacts, seen = [], set()
    for person in value:
        if not isinstance(person, dict):
            continue
        uid = person.get('id')
        if not isinstance(uid, str) or not re.fullmatch(r'ou_[A-Za-z0-9_]{1,128}', uid) or uid in seen:
            continue
        seen.add(uid)
        contacts.append(CaseContact(name=text_value(person.get('name')) or 'AI 官',
                                    chat_url='https://applink.feishu.cn/client/chat/open?openId=' + uid))
    return contacts


class CaseService(CachedSnapshotService, PrivateImageCache):
    snapshot_schema = CaseDirectory
    image_size = (1600, 1600)

    def __init__(self, store, source, settings):
        self.store, self.source, self.settings = store, source, settings
        identity = '|'.join([settings.feishu_app_id, settings.feishu_bitable_app_token.get_secret_value(),
                             settings.cases_table_id, settings.cases_view_id])
        self.key = 'cases:v3:' + hashlib.sha256(identity.encode()).hexdigest()[:24]

        self.root = settings.media_root / 'cases'
        self.cache_seconds = settings.cases_cache_seconds
        self.stale_seconds = settings.cases_stale_seconds

    def normalize(self, records):
        items, seen, media = [], set(), {}
        for record in records:
            record_id, fields = record['record_id'], record['fields']
            if record_id in seen:
                raise ValueError('Duplicate case record')
            seen.add(record_id)
            if '敏感风控' not in fields:
                raise ValueError('Missing case visibility field; check source permissions')
            access = text_value(fields.get('敏感风控'))
            # The website owner permits every row except explicitly unpublished cases.
            # Target systems continue to enforce access to their original links.
            if access == '暂不公开':
                continue
            title = text_value(fields.get('作品名'))
            if not title:
                raise ValueError('Missing case title')
            authors = fields.get('第一作者') or []
            author = text_value(authors)
            contact_url = None
            if isinstance(authors, list) and len(authors) == 1 and isinstance(authors[0], dict):
                uid = authors[0].get('id')
                if isinstance(uid, str) and re.fullmatch(r'ou_[A-Za-z0-9_]{1,128}', uid):
                    contact_url = 'https://applink.feishu.cn/client/chat/open?openId=' + uid
            raw_tags = fields.get('作品tag') or []
            if isinstance(raw_tags, dict):
                raw_tags = raw_tags.get('value', [])
            tags = [text_value(t) for t in raw_tags] if isinstance(raw_tags, list) else [text_value(raw_tags)]
            image = None
            attachments = fields.get('封面') or []
            if isinstance(attachments, list):
                for attachment in attachments:
                    if not isinstance(attachment, dict):
                        continue
                    token = attachment.get('file_token')
                    mime = attachment.get('type', '')
                    if not isinstance(token, str) or not token or (mime and mime not in {
                        'image/png', 'image/jpeg', 'image/webp', 'image/gif', 'image/mpo'
                    }):
                        continue
                    digest = hashlib.sha256((self.key + '|' + token).encode()).hexdigest()
                    media[digest] = {'file_token': token, 'url': attachment.get('url') or ''}
                    image = '/api/v1/cases/media/' + digest
                    break
            items.append(Case(id=record_id, title=title, summary=text_value(fields.get('一句话描述')) or text_value(fields.get('网站案例描述')) or text_value(fields.get('一句话简单描述')),
                              team=text_value(fields.get('所属团队')), author=author,
                              tags=list(dict.fromkeys(filter(None, tags))), product_type=text_value(fields.get('产物类型')),
                              source=text_value(fields.get('案例来源')), number=text_value(fields.get('案例编号')),
                              access=access, contact_url=contact_url, image=image,
                              ai_officers=officer_contacts(fields.get('团队 AI 官')),
                              url=safe_link(fields.get('文档 / 链接'))))
        # Preserve upstream view order, including across pages. Home uses items[:3].
        return CaseDirectory(items=items, updated_at=datetime.now(UTC)), media

