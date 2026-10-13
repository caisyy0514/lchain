"""Langfuse 客户端 & LangChain CallbackHandler 封装。

Langfuse Python SDK v3+ 基于 OpenTelemetry：
- `get_client()` 返回单例客户端（读取 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST 环境变量）
- `CallbackHandler` 挂到 LangChain / LangGraph 的 callbacks 上，自动把 chain / llm / tool 调用记录为 trace 明细
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

from langfuse import Langfuse, get_client
from langfuse.langchain import CallbackHandler

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _export_env() -> None:
    """SDK 读取的是环境变量，这里把 .env 中的配置同步过去。"""
    s = get_settings()
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", s.langfuse_public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", s.langfuse_secret_key)
    os.environ.setdefault("LANGFUSE_HOST", s.langfuse_host)
    os.environ.setdefault("LANGFUSE_TRACING_ENVIRONMENT", s.app_env)


@lru_cache
def get_langfuse() -> Langfuse | None:
    s = get_settings()
    if not s.langfuse_enabled:
        logger.warning("Langfuse 未配置（LANGFUSE_PUBLIC_KEY / SECRET_KEY 为空），将不上报 trace")
        return None
    _export_env()
    return get_client()


def new_trace_id(seed: str | None = None) -> str | None:
    if get_langfuse() is None:
        return None
    return Langfuse.create_trace_id(seed=seed)


def make_callback_handler(trace_id: str | None = None) -> CallbackHandler | None:
    """每个请求新建一个 handler；传入 trace_id 以便前端拿到链接直接跳转。"""
    if get_langfuse() is None:
        return None
    ctx = {"trace_id": trace_id} if trace_id else None
    return CallbackHandler(trace_context=ctx)


_project_url: str | None = None


def project_url() -> str | None:
    """当前 API Key 对应的 Langfuse 项目地址，例如 https://cloud.langfuse.com/project/xxx（只缓存成功结果）"""
    global _project_url
    if _project_url:
        return _project_url
    lf = get_langfuse()
    if not lf:
        return None
    try:
        pid = lf._get_project_id()
    except Exception as e:  # noqa: BLE001
        logger.warning("获取 Langfuse 项目 ID 失败: %s", e)
        return None
    if pid:
        _project_url = f"{get_settings().langfuse_host.rstrip('/')}/project/{pid}"
    return _project_url


def trace_url(trace_id: str | None) -> str | None:
    lf = get_langfuse()
    if not lf or not trace_id:
        return None
    base = project_url()
    return f"{base}/traces/{trace_id}" if base else None
