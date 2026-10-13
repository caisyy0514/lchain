"""多节点协调：节点心跳 + MySQL 命名锁（GET_LOCK）。

部署形态：多个 web 节点（处理群消息）+ 一个定时任务进程（python -m app.scheduler）。

- 节点心跳（wecom_nodes）：每个进程定期更新 last_seen；定时任务据此发现失联的 web 节点并接管其未完成的任务
- 定时任务单例锁：定时任务进程启动时获取 MySQL 命名锁 `lchain:scheduler`，拿不到就退出/等待，
  即使误部署了两个定时任务进程也只有一个在跑（锁绑定在数据库连接上，进程崩溃后 MySQL 自动释放）
- 群锁：同一个群同一时间只允许一个节点处理（`lchain:group:<id>`）。负载均衡把同一个群的消息分到不同 web 节点时，
  后到的节点等前一个节点处理完再处理，保证群内串行（打断合并只在同一节点内生效，建议网关按 group_id 做粘性路由）
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timedelta

import aiomysql
from sqlalchemy import select, update
from sqlalchemy.dialects.mysql import insert

from app.core.config import get_settings
from app.core.db import session_scope
from app.wecom.models import Node

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- MySQL 命名锁
def _lock_name(name: str) -> str:
    full = f"lchain:{name}"
    # GET_LOCK 名称最长 64 字符
    return full if len(full) <= 64 else "lchain:" + hashlib.md5(full.encode()).hexdigest()


class NamedLock:
    """MySQL GET_LOCK 封装。锁跟随一条专用连接：连接断开（进程崩溃）时 MySQL 自动释放。"""

    def __init__(self, name: str) -> None:
        self.name = _lock_name(name)
        self._conn: aiomysql.Connection | None = None

    async def acquire(self, timeout: float = 0) -> bool:
        from langgraph.checkpoint.mysql.aio import AIOMySQLSaver

        if self._conn is None:
            params = AIOMySQLSaver.parse_conn_string(get_settings().mysql_url)
            self._conn = await aiomysql.connect(**params, autocommit=True, charset="utf8mb4")
        async with self._conn.cursor() as cur:
            await cur.execute("SELECT GET_LOCK(%s, %s)", (self.name, timeout))
            (ok,) = await cur.fetchone()
        return ok == 1

    async def alive(self) -> bool:
        """锁仍然持有（连接没断）。"""
        if self._conn is None:
            return False
        try:
            async with self._conn.cursor() as cur:
                await cur.execute("SELECT IS_USED_LOCK(%s) = CONNECTION_ID()", (self.name,))
                (ok,) = await cur.fetchone()
            return ok == 1
        except Exception:  # noqa: BLE001
            return False

    async def release(self) -> None:
        if self._conn is None:
            return
        try:
            async with self._conn.cursor() as cur:
                await cur.execute("SELECT RELEASE_LOCK(%s)", (self.name,))
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._conn.close()
            self._conn = None


# --------------------------------------------------------------------------- 节点心跳
class Heartbeat:
    def __init__(self, role: str) -> None:
        self.role = role
        self.node_id = get_settings().node_name(role)
        self._task: asyncio.Task | None = None

    async def beat(self, status: str = "online") -> None:
        now = datetime.now()
        async with session_scope() as s:
            stmt = insert(Node).values(node_id=self.node_id, role=self.role, pid=os.getpid(), status=status,
                                       started_at=now, last_seen=now)
            upd = {"pid": os.getpid(), "status": status, "last_seen": now, "role": self.role}
            await s.execute(stmt.on_duplicate_key_update(**upd))

    async def start(self) -> None:
        await self.beat()
        async with session_scope() as s:  # 记录本次启动时间
            await s.execute(update(Node).where(Node.node_id == self.node_id).values(started_at=datetime.now()))
        self._task = asyncio.create_task(self._loop())
        logger.info("节点已注册：%s（%s，pid=%s）", self.node_id, self.role, os.getpid())

    async def _loop(self) -> None:
        interval = get_settings().node_heartbeat_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                await self.beat()
            except Exception:  # noqa: BLE001
                logger.exception("节点心跳失败")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        try:
            await self.beat("stopped")
        except Exception:  # noqa: BLE001
            pass


async def dead_web_nodes() -> list[str]:
    """失联的 web 节点：心跳超时的 online 节点（正常停止的 stopped 节点也算，需要接管其遗留任务）。"""
    deadline = datetime.now() - timedelta(seconds=get_settings().node_dead_seconds)
    async with session_scope() as s:
        rows = (await s.execute(select(Node.node_id).where(
            Node.role == "web", Node.last_seen < deadline, Node.status.in_(["online", "stopped"])))).scalars().all()
    return list(rows)


async def is_node_alive(node_id: str | None) -> bool:
    if not node_id:
        return False
    deadline = datetime.now() - timedelta(seconds=get_settings().node_dead_seconds)
    async with session_scope() as s:
        n = await s.get(Node, node_id)
    return bool(n and n.status == "online" and n.last_seen >= deadline)


async def mark_node(node_id: str, status: str) -> None:
    async with session_scope() as s:
        await s.execute(update(Node).where(Node.node_id == node_id).values(status=status))


async def list_nodes() -> list[dict]:
    deadline = datetime.now() - timedelta(seconds=get_settings().node_dead_seconds)
    async with session_scope() as s:
        rows = (await s.execute(select(Node).order_by(Node.role, Node.node_id))).scalars().all()
    return [{"node_id": n.node_id, "role": n.role, "pid": n.pid, "status": n.status,
             "alive": n.status == "online" and n.last_seen >= deadline,
             "started_at": n.started_at.isoformat(timespec="seconds"),
             "last_seen": n.last_seen.isoformat(timespec="seconds")} for n in rows]


_heartbeat: Heartbeat | None = None


def current_node_id() -> str:
    return _heartbeat.node_id if _heartbeat else get_settings().node_name("web")


async def start_heartbeat(role: str) -> Heartbeat:
    global _heartbeat
    _heartbeat = Heartbeat(role)
    await _heartbeat.start()
    return _heartbeat


async def stop_heartbeat() -> None:
    global _heartbeat
    if _heartbeat:
        await _heartbeat.stop()
        _heartbeat = None
