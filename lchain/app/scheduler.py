"""独立的定时任务进程：python -m app.scheduler（或 make scheduler）

部署：web 节点可以部署多个（make dev / uvicorn），定时任务进程**只部署一个**。
- 单例保证：启动时获取 MySQL 命名锁 `lchain:scheduler`。已有实例在运行时，新实例进入等待（standby），
  直到前一个实例退出 / 崩溃（锁随数据库连接断开自动释放）才接替运行，所以误部署两个也不会重复执行任务
- 运行期间每轮检查锁是否仍然持有（数据库连接断开会丢锁），丢锁后停止执行任务、重新抢锁
- 自身也注册心跳（wecom_nodes，role=scheduler），可在 /api/wecom/cluster 查看

任务列表见 app/wecom/jobs.py。
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time

from dotenv import load_dotenv

load_dotenv()

from app.core.logging_setup import setup_logging  # noqa: E402

setup_logging("scheduler")

from app.core.cluster import NamedLock, start_heartbeat, stop_heartbeat  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.db import engine, init_db  # noqa: E402
from app.core.langfuse_client import get_langfuse  # noqa: E402

logger = logging.getLogger("app.scheduler")

JOBS = [
    # (名称, 间隔秒数 None=WECOM_JOB_INTERVAL_SECONDS, 协程函数路径)
    ("check_overdue", None, "check_overdue"),
    ("redeliver", None, "redeliver"),
    ("takeover_dead_nodes", None, "takeover_dead_nodes"),
    ("cleanup_checkpoints", 3600, "cleanup_checkpoints"),
    # 家庭医生·居家观察闭环（第 24 节）
    ("fd_followup_review", None, "fd_followup_review"),
    ("fd_contact_overdue", None, "fd_contact_overdue"),
    ("fd_expired_observations", None, "fd_expired_observations"),
]


async def _run_jobs(stop: asyncio.Event, lock: NamedLock) -> None:
    from app.wecom import jobs

    s = get_settings()
    base = max(1, s.wecom_job_interval_seconds)
    last: dict[str, float] = {}
    while not stop.is_set():
        if not await lock.alive():
            logger.error("定时任务单例锁已丢失（数据库连接断开？），停止执行并重新抢锁")
            return
        now = time.monotonic()
        for name, interval, fn in JOBS:
            if now - last.get(name, 0) < (interval or base):
                continue
            last[name] = now
            t0 = time.perf_counter()
            try:
                r = await getattr(jobs, fn)()
                n = len(r) if isinstance(r, list) else r
                if n:
                    logger.info("任务 %s 完成：%s（%dms）", name, n, (time.perf_counter() - t0) * 1000)
            except Exception:  # noqa: BLE001
                logger.exception("任务 %s 执行失败", name)
        try:
            await asyncio.wait_for(stop.wait(), timeout=base)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    s = get_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    while not await init_db():
        logger.error("MySQL 不可用，10 秒后重试")
        try:
            await asyncio.wait_for(stop.wait(), timeout=10)
            return
        except asyncio.TimeoutError:
            pass

    from app.wecom.checkpoint import close_checkpointer, init_checkpointer

    await init_checkpointer()  # 接管失联节点的任务时需要续跑 checkpoint
    await start_heartbeat("scheduler")
    lock = NamedLock("scheduler")
    try:
        while not stop.is_set():
            if await lock.acquire(timeout=0):
                logger.info("已获得定时任务单例锁，开始执行任务：%s", [j[0] for j in JOBS])
                await _run_jobs(stop, lock)
                await lock.release()
            else:
                logger.warning("已有定时任务进程在运行，本进程进入等待（standby），前一个退出后自动接替")
                # 阻塞等待锁（每 5 秒醒来检查一次是否要退出）
                while not stop.is_set():
                    if await lock.acquire(timeout=5):
                        logger.info("前一个定时任务进程已退出，本进程接替运行")
                        await _run_jobs(stop, lock)
                        await lock.release()
                        break
    finally:
        await lock.release()
        await stop_heartbeat()
        await close_checkpointer()
        await engine.dispose()
        lf = get_langfuse()
        if lf:
            lf.shutdown()
        logger.info("定时任务进程已退出")


if __name__ == "__main__":
    asyncio.run(main())
