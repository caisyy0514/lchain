"""各节点的 LLM 调用：提示词从 Langfuse 拉取（本地兜底），生成挂到同一条 Langfuse trace 下。"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langfuse.model import TemplateParser

from app.core.config import get_settings
from app.core.llm import build_llm, clean_reasoning_effort
from app.prompts.manager import DEFAULT_PROMPTS, ResolvedPrompt, resolve_prompt_async
from app.wecom.prompts import PROMPT_TIER
from app.wecom.fd.prompts import FD_PROMPT_TIER

logger = logging.getLogger(__name__)

# 提示词的模型档位：fast=判断类（低推理强度），reply=回复生成，doc=文件解读
PROMPT_TIERS: dict[str, str] = {**PROMPT_TIER, **FD_PROMPT_TIER}


def model_for(prompt_name: str, prompt: ResolvedPrompt | None = None) -> tuple[str, dict[str, Any]]:
    """返回 (模型名, 额外参数)。优先级：Langfuse 提示词 config.model > 档位配置 > LLM_MODEL"""
    s = get_settings()
    tier = PROMPT_TIERS.get(prompt_name, "reply")
    extra: dict[str, Any] = {}
    if tier == "fast":
        model = s.wecom_fast_model or s.llm_model
        if effort := clean_reasoning_effort(s.wecom_fast_reasoning_effort):
            extra["reasoning_effort"] = effort
    elif tier == "doc":
        model = s.wecom_doc_model or s.llm_model
    else:
        model = s.wecom_model or s.llm_model
    if prompt and prompt.config.get("model"):
        model = prompt.config["model"]
    if prompt and prompt.config.get("reasoning_effort"):
        if effort := clean_reasoning_effort(prompt.config["reasoning_effort"]):
            extra["reasoning_effort"] = effort
    return model, extra


async def get_prompt(name: str, overrides: dict[str, str] | None = None, **variables: Any) -> ResolvedPrompt:
    """overrides：前端调试时临时覆盖某个节点的提示词（不保存）。

    走 `resolve_prompt_async`：Langfuse 拉取是同步 HTTP，丢线程池，别卡事件循环。
    """
    return await resolve_prompt_async(name, override_text=(overrides or {}).get(name) or None, variables=variables)


def _config(cfg: RunnableConfig | None, prompt: ResolvedPrompt, run_name: str) -> RunnableConfig:
    c: dict[str, Any] = dict(cfg or {})
    md = dict(c.get("metadata") or {})
    md.update({"prompt_name": prompt.name, "prompt_version": prompt.version, "prompt_source": prompt.source})
    if prompt.client is not None:
        md["langfuse_prompt"] = prompt.client   # generation 关联到 Langfuse 提示词版本
    else:
        md.pop("langfuse_prompt", None)
    c["metadata"] = md
    c["run_name"] = run_name
    return c  # type: ignore[return-value]


def _parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0) if m else text)
    except Exception:  # noqa: BLE001
        logger.warning("JSON 解析失败: %s", text[:300])
        return {}


async def call_json(prompt_name: str, user_content: str, *, cfg: RunnableConfig | None = None,
                    overrides: dict[str, str] | None = None, **variables: Any) -> tuple[dict[str, Any], ResolvedPrompt]:
    prompt = await get_prompt(prompt_name, overrides, **variables)
    model, extra = model_for(prompt_name, prompt)
    llm = build_llm(model=model, temperature=prompt.config.get("temperature", 0), **extra).bind(
        response_format={"type": "json_object"})
    resp = await llm.ainvoke([SystemMessage(prompt.text), HumanMessage(user_content)],
                             config=_config(cfg, prompt, prompt_name))
    return _parse_json(resp.text), prompt


async def call_text(prompt_name: str, user_content: str | list[dict[str, Any]], *,
                    cfg: RunnableConfig | None = None, overrides: dict[str, str] | None = None,
                    history: list[BaseMessage] | None = None, **variables: Any) -> tuple[str, ResolvedPrompt]:
    prompt = await get_prompt(prompt_name, overrides, **variables)
    # 回复字数上限：Langfuse 提示词 config.max_chars > 本地默认 config > 120
    max_chars = prompt.config.get("max_chars") or DEFAULT_PROMPTS.get(prompt_name, {}).get("config", {}).get("max_chars", 120)
    prompt.variables["max_chars"] = max_chars
    prompt.text = TemplateParser.compile_template(prompt.template, prompt.variables)
    model, extra = model_for(prompt_name, prompt)
    llm = build_llm(model=model, temperature=prompt.config.get("temperature", 0.5), **extra)
    msgs: list[BaseMessage] = [SystemMessage(prompt.text), *(history or []), HumanMessage(content=user_content)]
    resp = await llm.ainvoke(msgs, config=_config(cfg, prompt, prompt_name))
    return resp.text.strip(), prompt
