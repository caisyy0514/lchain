"""MySQL（SQLAlchemy 2.x async + aiomysql）。"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


engine = create_async_engine(get_settings().mysql_url, pool_pre_ping=True, pool_recycle=1800,
                             pool_size=get_settings().mysql_pool_size,
                             max_overflow=get_settings().mysql_max_overflow)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

_db_ready = False


def db_ready() -> bool:
    return _db_ready


async def init_db() -> bool:
    """建表（不存在时创建）。MySQL 不可用时只记录日志，不影响其他功能启动。"""
    global _db_ready
    from app.wecom import models  # noqa: F401  注册 ORM 模型

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        _db_ready = True
        logger.info("MySQL 已连接，表结构已就绪")
    except Exception as e:  # noqa: BLE001
        _db_ready = False
        logger.error("MySQL 连接失败（企微群回复功能不可用，执行 make mysql-up 启动）: %s", e)
    return _db_ready


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as s:
        try:
            yield s
            await s.commit()
        except BaseException:  # 包括 CancelledError（连续发送打断合并时会取消正在执行的任务）
            await s.rollback()
            raise
