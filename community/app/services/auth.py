import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from app.core.errors import AppError
from app.core.error_codes import ErrorCode
from app.repositories.store import Store
from app.schemas.auth import LoginResult, Principal, SessionRecord


class AuthService:
    def __init__(self, store: Store, ttl: int):
        self.store, self.ttl = store, ttl

    @staticmethod
    def key(token: str) -> str:
        return "session:" + hashlib.sha256(token.encode()).hexdigest()

    async def issue(self, principal: Principal) -> LoginResult:
        token = secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + timedelta(seconds=self.ttl)
        record = SessionRecord(principal=principal, expires_at=expires)
        await self.store.set(self.key(token), record.model_dump_json(), ttl=self.ttl)
        return LoginResult(access_token=token, expires_at=expires, user=principal)

    async def authenticate(self, token: str) -> Principal:
        if len(token) > 512:
            raise self.unauthorized()
        raw = await self.store.get(self.key(token))
        if raw is None:
            raise self.unauthorized()
        session = SessionRecord.model_validate_json(raw)
        if session.expires_at <= datetime.now(UTC):
            await self.revoke(token)
            raise self.unauthorized()
        return session.principal

    async def revoke(self, token: str) -> None:
        await self.store.delete(self.key(token))

    @staticmethod
    def unauthorized() -> AppError:
        return AppError(ErrorCode.UNAUTHORIZED)
