from app.core.errors import AppError
from app.core.error_codes import ErrorCode
from app.repositories.store import Store
from app.schemas.content import ContentSnapshot


class SnapshotRepository:
    """Whole snapshot, one atomic SET, no TTL and no request-time Feishu fallback.

    First-version implementation for modest datasets. A dedicated future worker
    prepares all data/images before calling publish(). Only one writer is allowed.
    """
    key = "content:current"

    def __init__(self, store: Store):
        self.store = store

    async def read(self) -> ContentSnapshot:
        raw = await self.store.get(self.key)
        if raw is None:
            raise AppError(ErrorCode.SNAPSHOT_NOT_READY)
        return ContentSnapshot.model_validate_json(raw)

    async def publish(self, snapshot: ContentSnapshot) -> None:
        # Validate before replacing the current value. Redis SET is atomic.
        validated = ContentSnapshot.model_validate(snapshot.model_dump())
        await self.store.set(self.key, validated.model_dump_json())
