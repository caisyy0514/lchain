"""HTTP 接口。"""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.agent.agent import ChatOptions, clear_session, get_history, stream_chat
from app.agent.search import format_results, search, search_status
from app.agent.skills import load_skills
from app.core.config import get_settings
from app.core.langfuse_client import get_langfuse, project_url
from app.prompts.manager import DEFAULT_PROMPTS, resolve_prompt
from app.prompts.playground import run_compare

router = APIRouter(prefix="/api")

# /api/health 的 Langfuse 连通性缓存（auth_check 是同步 HTTP，不能每个请求都调）
_health_ok: bool | None = None
_health_checked_at: float = 0.0


# ------------------------------------------------------------------ 对话
class ChatRequest(BaseModel):
    message: str
    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    user_id: str = "web-user"
    model: str | None = None
    temperature: float | None = None
    prompt_name: str = "agent-system"
    prompt_label: str | None = None
    prompt_version: int | None = None
    prompt_override: str | None = None
    prompt_variables: dict[str, Any] = Field(default_factory=dict)
    use_tools: bool = True
    web_search: bool = True
    use_skills: bool = True
    scenario: str | None = None


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    return EventSourceResponse(stream_chat(ChatOptions(**req.model_dump())))


@router.get("/sessions")
async def sessions_list(scenario: str = "", limit: int = 10, offset: int = 0):
    """通用对话的会话历史（分页）；问诊的会话列表在 /api/consult/sessions。"""
    from app.agent.sessions import list_sessions

    if scenario and scenario != "chat":
        return {"sessions": [], "total": 0, "offset": 0, "limit": limit}
    limit = max(1, min(limit, 100))
    rows, total = list_sessions(limit=limit, offset=max(offset, 0))
    return {"sessions": rows, "total": total, "offset": max(offset, 0), "limit": limit}


class DeleteSessions(BaseModel):
    session_ids: list[str] = Field(default_factory=list)


@router.post("/sessions/delete")
async def sessions_delete(req: DeleteSessions):
    """批量删除通用对话会话（会话历史里的多选删除）。"""
    from app.agent.sessions import delete_sessions

    if not req.session_ids:
        return {"ok": True, "removed": 0}
    if len(req.session_ids) > 200:
        raise HTTPException(400, "一次最多删除 200 个会话")
    return {"ok": True, "removed": delete_sessions(req.session_ids)}


@router.get("/sessions/{session_id}/history")
async def session_history(session_id: str):
    return {"session_id": session_id, "messages": await get_history(session_id)}


@router.delete("/sessions/{session_id}")
async def session_clear(session_id: str):
    await clear_session(session_id)
    return {"ok": True}


# ------------------------------------------------------------------ 反馈（写入 Langfuse Score）
class FeedbackRequest(BaseModel):
    trace_id: str
    value: float = Field(description="1=👍  0=👎，或任意数值评分")
    name: str = "user-feedback"
    comment: str | None = None


@router.post("/feedback")
async def feedback(req: FeedbackRequest):
    lf = get_langfuse()
    if not lf:
        raise HTTPException(400, "Langfuse 未配置")
    lf.create_score(trace_id=req.trace_id, name=req.name, value=req.value, comment=req.comment)
    lf.flush()
    return {"ok": True}


# ------------------------------------------------------------------ 提示词
@router.get("/prompts")
async def list_prompts():
    lf = get_langfuse()
    items: list[dict[str, Any]] = []
    if lf:
        try:
            resp = lf.api.prompts.list(limit=100)
            items = [{"name": p.name, "versions": p.versions, "labels": p.labels} for p in resp.data]
        except Exception as e:  # noqa: BLE001
            return {"items": [{"name": n, "versions": [], "labels": ["local"]} for n in DEFAULT_PROMPTS], "error": str(e)}
    names = {i["name"] for i in items}
    items += [{"name": n, "versions": [], "labels": ["local"]} for n in DEFAULT_PROMPTS if n not in names]
    return {"items": items}


@router.get("/prompts/{name}")
async def get_prompt(name: str, label: str | None = None, version: int | None = None):
    try:
        p = resolve_prompt(name, label=label, version=version)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return {**p.meta(), "template": p.template, "compiled": p.text, "variables": p.variables, "config": p.config}


class SavePromptRequest(BaseModel):
    prompt: str
    labels: list[str] = Field(default_factory=list)
    config: dict[str, Any] | None = None
    commit_message: str | None = None


@router.post("/prompts/{name}")
async def save_prompt(name: str, req: SavePromptRequest):
    """把调试好的提示词保存为 Langfuse 新版本（可选直接打 production label 发布）。"""
    lf = get_langfuse()
    if not lf:
        raise HTTPException(400, "Langfuse 未配置")
    p = lf.create_prompt(name=name, prompt=req.prompt, type="text", labels=req.labels,
                         config=req.config or {}, commit_message=req.commit_message)
    return {"name": p.name, "version": p.version, "labels": p.labels}


