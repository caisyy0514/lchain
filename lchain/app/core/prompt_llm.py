"""工作流节点的通用 LLM 调用：判断类走 JSON、生成类走流式，档位与提示词由调用方注入。

被 `app/consult/llm.py`（城市结构化问诊）与 `app/rural/llm.py`（山区远程问诊）复用：
两个工作流的差别只在**提示词、档位配置项、字数上限**，调用逻辑完全一样，所以抽到这里，
避免两套实现慢慢长歪（改了一处忘了另一处）。

一个工作流实例化一个 `WorkflowLLM`：
- `prompts` / `tiers`：该工作流的默认提示词表与模型档位（fast=判断类，reply=生成类）
- `model_key` / `fast_model_key` / `fast_effort_key`：Settings 里的配置字段名（consult_* / rural_*）

约定（与项目整体一致）：
- 判断类提示词开启 `response_format=json_object`，**提示词里必须出现 “JSON” 字样**，temperature=0
- 生成类的字数上限来自提示词 config 的 `max_chars`，以 `{{max_chars}}` 注入——字数由配置硬限，不靠模型自觉
- 提示词一律走 `resolve_prompt_async`（Langfuse 优先，本地兜底，前端可临时覆盖）；
  Langfuse 的拉取是同步 HTTP，丢线程池，避免卡住事件循环
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langfuse.model import TemplateParser

from app.core.config import get_settings
from app.core.llm import build_llm, clean_reasoning_effort
from app.prompts.manager import ResolvedPrompt, resolve_prompt_async

logger = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 140

# 判断类提示词要求 JSON 输出；这里额外记一份公共变量说明，便于排查（不参与运行逻辑）
_JSON_HINT = "判断类提示词必须包含 “JSON” 字样（response_format=json_object）"


async def get_prompt(name: str, overrides: dict[str, str] | None = None, **variables: Any) -> ResolvedPrompt:
    """overrides：前端/测试临时覆盖某个节点的提示词（不保存到 Langfuse）。"""
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
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(m.group(0) if m else text)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        logger.warning("工作流节点 JSON 解析失败：%s", text[:300])
        return {}


@dataclass
class WorkflowLLM:
    """一个工作流的 LLM 调用器（提示词表 + 档位 + Settings 字段名）。"""

    prompts: dict[str, dict[str, Any]] = field(default_factory=dict)
    tiers: dict[str, str] = field(default_factory=dict)
    model_key: str = "llm_model"
    fast_model_key: str = ""
    fast_effort_key: str = ""
    default_max_chars: int = DEFAULT_MAX_CHARS
    tag: str = "workflow"            # 仅用于日志

    # ---------------------------------------------------------------- 模型选择
    def model_for(self, prompt_name: str, prompt: ResolvedPrompt | None = None) -> tuple[str, dict[str, Any]]:
        """返回 (模型名, 额外参数)。优先级：Langfuse 提示词 config.model > 档位配置 > LLM_MODEL。"""
        s = get_settings()
        tier = (self.tiers or {}).get(prompt_name, "reply")
        extra: dict[str, Any] = {}
        if tier == "fast" and self.fast_model_key:
            model = getattr(s, self.fast_model_key, "") or s.llm_model
            if self.fast_effort_key and (effort := clean_reasoning_effort(getattr(s, self.fast_effort_key, ""))):
                extra["reasoning_effort"] = effort
        else:
            model = getattr(s, self.model_key, "") or s.llm_model
        if prompt and prompt.config.get("model"):
            model = prompt.config["model"]
        if prompt and prompt.config.get("reasoning_effort"):
            if effort := clean_reasoning_effort(prompt.config["reasoning_effort"]):
                extra["reasoning_effort"] = effort
        return model, extra

    # ---------------------------------------------------------------- 生成类
    async def compile_prompt(self, name: str, overrides: dict[str, str] | None = None,
                             **variables: Any) -> ResolvedPrompt:
        """解析提示词并把 max_chars 注入、编译好模板（生成类节点用）。"""
        prompt = await get_prompt(name, overrides, **variables)
        max_chars = (prompt.config.get("max_chars")
                     or (self.prompts.get(name) or {}).get("config", {}).get("max_chars")
                     or self.default_max_chars)
        prompt.variables["max_chars"] = max_chars
        prompt.text = TemplateParser.compile_template(prompt.template, prompt.variables)
        prompt.config.setdefault("max_chars", max_chars)
        return prompt

    async def stream_text(self, prompt_name: str, user_content: str, *, cfg: RunnableConfig | None = None,
                          overrides: dict[str, str] | None = None, **variables: Any) -> AsyncIterator[str]:
        """生成类节点：流式产出文本片段。调用方负责拼接与问题数截断。"""
        prompt = await self.compile_prompt(prompt_name, overrides, **variables)
        model, extra = self.model_for(prompt_name, prompt)
        llm = build_llm(model=model, temperature=prompt.config.get("temperature", 0.3), **extra)
        msgs: list[BaseMessage] = [SystemMessage(prompt.text), HumanMessage(content=user_content)]
        async for chunk in llm.astream(msgs, config=_config(cfg, prompt, prompt_name)):
            text = chunk.text if isinstance(chunk.text, str) else ""
            if text:
                yield text

    # ---------------------------------------------------------------- 判断类
    async def call_json(self, prompt_name: str, user_content: str, *, cfg: RunnableConfig | None = None,
                        overrides: dict[str, str] | None = None,
                        messages: list[BaseMessage] | None = None,
                        **variables: Any) -> tuple[dict[str, Any], ResolvedPrompt]:
        """判断类节点：JSON 模式、temperature=0，返回解析后的字典（解析失败返回空字典）。"""
        prompt = await get_prompt(prompt_name, overrides, **variables)
        model, extra = self.model_for(prompt_name, prompt)
        llm = build_llm(model=model, temperature=prompt.config.get("temperature", 0), **extra).bind(
            response_format={"type": "json_object"})
        msgs: list[BaseMessage] = [SystemMessage(prompt.text), *(messages or []), HumanMessage(content=user_content)]
        for attempt in range(2):
            resp = await llm.ainvoke(msgs, config=_config(cfg, prompt, prompt_name))
            data = _parse_json(resp.text)
            if data and set(data) != {"type"}:   # 排除模型把 response_format 原样吐回来的情况
                return data, prompt
            logger.warning("%s 节点 %s 第 %d 次返回空 JSON，重试：%s",
                           self.tag, prompt_name, attempt + 1, (resp.text or "")[:120])
        return {}, prompt
