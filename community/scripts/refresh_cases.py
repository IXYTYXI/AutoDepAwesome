"""Refresh only the configured case snapshot after editing the Feishu table."""
import asyncio

from app.core.config import Settings
from app.main import create_app


async def refresh():
    settings = Settings(news_sync_enabled=False)
    if settings.store_backend != 'redis':
        raise SystemExit('该命令需要 Redis，独立进程的内存缓存不能更新运行中的服务。')
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        service = app.state.cases
        # Preserve the last successful snapshot and use the normal refresh lease.
        await service.store.delete(service.key + ':fresh')
        await service.store.delete(service.key + ':cooldown')
        result = await service.read()
        if result.stale:
            raise SystemExit('飞书读取失败，已保留上一次成功的案例快照；请检查权限或稍后重试。')
        print(f'案例缓存已刷新：{len(result.items)} 条，其中 {sum(bool(item.image) for item in result.items)} 条有封面。')


if __name__ == '__main__':
    asyncio.run(refresh())
