"""山区问诊会话状态：`rural.json` 的读写与结构。

状态是工作流的**唯一数据源**（不依赖内存 checkpointer）：

    data/report_workspace/threads/rural-<会话id>/rural.json

里面同时存了**对话记录**（turns），所以服务重启后不仅阶段还在，判断节点也能看到之前的问答。
写入用「先写 .tmp 再 replace」的原子方式，避免半截文件。

与 `app/consult/state.py` 的关系：结构几乎一样（同一套工作流思路），差别是
- 目录/文件前缀换成 `rural-`，两个场景的会话互不干扰
- 多一份 `access`（就医条件：距离/交通/陪同/本地可用条件）与 `asked_access`
- 列会话时显示的是「处置层级 + 分诊科室」而不是「分诊科室 + 诊断版本」
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

from app.rural.gates import ACCESS_KEYS, DISPOSITIONS, STAGES

logger = logging.getLogger(__name__)

PREFIX = "rural-"


# ------------------------------------------------------------------ 会话目录与文件
def session_dir_name(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "")[:64]
    if not safe:
        raise ValueError("会话 id 无效")
    return f"{PREFIX}{safe}"


def session_dir(session_id: str) -> Path:
    from app.agent.report_tools import workspace

    d = workspace() / "threads" / session_dir_name(session_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path(session_id: str) -> Path:
    """状态文件路径，**不创建目录**：读取状态不该有副作用（否则每查一次就多一个空目录）。"""
    from app.agent.report_tools import workspace

    return workspace() / "threads" / session_dir_name(session_id) / "rural.json"


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
        logger.warning("山区问诊状态文件损坏：%s", f)
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
    """批量删除山区问诊会话（整个 `threads/rural-<id>/` 目录），防路径穿越。"""
    from app.agent.report_tools import workspace

    root = (workspace() / "threads").resolve()
    removed = 0
    for sid in session_ids or []:
        try:
            d = (root / session_dir_name(str(sid))).resolve()
        except ValueError:
            logger.warning("跳过非法的山区问诊会话 id：%r", sid)
            continue
        if d.is_relative_to(root) and d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    logger.info("删除山区问诊会话 %d 个（请求 %d 个）", removed, len(session_ids or []))
    return removed


def list_sessions(limit: int = 10, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """按目录 mtime 倒序**分页**列出会话；只解析当前页（会话多了也不会变慢）。"""
    from app.agent.report_tools import workspace

    root = workspace() / "threads"
    if not root.is_dir():
        return [], 0
    dirs = [d for d in root.glob(f"{PREFIX}*") if (d / "rural.json").is_file()]
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    rows: list[dict[str, Any]] = []
    for d in dirs[max(offset, 0): max(offset, 0) + max(limit, 0)]:
        data = load_state(d.name.removeprefix(PREFIX))
        if not data:
            continue
        triage = data.get("triage") or {}
        diagnoses = data.get("diagnoses") or []
        bits = [f"现病史 {data.get('record_rounds') or 0} 轮"]
        if triage:
            bits.append(f"就医建议 {triage.get('department')}")
            if triage.get("disposition"):
                bits.append(DISPOSITIONS.get(triage["disposition"], "").split("（")[0])
        if diagnoses:
            bits.append(f"诊断 v{len(diagnoses)}")
        rows.append({
            "session_id": data.get("session_id") or d.name.removeprefix(PREFIX),
            "scenario": "rural",
            "title": data.get("chief_complaint") or "（未记录主诉）",
            "stage": data.get("stage"),
            "updated_at": data.get("updated_at") or data.get("created_at"),
            "count": len(data.get("turns") or []),
            "note": "｜".join([b for b in bits if b]),
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
        "setting": "山区远程问诊",
        "history": {},              # 10 项现病史要素：key -> 患者答复
        "history_extra": {},        # 不在这 10 项里的补充信息（烟酒史、家族史…）
        "access": {k: "" for k in ACCESS_KEYS},   # 就医条件：距离/交通/陪同/本地可用
        "asked_access": {},         # 每个就医条件被问过几次
        "access_asked": False,      # 就医条件是否已经问过一轮（问过就不再纠缠，按最坏情况建议）
        "record_rounds": 0,         # 现病史采集轮次
        "triage": None,             # 就医建议（只给一次）
        "differential": [],         # 鉴别问询记录：[{round, questions, answers, reason, at}]
        "red_flag_screening": "",   # 危险信号筛查结论
        "diagnoses": [],            # 诊断版本：[{version, primary, ...}]
        "changes": [],              # 诊断后的病情变化记录
        "found_red_flags": [],      # 从患者话里识别到的危险信号（用于抬升处置层级）
        "turns": [],                # 对话记录：[{role, text, action?, at}]
        "asked_times": {},          # 每个要素被问过几次（问太多次的记「未明确」，不再纠缠）
        "last_questions": [],       # 上一轮问的问题（给判断节点看）
        "last_action": "",          # 上一轮动作
        "followup_answered": False, # 病情变化是否已追问到位
        "created_at": now,
        "updated_at": now,
    }


__all__ = ["PREFIX", "STAGES", "session_dir", "session_dir_name", "load_state", "save_state",
           "reset_state", "delete_sessions", "list_sessions", "new_state"]
