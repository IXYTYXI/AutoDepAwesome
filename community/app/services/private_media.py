"""Private, versioned image files; attachment credentials never reach the browser."""
import asyncio
import io
import json
import logging
import os
import secrets
import re
import time
from pathlib import Path
import httpx
from PIL import Image, ImageOps
from app.core.errors import AppError
from app.core.error_codes import ErrorCode

logger = logging.getLogger('ows_community')

class PrivateImageCache:
    image_size = (960, 1200)

    async def image(self, image_id):
        if len(image_id) != 64 or any(c not in '0123456789abcdef' for c in image_id):
            raise AppError(ErrorCode.NOT_FOUND)
        snapshot = await self.read()
        if not self.contains_image(snapshot, image_id):
            raise AppError(ErrorCode.NOT_FOUND)
        raw = await self.store.get(self.key + ':media:' + image_id)
        if not raw:
            # Rebuild evicted indexes under the snapshot lease; disk files alone
            # never authorize an attachment removed from the published directory.
            if not await self.can_refresh():
                raise AppError(ErrorCode.UPSTREAM_ERROR)
            await self.store.delete(self.key + ':fresh')
            snapshot = await self.read()
            if not self.contains_image(snapshot, image_id):
                raise AppError(ErrorCode.NOT_FOUND)
            raw = await self.store.get(self.key + ':media:' + image_id)
        if not raw:
            raise AppError(ErrorCode.NOT_FOUND)
        target = self.root / (image_id + '.webp')
        if target.is_file():
            return target
        owner = secrets.token_hex(16)
        lock = self.key + ':image-lock:' + image_id
        if not await self.store.acquire(lock, owner, 120):
            for _ in range(100):
                await asyncio.sleep(.1)
                if target.is_file():
                    return target
            raise AppError(ErrorCode.SERVICE_UNAVAILABLE)
        try:
            if target.is_file():
                return target
            if await self.store.get(lock + ':cooldown'):
                raise AppError(ErrorCode.SERVICE_UNAVAILABLE)
            # Coordinate all workers to stay below Feishu media's 5 requests/sec limit.
            for _ in range(160):
                if await self.store.acquire(self.media_rate_key, owner, 1):
                    break
                await asyncio.sleep(.2)
            else:
                raise AppError(ErrorCode.SERVICE_UNAVAILABLE)
            async with asyncio.timeout(35):
                data = await self.source.download(json.loads(raw))
                await asyncio.to_thread(self.save_image, data, target)
            return target
        except (AppError, httpx.HTTPError, TimeoutError, ValueError, OSError, Image.DecompressionBombError) as exc:
            logger.warning('private_image_failed', extra={'exception_type': type(exc).__name__})
            await self.store.set(lock + ':cooldown', '1', ttl=30)
            if isinstance(exc, AppError):
                raise
            raise AppError(ErrorCode.UPSTREAM_ERROR) from None
        finally:
            await self.store.release(lock, owner)

    @property
    def media_rate_key(self):
        # All tables use the same app credential and share its upstream quota.
        return 'feishu:media-rate:' + self.settings.feishu_app_id

    @staticmethod
    def contains_image(snapshot, image_id):
        items = getattr(snapshot, 'items', None)
        if items is None:
            item = getattr(snapshot, 'item', None)
            items = [item] if item else []
        return any(item.image and item.image.rsplit('/', 1)[-1] == image_id for item in items)

    @classmethod
    def save_image(cls, data: bytes, target: Path):
        # Decode raster images, strip metadata and generate bounded thumbnails.
        with Image.open(io.BytesIO(data)) as original:
            if original.width * original.height > 60_000_000 or original.format not in {'JPEG','MPO','PNG','WEBP','GIF'}:
                raise ValueError('Unsupported image')
            # Camera JPEGs can exceed 40 MP. Ask the decoder for a reduced image
            # before EXIF rotation to avoid allocating the full-resolution raster.
            original.draft('RGB', tuple(size * 2 for size in cls.image_size))
            image = ImageOps.exif_transpose(original)
            image.thumbnail(cls.image_size)
            converted = image.convert('RGB')
            image.close()
            image = converted
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix('.' + secrets.token_hex(8) + '.tmp')
            try:
                image.save(temp, format='WEBP', quality=85)
                os.replace(temp, target)
            finally:
                image.close()
                temp.unlink(missing_ok=True)
            cls.prune_cache(target.parent)

    @staticmethod
    def prune_cache(root):
        """Versioned files can be fetched again; never touch arbitrary user files."""
        now = time.time()
        try:
            for path in root.iterdir():
                if path.is_symlink() or not path.is_file():
                    continue
                if re.fullmatch(r'[a-f0-9]{64}\.webp', path.name):
                    retention = 30 * 86400
                elif re.fullmatch(r'[a-f0-9]{64}\.[a-f0-9]{16}\.tmp', path.name):
                    retention = 86400
                else:
                    continue
                if now - path.stat().st_mtime > retention:
                    path.unlink(missing_ok=True)
        except OSError:
            logger.warning('private_image_cleanup_failed')
