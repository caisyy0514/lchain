"""FastAPI 入口：uvicorn app.main:app --reload"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # 需要在导入 langfuse 相关模块之前加载

from app.core.logging_setup import setup_logging  # noqa: E402

setup_logging()  # 尽早初始化：日志写入 LOG_DIR（默认 ./logs）：app.log / error.log / access.log

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app.api.routes import router  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.db import engine, init_db  # noqa: E402
from app.core.langfuse_client import get_langfuse, project_url  # noqa: E402
from app.core.cluster import start_heartbeat, stop_heartbeat  # noqa: E402
from app.consult.routes import router as consult_router  # noqa: E402
from app.rural.routes import router as rural_router  # noqa: E402
from app.wecom.routes import router as wecom_router  # noqa: E402

WEB_DIR = Path(__file__).parent / "web"


@asynccontextmanager
async def lifespan(_: FastAPI):
    if get_langfuse() is not None:
        url = project_url()
        logging.getLogger("app").info("Langfuse 已连接: %s", url or "（认证失败，请检查 key / host）")
    db_ok = await init_db()
    if db_ok:
        from app.wecom.checkpoint import init_checkpointer
        from app.wecom.dispatcher import resume_interrupted_on_startup

        await init_checkpointer()
        await start_heartbeat("web")  # 注册节点心跳；定时任务进程据此发现失联节点并接管其任务
        resumed = await resume_interrupted_on_startup()  # 只恢复本节点上次未完成的任务
        if resumed:
            logging.getLogger("app").info("已续跑 %d 个中断的执行: %s", len(resumed), resumed)
    # 注意：web 节点不运行定时任务，定时任务由独立进程运行：python -m app.scheduler（只部署一个）
    yield
    if db_ok:
        from app.wecom.dispatcher import drain

        await drain(get_settings().shutdown_drain_seconds)  # 优雅停止：等待进行中的任务完成
        await stop_heartbeat()
    from app.wecom.checkpoint import close_checkpointer

    await close_checkpointer()
    await engine.dispose()
    lf = get_langfuse()
    if lf:
        lf.shutdown()  # 退出前把未上报的 trace 刷出去


app = FastAPI(title=get_settings().app_name, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(router)
app.include_router(consult_router)
app.include_router(rural_router)
app.include_router(wecom_router)
class NoCacheStatic(StaticFiles):
    """静态文件禁止缓存：改了 CSS/JS 刷新一定拿到新版本。

    默认没有 Cache-Control 时浏览器按启发式规则长期用本地副本；
    部分内嵌 WebView 对 no-cache 的协商缓存处理不可靠，因此直接 no-store，
    保证「新 HTML 配旧 JS/CSS」或「页面还是旧版卡片」的问题不再出现。
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp


app.mount("/static", NoCacheStatic(directory=WEB_DIR), name="static")

# 页面本身也禁止缓存，否则浏览器会把旧 HTML 和新的静态资源混着用（内嵌 WebView 尤其明显）
HTML_HEADERS = {"Cache-Control": "no-cache, no-store, must-revalidate, max-age=0", "Pragma": "no-cache"}


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(WEB_DIR / "index.html", headers=HTML_HEADERS)


@app.get("/wecom", include_in_schema=False)
async def wecom_page():
    return FileResponse(WEB_DIR / "wecom.html", headers=HTML_HEADERS)


@app.get("/wecom-card/{message_id}", include_in_schema=False)
async def wecom_card_page(message_id: int):
    """就医规划卡 H5（患者手机 / 微信内打开）：渲染群里某条 plan_card，与测试页同一套样式。"""
    return FileResponse(WEB_DIR / "plan-card.html", headers=HTML_HEADERS)


@app.get("/wecom-plan", include_in_schema=False)
async def plan_preview_page():
    """就医规划卡样式预览（示例数据，用于确认患者端卡片设计）。"""
    return FileResponse(WEB_DIR / "plan-preview.html", headers=HTML_HEADERS)


@app.get("/wecom-eval", include_in_schema=False)
async def wecom_eval_page():
    """企微群回复的批量评测报告页（用例在 eval/wecom/**，目录 = 测试集合）。"""
    return FileResponse(WEB_DIR / "wecom-eval.html", headers=HTML_HEADERS)


@app.get("/playground", include_in_schema=False)
async def playground():
    return FileResponse(WEB_DIR / "playground.html", headers=HTML_HEADERS)


@app.get("/eval", include_in_schema=False)
async def eval_page():
    """测试报告页（问诊用例跑批结果、逐例通过判定、统计与 AI 分析）。"""
    return FileResponse(WEB_DIR / "eval.html", headers=HTML_HEADERS)
