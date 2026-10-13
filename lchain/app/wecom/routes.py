"""企业微信群回复接口。

主接口（对接企微消息回调 / 会话存档服务）：
  POST /api/wecom/messages         推送一条群消息，立即返回（异步处理；回复通过 webhook / 事件流下发）——推荐
  GET  /api/wecom/groups/{id}/events  SSE：群内实时事件（新消息 / 节点执行 / 回复动作 / 打断合并 / 输入中）
  POST /api/wecom/reply            推送一条群消息并等待该消息的处理结果（同步）
  POST /api/wecom/upload           上传图片/PDF/文档，返回 attachment，放进消息的 attachments 里

连续发送：每条消息立即处理；回复还没发出时又来新消息，会打断本轮、合并后立即重新回复（见 dispatcher.py）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.agent.search import _is_public_url
from app.core.cluster import current_node_id, list_nodes
from app.core.config import get_settings
from app.core.db import db_ready, init_db
from app.wecom import dispatcher, jobs, store
from app.wecom import eval as weval
from app.wecom.compress import maybe_compress
from app.wecom.documents import SavedFile, file_kind, save_bytes, upload_dir
from app.wecom.prompts import WECOM_PROMPTS

router = APIRouter(prefix="/api/wecom", tags=["wecom"])

logger = logging.getLogger(__name__)

Role = Literal["patient", "family", "case_manager", "doctor"]


class Attachment(BaseModel):
    name: str
    path: str | None = Field(None, description="upload 接口返回的本地路径")
    url: str | None = Field(None, description="或者给一个可下载的公网 URL（企微媒体文件地址）")
    mime: str | None = None
    size: int | None = None
    kind: str | None = None


class GroupInfo(BaseModel):
    name: str | None = None
    profile: str | None = Field(None, description="基础信息：患者档案 / 服务包 / 备注")
    case_manager_id: str | None = None
    case_manager_name: str | None = None


class WecomMessage(BaseModel):
    group_id: str
    msg_id: str | None = Field(None, description="企微侧消息 id，用于幂等去重")
    sender_id: str = "user"
    sender_name: str = ""
    sender_role: Role = "patient"
    content: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)
    group: GroupInfo | None = Field(None, description="可选：顺带创建/更新群信息")
    # 调试参数
    force_reply: bool = False
    prompt_overrides: dict[str, str] = Field(default_factory=dict, description="临时覆盖节点提示词 {name: text}")


def _require_db() -> None:
    if not db_ready():
        raise HTTPException(503, "MySQL 未连接，请先执行 make mysql-up 后调用 POST /api/wecom/db/init 或重启服务")


async def _materialize(att: Attachment) -> dict[str, Any]:
    if att.path:
        p = Path(att.path).resolve()
        if upload_dir().resolve() not in p.parents or not p.exists():
            raise HTTPException(400, f"附件路径无效：{att.path}")
        return {"name": att.name, "path": str(p), "mime": att.mime or "", "size": att.size or p.stat().st_size,
                "kind": att.kind or file_kind(att.name, att.mime or ""), "url": "/api/wecom/files/" + p.name}
    if att.url:
        if not _is_public_url(att.url):
            raise HTTPException(400, "附件 URL 不允许访问")
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.get(att.url)
            r.raise_for_status()
        return save_bytes(att.name, r.content, att.mime or r.headers.get("content-type", "").split(";")[0]).as_attachment()
    raise HTTPException(400, "附件需要 path 或 url")


async def _ingest(req: WecomMessage) -> tuple[int, bool]:
    """消息入库，返回 (message_id, 是否重复)。"""
    _require_db()
    if req.msg_id:
        old = await store.find_by_ext_id(req.msg_id)
        if old:
            return old.id, True
    g = req.group or GroupInfo()
    await store.upsert_group(req.group_id, **g.model_dump())
    atts = [await _materialize(a) for a in req.attachments]
    m = await store.add_message(group_id=req.group_id, ext_msg_id=req.msg_id, node_id=current_node_id(),
                                sender_id=req.sender_id,
                                sender_name=req.sender_name or req.sender_id, sender_role=req.sender_role,
                                msg_type="file" if atts else "text", content=req.content,
                                attachments=atts or None, mentions=req.mentions or None)
    return m.id, False


def _opts(req: WecomMessage) -> dict[str, Any]:
    return {"force_reply": req.force_reply, "prompt_overrides": req.prompt_overrides}


@router.post("/messages")
async def post_message(req: WecomMessage):
    """异步接口：消息入库后立即返回，由调度器攒批 / 合并后处理。"""
    mid, dup = await _ingest(req)
    if not dup:
        from app.wecom.outbound import crash_point

        crash_point("ingested")  # 消息已入库、尚未开始处理
        await dispatcher.dispatch(await store.get_message(mid), _opts(req))
    return {"message_id": mid, "duplicate": dup, **dispatcher.worker(req.group_id).status()}


@router.post("/reply")
async def reply(req: WecomMessage):
    """同步接口：等待这条消息的处理结果。

    - 连续发送被合并时，只有**这批消息的最后一条**的结果里带 actions；其余返回 status=merged、actions=[]，调用方不要重复发送
    - 病历经理/医生消息：立即返回 need_reply=false（并取消助手尚未发出的回复）
    """
    mid, dup = await _ingest(req)
    if dup:
        return {"duplicate": True, "message_id": mid}
    fut = await dispatcher.dispatch(await store.get_message(mid), _opts(req))
    if fut is None:
        return {"message_id": mid, "need_reply": False, "status": "skipped", "actions": []}
    try:
        r = await asyncio.wait_for(asyncio.shield(fut), timeout=180)
    except asyncio.TimeoutError:
        return {"message_id": mid, "status": "timeout", "actions": []}
    return {"message_id": mid, **r}


@router.get("/groups/{group_id}/events")
async def group_events(group_id: str):
    """SSE：订阅群内实时事件。事件类型：message / queued / typing / meta / step / action / interrupt / merged /
    handover / done / error"""
    w = dispatcher.worker(group_id)
    q = w.subscribe()

    async def gen():
        try:
            yield {"event": "hello", "data": json.dumps(w.status())}
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {"event": ev["type"], "data": json.dumps(ev, ensure_ascii=False, default=str)}
        finally:
            w.unsubscribe(q)

    return EventSourceResponse(gen())


@router.post("/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    try:
        f: SavedFile = await asyncio.to_thread(save_bytes, file.filename or "file", data, file.content_type)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return f.as_attachment()


@router.get("/files/{name}", include_in_schema=False)
async def get_file(name: str):
    p = (upload_dir() / Path(name).name).resolve()
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


# ------------------------------------------------------------------ 群 / 消息管理（测试页使用）
@router.get("/groups")
async def groups(limit: int = 20, offset: int = 0, q: str = ""):
    """群（会话）列表：分页 + 搜索，按最近有消息排序。测试页左侧「会话历史」用它。

    每行带最后一条消息预览 / 数量 / 执行次数；`q` 可搜群名 / ID / 病历经理 / 患者档案，
    也能搜消息内容与发言人（按患者名字找群）。
    """
    _require_db()
    items, total = await store.list_groups_page(limit=limit, offset=offset, q=q)
    return {"items": items, "total": total, "limit": max(1, min(limit, 100)), "offset": max(0, offset)}


class DeleteGroups(BaseModel):
    group_ids: list[str] = Field(default_factory=list, description="要删除的群 id（连同消息 / 执行记录 / checkpoint 一起删）")


@router.post("/groups/delete")
async def delete_groups(req: DeleteGroups):
    """批量删除群（会话历史里的多选删除）：连同消息、文件、待回复、执行记录与 checkpoint。"""
    _require_db()
    if len(req.group_ids) > 100:
        raise HTTPException(400, "一次最多删除 100 个群")
    return {"ok": True, "removed": await store.delete_groups(req.group_ids)}


@router.put("/groups/{group_id}")
async def put_group(group_id: str, g: GroupInfo):
    _require_db()
    return store.group_dict(await store.upsert_group(group_id, **g.model_dump()))


@router.get("/groups/{group_id}")
async def get_group(group_id: str):
    _require_db()
    g = await store.get_group(group_id)
    if not g:
        raise HTTPException(404, "群不存在")
    return store.group_dict(g)


@router.get("/groups/{group_id}/messages")
async def group_messages(group_id: str, after_id: int = 0, limit: int = 200):
    _require_db()
    return {"items": [store.msg_dict(m) for m in await store.list_messages(group_id, after_id, limit)]}


@router.get("/messages/{message_id}")
async def get_message(message_id: int):
    """单条消息（患者端 H5 规划卡 /wecom-card/{id} 用它取数据）。"""
    _require_db()
    m = await store.get_message(message_id)
    if m is None:
        raise HTTPException(status_code=404, detail="message not found")
    return store.msg_dict(m)


@router.get("/groups/{group_id}/runs")
async def group_runs(group_id: str, limit: int = 20):
    """群内最近 N 次执行（含每个节点的输入 / 输出）。测试页刷新后右侧「Agent 执行过程」用它恢复历史。"""
    _require_db()
    return {"items": await store.recent_runs(group_id, limit)}


@router.delete("/groups/{group_id}/messages")
async def clear_messages(group_id: str):
    _require_db()
    await store.clear_group(group_id)
    return {"ok": True}


@router.get("/groups/{group_id}/pending")
async def group_pending(group_id: str):
    _require_db()
    return {"items": await store.list_pending(group_id)}


# ------------------------------------------------------------------ 家庭医生 Agent（VBC 分级诊疗）
@router.get("/groups/{group_id}/fd-plan")
async def fd_plan(group_id: str):
    """当前有效的家庭医生计划（完整计划 + 患者视图 + 观察任务），供测试页与审计查看。"""
    _require_db()
    from app.wecom.fd import store as fd_store

    case = await fd_store.get_case(group_id)
    obs = await fd_store.list_observations(group_id)
    if not case:
        return {"case": None, "plan": None, "observations": obs, "active_observation": None}
    return {"case": case, "plan": case.get("plan") or {}, "observations": obs,
            "active_observation": next((o for o in obs if o["state"] in
                                        ("ACTIVE", "REVIEW_PENDING", "CONTACT_OVERDUE")), None)}


@router.get("/fd/config")
async def fd_config():
    """家庭医生 Agent 的放行状态与规则数量（第 10 节：环境/放行状态可查）。"""
    from app.wecom.fd import pathways as fd_pathways
    from app.wecom.fd import red_flags

    s = get_settings()
    return {"enabled": s.wecom_fd_enabled, "name": s.wecom_fd_name,
            "release_stage": s.wecom_fd_release_stage, "observation_enabled": s.wecom_fd_observation_enabled,
            "emergency_number": s.wecom_fd_emergency_number, "plan_card": s.wecom_fd_plan_card,
            "red_flag_rules": red_flags.rule_count(),
            "red_flag_registry_version": red_flags.RED_FLAG_REGISTRY_VERSION,
            "pathways": fd_pathways.pathway_count(),
            "review_status": red_flags.REVIEW_STATUS}


@router.post("/fd/jobs/run")
async def fd_run_jobs():
    """手动触发居家观察的三个定时任务（本地联调用；生产由 scheduler 进程自动跑）。"""
    _require_db()
    sent = await jobs.fd_followup_review()
    overdue = await jobs.fd_contact_overdue()
    expired = await jobs.fd_expired_observations()
    return {"followup": len(sent), "overdue": len(overdue), "expired": len(expired)}


@router.post("/groups/{group_id}/compress")
async def compress(group_id: str, force: bool = True):
    _require_db()
    return await maybe_compress(group_id, force=force)


@router.get("/runs/{run_id}")
async def run_detail(run_id: str):
    _require_db()
    r = await store.get_run(run_id)
    if not r:
        raise HTTPException(404)
    return r


# ------------------------------------------------------------------ checkpoint / 断点续跑
@router.get("/runs/{run_id}/checkpoint")
async def run_checkpoint(run_id: str):
    """当前 checkpoint 状态（已完成节点 / 下一步要执行的节点）+ checkpoint 历史。"""
    _require_db()
    from app.wecom.checkpoint import list_checkpoints
    from app.wecom.graph import checkpoint_state

    return {**await checkpoint_state(run_id), "history": await list_checkpoints(run_id)}


@router.post("/runs/{run_id}/resume")
async def run_resume(run_id: str, wait: bool = False):
    """从 checkpoint 断点续跑一次中断/失败的执行：已完成的节点跳过，从失败节点继续，已发送的消息不会重复发送。"""
    _require_db()
    from app.wecom.graph import checkpoint_state

    run = await store.get_run_row(run_id)
    if run is None:
        raise HTTPException(404, "run 不存在")
    if run.status in ("done", "skipped"):
        raise HTTPException(400, f"该执行状态为 {run.status}，无需续跑")
    w = dispatcher.worker(run.group_id)
    if w.running and w.current_run_id == run_id:
        raise HTTPException(409, "该执行正在运行中")
    st = await checkpoint_state(run_id)
    if not st.get("exists") or not st.get("next"):
        raise HTTPException(400, "没有可续跑的 checkpoint")
    fut = await dispatcher.worker(run.group_id).resume(run_id, run.message_ids or [run.message_id])
    if not wait:
        return {"ok": True, "run_id": run_id, "resume_from": st["next"], "completed_nodes": st["completed_nodes"]}
    return await asyncio.wait_for(fut, timeout=180)


@router.get("/runs")
async def list_runs(status: str = "interrupted,failed,running", group_id: str | None = None):
    _require_db()
    return {"items": await store.runs_by_status(status.split(","), group_id)}


# ------------------------------------------------------------------ 批量评测（/wecom-eval 页面）
# 声明顺序：`/evals/run*` 必须在 `/evals/{eval_id}` 之前，否则 "run" 会被当成 eval_id
@router.get("/evals/sets")
async def eval_sets():
    """可选的测试集合（= `eval/wecom` 下的场景目录）与每个集合的用例数。"""
    return {"sets": weval.list_sets()}


@router.post("/evals/run")
async def eval_run(limit: int | None = None, sets: str | None = None, set: str | None = None):
    """**后台启动**一轮企微回复评测，立即返回状态（前端轮询 `/evals/run/status`）。

    每条用例会新建一个 `eval-wecom-*` 临时群跑真实一轮，跑完删除；这类群的消息**不外发**。
    任务与浏览器连接解耦：刷新 / 关标签页不会中断，只有显式取消或服务重启才结束。
    `sets=医疗咨询,安全急救` 选集合（可多选，`set=` 是老写法），`limit=N` 只跑前 N 条。
    """
    picked = [s.strip() for s in (sets or "").split(",") if s.strip()]
    if set:
        picked.append(set)
    try:
        return weval.start_run(set_keys=picked, limit=limit)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/evals/run/status")
async def eval_run_status():
    """当前/最近一次跑批的状态（进度、日志尾、报告 id）；没跑过返回 `{"status": "idle"}`。"""
    return weval.current_run()


@router.post("/evals/run/cancel")
async def eval_run_cancel():
    """取消正在跑的批次。"""
    return {"ok": weval.cancel_run()}


@router.get("/evals")
async def eval_list(limit: int = 30):
    """历史评测报告列表。"""
    return {"evals": weval.list_reports(limit=limit)}


@router.get("/evals/{eval_id}")
async def eval_detail(eval_id: str):
    """单份评测报告（含每条用例的判定、回复原文、节点步骤）。"""
    report = weval.load_report(eval_id)
    if not report:
        raise HTTPException(404, "评测报告不存在")
    return report


@router.post("/evals/{eval_id}/analyze")
async def eval_analyze(eval_id: str):
    """用 DeepSeek 分析这份报告，结论写回报告的 analysis 字段并返回。"""
    report = weval.load_report(eval_id)
    if not report:
        raise HTTPException(404, "评测报告不存在")
    try:
        text = await weval.analyze_report(report)
    except Exception as e:  # noqa: BLE001
        logger.exception("AI 分析失败 eval=%s", eval_id)
        raise HTTPException(500, f"AI 分析失败：{type(e).__name__}: {e}") from e
    report["analysis"] = {"text": text, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    weval.save_report(report)
    return report["analysis"]


@router.get("/cluster")
async def cluster():
    """集群状态：各 web 节点 / 定时任务进程的心跳。"""
    _require_db()
    return {"current_node": current_node_id(), "nodes": await list_nodes()}


@router.post("/jobs/check-timeout")
async def trigger_timeout_check():
    """手动触发一次超时检查（测试用；正常由定时任务进程轮询）。任务本身幂等，与定时任务重叠执行也不会重复通知。"""
    _require_db()
    return {"notified": await jobs.check_overdue()}


@router.post("/db/init")
async def db_init():
    return {"ok": await init_db()}


@router.get("/config")
async def wecom_config():
    s = get_settings()
    return {"db_ready": db_ready(), "bot_name": s.wecom_bot_name, "prompts": list(WECOM_PROMPTS),
            "timeout_seconds": s.wecom_pending_timeout_seconds, "recent_messages": s.wecom_recent_messages,
            "summary_trigger": s.wecom_summary_trigger, "doc_model": s.wecom_doc_model,
            "demo_group_id": "demo-" + uuid.uuid4().hex[:6]}
