"""Unified local development entry point: uv run python -m app."""
import argparse
import os
import signal
import shutil
import subprocess
import sys
import threading

from app.core.config import PROJECT_ROOT, Settings


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS may reject a process-group signal after its leader exits.
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        except PermissionError:
            if process.poll() is None:
                process.kill()
        process.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description="同时启动 ows-community 前端和后端")
    parser.add_argument("--backend-only", action="store_true", help="仅启动后端")
    args = parser.parse_args()
    settings = Settings()
    if not args.backend_only:
        if settings.environment == "production":
            parser.error("前端开发服务器仅用于开发；生产请用 Nginx，后端可用 --backend-only")
        if settings.port == settings.frontend_port:
            parser.error("前后端端口不能相同")
        if not (PROJECT_ROOT / "frontend/index.html").is_file():
            parser.error("未找到 frontend/index.html")
    frontend_root = PROJECT_ROOT / "frontend"
    if not args.backend_only:
        if not shutil.which("npm"):
            parser.error("需要 Node.js 22.12+ 和 npm 来构建 Vue 前端")
        if not (frontend_root / "node_modules/vite").is_dir():
            subprocess.run(["npm", "ci"], cwd=frontend_root, check=True)
        subprocess.run(["npm", "run", "build"], cwd=frontend_root, check=True)
    elif not (frontend_root / "dist/index.html").is_file():
        parser.error("缺少 Vue 构建产物；先执行 npm --prefix frontend ci && npm --prefix frontend run build")
    backend = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", settings.host,
               "--port", str(settings.port), "--no-access-log"]
    if settings.environment == "development":
        backend += ["--reload", "--reload-dir", str(PROJECT_ROOT / "app")]
    commands = [("后端", backend)]
    if not args.backend_only:
        commands.append(("前端", [sys.executable, "-m", "scripts.serve_frontend"]))
    if not args.backend_only and settings.environment == "development":
        commands.append(("Vue 构建监听", ["npm", "--prefix", str(frontend_root), "run", "dev"]))
    if settings.deploy_enabled:
        commands.append(("部署 Worker / 同步", [sys.executable, "-m", "app.deployment.worker"]))
    stopping = threading.Event()
    previous_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[sig] = signal.signal(sig, lambda *_: stopping.set())
    processes = []
    try:
        for name, command in commands:
            process = subprocess.Popen(command, cwd=PROJECT_ROOT, start_new_session=os.name == "posix")
            processes.append((name, process))
        print(f"正在启动后端：http://{settings.host}:{settings.port}", flush=True)
        if settings.docs_enabled:
            print(f"接口文档：http://{settings.host}:{settings.port}/docs", flush=True)
        if not args.backend_only:
            print(f"正在启动前端：http://{settings.host}:{settings.frontend_port}/index.html", flush=True)
        print("按 Ctrl+C 同时停止所有服务。", flush=True)
        while not stopping.wait(0.2):
            for name, process in list(processes):
                code = process.poll()
                if code is not None and name == "部署 Worker / 同步":
                    print("部署 Worker 已退出，社区继续运行；检查部署配置后单独重启 Worker。", file=sys.stderr, flush=True)
                    processes.remove((name, process))
                    continue
                if code is not None:
                    print(f"{name}进程已退出（{code}），正在停止其他服务。", file=sys.stderr, flush=True)
                    return code if code > 0 else 1
        return 0
    finally:
        # Signal every sibling before waiting for any slow worker shutdown.
        for _, child in processes:
            try:
                if os.name == "posix":
                    os.killpg(child.pid, signal.SIGTERM)
                elif child.poll() is None:
                    child.terminate()
            except (ProcessLookupError, PermissionError):
                pass
        for _, child in reversed(processes):
            stop_process(child)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
