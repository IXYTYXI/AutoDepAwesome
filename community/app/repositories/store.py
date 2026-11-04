import asyncio
import time
from typing import Protocol

from redis.asyncio import Redis


class Store(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl: int | None = None) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def take(self, key: str) -> str | None: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...
    async def acquire(self, key: str, owner: str, ttl: int) -> bool: ...
    async def release(self, key: str, owner: str) -> None: ...
    async def set_many(self, entries: dict[str, tuple[str, int | None]]) -> None: ...


class MemoryStore:
    """Single-process development adapter. Never selected as a Redis fallback."""

    def __init__(self):
        self._values: dict[str, tuple[str, float | None]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> str | None:
        async with self._lock:
            value = self._values.get(key)
            if value is None:
                return None
            text, expiry = value
            if expiry is not None and expiry <= time.monotonic():
                del self._values[key]
                return None
            return text

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        async with self._lock:
            now = time.monotonic()
            self._values = {k: v for k, v in self._values.items() if v[1] is None or v[1] > now}
            self._values[key] = (value, now + ttl if ttl is not None else None)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._values.pop(key, None)

    async def take(self, key: str) -> str | None:
        async with self._lock:
            value = self._values.pop(key, None)
            if value is None or (value[1] is not None and value[1] <= time.monotonic()):
                return None
            return value[0]

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        self._values.clear()

    async def acquire(self, key: str, owner: str, ttl: int) -> bool:
        async with self._lock:
            saved = self._values.get(key)
            if saved and (saved[1] is None or saved[1] > time.monotonic()):
                return False
            self._values[key] = (owner, time.monotonic() + ttl)
            return True

    async def release(self, key: str, owner: str) -> None:
        async with self._lock:
            if self._values.get(key, (None, None))[0] == owner:
                self._values.pop(key, None)

    async def set_many(self, entries: dict[str, tuple[str, int | None]]) -> None:
        async with self._lock:
            now = time.monotonic()
            self._values.update({key: (value, now + ttl if ttl is not None else None)
                                 for key, (value, ttl) in entries.items()})


class RedisStore:
    def __init__(self, client: Redis, prefix: str):
        self.client, self.prefix = client, prefix

    def _key(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    async def get(self, key: str) -> str | None:
        return await self.client.get(self._key(key))

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        await self.client.set(self._key(key), value, ex=ttl)

    async def delete(self, key: str) -> None:
        await self.client.delete(self._key(key))

    async def take(self, key: str) -> str | None:
        return await self.client.getdel(self._key(key))

    async def ping(self) -> bool:
        return bool(await self.client.ping())

    async def close(self) -> None:
        await self.client.aclose()

    async def acquire(self, key: str, owner: str, ttl: int) -> bool:
        return bool(await self.client.set(self._key(key), owner, nx=True, ex=ttl))

    async def release(self, key: str, owner: str) -> None:
        from redis.exceptions import WatchError
        key = self._key(key)
        async with self.client.pipeline() as pipe:
            try:
                await pipe.watch(key)
                if await pipe.get(key) != owner:
                    return
                pipe.multi()
                pipe.delete(key)
                await pipe.execute()
            except WatchError:
                pass  # A new owner acquired the expired lease; never delete its lock.

    async def set_many(self, entries: dict[str, tuple[str, int | None]]) -> None:
        async with self.client.pipeline(transaction=True) as pipe:
            for key, (value, ttl) in entries.items():
                pipe.set(self._key(key), value, ex=ttl)
            await pipe.execute()
