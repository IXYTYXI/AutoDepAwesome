"""One immediate synchronization; background schedule lives in the API lifespan."""
import asyncio
import json
from app.core.config import Settings
from app.main import create_app

async def main():
    settings = Settings(news_sync_enabled=False)
    if settings.store_backend != 'redis':
        raise SystemExit('资讯同步需要 Redis')
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        print(json.dumps(await app.state.news_sync.sync(force=True),ensure_ascii=False))

if __name__ == '__main__':
    asyncio.run(main())
