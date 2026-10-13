"""问诊会话状态：`consult.json` 的读写与结构。

状态是工作流的**唯一数据源**（不依赖内存 checkpointer）：

    data/report_workspace/threads/consult-<会话id>/consult.json

里面同时存了**对话记录**（turns），所以服务重启后不仅阶段还在，判断节点也能看到之前的问答。
写入用「先写 .tmp 再 replace」的原子方式，避免半截文件。

为什么轮次显式计数（record_rounds / differential 轮次）而不是从消息数推断：
现在一次运行 = 一问一答，轮次就是「问过几次」，直接计数最直观，也方便前端进度条展示。
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ 会话目录与文件
def session_dir_name(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "")[:64]
    if not safe:
        raise ValueError("会话 id 无效")
    return f"consult-{safe}"


def session_dir(session_id: str) -> Path:
    from app.agent.report_tools import workspace

    d = workspace() / "threads" / session_dir_name(session_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path(session_id: str) -> Path:
    """状态文件路径，**不创建目录**：读取状态不该有副作用（否则每查一次就多一个空目录）。"""
    from app.agent.report_tools import workspace

    return workspace() / "threads" / session_dir_name(session_id) / "consult.json"


def _state_file(session_id: str) -> Path:
    return state_path(session_id)


def load_state(session_id: str) -> dict[str, Any] | None:
    f = _state_file(session_id)
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        logger.warning("问诊状态文件损坏：%s", f)
        return None


def save_state(session_id: str, state: dict[str, Any]) -> None:
    """写入状态（原子替换）。目录在**写**的时候才创建——读状态不该有副作用。"""
    f = _state_file(session_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)


def reset_state(session_id: str) -> None:
    f = _state_file(session_id)
    if f.is_file():
        f.unlink()


def delete_sessions(session_ids: list[str]) -> int:
    """批量删除问诊会话（连同会话目录），返回实际删掉的数量。

    只允许删 `threads/consult-<会话id>/` 下的目录：先用 `session_dir_name` 洗一遍 id，
    再校验解析后的路径确实在工作区 threads 里，避免路径穿越删到别的东西。
    """
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
    logger.info("删除问诊会话 %d 个（请求 %d 个）", removed, len(session_ids or []))
    return removed


def list_sessions(limit: int = 10, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """按更新时间倒序**分页**列出问诊会话，返回 (当前页, 总数)。

    先按目录 mtime 排序（每一次 save_state 都会刷新 mtime，比解析 JSON 便宜得多），
    只解析当前页的文件——会话多了也不会因为“列一页要读全部 JSON”而变慢。
    """
    from app.agent.report_tools import workspace

    root = workspace() / "threads"
    if not root.is_dir():
        return [], 0
    dirs = [d for d in root.glob("consult-*") if (d / "consult.json").is_file()]
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    rows: list[dict[str, Any]] = []
    for d in dirs[max(offset, 0): max(offset, 0) + max(limit, 0)]:
        data = load_state(d.name.removeprefix("consult-"))
        if not data:
            continue
        triage = data.get("triage") or {}
        diagnoses = data.get("diagnoses") or []
        bits = [f"现病史 {data.get('record_rounds') or 0} 轮"]
        if triage:
            bits.append(f"分诊 {triage.get('department')}")
        if diagnoses:
            bits.append(f"诊断 v{len(diagnoses)}")
        rows.append({
            "session_id": data.get("session_id") or d.name.removeprefix("consult-"),
            "scenario": "consult",
            "title": data.get("chief_complaint") or "（未记录主诉）",
            "stage": data.get("stage"),
            "updated_at": data.get("updated_at") or data.get("created_at"),
            "count": len(data.get("turns") or []),
            "note": "｜".join(bits),
        })
    return rows, len(dirs)


def new_state(session_id: str) -> dict[str, Any]:
    """一次新问诊的初始状态。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "session_id": session_id,
        "stage": "intake",
        "chief_complaint": "",
        "patient": "",
        "setting": "线上问诊",
        "history": {},              # 10 项现病史要素：key -> 患者答复
        "history_extra": {},        # 不在这 10 项里的补充信息（烟酒史、家族史…）
        "record_rounds": 0,         # 现病史采集轮次
        "triage": None,             # 分诊结果（只给一次）
        "differential": [],         # 鉴别问询记录：[{round, questions, answers, reason, at}]
        "red_flag_screening": "",   # 红旗征筛查结论
        "diagnoses": [],            # 诊断版本：[{version, primary, ...}]
        "changes": [],              # 诊断后的病情变化记录
        "found_red_flags": [],      # 从患者话里识别到的危险信号（用于抬升 urgency）
        "turns": [],                # 对话记录：[{role, text, action?, at}]
        "asked_times": {},          # 每个要素被问过几次（问太多次的记「未明确」，不再纠缠）
        "last_questions": [],       # 上一轮问的问题（给判断节点看）
        "last_action": "",          # 上一轮动作
        "followup_answered": False, # 病情变化是否已追问到位
        "created_at": now,
        "updated_at": now,
    }
