"""AIHOT selected snapshot + change ledger -> Feishu -> Redis, every ten minutes."""
import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from datetime import UTC, datetime
import httpx

from app.services.officers import text_value

log = logging.getLogger('ows_community')
CATEGORIES = {'ai-models':'AI 模型','ai-products':'AI 产品','industry':'行业动态','paper':'论文','tip':'教程','opinion':'观点'}


class SnapshotRequired(Exception):
    pass


class UpstreamWait(Exception):
    def __init__(self, seconds=600):
        self.seconds = max(60, seconds)


def timestamp(value):
    return int(datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()*1000) if value else None


def source_fields(item):
    if not isinstance(item.get('id'), str) or not item['id'] or not isinstance(item.get('title'), str):
        raise ValueError('Malformed AIHOT item')
    fields = {'AIHOT ID':item['id'], '标题':item['title'], '原标题':item.get('originalTitle') or '',
        '摘要':item.get('summary') or '', '原始来源':item['source']['name'],
        '分类':CATEGORIES.get(item.get('category'), '其他'), '分类代码':item.get('category') or '',
        '原文链接':{'link':item['links']['original'],'text':'阅读原文'},
        'AIHOT链接':{'link':item['links']['aihot'],'text':'AIHOT'},
        '原文发布时间':timestamp(item.get('publishedAt')), '发现时间':timestamp(item['discoveredAt']),
        '来源评分':item.get('score'), '上游精选':bool(item['selected']),
        '同步状态':'已同步' if item['selected'] else '已撤选',
        '来源署名':item.get('attribution',{}).get('name','AIHOT'),
        '来源署名链接':item.get('attribution',{}).get('url',item['links']['aihot'])}
    fields['同步指纹'] = hashlib.sha256(json.dumps(fields,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return fields


class NewsSync:
    def __init__(self, client, table, news, settings):
        self.client, self.table, self.news, self.settings = client, table, news, settings
        self.store = news.store
        self.key = news.key + ':sync'
        self.path = settings.news_sync_state_root / (hashlib.sha256(self.key.encode()).hexdigest()[:24]+'.json')
        self.stats = {}

    def load_state(self):
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text())
        if not isinstance(data, dict):
            raise ValueError('Invalid synchronization checkpoint')
        return data

    def save_state(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        with tmp.open('w') as stream:
            json.dump(state,stream,ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp,self.path)

    async def fetch(self, endpoint, params, etag=None):
        headers = {'User-Agent':'ows-community/1.0', 'Accept-Encoding':'gzip'}
        if etag:
            headers['If-None-Match'] = etag
        for attempt in range(3):
            try:
                response = await self.client.get('https://aihot.news/api/v1/selected/'+endpoint,
                                                 params=params, headers=headers, timeout=60)
                break
            except httpx.TransportError:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** (attempt + 1))
        if response.status_code == 409:
            raise SnapshotRequired()
        if response.status_code in {429,503,566,567}:
            retry = response.headers.get('Retry-After','600')
            raise UpstreamWait(int(retry) if retry.isdigit() else 600)
        if response.status_code == 304:
            return None, etag
        response.raise_for_status()
        data = response.json()
        if data.get('schemaVersion') != 1 or data.get('fields') != 'default':
            raise ValueError('Unsupported AIHOT schema')
        return data, response.headers.get('ETag')

    async def apply(self, items, removed, index):
        # Coalesce repeated IDs in one ledger page to the final operation.
        creates, updates = [], []
        now = int(time.time()*1000)
        for ident, item in items.items():
            fields = source_fields(item)
            current = index.get(ident)
            if current and text_value(current['fields'].get('同步指纹')) == fields['同步指纹'] and current['fields'].get('上游精选') == item['selected']:
                continue
            fields['同步时间'] = now
            if current:
                updates.append({'record_id':current['record_id'],'fields':fields})
            else:
                creates.append({'fields':fields})
        for ident in removed:
            current = index.get(ident)
            if current and (current['fields'].get('上游精选') or text_value(current['fields'].get('同步状态')) != '已撤选'):
                updates.append({'record_id':current['record_id'],'fields':{'上游精选':False,'同步状态':'已撤选','同步指纹':'','同步时间':now}})
        if creates or updates:
            await self.store.set(self.key + ':pending', '1')
        for update, batch in [(False,creates),(True,updates)]:
            for start in range(0,len(batch),100):
                chunk = batch[start:start+100]
                result = await self.table.write(chunk,update=update)
                # Unknown/partial write outcomes abort the run. Next run re-reads the table before retrying.
                if not update:
                    for row in result:
                        ident = text_value(row['fields'].get('AIHOT ID'))
                        if not ident:
                            raise ValueError('Missing identity in create acknowledgement')
                        index[ident] = row
                else:
                    by_record = {row['record_id']:row for row in index.values()}
                    for row in chunk:
                        by_record[row['record_id']]['fields'].update(row['fields'])
                self.stats['updated' if update else 'created'] += len(chunk)

    async def bootstrap(self,index):
        items, page, cursor, seen = {}, None, None, set()
        for _ in range(200):
            params = {'limit':100,'fields':'default'} if not page else {'page':page}
            data,_ = await self.fetch('snapshot',params)
            cursor = cursor or data['cursor']
            if data['cursor'] != cursor:
                raise ValueError('Snapshot watermark changed')
            for item in data['items']:
                items[item['id']] = item
            if not data['hasMore']:
                break
            page = data.get('nextPage')
            if not page or page in seen:
                raise ValueError('Invalid snapshot pagination')
            seen.add(page)
            await asyncio.sleep(1.1)
        else:
            raise ValueError('Snapshot pagination limit exceeded')
        await self.apply(items,set(index)-set(items),index)
        state = {'cursor':cursor}
        self.save_state(state)
        return state

    async def read_index(self):
        """Only read the writer's full table when upstream changes need applying."""
        index = {}
        for record in await self.table.records():
            ident = text_value(record['fields'].get('AIHOT ID'))
            if ident:
                if ident in index:
                    raise ValueError('Duplicate AIHOT ID in Feishu')
                index[ident] = record
        return index

    async def sync(self, *, force=False):
        owner = secrets.token_hex(16)
        if not await self.store.acquire(self.key+':lock',owner,900):
            return {'status':'busy'}
        cache_locked = False
        try:
            due = float(await self.store.get(self.key+':next') or 0)
            if not force and due > time.time():
                return {'status':'not_due'}
            cache_locked = await self.store.acquire(self.news.key+':lock', owner, 900)
            if not cache_locked:
                return {'status':'busy'}
            self.stats = {'created':0,'updated':0}
            async with asyncio.timeout(840):
                index = None
                state = self.load_state()
                if not state.get('cursor'):
                    index = await self.read_index()
                    state = await self.bootstrap(index)
                # Pull changes from the initial snapshot watermark, not the final page's time.
                for _ in range(200):
                    cursor = state['cursor']
                    try:
                        data, etag = await self.fetch('changes',{'cursor':cursor,'limit':100},
                            state.get('etag') if state.get('etag_cursor') == cursor else None)
                    except SnapshotRequired:
                        if index is None:
                            index = await self.read_index()
                        state = await self.bootstrap(index)
                        continue
                    if data is None:
                        break
                    items, removed = {}, set()
                    for change in data['changes']:
                        if change['op'] == 'upsert':
                            item = change['item']; items[item['id']] = item; removed.discard(item['id'])
                        elif change['op'] == 'remove':
                            ident = change['id']; items.pop(ident,None); removed.add(ident)
                        else:
                            raise ValueError('Unknown AIHOT operation')
                    if items or removed:
                        if index is None:
                            index = await self.read_index()
                        await self.apply(items,removed,index)
                    state = {'cursor':data['cursor'],'etag_cursor':cursor,'etag':etag}
                    self.save_state(state)  # Writes must finish before the checkpoint advances.
                    if not data['hasMore']:
                        break
                    if data['cursor'] == cursor:
                        raise ValueError('Incremental cursor did not advance')
                    await asyncio.sleep(1.1)
                else:
                    raise ValueError('Incremental pagination limit exceeded')
                # Read Feishu again, so human edits and the acknowledged writes become the cache truth.
                directory = await self.news.refresh(from_sync=True)
                await self.store.delete(self.key + ':pending')
                result = {'status':'ok', **self.stats, 'visible':len(directory.items),
                          'completed_at':datetime.now(UTC).isoformat()}
                await self.store.set(self.key+':status',json.dumps(result))
                await self.store.set(self.key+':next',str(time.time()+self.settings.news_sync_interval_seconds))
                return result
        except Exception as exc:
            delay = exc.seconds if isinstance(exc,UpstreamWait) else self.settings.news_sync_interval_seconds
            await self.store.set(self.key+':next',str(time.time()+delay))
            await self.store.set(self.key+':status',json.dumps({'status':'failed','error_type':type(exc).__name__,
                'attempted_at':datetime.now(UTC).isoformat()}))
            raise
        finally:
            if cache_locked:
                await self.store.release(self.news.key+':lock',owner)
            await self.store.release(self.key+':lock',owner)

    async def run(self):
        """One cancellable coroutine, no scheduler dependency or frequent polling."""
        while True:
            delay = self.settings.news_sync_interval_seconds
            try:
                result = await self.sync()
                if result['status'] == 'busy':
                    # Another worker or cache fill owns the lease. Retry without spinning.
                    delay = 30
                else:
                    due = float(await self.store.get(self.key + ':next') or 0)
                    delay = max(1, due - time.time()) if due else delay
            except Exception as exc:
                log.warning('news_sync_failed', extra={'exception_type': type(exc).__name__})
                if isinstance(exc, UpstreamWait):
                    delay = exc.seconds
            await asyncio.sleep(delay)
