"""山区问诊工作流各节点的 LLM 调用（通用实现在 `app/core/prompt_llm.py`）。

本模块只绑定山区问诊自己的提示词表与配置字段（`RURAL_*`），调用逻辑与
`app/consult/llm.py` 完全一致——两个工作流共用一套实现，避免慢慢长歪。

- `call_json`：**判断类**节点（信息提取、本轮动作），temperature=0 + JSON 模式
- `stream_text`：**生成类**节点（提问、追问），流式产出，字数上限来自提示词 config 的 `max_chars`
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig

from app.core.prompt_llm import WorkflowLLM, get_prompt as _get_prompt
from app.rural.prompts import PROMPT_TIER, RURAL_PROMPTS

__all__ = ["model_for", "get_prompt", "compile_prompt", "call_json", "stream_text", "llm"]

llm = WorkflowLLM(prompts=RURAL_PROMPTS, tiers=PROMPT_TIER, model_key="rural_model",
                  fast_model_key="rural_fast_model", fast_effort_key="rural_fast_reasoning_effort",
                  tag="山区问诊")


def model_for(prompt_name: str, prompt: Any = None) -> tuple[str, dict[str, Any]]:
    return llm.model_for(prompt_name, prompt)


async def get_prompt(name: str, overrides: dict[str, str] | None = None, **variables: Any):
    return await _get_prompt(name, overrides, **variables)


async def compile_prompt(name: str, overrides: dict[str, str] | None = None, **variables: Any):
    return await llm.compile_prompt(name, overrides, **variables)


async def call_json(prompt_name: str, user_content: str, *, cfg: RunnableConfig | None = None,
                    overrides: dict[str, str] | None = None,
                    messages: list[BaseMessage] | None = None,
                    **variables: Any) -> tuple[dict[str, Any], Any]:
    return await llm.call_json(prompt_name, user_content, cfg=cfg, overrides=overrides,
                               messages=messages, **variables)


def stream_text(prompt_name: str, user_content: str, *, cfg: RunnableConfig | None = None,
                overrides: dict[str, str] | None = None, **variables: Any) -> AsyncIterator[str]:
    return llm.stream_text(prompt_name, user_content, cfg=cfg, overrides=overrides, **variables)
