"""问诊工作流的接口：会话状态、会话列表、用例跑批与报告。

路径都在 `/api/consult/*` 下。注意**路由声明顺序**：`/sessions`、`/evals*` 必须写在 `/{session_id}` 之前，
否则会被后者当成 session_id 匹配掉。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.consult import eval as ceval
from app.consult import state as cstate
from app.consult.gates import MIN_DIFFERENTIAL_ROUNDS, MIN_INTAKE_ROUNDS, STAGES, missing_history

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/consult", tags=["consult"])


def _check_sid(session_id: str) -> None:
    """只校验会话 id 是否合法，**不要建目录**：读状态的接口不该有副作用（否则问一次就多一个空目录）。"""
    try:
        cstate.session_dir_name(session_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ------------------------------------------------------------------ 会话列表（要在 /{session_id} 之前）
@router.get("/sessions")
async def consult_sessions(limit: int = 10, offset: int = 0):
    """问诊会话历史（分页）：返回当前页、总数与分页信息，供对话页右侧「会话历史」。"""
    limit = max(1, min(limit, 100))
    rows, total = cstate.list_sessions(limit=limit, offset=max(offset, 0))
    return {"sessions": rows, "total": total, "offset": max(offset, 0), "limit": limit}


class DeleteSessions(BaseModel):
    session_ids: list[str] = Field(default_factory=list)


@router.post("/sessions/delete")
async def consult_sessions_delete(req: DeleteSessions):
    """批量删除问诊会话（会话历史里的多选删除）。"""
    if not req.session_ids:
        return {"ok": True, "removed": 0}
    if len(req.session_ids) > 200:
        raise HTTPException(400, "一次最多删除 200 个会话")
    return {"ok": True, "removed": cstate.delete_sessions(req.session_ids)}


# ------------------------------------------------------------------ 用例跑批
@router.get("/eval/sets")
async def eval_sets():
    """可选的测试集合（按 eval 目录下的科室分类）与每个集合的用例数。"""
    return {"sets": ceval.list_sets()}


@router.post("/eval/run")
async def eval_run(limit: int | None = None, set: str | None = None, sets: str | None = None):
    """**在后台启动**一轮跑批，立即返回初始状态（前端随后轮询 `/eval/run/status`）。

    任务跑在 asyncio.Task 里，与浏览器连接解耦：**刷新页面/关标签页都不会中断**，
    只有显式调用 `/eval/run/cancel` 或服务重启才会结束。
    `limit=N` 只跑前 N 个用例；`sets=消化内科,儿科` 选测试集合（可多选），`set=` 是老写法。
    """
    picked = [s.strip() for s in (sets or "").split(",") if s.strip()]
    if set:
        picked.append(set)
    try:
        return ceval.start_run(set_keys=picked, limit=limit)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/eval/run/status")
async def eval_run_status():
    """当前/最近一次跑批的状态（进度、日志尾、报告 id）；没有跑过返回 `{"status": "idle"}`。

    前端刷新后靠这个接口把进度条与日志恢复回来。
    """
    return ceval.current_run()


@router.post("/eval/run/cancel")
async def eval_run_cancel():
    """取消正在跑的批次（点了才发现选错集合时用）。"""
    return {"ok": ceval.cancel_run()}


@router.get("/evals")
async def eval_list(limit: int = 30):
    """历史评测报告列表。"""
    return {"evals": ceval.list_reports(limit=limit)}


@router.get("/evals/{eval_id}")
async def eval_detail(eval_id: str):
    """单份评测报告（含每个病例的判定、指标与对话记录）。"""
    report = ceval.load_report(eval_id)
    if not report:
        raise HTTPException(404, "评测报告不存在")
    return report


@router.post("/evals/{eval_id}/analyze")
async def eval_analyze(eval_id: str):
    """用 DeepSeek 分析这份报告，把结论写回报告的 analysis 字段并返回。"""
    report = ceval.load_report(eval_id)
    if not report:
        raise HTTPException(404, "评测报告不存在")
    try:
        text = await ceval.analyze_report(report)
    except Exception as e:  # noqa: BLE001
        logger.exception("AI 分析失败 eval=%s", eval_id)
        raise HTTPException(500, f"AI 分析失败：{type(e).__name__}: {e}") from e
    report["analysis"] = {"text": text, "at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    ceval.save_report(report)
    return report["analysis"]


# ------------------------------------------------------------------ 单会话状态（放在最后，避免吃掉上面的路径）
@router.get("/{session_id}")
async def consult_state(session_id: str):
    """问诊进度（阶段、轮次、分诊与诊断版本、对话记录），前端用来显示流程进度与回看历史。"""
    _check_sid(session_id)
    state = cstate.load_state(session_id)
    if not state:
        return {"active": False, "turns": [],
                "stages": [{"key": k, "label": v} for k, v in STAGES.items() if k != "closed"]}
    return {"active": True, "stage": state.get("stage"), "stage_label": STAGES.get(state.get("stage", ""), ""),
            "chief_complaint": state.get("chief_complaint"), "patient": state.get("patient"),
            "history": state.get("history") or {}, "missing": missing_history(state),
            "intake_rounds": state.get("record_rounds", 0), "min_intake_rounds": MIN_INTAKE_ROUNDS,
            "differential_rounds": len(state.get("differential") or []),
            "min_differential_rounds": MIN_DIFFERENTIAL_ROUNDS,
            "triage": state.get("triage"), "diagnoses": state.get("diagnoses") or [],
            "changes": state.get("changes") or [], "log": (state.get("history_log") or [])[-20:],
            "turns": state.get("turns") or [],
            "stages": [{"key": k, "label": v} for k, v in STAGES.items() if k != "closed"]}


@router.delete("/{session_id}")
async def consult_reset(session_id: str):
    """重新开始问诊：清掉本会话的问诊状态（对话记录请另行清空会话）。"""
    _check_sid(session_id)
    cstate.reset_state(session_id)
    return {"ok": True}
