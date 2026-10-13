"""LLM 工厂：统一走 OpenAI 兼容协议。

**客户端按 (model, temperature, 额外参数) 缓存复用**：以前每次 LLM 调用都 `ChatOpenAI(...)`，
等于每次新建一个 httpx 客户端（新建连接池 + TLS 握手），一轮问诊 3 次调用就是 3 个，50 并发时
会有 150 个连接池互相抢连接、TIME_WAIT 堆积。复用之后所有请求共享连接池（keep-alive 生效）。

`ChatOpenAI` / `httpx.AsyncClient` 本身是并发安全的，多请求共用一个实例没问题；
需要「每个请求独立配置」时（比如绑 response_format）用 `.bind()`，它只包一层、不新建连接。
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from langchain_openai import ChatOpenAI

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 推理强度是**严格枚举**（deepseek v4 网关会校验，写错一个字整轮执行直接 422）
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "ultra", "max")


def clean_reasoning_effort(value: Any) -> str:
    """清洗 reasoning_effort：去空白 / 手写的引号，不在支持取值里就返回空（调用方当作“用模型默认”）。

    .env 写成 `'low'`（带引号）或在 Langfuse 提示词 config 里写错大小写都很常见，
    这里统一挑掉，避免把非法值发给模型。
    """
    if not isinstance(value, str):
        return ""
    v = value.strip().strip("'\"").strip().lower()
    if v in REASONING_EFFORTS:
        return v
    if v:
        logger.warning("reasoning_effort=%r 不在支持取值 %s 中，已忽略（用模型默认）", value, list(REASONING_EFFORTS))
    return ""


def _cache_key(**kwargs: Any) -> str:
    """缓存键：额外参数序列化成 JSON。

    **不要用 repr 拼字符串再反解**（老实现就是那样）：repr 会给字符串加引号，
    `_build` 里按 `=` / `|` 切回来就得到 "'low'" 这种带引号的脏值，直接发给模型 422。
    """
    return json.dumps(kwargs, sort_keys=True, ensure_ascii=False, default=str) if kwargs else ""


def _build(model: str, temperature: float, kwargs_json: str) -> ChatOpenAI:
    s = get_settings()
    extra: dict[str, Any] = json.loads(kwargs_json) if kwargs_json else {}
    return ChatOpenAI(
        model=model,
        temperature=temperature,
        api_key=s.llm_api_key or None,
        base_url=s.llm_base_url or None,
        streaming=True,
        # 让流式输出也返回 token 用量，Langfuse 才能统计 token / 成本
        stream_usage=True,
        timeout=s.llm_timeout,
        max_retries=s.llm_max_retries,
        **extra,  # type: ignore[arg-type]
    )


@lru_cache(maxsize=32)
def _cached(model: str, temperature: float, kwargs_json: str) -> ChatOpenAI:
    return _build(model, temperature, kwargs_json)


def build_llm(model: str | None = None, temperature: float | None = None, **kwargs: Any) -> ChatOpenAI:
    """返回可复用的 LLM 客户端（同参数共享一个实例与连接池）。"""
    s = get_settings()
    return _cached(model or s.llm_model,
                   s.llm_temperature if temperature is None else float(temperature),
                   _cache_key(**kwargs))


def llm_cache_info() -> dict[str, Any]:
    """给 /api/health 或排查用：当前缓存了几个客户端实例。"""
    info = _cached.cache_info()
    return {"clients": info.currsize, "hits": info.hits, "misses": info.misses}
