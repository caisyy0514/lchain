"""LangGraph checkpoint（MySQL 持久化）：群回复 Agent 的断点续跑。

- 使用官方 `langgraph-checkpoint-mysql` 的 AIOMySQLSaver，表建在业务库里（checkpoints / checkpoint_blobs /
  checkpoint_writes / checkpoint_migrations）
- thread_id = run_id：每次执行一个线程；每个 super-step 完成后同步写入 checkpoint（durability="sync"）
- 中断后（节点异常 / 服务重启 / 进程崩溃）用同一个 thread_id 以 `astream(None, config)` 继续：
  已完成的节点直接跳过，从失败/未完成的节点重新执行
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import aiomysql
from langgraph.checkpoint.mysql.aio import AIOMySQLSaver

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_pool: aiomysql.Pool | None = None
_saver: AIOMySQLSaver | None = None


async def init_checkpointer() -> AIOMySQLSaver | None:
    global _pool, _saver
    if _saver is not None:
        return _saver
    try:
        params = AIOMySQLSaver.parse_conn_string(get_settings().mysql_url)
        _pool = await aiomysql.create_pool(**params, autocommit=True, minsize=1, maxsize=5, charset="utf8mb4")
        saver = AIOMySQLSaver(conn=_pool)
        await saver.setup()  # 建表 / 迁移（幂等）
        await _fix_collation(_pool)
        _saver = saver
        logger.info("LangGraph checkpointer 已就绪（MySQL）")
    except Exception as e:  # noqa: BLE001
        logger.error("LangGraph checkpointer 初始化失败，断点续跑不可用: %s", e)
        _saver = None
    return _saver


CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations")


async def _fix_collation(pool: aiomysql.Pool) -> None:
    """langgraph-checkpoint-mysql 的查询里用 json_table(... CHARACTER SET utf8mb4)，其排序规则是 MySQL 8 默认的
    utf8mb4_0900_ai_ci；而本项目库默认 utf8mb4_unicode_ci，会报 "Illegal mix of collations"。
    这里把 checkpoint 相关表统一转成 utf8mb4_0900_ai_ci（幂等）。"""
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "select table_name, table_collation from information_schema.tables "
            "where table_schema = database() and table_name in %s", (CHECKPOINT_TABLES,))
        for name, coll in await cur.fetchall():
            if coll != "utf8mb4_0900_ai_ci":
                await cur.execute(f"alter table `{name}` convert to character set utf8mb4 collate utf8mb4_0900_ai_ci")
                logger.info("checkpoint 表 %s 排序规则 %s -> utf8mb4_0900_ai_ci", name, coll)


def get_checkpointer() -> AIOMySQLSaver | None:
    return _saver


async def close_checkpointer() -> None:
    global _pool, _saver
    if _pool is not None:
        _pool.close()
        await _pool.wait_closed()
    _pool, _saver = None, None


async def list_checkpoints(run_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """某次执行的 checkpoint 历史（新 -> 旧），用于在页面上查看执行到了哪一步。"""
    if _saver is None:
        return []
    out = []
    async for t in _saver.alist({"configurable": {"thread_id": run_id}}, limit=limit):
        md = t.metadata or {}
        ch = (t.checkpoint or {}).get("channel_values", {})
        out.append({
            "checkpoint_id": t.config["configurable"]["checkpoint_id"],
            "step": md.get("step"), "source": md.get("source"),
            "ts": (t.checkpoint or {}).get("ts"),
            "completed_nodes": [s.get("node") for s in ch.get("steps", []) if isinstance(s, dict)],
        })
    return out


async def delete_thread(run_id: str) -> None:
    if _saver is not None:
        await _saver.adelete_thread(run_id)


async def cleanup(run_ids: list[str]) -> int:
    for rid in run_ids:
        await delete_thread(rid)
    return len(run_ids)


def ttl_deadline() -> datetime:
    return datetime.now() - timedelta(days=get_settings().wecom_checkpoint_ttl_days)