class CompareRequest(BaseModel):
    input: str
    variants: list[dict[str, Any]] = Field(
        description="每个变体可包含 variant_name / prompt_name / prompt_label / prompt_version / "
                    "prompt_override / variables / model / temperature")


@router.post("/playground/compare")
async def playground_compare(req: CompareRequest):
    if not req.variants:
        raise HTTPException(400, "至少需要一个变体")
    return {"results": await run_compare(req.input, req.variants)}


# ------------------------------------------------------------------ 搜索 / Skills
@router.get("/search")
async def search_debug(q: str, topic: str = "general", time_range: str | None = None, max_results: int = 5):
    """直接调用搜索服务，便于调试搜索质量（不经过 LLM）。"""
    try:
        provider, results = await asyncio.to_thread(search, q, max_results=max_results, topic=topic,
                                                    time_range=time_range)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"搜索失败: {e}") from e
    return {"provider": provider, "results": results, "formatted": format_results(results)}


@router.get("/reports")
async def list_reports():
    """报告类 Skill 生成的文件（data/report_workspace/o_outcome）。"""
    from app.agent.report_tools import workspace

    d = workspace() / "o_outcome"
    items = [{"name": p.name, "size": p.stat().st_size, "url": f"/api/reports/{p.name}"}
             for p in sorted(d.glob("*.*"), key=lambda p: p.stat().st_mtime, reverse=True) if not p.name.startswith(".")]
    return {"items": items}


@router.get("/reports/{name}")
async def download_report(name: str):
    from fastapi.responses import FileResponse

    from app.agent.report_tools import workspace

    d = workspace() / "o_outcome"
    p = (d / Path(name).name).resolve()
    if not p.is_relative_to(d) or not p.is_file() or p.name.startswith("."):
        raise HTTPException(404, "报告不存在")
    media = {".html": "text/html; charset=utf-8",
             ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
    return FileResponse(p, media_type=media.get(p.suffix.lower(), "application/octet-stream"), filename=p.name)


@router.get("/ppt/preview/{thread}/{stem}/{name}")
async def ppt_preview(thread: str, stem: str, name: str):
    """ppt_verify 生成的渲染预览图（threads/ppt-<会话>/preview/<文件名>/*.png）。"""
    from fastapi.responses import FileResponse

    from app.agent.report_tools import workspace

    root = (workspace() / "threads").resolve()
    if not thread.startswith("ppt-") or not name.endswith(".png"):
        raise HTTPException(404, "预览不存在")
    p = (root / thread / "preview" / stem / Path(name).name).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        raise HTTPException(404, "预览不存在")
    return FileResponse(p, media_type="image/png")


@router.get("/workspace/files/{path:path}")
async def workspace_file(path: str):
    """工作区 threads/ 下的图片（AI 生图、下载的配图、示意图），供对话页直接显示。只允许图片类型。"""
    from fastapi.responses import FileResponse

    from app.agent.report_tools import workspace

    root = (workspace() / "threads").resolve()
    p = (workspace() / path).resolve()
    media = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
    if not p.is_relative_to(root) or not p.is_file() or p.suffix.lower() not in media:
        raise HTTPException(404, "文件不存在")
    return FileResponse(p, media_type=media[p.suffix.lower()])


@router.get("/ppt/workflow/{session_id}")
async def ppt_workflow_state(session_id: str):
    """本会话 PPT 制作流程状态（阶段、大纲版本、生成与验证记录），前端用来显示进度条。"""
    from app.agent import ppt_workflow as wf

    try:
        d = wf.session_dir(session_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    state = wf.load_state(session_id)
    if not state:
        return {"active": False}
    return {"active": True, "stage": state.get("stage"), "stage_label": wf.STAGES.get(state.get("stage", ""), ""),
            "outline_version": (state.get("outlines") or [{}])[-1].get("version"),
            "approved_version": state.get("approved_version"), "outputs": state.get("outputs", []),
            "last_verify": {k: v for k, v in (state.get("last_verify") or {}).items() if k != "slides"} or None,
            "dir": wf.repo_path(d)}


@router.get("/skills")
async def list_skills():
    return {"items": [{"name": s.name, "description": s.description, "requires": s.requires,
                       "files": s.files} for s in load_skills().values()]}


# ------------------------------------------------------------------ 系统
@router.get("/config")
async def app_config():
    s = get_settings()
    return {
        "app_name": s.app_name, "models": s.model_options, "default_model": s.llm_model,
        "temperature": s.llm_temperature, "langfuse_enabled": s.langfuse_enabled,
        "langfuse_host": s.langfuse_host, "langfuse_project_url": project_url(),
        "prompt_label": s.langfuse_prompt_label,
        "search": search_status(),
        "skills": [sk.name for sk in load_skills().values()],
    }


@router.get("/health")
async def health():
    lf = get_langfuse()
    ok = None
    if lf:
        try:
            ok = lf.auth_check()
        except Exception:  # noqa: BLE001
            ok = False
    return {"status": "ok", "langfuse": ok}
