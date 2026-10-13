"""通用对话的会话记录（落盘）+ 会话列表。

Agent 的对话记忆是 `InMemorySaver`（进程内），服务一重启历史就没了。这里把每轮对话同步写一份到
`data/report_workspace/threads/chat-<会话id>/session.json`：

- 对话页左侧/右侧的「会话历史」列表需要能列出历史会话（内存里的 saver 没法枚举）
- 重启后还能看到之前的对话内容（`get_history` 优先读这里）
- 每个会话只保留最近 N 条消息，避免文件无限增长

只存 role / content（不存工具调用细节，那些在 Langfuse trace 里）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_MESSAGES = 200      # 单会话保留的消息条数上限


def session_dir_name(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "")[:64]
    if not safe:
        raise ValueError("会话 id 无效")
    return f"chat-{safe}"


def session_dir(session_id: str) -> Path:
    from app.agent.report_tools import workspace

    d = workspace() / "threads" / session_dir_name(session_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file(session_id: str) -> Path:
    return session_dir(session_id) / "session.json"


def save_session(session_id: str, messages: list[dict[str, Any]], *, scenario: str = "",
                 extra_last_assistant: dict[str, Any] | None = None) -> None:
    """写入/更新会话记录。title 取第一条用户消息（列表里好认）。

    extra_last_assistant：附加到最后一条 assistant 消息上的东西（本轮工具调用、trace 链接、token 用量）。
    这样点历史会话时，能像刚说完一样把工具卡片和「查看 Langfuse Trace」入口都还原出来。
    """
    clean = [{"role": str(m.get("role") or "user"), "content": str(m.get("content") or "")[:4000],
              **({"tools": m["tools"]} if m.get("tools") else {}),
              **({"trace_id": m["trace_id"]} if m.get("trace_id") else {}),
              **({"trace_url": m["trace_url"]} if m.get("trace_url") else {}),
              **({"usage": m["usage"]} if m.get("usage") else {})}
             for m in messages if str(m.get("content") or "").strip()][-MAX_MESSAGES:]
    if extra_last_assistant:
        idx = next((i for i in range(len(clean) - 1, -1, -1) if clean[i]["role"] == "assistant"), None)
        if idx is not None:
            clean[idx].update({k: v for k, v in extra_last_assistant.items() if v})
    title = next((m["content"] for m in clean if m["role"] == "user"), "")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data: dict[str, Any] = {
        "session_id": session_id,
        "scenario": scenario or "chat",
        "title": title[:60],
        "updated_at": now,
        "count": len(clean),
        "messages": clean,
    }
    old = load_session(session_id)
    data["created_at"] = (old or {}).get("created_at") or now
    f = _file(session_id)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)


def load_session(session_id: str) -> dict[str, Any] | None:
    f = _file(session_id)
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        logger.warning("会话记录损坏：%s", f)
        return None


def delete_sessions(session_ids: list[str]) -> int:
    """批量删除通用对话会话（连同会话目录），返回实际删掉的数量。同问诊：限定在 threads/chat-* 下。"""
    import shutil

    from app.agent.report_tools import workspace

    root = (workspace() / "threads").resolve()
    removed = 0
    for sid in session_ids or []:
        try:
            d = (root / session_dir_name(str(sid))).resolve()
        except ValueError:
            logger.warning("跳过非法会话 id：%r", sid)
            continue
        if d.is_relative_to(root) and d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    logger.info("删除对话会话 %d 个（请求 %d 个）", removed, len(session_ids or []))
    return removed


def list_sessions(limit: int = 10, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """按更新时间倒序**分页**列出通用对话会话，返回 (当前页, 总数)。

    同问诊：先按目录 mtime 排序，只解析当前页。
    """
    from app.agent.report_tools import workspace

    root = workspace() / "threads"
    if not root.is_dir():
        return [], 0
    dirs = [d for d in root.glob("chat-*") if (d / "session.json").is_file()]
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    rows: list[dict[str, Any]] = []
    for d in dirs[max(offset, 0): max(offset, 0) + max(limit, 0)]:
        data = load_session(d.name.removeprefix("chat-"))
        if not data:
            continue
        rows.append({"session_id": data["session_id"], "scenario": "chat",
                     "title": data.get("title") or "（空会话）",
                     "updated_at": data.get("updated_at"), "count": data.get("count", 0),
                     "note": f"{data.get('count', 0)} 条消息"})
    return rows, len(dirs)
