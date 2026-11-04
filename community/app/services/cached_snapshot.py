"""Shared cache-aside snapshots with atomic publication, leases and stale fallback."""
import asyncio
import json
import logging
import secrets
import httpx
from app.core.errors import AppError
from app.core.error_codes import ErrorCode

logger = logging.getLogger('ows_community')

class CachedSnapshotService:
    """Subclasses provide schema, source, key, TTLs and normalize(records)."""
    async def can_refresh(self):
        return True

    async def read(self):
        raw = await self.store.get(self.key + ':snapshot')
        if raw and await self.store.get(self.key + ':fresh'):
            return self.snapshot_schema.model_validate_json(raw)
        if await self.store.get(self.key + ':cooldown'):
            return self.stale(raw)
        owner = secrets.token_hex(16)
        if not await self.store.acquire(self.key + ':lock', owner, 60):
            # Wait for the winner; requests never independently fan out to Feishu.
            for _ in range(100):
                await asyncio.sleep(.1)
                latest = await self.store.get(self.key + ':snapshot')
                if latest and await self.store.get(self.key + ':fresh'):
                    return self.snapshot_schema.model_validate_json(latest)
                if await self.store.get(self.key + ':cooldown'):
                    return self.stale(latest or raw)
            return self.stale(raw)
        try:
            # A previous winner may have published between our read and lock acquisition.
            latest = await self.store.get(self.key + ':snapshot')
            if latest and await self.store.get(self.key + ':fresh'):
                return self.snapshot_schema.model_validate_json(latest)
            if not await self.can_refresh():
                return self.stale(latest or raw)
            try:
                async with asyncio.timeout(45):
                    records = await self.source.records()
                    directory, media = self.normalize(records)
                await self.publish(directory, media)
                return directory
            except (AppError, httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError) as exc:
                logger.warning('snapshot_refresh_failed', extra={'exception_type': type(exc).__name__})
                await self.store.set(self.key + ':cooldown', '1', ttl=15)
                if latest or raw:
                    return self.stale(latest or raw)
                if isinstance(exc, AppError):
                    raise
                raise AppError(ErrorCode.UPSTREAM_ERROR) from None
        finally:
            await self.store.release(self.key + ':lock', owner)

    async def publish(self, snapshot, media):
        """Publish data and attachment indexes in one Redis transaction."""
        entries = {self.key + ':snapshot': (snapshot.model_dump_json(), self.stale_seconds),
                   self.key + ':fresh': ('1', self.cache_seconds)}
        entries.update({self.key + ':media:' + key: (json.dumps(value), self.stale_seconds)
                        for key, value in media.items()})
        await self.store.set_many(entries)

    def stale(self, raw):
        if not raw:
            raise AppError(ErrorCode.SNAPSHOT_NOT_READY)
        result = self.snapshot_schema.model_validate_json(raw)
        result.stale = True
        return result

