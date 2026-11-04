"""Separate executor/synchronizer process: uv run python -m app.deployment.worker."""

import asyncio
import fcntl
import os
import signal
import sys
import time

import httpx
from awesome_deploy.runtime import DeploymentBackend
from awesome_deploy.source import GitSource
from awesome_deploy.worker import Worker
from redis.asyncio import Redis

from app.core.config import Settings
from app.deployment.service import DeploymentService
from app.deployment.sync import DeploymentSync
from app.repositories.store import RedisStore


async def main():
    settings = Settings()
    if not settings.deploy_enabled:
        return
    service = DeploymentService(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def clear_readiness():
        with service.store._connection(write=True) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS community_runtime(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            db.execute("DELETE FROM community_runtime WHERE key='heartbeat'")

    def request_stop():
        stop.set()

    # One synchronizer per SQLite DB; inherited engine lock still protects Docker.
    with open(service.engine.state_dir / "community-sync.lock", "a") as lock:  # noqa: ASYNC230 — startup-only local flock
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Deployment worker already running")
        clear_readiness()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, request_stop)
        cache = RedisStore(
            Redis.from_url(
                settings.redis_url.get_secret_value(),
                decode_responses=True,
                socket_timeout=3,
                socket_connect_timeout=3,
                max_connections=4,
            ),
            settings.redis_prefix,
        )
        async with httpx.AsyncClient(
            timeout=15, limits=httpx.Limits(max_connections=2)
        ) as client:
            sync = DeploymentSync(service, cache, client)
            tasks = []
            execution_task = None
            if settings.deploy_sync_enabled:
                tasks.append(asyncio.create_task(sync.run()))
            try:
                if settings.deploy_execution_enabled:
                    if sys.platform != "linux" or not settings.deploy_engine_config:
                        raise RuntimeError("真实部署需要 Linux 和独立引擎配置")
                    if not service.engine.feishu_tenant_key:
                        raise RuntimeError("必须配置部署租户")
                    memory_mib = (
                        os.sysconf("SC_PAGE_SIZE")
                        * os.sysconf("SC_PHYS_PAGES")
                        // (1024 * 1024)
                    )
                    if memory_mib < service.engine.max_memory_mib * 2 + 2048:
                        raise RuntimeError(
                            "需为构建器、应用预算和社区至少 2 GiB 保留空间分别分配内存"
                        )
                    backend = DeploymentBackend(service.engine)
                    await asyncio.to_thread(backend.check)
                    worker = Worker(
                        service.engine,
                        service.store,
                        GitSource(service.engine),
                        backend,
                    )
                    worker.access = service.access
                    with service.store._connection() as db:
                        db.execute(
                            "CREATE TABLE IF NOT EXISTS community_runtime(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
                        )

                    async def heartbeat():
                        while True:
                            with service.store._connection(write=True) as db:
                                db.execute(
                                    "INSERT OR REPLACE INTO community_runtime VALUES('heartbeat',?)",
                                    (str(time.time()),),
                                )
                            await asyncio.sleep(5)

                    tasks.append(asyncio.create_task(heartbeat()))

                    async def execute():
                        while not stop.is_set():
                            await asyncio.to_thread(worker.run_once)
                            if not stop.is_set():
                                await asyncio.sleep(2)

                    execution_task = asyncio.create_task(execute())
                    tasks.append(execution_task)
                stopping = asyncio.create_task(stop.wait())
                try:
                    done, _ = await asyncio.wait(
                        [stopping, *tasks], return_when=asyncio.FIRST_COMPLETED
                    )
                    clear_readiness()
                    for task in done:
                        if task is not stopping:
                            task.result()  # Fail visibly; supervisor restarts a broken loop.
                            raise RuntimeError(
                                "Deployment background task stopped unexpectedly"
                            )
                finally:
                    stopping.cancel()
                    await asyncio.gather(stopping, return_exceptions=True)
            finally:
                stop.set()
                clear_readiness()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.remove_signal_handler(sig)
                for task in tasks:
                    if task is not execution_task:
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await cache.close()


if __name__ == "__main__":
    asyncio.run(main())
