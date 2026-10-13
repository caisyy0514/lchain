"""聊天记录压缩：使用 LangChain `SummarizationMiddleware`（langchain.agents.middleware）。

策略：
- 上下文 = 群基础信息 + 压缩摘要 + 摘要之后的原始消息（最多 recent + trigger 条）
- 摘要之后的原始消息数 ≥ recent + trigger 时触发压缩：旧摘要 + 超出最近 recent 条的部分 -> 新摘要
  （即 middleware 的 trigger=("messages", recent+trigger+1)、keep=("messages", recent)，+1 是旧摘要那条）
- 压缩在回复完成后后台执行，不增加回复延迟；也可以在页面上手动触发
- 多节点部署：用 MySQL 命名锁 `lchain:compress:<group_id>` 保证同一个群同一时间只有一个节点在压缩，
  拿不到锁直接跳过（另一个节点正在压缩），避免两次压缩互相覆盖摘要
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage

from app.core.config import get_settings
from app.core.langfuse_client import make_callback_handler
from app.core.llm import build_llm
from app.wecom import store
from app.wecom.llm import get_prompt, model_for

logger = logging.getLogger(__name__)
_locks: dict[str, asyncio.Lock] = {}


def to_lc_messages(msgs: list[Any], docs: dict[int, list[Any]]) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for m in msgs:
        text = store.format_message(m, docs.get(m.id), doc_chars=800)
        cls = AIMessage if m.sender_role == "assistant" else HumanMessage
        out.append(cls(content=text, id=f"wecom-{m.id}"))
    return out


async def maybe_compress(group_id: str, *, force: bool = False, overrides: dict[str, str] | None = None) -> dict[str, Any]:
    from app.core.cluster import NamedLock

    s = get_settings()
    lock = _locks.setdefault(group_id, asyncio.Lock())  # 进程内
    async with lock:
        dlock = NamedLock(f"compress:{group_id}")        # 跨节点
        if not await dlock.acquire(timeout=0):
            return {"compressed": False, "reason": "其他节点正在压缩该群"}
        try:
            return await _compress(group_id, s, force, overrides)
        finally:
            await dlock.release()


async def _compress(group_id: str, s: Any, force: bool, overrides: dict[str, str] | None) -> dict[str, Any]:
    g = await store.get_group(group_id)
    if g is None:
        return {"compressed": False, "reason": "群不存在"}
    keep = s.wecom_recent_messages
    msgs = await store.list_messages(group_id, after_id=g.summary_until_id, limit=2000)
    threshold = keep + s.wecom_summary_trigger
    if force:  # 手动压缩：只保留最近 10 条原文（便于测试压缩效果）
        keep = min(keep, 10)
    if len(msgs) <= keep or (not force and len(msgs) < threshold):
        return {"compressed": False, "reason": f"未压缩消息 {len(msgs)} 条，未达到阈值 {threshold}"}

    docs = await store.documents_for_messages([m.id for m in msgs])
    lc = to_lc_messages(msgs, docs)
    prev = HumanMessage(content=f"【之前的聊天摘要】\n{g.summary or '（无）'}", id="wecom-summary")

    prompt = get_prompt("wecom-summary", overrides)
    model, extra = model_for("wecom-summary", prompt)
    mw = SummarizationMiddleware(
        model=build_llm(model=model, temperature=prompt.config.get("temperature", 0.2), **extra),
        trigger=("messages", 1), keep=("messages", keep),
        summary_prompt=prompt.text, trim_tokens_to_summarize=30000,
    )
    handler = make_callback_handler()
    cfg = {"callbacks": [handler] if handler else [], "run_name": "wecom-compress",
           "metadata": {"langfuse_session_id": group_id, "langfuse_tags": ["wecom", "compress"]}}
    # 让 middleware 内部的模型调用也挂到 Langfuse：middleware 使用 invoke 时不带 config，这里通过 with_config 注入
    mw._summary_model = mw._summary_model.with_config(cfg)  # noqa: SLF001
    result = await mw.abefore_model({"messages": [prev, *lc]}, None)  # type: ignore[arg-type]
    if not result:
        return {"compressed": False, "reason": "middleware 未触发"}
    new_msgs = [m for m in result["messages"] if not isinstance(m, RemoveMessage)]
    summary_msg = new_msgs[0]
    summary = str(summary_msg.content).split("\n\n", 1)[-1].strip()  # 去掉 "Here is a summary..." 前缀
    preserved = len(new_msgs) - 1
    summarized = msgs[: len(msgs) - preserved]
    until = summarized[-1].id if summarized else g.summary_until_id
    await store.update_group(group_id, summary=summary, summary_until_id=until, summary_updated_at=datetime.now())
    logger.info("群 %s 聊天记录已压缩：%d 条 -> 摘要（保留最近 %d 条）", group_id, len(summarized), preserved)
    return {"compressed": True, "summarized_messages": len(summarized), "kept": preserved,
            "summary_until_id": until, "summary": summary}


def compress_in_background(group_id: str) -> None:
    async def _run():
        try:
            await maybe_compress(group_id)
        except Exception:  # noqa: BLE001
            logger.exception("聊天记录压缩失败")
    asyncio.create_task(_run())
