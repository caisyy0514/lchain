"""一键启动：python run.py   （等价于 uvicorn app.main:app --reload）"""
import asyncio
from concurrent.futures import ThreadPoolExecutor

import uvicorn
from dotenv import load_dotenv

load_dotenv()

from app.core.config import get_settings  # noqa: E402
from app.core.logging_setup import setup_logging, uvicorn_log_config  # noqa: E402


def _bump_thread_pool(size: int) -> None:
    """放大事件循环默认线程池。

    `asyncio.to_thread`（提示词拉取、Langfuse flush、文件与图片处理）用的是默认 executor，
    默认只有 min(32, cpu+4) 个线程；高并发时这些阻塞任务会在这里排队，表现为「并发上不去」。
    """
    loop = asyncio.get_event_loop_policy().new_event_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=size, thread_name_prefix="lchain"))
    asyncio.set_event_loop(loop)
    loop.close()
    asyncio.set_event_loop(asyncio.new_event_loop())
    asyncio.get_event_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=size, thread_name_prefix="lchain"))


if __name__ == "__main__":
    s = get_settings()
    _bump_thread_pool(s.app_thread_pool_size)
    logs = setup_logging()
    print(f"\n  对话页:       http://{s.app_host}:{s.app_port}")
    print(f"  提示词调试:   http://{s.app_host}:{s.app_port}/playground")
    lf = s.langfuse_host if s.langfuse_enabled else "未配置（在 .env 中填写 LANGFUSE_PUBLIC_KEY / SECRET_KEY）"
    print(f"  Langfuse:     {lf}")
    print(f"  线程池:       {s.app_thread_pool_size}（to_thread 用）")
    print(f"  日志目录:     {logs}（app.log / error.log / access.log）\n")
    uvicorn.run("app.main:app", host=s.app_host, port=s.app_port, reload=True, reload_dirs=["app"],
                limit_concurrency=s.app_limit_concurrency or None, backlog=s.app_backlog,
                timeout_keep_alive=30, log_config=uvicorn_log_config())
