"""山区问诊工作流的接口：会话状态、会话列表、删除，以及**用例跑批与报告**。

路径都在 `/api/rural/*` 下。与 `app/consult/routes.py` 同样的注意点：
**路由声明顺序**——`/sessions*`、`/evals*` 必须写在 `/{session_id}` 之前，否则会被后者当成 session_id 匹配掉。

跑批（`/eval/run`）是**后台任务**：启动就立即返回，任务在 `asyncio.Task` 里跑，
前端只负责「启动 + 轮询状态」，所以刷新页面不会把跑批取消（详见 `app/core/workflow_eval.py`）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.rural import eval as reval
from app.rural import state as rstate
from app.rural.gates import (
    DISPOSITIONS,
    MIN_DIFFERENTIAL_ROUNDS,
    MIN_INTAKE_ROUNDS,
    STAGES,
    missing_access,
    missing_history,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rural", tags=["rural"])


def _check_sid(session_id: str) -> None:
    """只校验会话 id 是否合法，**不要建目录**：读状态的接口不该有副作用（否则问一次就多一个空目录）。"""
    try:
        rstate.session_dir_name(session_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ------------------------------------------------------------------ 会话列表（要在 /{session_id} 之前）
@router.get("/sessions")
async def rural_sessions(limit: int = 10, offset: int = 0):
    """山区问诊会话历史（分页）：供对话页右侧「会话历史」使用。"""
    limit = max(1, min(limit, 100))
    rows, total = rstate.list_sessions(limit=limit, offset=max(offset, 0))
    return {"sessions": rows, "total": total, "offset": max(offset, 0), "limit": limit}


class DeleteSessions(BaseModel):
    session_ids: list[str] = Field(default_factory=list)


@router.post("/sessions/delete")
async def rural_sessions_delete(req: DeleteSessions):
    """批量删除山区问诊会话（会话历史里的多选删除）。"""
    if not req.session_ids:
        return {"ok": True, "removed": 0}
    if len(req.session_ids) > 200:
        raise HTTPException(400, "一次最多删除 200 个会话")
    return {"ok": True, "removed": rstate.delete_sessions(req.session_ids)}


# ------------------------------------------------------------------ 用例跑批与报告
@router.get("/eval/sets")
async def eval_sets():
    """可选的测试集合（= `eval/rural/` 下的处置层级目录）与每个集合的用例数。"""
    return {"sets": reval.list_sets()}


@router.post("/eval/run")
async def eval_run(limit: int | None = None, set: str | None = None, sets: str | None = None):
    """**在后台启动**一轮跑批，立即返回初始状态（前端随后轮询 `/eval/run/status`）。

    任务跑在 asyncio.Task 里，与浏览器连接解耦：刷新页面/关标签页都不会中断。
    `limit=N` 只跑前 N 个用例；`sets=紧急转运,县医院` 选测试集合（可多选），`set=` 是老写法。
    """
    picked = [s.strip() for s in (sets or "").split(",") if s.strip()]
    if set:
        picked.append(set)
    try:
        return reval.start_run(set_keys=picked, limit=limit)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/eval/run/status")
async def eval_run_status():
    """当前/最近一次跑批的状态（进度、日志尾、报告 id）；没有跑过返回 `{"status": "idle"}`。"""
    return reval.current_run()


@router.post("/eval/run/cancel")
async def eval_run_cancel():
    """取消正在跑的批次（点了才发现选错集合时用）。"""
    return {"ok": reval.cancel_run()}


@router.get("/evals")
async def eval_list(limit: int = 30):
    """历史评测报告列表。"""
    return {"evals": reval.list_reports(limit=limit)}


@router.get("/evals/{eval_id}")
async def eval_detail(eval_id: str):
    """单份评测报告（含每个用例的 8 项判定、指标与对话记录）。"""
    report = reval.load_report(eval_id)
    if not report:
        raise HTTPException(404, "评测报告不存在")
    return report


@router.post("/evals/{eval_id}/analyze")
async def eval_analyze(eval_id: str):
    """用 DeepSeek 分析这份报告，把结论写回报告的 analysis 字段并返回。"""
    report = reval.load_report(eval_id)
    if not report:
        raise HTTPException(404, "评测报告不存在")
    try:
        text = await reval.analyze_report(report)
    except Exception as e:  # noqa: BLE001
        logger.exception("山区问诊 AI 分析失败 eval=%s", eval_id)
        raise HTTPException(500, f"AI 分析失败：{type(e).__name__}: {e}") from e
    report["analysis"] = {"text": text,
                          "at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    reval.save_report(report)
    return report["analysis"]


# ------------------------------------------------------------------ 单会话状态（放在最后，避免吃掉上面的路径）
@router.get("/{session_id}")
async def rural_state(session_id: str):
    """问诊进度（阶段、轮次、就医条件、就医建议与诊断版本、对话记录）。"""
    _check_sid(session_id)
    state = rstate.load_state(session_id)
    if not state:
        return {"active": False, "turns": [],
                "stages": [{"key": k, "label": v} for k, v in STAGES.items() if k != "closed"]}
    return {
        "active": True, "stage": state.get("stage"), "stage_label": STAGES.get(state.get("stage", ""), ""),
        "chief_complaint": state.get("chief_complaint"), "patient": state.get("patient"),
        "history": state.get("history") or {}, "missing": missing_history(state),
        "access": state.get("access") or {}, "missing_access": missing_access(state),
        "intake_rounds": state.get("record_rounds", 0), "min_intake_rounds": MIN_INTAKE_ROUNDS,
        "differential_rounds": len(state.get("differential") or []),
        "min_differential_rounds": MIN_DIFFERENTIAL_ROUNDS,
        "triage": state.get("triage"), "diagnoses": state.get("diagnoses") or [],
        "changes": state.get("changes") or [],
        "dispositions": DISPOSITIONS,
        "turns": state.get("turns") or [],
        "stages": [{"key": k, "label": v} for k, v in STAGES.items() if k != "closed"],
    }


@router.delete("/{session_id}")
async def rural_reset(session_id: str):
    """重新开始：清掉本会话的问诊状态（对话记录请另行清空会话）。"""
    _check_sid(session_id)
    rstate.reset_state(session_id)
    return {"ok": True}
