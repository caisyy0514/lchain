"""提示词调试台（Playground）：同一输入、多套提示词/模型并行执行，对比效果。

每次运行都会在 Langfuse 生成一条带 `playground` 标签的 trace，并关联到对应提示词版本，
方便在 Langfuse 中横向对比延迟、token、成本，以及人工打分。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.langfuse_client import get_langfuse, make_callback_handler, new_trace_id, trace_url
from app.core.llm import build_llm
from app.prompts.manager import resolve_prompt


async def run_variant(
    *,
    user_input: str,
    prompt_name: str = "agent-system",
    prompt_label: str | None = None,
    prompt_version: int | None = None,
    prompt_override: str | None = None,
    variables: dict[str, Any] | None = None,
    model: str | None = None,
    temperature: float | None = None,
    variant_name: str = "A",
    session_id: str | None = None,
) -> dict[str, Any]:
    prompt = resolve_prompt(
        prompt_name, label=prompt_label, version=prompt_version,
        override_text=prompt_override, variables=variables,
    )
    temp = temperature if temperature is not None else prompt.config.get("temperature")
    llm = build_llm(model=model or prompt.config.get("model"), temperature=temp)
    trace_id = new_trace_id()
    handler = make_callback_handler(trace_id)
    metadata: dict[str, Any] = {
        "langfuse_tags": ["playground", f"variant:{variant_name}", f"prompt:{prompt.source}"],
        "variant": variant_name,
        "prompt_name": prompt.name,
        "prompt_version": prompt.version,
    }
    if session_id:
        metadata["langfuse_session_id"] = session_id
    if prompt.client is not None:
        metadata["langfuse_prompt"] = prompt.client

    t0 = time.perf_counter()
    try:
        resp = await llm.ainvoke(
            [SystemMessage(prompt.text), HumanMessage(user_input)],
            config={"callbacks": [handler] if handler else [], "metadata": metadata,
                    "run_name": f"playground-{variant_name}"},
        )
        output, error = resp.content, None
        usage = resp.usage_metadata
    except Exception as e:  # noqa: BLE001
        output, error, usage = "", f"{type(e).__name__}: {e}", None
    latency = round(time.perf_counter() - t0, 3)

    return {
        "variant": variant_name, "output": output, "error": error, "latency_s": latency,
        "usage": usage, "model": llm.model_name, "temperature": llm.temperature,
        "prompt": prompt.meta(), "system_prompt": prompt.text,
        "trace_id": trace_id, "trace_url": trace_url(trace_id),
    }


async def run_compare(user_input: str, variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    session_id = f"playground-{int(time.time())}"
    tasks = [
        run_variant(user_input=user_input, variant_name=v.pop("variant_name", chr(65 + i)),
                    session_id=session_id, **v)
        for i, v in enumerate(variants)
    ]
    results = await asyncio.gather(*tasks)
    lf = get_langfuse()
    if lf:
        lf.flush()
    return list(results)
