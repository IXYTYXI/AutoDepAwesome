"""Refresh only the configured Banner snapshot after editing the Feishu table."""
import asyncio

from app.core.config import Settings
from app.main import create_app


async def refresh():
    settings = Settings(news_sync_enabled=False)
    if settings.store_backend != 'redis':
        raise SystemExit('该命令需要 Redis，独立进程的内存缓存不能更新运行中的服务。')
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        service = app.state.banner
        # Preserve the last successful snapshot and use the normal refresh lease.
        await service.store.delete(service.key + ':fresh')
        await service.store.delete(service.key + ':cooldown')
        result = await service.read()
        if result.stale:
            raise SystemExit('飞书读取失败，已保留上一次成功的 Banner；请检查权限或稍后重试。')
        print('Banner 已刷新：' + ((result.item.title or '无标题（' + result.item.id + '）') if result.item else '表中没有记录，使用默认品牌 Banner'))


if __name__ == '__main__':
    asyncio.run(refresh())
