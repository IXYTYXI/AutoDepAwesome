"""Fixed-lifetime, owner-isolated conversations and explicit ticket confirmation."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4
from pydantic import ValidationError
from app.core.error_codes import ErrorCode
from app.core.errors import AppError
from app.schemas.assistant import TicketDraft


class AssistantService:
    def __init__(self, store, source, settings, faq):
        self.store, self.source, self.settings, self.faq = store, source, settings, faq
        self.guide = (Path(__file__).resolve().parents[1] / 'data/assistant-guide.txt').read_text()

    def owner(self, user):
        return hashlib.sha256(f'{user.tenant_id}:{user.user_id}'.encode()).hexdigest()

    def key(self, user):
        return 'assistant:v1:' + self.owner(user)

    @asynccontextmanager
    async def locked(self, user):
        key, token = self.key(user) + ':lock', str(uuid4())
        if not await self.store.acquire(key, token, 30):
            raise AppError(ErrorCode.CONFLICT)
        try:
            async with asyncio.timeout(25):
                yield
        except TimeoutError:
            raise AppError(ErrorCode.UPSTREAM_TIMEOUT) from None
        finally:
            await self.store.release(key, token)

    async def save(self, user, session):
        remaining = int(session['expires_at'] - time.time())
        if remaining <= 0:
            raise AppError(ErrorCode.GONE)
        await self.store.set(self.key(user), json.dumps(session, ensure_ascii=False), ttl=remaining)

    async def load(self, user, expected=None):
        raw = await self.store.get(self.key(user))
        session = json.loads(raw) if raw else None
        if not session or session['expires_at'] <= time.time():
            if expected:
                raise AppError(ErrorCode.GONE)
            session = {'id': str(uuid4()), 'expires_at': time.time() + 3600, 'messages': [], 'remote_id': None, 'pending': None, 'draft': None, 'last_send': 0}
            await self.save(user, session)
        if expected and str(expected) != session['id']:
            raise AppError(ErrorCode.GONE)
        return session

    @staticmethod
    def public(session):
        return {k: session[k] for k in ('id', 'expires_at', 'messages', 'draft')} | {'pending': bool(session['pending'])}

    async def open(self, user):
        async with self.locked(user):
            return self.public(await self.load(user))

    async def knowledge(self):
        # Existing FAQ service already implements Redis cache-aside and refresh locks.
        text = self.guide
        try:
            async with asyncio.timeout(3):
                snapshot = await self.faq.read()
                text += '\n首页 FAQ：\n' + '\n'.join(f'{x.question}\n{x.answer}' for x in snapshot.items)[:2500]
        except (AppError, TimeoutError):
            pass
        return text[:6000]

    async def send(self, user, data):
        async with self.locked(user):
            session = await self.load(user, data.session_id)
            request_id = str(data.request_id)
            if any(m.get('request_id') == request_id for m in session['messages']):
                return self.public(session)
            if session['pending']:
                raise AppError(ErrorCode.CONFLICT)
            if len(session['messages']) >= 80 or time.time() - session['last_send'] < 3:
                raise AppError(ErrorCode.RATE_LIMITED)
            # Distributed limit prevents chat polling from exhausting the shared API pool.
            slot = None
            for i in range(4):
                if await self.store.acquire(f'assistant:slot:{i}', request_id, 180):
                    slot = i
                    break
            if slot is None:
                raise AppError(ErrorCode.RATE_LIMITED)
            try:
                message = json.dumps({'community_request': {'question': data.message, 'page': data.page, 'knowledge': await self.knowledge()}}, ensure_ascii=False)
                result = await self.source.start(message, session['remote_id'])
            except BaseException:
                await self.store.release(f'assistant:slot:{slot}', request_id)
                raise
            session['remote_id'] = result['session_id']
            session['messages'].append({'role': 'user', 'text': data.message, 'request_id': request_id})
            session['pending'] = {'chat_id': result['agent_chat_id'], 'started_at': time.time(), 'page': data.page, 'slot': slot, 'owner': request_id}
            session['last_send'] = time.time()
            session['draft'] = None
            await self.save(user, session)
            return self.public(session)

    async def poll(self, user, session_id):
        async with self.locked(user):
            session = await self.load(user, session_id)
            pending = session['pending']
            if not pending:
                return self.public(session)
            if time.time() - pending.get('polled_at', 0) < 2:
                return self.public(session)
            expired = time.time() - pending['started_at'] > 170
            result = {'status': 'timeout'} if expired else await self.source.result(pending['chat_id'])
            status = result.get('status', '').lower()
            pending['polled_at'] = time.time()
            if status in ('completed', 'failed', 'cancelled', 'timeout'):
                answer = '这次回复未完成，请稍后再试。'
                if status == 'completed':
                    text = '\n'.join(x.get('text', '') for x in result.get('content', []) if x.get('type') == 'text')[:16000]
                    answer, draft = self.parse(text)
                    if draft:
                        session['draft'] = draft | {'id': str(uuid4()), 'page': pending['page'], 'status': 'pending'}
                session['messages'].append({'role': 'assistant', 'text': answer})
                await self.store.release(f"assistant:slot:{pending['slot']}", pending['owner'])
                session['pending'] = None
            await self.save(user, session)
            return self.public(session)

    @staticmethod
    def parse(text):
        try:
            clean = text.strip()
            if clean.startswith('```'):
                clean = clean.split('\n', 1)[1].rsplit('```', 1)[0]
            payload = json.loads(clean)
            answer = payload['answer']
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError()
            draft = TicketDraft.model_validate(payload['ticket']).model_dump() if payload.get('ticket') else None
            return answer[:8000], draft
        except (ValueError, KeyError, TypeError, ValidationError):
            # A malformed model response never becomes an executable ticket draft.
            return text[:8000] or '暂时没有找到相关信息。', None

    async def decide(self, user, session_id, draft_id, confirmed):
        async with self.locked(user):
            session = await self.load(user, session_id)
            draft = session['draft']
            if not draft or draft['id'] != str(draft_id):
                raise AppError(ErrorCode.NOT_FOUND)
            if draft['status'] in ('submitted', 'cancelled'):
                return self.public(session)
            if session['pending']:
                raise AppError(ErrorCode.CONFLICT)
            if not confirmed:
                draft['status'] = 'cancelled'
            else:
                # UUID is stable across retries, including a lost HTTP acknowledgement.
                ticket_no = 'OWS-' + draft['id'][:8].upper()
                fields = {'工单编号': ticket_no, '标题': draft['title'], '类型': draft['category'],
                    '问题描述': draft['description'], '所在页面': draft['page'], '提交人': user.display_name,
                    '提交人标识': user.tenant_id + ':' + user.user_id, '状态': '待处理'}
                record = await self.source.ticket(draft['id'], fields)
                draft.update(status='submitted', ticket_no=ticket_no, record_id=record)
                session['messages'].append({'role': 'assistant', 'text': f'工单 {ticket_no} 已提交。'})
            await self.save(user, session)
            return self.public(session)
