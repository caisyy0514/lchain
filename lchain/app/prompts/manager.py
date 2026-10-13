"""提示词管理：Langfuse Prompt Management 为主，本地默认值兜底。

- 生产流程：在 Langfuse UI 编辑提示词 -> 发布新版本 -> 打上 `production` label -> 服务自动拉取
- 调试流程：前端可以指定 label / version，或者直接传入临时提示词文本做 A/B 对比
- Langfuse 不可用时回退到 DEFAULT_PROMPTS，保证服务可用

**并发注意**：`lf.get_prompt()` 是同步 HTTP 调用。虽然 `LANGFUSE_PROMPT_CACHE_TTL` 默认 300 秒
（缓存命中时只是本地字典查找），但缓存过期那一次仍然是同步的，会**阻塞整个事件循环**。
所以异步路径请用 `resolve_prompt_async()`（丢到线程池），不要直接调同步版。
"""
from __future__ import annotations

import asyncio
import functools
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from langfuse.model import TextPromptClient, TemplateParser

from app.core.config import get_settings
from app.core.langfuse_client import get_langfuse

logger = logging.getLogger(__name__)

# 默认提示词：Langfuse 模板语法使用 {{variable}}
DEFAULT_PROMPTS: dict[str, dict[str, Any]] = {
    "agent-system": {
        "prompt": (
            "你是 {{assistant_name}}，一个专业、友好的 AI 助手。\n"
            "当前时间：{{current_time}}\n\n"
            "工作原则：\n"
            "1. 需要实时信息或精确计算时，优先调用工具，不要编造结果；\n"
            "2. 回答使用 {{language}}，结构清晰，必要时使用 Markdown；\n"
            "3. 不确定时如实说明。"
        ),
        "config": {"temperature": 0.7},
        "labels": ["production"],
    },
}

# 企微群回复 Agent 的各节点提示词（app/wecom/prompts.py），一并纳入 Langfuse 管理
from app.wecom.prompts import WECOM_PROMPTS  # noqa: E402

DEFAULT_PROMPTS.update(WECOM_PROMPTS)

# 结构化问诊工作流的各节点提示词（app/consult/prompts.py）
from app.consult.prompts import CONSULT_PROMPTS  # noqa: E402

DEFAULT_PROMPTS.update(CONSULT_PROMPTS)

# 山区远程问诊工作流的各节点提示词（app/rural/prompts.py）
from app.rural.prompts import RURAL_PROMPTS  # noqa: E402

DEFAULT_PROMPTS.update(RURAL_PROMPTS)

# 家庭医生 Agent（VBC 分级诊疗）的提示词（app/wecom/fd/prompts.py）
from app.wecom.fd.prompts import FD_PROMPTS  # noqa: E402

DEFAULT_PROMPTS.update(FD_PROMPTS)


@dataclass
class ResolvedPrompt:
    name: str
    text: str                                 # 编译后的最终提示词
    template: str                             # 编译前模板
    variables: dict[str, Any] = field(default_factory=dict)
    version: int | None = None
    source: str = "local"                     # langfuse / local / override
    config: dict[str, Any] = field(default_factory=dict)
    client: TextPromptClient | None = None    # 用于把 generation 关联到提示词版本

    def meta(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "source": self.source}


def default_variables() -> dict[str, Any]:
    return {
        "assistant_name": get_settings().app_name,
        "current_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S %A"),
        "language": "中文",
        "bot_name": get_settings().wecom_bot_name,
    }


def resolve_prompt(
    name: str = "agent-system",
    *,
    label: str | None = None,
    version: int | None = None,
    override_text: str | None = None,
    variables: dict[str, Any] | None = None,
) -> ResolvedPrompt:
    s = get_settings()
    vars_ = {**default_variables(), **(variables or {})}

    # 1) 前端直接传入的临时提示词（调试用，不会写回 Langfuse）
    if override_text:
        return ResolvedPrompt(
            name=name, template=override_text, variables=vars_, source="override",
            text=TemplateParser.compile_template(override_text, vars_),
        )

    # 2) 从 Langfuse 拉取
    lf = get_langfuse()
    fallback = DEFAULT_PROMPTS.get(name, {}).get("prompt")
    if lf is not None:
        try:
            kwargs: dict[str, Any] = {"type": "text", "cache_ttl_seconds": s.langfuse_prompt_cache_ttl}
            if version is not None:
                kwargs["version"] = version
            else:
                kwargs["label"] = label or s.langfuse_prompt_label
            if fallback:
                kwargs["fallback"] = fallback
            p: TextPromptClient = lf.get_prompt(name, **kwargs)  # type: ignore[assignment]
            return ResolvedPrompt(
                name=name, template=p.prompt, text=p.compile(**vars_), variables=vars_,
                version=None if p.is_fallback else p.version,
                source="local" if p.is_fallback else "langfuse",
                config=p.config or {},
                client=None if p.is_fallback else p,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("从 Langfuse 获取提示词 %s 失败，使用本地默认: %s", name, e)

    # 3) 本地兜底
    if not fallback:
        raise ValueError(f"未找到提示词: {name}")
    return ResolvedPrompt(
        name=name, template=fallback, variables=vars_, source="local",
        text=TemplateParser.compile_template(fallback, vars_),
        config=DEFAULT_PROMPTS[name].get("config", {}),
    )


async def resolve_prompt_async(name: str = "agent-system", **kwargs: Any) -> ResolvedPrompt:
    """异步版的 resolve_prompt：把同步的 Langfuse 拉取丢到线程池，不阻塞事件循环。

    缓存命中时开销极小（一次线程切换）；缓存过期时也不会拖慢其他并发请求。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(resolve_prompt, name, **kwargs))
