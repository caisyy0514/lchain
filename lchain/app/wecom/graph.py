"""企业微信群回复 Agent（LangGraph StateGraph），节点与流程图一一对应：

  need_reply ──否──────────────────────────────────────────────────────────┐
     │是                                                                    │
  record_pending（记录当前请求 + 执行状态）                                    │
     │                                                                      │
  parse_docs（img/pdf/文档？→ 推送解析引导语 → deepseek-flash 解析 → 同步到 repo）  │
     │                                                                      │
  load_context（基础信息 + 聊天压缩记录 + 最近 20 轮对话，含卡片/系统判断消息）       │
     │                                                                      │
  plan（是否需要网络检索 / 是否需要检索历史内容）                                  │
     ├── web_search（推送引导消息 → 检索 → 相关性判断 → 总结加载到上下文）           │
     └── history_search（检索历史内容并总结；medex 接入前用本地 MySQL 检索）         │
  assemble（上下文组装）                                                      │
     ├── clarify（信息不足是否需要追问）                                         │
     └── intent（意图识别：严重 / 找医生 / 复杂医疗 / 普通医疗 / 非医疗）             │
  respond（追问 / 简单回复 / 复杂+找医生引导 / 找医生 / 严重+线下就医 / 非医疗@病历经理，推医生卡片）
     │                                                                      │
  finalize（回复则移除待回复记录、写入系统判断消息、记录 run、后台压缩聊天记录） ◄───┘

定时任务（2 分钟群里无人回复 → @病历经理接管、补投递、接管失联节点）见 app/wecom/jobs.py，
由独立的定时任务进程 app/scheduler.py 运行，web 节点不跑定时任务
"""
from __future__ import annotations

import asyncio
import logging
import operator
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph._internal._retry import default_retry_on
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from app.agent.search import search as web_search_api
from app.core.config import get_settings
from app.core.langfuse_client import get_langfuse, make_callback_handler, new_trace_id, trace_url
from app.wecom import store, templates
from app.wecom.compress import compress_in_background
from app.wecom.documents import parse_attachment
from app.wecom.llm import call_json, call_text
from app.wecom.fd import engine as fd_engine
from app.wecom.fd import render as fd_render
from app.wecom.fd import store as fd_store
from app.wecom.fd.contracts import ActionablePlan, PatientPlanView, Urgency
from app.wecom.outbound import crash_point, emit

logger = logging.getLogger(__name__)

INTENTS = {"severe", "find_doctor", "complex_medical", "simple_medical", "public_info", "non_medical"}
INTENT_LABEL = {"severe": "严重情况", "find_doctor": "明确找医生/调整治疗方案", "complex_medical": "复杂医疗问题",
                "simple_medical": "普通医疗问题", "public_info": "公共信息咨询（按联网结果回复）",
                "non_medical": "非医疗问题", "clarify": "信息不足需追问", "family_doctor": "家庭医生分诊"}

# 走家庭医生 Agent（安全门控 + 分级诊疗 + 计划卡）的意图
FD_INTENTS = {"severe", "find_doctor", "complex_medical", "simple_medical"}


class WecomState(TypedDict, total=False):
    run_id: str
    group_id: str
    message_id: int                    # 本轮最后一条消息
    message_ids: list[int]             # 本轮合并处理的全部用户消息（连续发送、尚未回复的）
    overrides: dict[str, str]          # 调试：临时覆盖节点提示词
    force_reply: bool                  # 调试：跳过“是否需要回复”判断
    # 节点产出
    need_reply: bool
    need_reply_reason: str
    docs: list[dict[str, Any]]
    ctx_base: str
    ctx_summary: str
    ctx_recent: str
    ctx_current: str
    plan: dict[str, Any]
    web_result: str
    history_result: str
    context: str
    clarify: dict[str, Any]
    intent: dict[str, Any]
    route: str
    reply: str
    # 家庭医生 Agent
    fd_screen: dict[str, Any]          # 确定性安全初筛结果
    fd_observation: dict[str, Any]     # 当前进行中的居家观察（无则不设置）
    fd_next: str                       # fd_route 计算出的下一跳
    fd: dict[str, Any]                 # 分诊结果（计划 / 患者视图 / 动作标志）
    steps: Annotated[list[dict[str, Any]], operator.add]
    actions: Annotated[list[dict[str, Any]], operator.add]


# --------------------------------------------------------------------------- helpers
def _step(node: str, title: str, data: Any = None, t0: float | None = None) -> dict[str, Any]:
    st = {"node": node, "title": title, "data": data, "at": datetime.now().strftime("%H:%M:%S")}
    if t0 is not None:
        st["ms"] = int((time.perf_counter() - t0) * 1000)
    try:
        get_stream_writer()({"type": "step", **st})
    except Exception:  # noqa: BLE001  非流式调用时没有 writer
        pass
    return st


def _hooks(config: RunnableConfig | None) -> dict[str, Any]:
    return (config or {}).get("configurable", {}).get("wecom_hooks") or {}


def _guide_once(config: RunnableConfig | None, key: str) -> bool:
    """同一批消息的引导语只推一次（被新消息打断合并重跑时不再重复推送）。"""
    fn = _hooks(config).get("guide_once")
    return fn(key) if fn else True


def _commit(config: RunnableConfig | None) -> None:
    """发送正式回复前调用：之后本轮不再被新消息打断。若本轮已被取代，抛 CancelledError。"""
    fn = _hooks(config).get("commit")
    if fn and not fn():
        raise asyncio.CancelledError("superseded by newer messages")


def _idem(state: WecomState, name: str) -> str:
    """幂等键。
    - 引导语（guide:*）按“这批消息的第一条”锚定：打断合并 / 重启后换了 run_id 重新处理同一批消息，也只推一次
      （合并只会往批次里追加更新的消息，第一条不变）
    - 其他动作按 run_id 锚定：同一次执行（含 checkpoint 续跑）只发一次
    """
    if name.startswith("guide:"):
        return f"m{min(state['message_ids'])}:{name}"
    return f"{state['run_id']}:{name}"


async def _emit(state: WecomState, *, key: str | None = None, **kw: Any) -> dict[str, Any]:
    """发送动作。key 为本轮内的动作名，用作幂等键：checkpoint 续跑重放节点时不会重复发送。"""
    msg = await emit(state["group_id"], run_id=state["run_id"], idem_key=_idem(state, key or kw["kind"]), **kw)
    if msg.get("replayed"):
        return msg
    try:
        get_stream_writer()({"type": "action", "message": msg})
    except Exception:  # noqa: BLE001
        pass
    return msg


async def _load(state: WecomState):
    """返回 (群, 本轮最后一条消息)。"""
    g = await store.get_group(state["group_id"])
    m = await store.get_message(state["message_id"])
    assert g is not None and m is not None
    return g, m


async def _batch(state: WecomState):
    return await store.get_messages(state.get("message_ids") or [state["message_id"]])


def _format_current(batch: list[Any], docs: dict[int, list[Any]] | None = None, doc_chars: int = 4000) -> str:
    lines = "\n".join(store.format_message(x, (docs or {}).get(x.id), doc_chars=doc_chars) for x in batch)
    if len(batch) > 1:
        return (f"（用户连续发送了 {len(batch)} 条消息，请把它们当作一个整体理解，一次性回复，不要逐条回复）\n" + lines)
    return lines


def _fd_user_text(batch: list[Any], docs: dict[int, list[Any]] | None = None) -> str:
    """家庭医生安全筛查/分诊只用患者的原始文本 + 文件解读。

    不能用助手自己的历史回复做筛查，避免把已给出的警示误判为患者症状。
    """
    parts: list[str] = []
    for x in batch:
        if x.sender_role in ("assistant", "system"):
            continue
        if (x.content or "").strip():
            parts.append(x.content.strip())
        for d in (docs or {}).get(x.id) or []:
            if d.parsed_text:
                parts.append(d.parsed_text[:1500])
    return "\n".join(parts)


# --------------------------------------------------------------------------- nodes
async def node_need_reply(state: WecomState, config: RunnableConfig) -> dict[str, Any]:
    t0 = time.perf_counter()
    g, m = await _load(state)
    batch = await _batch(state)
    s = get_settings()

    def done(need: bool, reason: str, by: str) -> dict[str, Any]:
        return {"need_reply": need, "need_reply_reason": reason,
                "steps": [_step("need_reply", "判断是否需要回复", {"need_reply": need, "reason": reason, "by": by}, t0)]}

    if m.sender_role in store.STAFF_ROLES:
        n = await store.resolve_pending(group_id=g.group_id, status="replied", resolved_by=m.sender_id or m.sender_role)
        return done(False, f"病历经理/医生发言，不需要助手回复（人工已回复，移除 {n} 条待回复记录）", "rule")
    if m.sender_role in ("assistant", "system"):
        return done(False, "助手/系统消息", "rule")
    if state.get("force_reply"):
        return done(True, "调试：强制回复", "force")
    if any(x.attachments for x in batch):
        return done(True, "用户发送了图片/文件，需要解读", "rule")
    if s.wecom_bot_name and any(f"@{s.wecom_bot_name}" in (x.content or "") or "bot" in (x.mentions or [])
                                for x in batch):
        return done(True, "用户 @ 了助手", "rule")
    if not any((x.content or "").strip() for x in batch):
        return done(False, "空消息", "rule")
    # 家庭医生问诊 / 居家观察进行中：患者/家属的回复必须继续处理，不能被“不需要回复”规则跳过
    # （第 24.4 节；问诊中患者的短回答常被 LLM 判为“不用回”，会把多轮问诊卡死）
    if s.wecom_fd_enabled and m.sender_role in ("patient", "family"):
        from app.wecom.fd import store as fd_store

        try:
            if await fd_store.active_observation(g.group_id):
                return done(True, "居家观察进行中，患者回复需要复评", "rule")
            case = await fd_store.get_case(g.group_id)
            if ((case or {}).get("consultation") or {}).get("status") == "in_progress":
                return done(True, "家庭医生问诊进行中，患者的回答需要继续处理", "rule")
        except Exception as e:  # noqa: BLE001
            logger.warning("检查家庭医生状态失败（不影响主流程）：%s", e)

    recent = await store.recent_messages(g.group_id, batch[0].id, 8)
    user = ("## 最近聊天记录\n" + ("\n".join(store.format_message(x, doc_chars=200) for x in recent) or "（无）")
            + "\n\n## 当前消息\n" + _format_current(batch, doc_chars=200))
    r, _ = await call_json("wecom-need-reply", user, cfg=config, overrides=state.get("overrides"))
    return done(bool(r.get("need_reply", True)), r.get("reason", ""), "llm")


async def node_record_pending(state: WecomState) -> dict[str, Any]:
    ids = await store.claim_pending(state["group_id"], await _batch(state), state["run_id"],
                                    state.get("need_reply_reason", ""))
    return {"steps": [_step("record_pending", "记录当前请求 + 执行状态", {"message_ids": ids, "status": "running"})]}


async def node_parse_docs(state: WecomState, config: RunnableConfig) -> dict[str, Any]:
    g, m = await _load(state)
    batch = await _batch(state)
    # 被打断合并重跑时，已经解析过的文件直接复用
    parsed = await store.documents_for_messages([x.id for x in batch])
    todo = [(x, a) for x in batch if x.id not in parsed for a in (x.attachments or [])]
    reused = [{"file_name": d.file_name, "status": d.status, "error": d.error, "parsed_text": d.parsed_text}
              for ds in parsed.values() for d in ds]
    if not todo and not reused:
        return {"docs": [], "steps": [_step("parse_docs", "判断是否 img/pdf/文档", {"has_file": False})]}
    t0 = time.perf_counter()
    s = get_settings()
    if todo and s.wecom_push_parse_guide and _guide_once(config, "parse"):
        await _emit(state, kind="guide", key="guide:parse",
                    content=templates.PARSE_GUIDE.format(n=len(todo) + len(reused)),
                    mentions=[m.sender_id])
    docs = reused + list(await asyncio.gather(*[
        parse_attachment(a, group_id=g.group_id, message_id=x.id, user_text=x.content, cfg=config,
                         overrides=state.get("overrides")) for x, a in todo]))
    return {"docs": list(docs), "steps": [_step("parse_docs", f"解析文档内容并同步到 repo（{len(docs)} 份）",
                                                 [{"file": d["file_name"], "status": d["status"], "error": d["error"],
                                                   "parsed": d["parsed_text"][:1500]} for d in docs], t0)]}


async def node_load_context(state: WecomState) -> dict[str, Any]:
    g, m = await _load(state)
    batch = await _batch(state)
    batch_ids = {x.id for x in batch}
    s = get_settings()
    after = g.summary_until_id
    # 最近对话 = 本轮最后一条消息之前的记录，去掉本轮合并的消息（放在“当前消息”里）
    recent = [x for x in await store.recent_messages(g.group_id, m.id, s.wecom_recent_messages)
              if x.id > after and x.id not in batch_ids]
    docs = await store.documents_for_messages([x.id for x in recent] + list(batch_ids))
    base = (f"群名称：{g.name}\n病历经理：{g.case_manager_name or '未设置'}\n"
            f"找医生卡片：{'已于 ' + g.doctor_card_pushed_at.strftime('%m-%d %H:%M') + ' 推送过' if g.doctor_card_pushed_at else '尚未推送'}\n"
            f"患者档案：\n{g.profile or '（无）'}")
    recent_txt = "\n".join(store.format_message(x, docs.get(x.id)) for x in recent) or "（无）"
    current = _format_current(batch, docs)
    return {"ctx_base": base, "ctx_summary": g.summary or "（无）", "ctx_recent": recent_txt, "ctx_current": current,
            "steps": [_step("load_context", "基础信息 + 聊天压缩记录 + 最近对话",
                            {"recent_messages": len(recent), "current_messages": len(batch), "has_summary": bool(g.summary),
                             "summary_until_id": after})]}


def _brief_context(state: WecomState) -> str:
    return (f"## 群基础信息\n{state['ctx_base']}\n\n## 聊天摘要\n{state['ctx_summary']}\n\n"
            f"## 最近聊天记录\n{state['ctx_recent']}\n\n## 当前消息\n{state['ctx_current']}")


async def node_plan(state: WecomState, config: RunnableConfig) -> dict[str, Any]:
    t0 = time.perf_counter()
    r, _ = await call_json("wecom-plan", _brief_context(state), cfg=config, overrides=state.get("overrides"))
    plan = {"need_web_search": bool(r.get("need_web_search")), "search_query": r.get("search_query") or "",
            "need_history": bool(r.get("need_history")), "history_keywords": r.get("history_keywords") or [],
            "reason": r.get("reason", "")}
    return {"plan": plan, "steps": [_step("plan", "是否需要网络检索 / 是否需要检索历史内容", plan, t0)]}


async def node_web_search(state: WecomState, config: RunnableConfig) -> dict[str, Any]:
    plan = state.get("plan") or {}
    if not plan.get("need_web_search") or not plan.get("search_query"):
        return {"web_result": ""}
    t0 = time.perf_counter()
    _, m = await _load(state)
    if get_settings().wecom_push_search_guide and _guide_once(config, "search"):
        await _emit(state, kind="guide", key="guide:search", content=templates.SEARCH_GUIDE, mentions=[m.sender_id])
    q = plan["search_query"]
    # 查询词过长/带年份地区时容易无结果：失败后去掉年份等修饰词再试一次
    queries = [q]
    short = " ".join(w for w in q.split() if not w.isdigit())
    if short and short != q:
        queries.append(short)
    provider, results, err = "", [], None
    for qq in queries:
        try:
            provider, results = await asyncio.to_thread(web_search_api, qq, max_results=6)
        except Exception as e:  # noqa: BLE001
            err = e
            continue
        if results:
            q = qq
            break
    if not results and err:
        return {"web_result": "", "steps": [_step("web_search", "联网检索失败", {"query": q, "error": str(err)}, t0)]}
    if not results:
        return {"web_result": "", "steps": [_step("web_search", "联网检索无结果", {"query": q}, t0)]}
    listing = "\n\n".join(f"[{i}] {r.get('title')}\nURL: {r.get('url')}\n{(r.get('snippet') or '')[:400]}"
                          for i, r in enumerate(results, 1))
    judge, _ = await call_json("wecom-search-judge", f"用户问题：\n{state['ctx_current']}\n\n搜索结果：\n{listing}",
                               cfg=config, overrides=state.get("overrides"))
    relevant = bool(judge.get("relevant")) and bool(judge.get("summary"))
    used = [results[i - 1] for i in judge.get("used_sources") or [] if isinstance(i, int) and 0 < i <= len(results)]
    text = judge.get("summary", "") if relevant else ""
    if text and used:
        text += "\n来源：" + "；".join(f"[{results.index(u) + 1}] {u.get('title')}" for u in used)
    return {"web_result": text, "steps": [_step("web_search", "检索内容相关性判断 + 总结加载到上下文", {
        "query": q, "provider": provider, "relevant": relevant, "reason": judge.get("reason"),
        "summary": text, "results": [{"title": r.get("title"), "url": r.get("url")} for r in results]}, t0)]}


async def node_history_search(state: WecomState) -> dict[str, Any]:
    """检索历史内容：medex 暂不可用，先在已压缩（不在最近窗口内）的消息和文件解读里按关键词检索。"""
    plan = state.get("plan") or {}
    if not plan.get("need_history"):
        return {"history_result": ""}
    t0 = time.perf_counter()
    g, m = await _load(state)
    # 只查最近窗口之外的记录，窗口内的已经在上下文中
    recent = await store.recent_messages(g.group_id, state["message_ids"][0], get_settings().wecom_recent_messages)
    upto = (recent[0].id - 1) if recent else m.id - 1
    hits = await store.search_history(g.group_id, plan.get("history_keywords") or [], upto)
    text = "\n".join(f"- [{h['time']}] {h['who']}: {h['content']}" for h in hits)
    return {"history_result": text, "steps": [_step("history_search", "检索历史内容并加入上下文（本地检索，medex 待接入）",
                                                    {"keywords": plan.get("history_keywords"), "hits": len(hits),
                                                     "result": text[:1500]}, t0)]}


async def node_assemble(state: WecomState) -> dict[str, Any]:
    parts = [f"## 群基础信息\n{state['ctx_base']}", f"## 聊天摘要（较早的聊天记录压缩）\n{state['ctx_summary']}",
             f"## 最近聊天记录\n{state['ctx_recent']}"]
    if state.get("web_result"):
        parts.append(f"## 参考资料（联网检索整理，仅供参考）\n{state['web_result']}")
    if state.get("history_result"):
        parts.append(f"## 历史记录检索结果\n{state['history_result']}")
    parts.append(f"## 当前消息（需要你处理）\n{state['ctx_current']}")
    ctx = "\n\n".join(parts)
    return {"context": ctx, "steps": [_step("assemble", "上下文组装", {"chars": len(ctx), "context": ctx})]}


async def node_clarify(state: WecomState, config: RunnableConfig) -> dict[str, Any]:
    t0 = time.perf_counter()
    r, _ = await call_json("wecom-clarify", state["context"], cfg=config, overrides=state.get("overrides"))
    c = {"need_clarify": bool(r.get("need_clarify")), "missing_info": r.get("missing_info") or [],
         "reason": r.get("reason", "")}
    return {"clarify": c, "steps": [_step("clarify", "信息不足是否需要追问", c, t0)]}


async def node_intent(state: WecomState, config: RunnableConfig) -> dict[str, Any]:
    t0 = time.perf_counter()
    r, _ = await call_json("wecom-intent", state["context"], cfg=config, overrides=state.get("overrides"))
    intent = r.get("intent") if r.get("intent") in INTENTS else "simple_medical"
    it = {"intent": intent, "label": INTENT_LABEL[intent], "severity": r.get("severity", "none"),
          "reason": r.get("reason", "")}
    return {"intent": it, "steps": [_step("intent", "意图识别", it, t0)]}


def _route(state: WecomState) -> str:
    intent = state["intent"]["intent"]
    # 医保/药价/流程等公共信息：联网检索到相关结果就直接回复，查不到再转病历经理
    if intent == "public_info":
        return "public_info" if state.get("web_result") else "non_medical"
    # 严重情况优先，不追问；明确找医生 / 非医疗也不需要追问
    if intent in ("severe", "find_doctor", "non_medical"):
        return intent
    if state["clarify"].get("need_clarify"):
        return "clarify"
    return intent


def _fault(node: str) -> None:
    """故障注入（仅用于测试断点续跑）：WECOM_FAULT_INJECT=节点名 时该节点抛异常（不重试）。"""
    if get_settings().wecom_fault_inject == node:
        raise RuntimeError(f"[故障注入] {node} 节点模拟失败")


# --------------------------------------------------------------------------- 家庭医生 Agent
async def node_fd_screen(state: WecomState, config: RunnableConfig) -> dict[str, Any]:
    """确定性安全门控（独立硬门控）：先于检索/意图判断运行。

    - 已知危险信号：立即进入家庭医生急救路径，不等检索、意图判断或完整推理
    - 同时载入当前进行中的居家观察（患者回复时走复评，而不是重新分诊）
    """
    if not get_settings().wecom_fd_enabled:
        return {}
    t0 = time.perf_counter()
    g, _m = await _load(state)
    batch = await _batch(state)
    docs = await store.documents_for_messages([x.id for x in batch])
    user_text = _fd_user_text(batch, docs)
    res = fd_engine.screen(user_text, profile=g.profile or "",
                           emergency_number=get_settings().wecom_fd_emergency_number or "120")
    title = "家庭医生·确定性安全初筛" + ("（命中红旗，立即行动）" if res.emergency else "")
    out: dict[str, Any] = {"fd_screen": res.to_dict(), "steps": [_step(
        "fd_screen", title,
        {"version": res.version, "matched": [m["rule_id"] for m in res.matched],
         "urgency_floor": res.urgency_floor, "modifiers": res.modifiers, "user_text": user_text[:500]}, t0)]}
    try:
        obs = await fd_store.active_observation(g.group_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取居家观察失败：%s", e)
        obs = None
    if obs:
        out["fd_observation"] = obs
    return out


def _after_fd_screen(state: WecomState) -> str:
    if get_settings().wecom_fd_enabled and (state.get("fd_screen") or {}).get("emergency"):
        return "fd_assess"
    return "plan"


async def node_fd_route(state: WecomState) -> dict[str, Any]:
    """家庭医生路由：医疗意图 / 观察复评 → fd_assess（多轮问诊由家庭医生负责）。"""
    nxt = "respond"
    if get_settings().wecom_fd_enabled:
        intent = (state.get("intent") or {}).get("intent")
        if state.get("fd_observation") or intent in FD_INTENTS:
            nxt = "fd_assess"
    return {"fd_next": nxt, "steps": [_step("fd_route", "家庭医生路由", {"next": nxt})]}


async def node_fd_assess(state: WecomState, config: RunnableConfig) -> dict[str, Any]:
    """家庭医生分诊/复评：多轮问诊 + 确定性门控 + 服务端不变量校验 + 计划卡。"""
    t0 = time.perf_counter()
    g, m = await _load(state)
    batch = await _batch(state)
    docs = await store.documents_for_messages([x.id for x in batch])
    user_text = _fd_user_text(batch, docs)
    ctx = state.get("context") or _brief_context(state)
    obs = state.get("fd_observation")
    if obs:
        out = await fd_engine.review_observation(group_id=g.group_id, obs_row=obs, user_text=user_text,
                                                 context=ctx, profile=g.profile or "",
                                                 cfg=config, overrides=state.get("overrides"),
                                                 idem_key=state["run_id"])
    else:
        case = await fd_store.get_case(g.group_id)
        out = await fd_engine.assess(group_id=g.group_id, profile=g.profile or "", user_text=user_text,
                                     context=ctx, cfg=config, overrides=state.get("overrides"),
                                     existing_case_id=(case or {}).get("case_id"), idem_key=state["run_id"],
                                     consultation=(case or {}).get("consultation"))
        if out.consultation is not None:
            await fd_store.save_consultation(g.group_id, out.consultation, case_id=out.case_id)
    intent = (state.get("intent") or {}).get("intent", "")

    # 问诊阶段：只追问，不给结论、不推卡片
    if out.kind == "question":
        data = {"kind": "question", "questions": out.questions, "warnings": out.warnings}
        return {"fd": data, "route": "clarify", "reply": out.text,
                "steps": [_step("fd_assess", "家庭医生问诊：确认关键信息", data, t0)]}

    plan, view = out.plan, out.view
    data = {
        "kind": out.kind, "urgency": plan.urgency, "care_mode": plan.care_mode,
        "case_id": plan.case_id, "plan_version": plan.plan_version, "emergency": out.emergency,
        "notify_manager": out.notify_manager, "observation_id": out.observation_id,
        "warnings": out.warnings, "primary_action": plan.primary_action,
        "missing": plan.missing_critical_data,
        "condition_summary": plan.condition_summary, "risks_to_exclude": plan.risks_to_exclude,
        "plan": plan.model_dump(mode="json"), "view": view.model_dump(mode="json"),
    }
    # route 保持原有意图名（评测/看板兼容）；家庭医生分支用 extra.fd 标识
    route = intent or ("severe" if out.emergency else "family_doctor")
    return {"fd": data, "route": route, "reply": out.text,
            "steps": [_step("fd_assess", f"家庭医生结论：{plan.urgency} / {plan.care_mode}", data, t0)]}


async def node_fd_respond(state: WecomState, config: RunnableConfig) -> dict[str, Any]:
    """发送家庭医生回复 / 追问 + 结构化计划卡（结论由平台真人医生审核，不推找医生卡片）。"""
    t0 = time.perf_counter()
    g, m = await _load(state)
    s = get_settings()
    fd = state["fd"]
    batch = await _batch(state)
    mentions = list(dict.fromkeys(x.sender_id for x in batch if x.sender_id))
    kind = fd.get("kind", "plan")

    if kind == "question":
        text = state.get("reply") or ""
        _commit(config)
        actions = [await _emit(state, key="text", kind="text", content=text, mentions=mentions,
                               sender_name=s.wecom_fd_name,
                               extra={"route": "clarify", "reply_to": m.id,
                                      "reply_to_ids": state["message_ids"],
                                      "fd": {"kind": "question"}})]
        return {"reply": text, "actions": actions,
                "steps": [_step("fd_respond", "家庭医生追问", {"kind": "question"}, t0)]}

    plan = ActionablePlan.model_validate(fd["plan"])
    view = PatientPlanView.model_validate(fd["view"])
    send_card = bool(s.wecom_fd_plan_card and fd.get("view"))
    # 启用结构化计划卡时：完整规划由卡片承载，群里不再重复一遍长文本，只发一句引导；
    # 未启用（纯文本通道）时发完整文本，保证没有卡片也能拿到全部行动与警示。
    text = templates.FD_PLAN_LEAD if send_card else (state.get("reply") or view.chat_text())
    _commit(config)
    actions = [await _emit(state, key="text", kind="text", content=text, mentions=mentions,
                           sender_name=s.wecom_fd_name,
                           extra={"route": "family_doctor", "reply_to": m.id,
                                  "reply_to_ids": state["message_ids"],
                                  "fd": {"urgency": plan.urgency, "care_mode": plan.care_mode,
                                         "case_id": plan.case_id, "plan_version": plan.plan_version,
                                         "release_stage": plan.release_stage}})]
    if send_card:
        actions.append(await _emit(state, key="plan_card", kind="plan_card", content=view.headline,
                                   sender_name=s.wecom_fd_name, extra=fd_render.card_extra(view, plan)))
    if fd.get("notify_manager"):
        raw = " / ".join(x.content for x in batch if x.content)[:200]
        content = templates.SEVERE_NOTIFY.format(sender=m.sender_name or m.sender_id,
                                                 reason=f"家庭医生分诊 {plan.urgency} / {plan.care_mode}",
                                                 content=raw)
        actions.append(await _emit(state, key="notify", kind="notify",
                                   content=f"@{g.case_manager_name or '病历经理'} {content}",
                                   mentions=[g.case_manager_id] if g.case_manager_id else None))
    return {"reply": text, "actions": actions,
            "steps": [_step("fd_respond", "家庭医生结论 + 计划卡" if send_card else "家庭医生结论（纯文本）",
                            {"notify_manager": bool(fd.get("notify_manager")),
                             "urgency": plan.urgency, "doctor_review": bool(plan.doctor_review_note),
                             "plan_card": send_card}, t0)]}


async def node_respond(state: WecomState, config: RunnableConfig) -> dict[str, Any]:
    _fault("respond")
    t0 = time.perf_counter()
    g, m = await _load(state)
    s = get_settings()
    route = _route(state)
    ov = state.get("overrides")
    ctx = state["context"]
    # 续跑重放：本轮如果已经发出过回复 / 卡片，直接复用，不再调用模型、不重复发送
    sent_text = await store.find_by_idem(_idem(state, "text"))
    sent_card = await store.find_by_idem(_idem(state, "doctor_card"))
    card_first = g.doctor_card_pushed_at is None or sent_card is not None
    card_status = (templates.DOCTOR_CARD_THIS_TIME if card_first else
                   templates.DOCTOR_CARD_PUSHED_BEFORE.format(time=g.doctor_card_pushed_at.strftime("%m-%d %H:%M")))
    batch = await _batch(state)
    mentions = list(dict.fromkeys(x.sender_id for x in batch if x.sender_id))
    push_card = notify = False

    if sent_text is not None:
        reply = sent_text.content
        route = (sent_text.extra or {}).get("route", route)
        push_card = sent_card is not None or (card_first and route in ("complex_medical", "find_doctor"))
        notify = route == "severe" and s.wecom_severe_notify_manager
        if route == "non_medical" and g.case_manager_id:
            mentions = [g.case_manager_id]
    elif route == "clarify":
        reply, _ = await call_text("wecom-reply-clarify", ctx, cfg=config, overrides=ov,
                                   missing_info="、".join(state["clarify"].get("missing_info") or []) or "关键细节")
    elif route == "simple_medical":
        reply, _ = await call_text("wecom-reply-simple", ctx, cfg=config, overrides=ov)
    elif route == "complex_medical":
        reply, _ = await call_text("wecom-reply-complex", ctx, cfg=config, overrides=ov, doctor_card_status=card_status)
        push_card = card_first
    elif route == "find_doctor":
        reply, _ = await call_text("wecom-reply-doctor", ctx, cfg=config, overrides=ov, doctor_card_status=card_status)
        push_card = card_first
    elif route == "severe":
        reply, _ = await call_text("wecom-reply-severe", ctx, cfg=config, overrides=ov,
                                   severity=state["intent"].get("severity", "high"))
        notify = s.wecom_severe_notify_manager
    elif route == "public_info":
        reply, _ = await call_text("wecom-reply-public", ctx, cfg=config, overrides=ov)
    else:  # non_medical
        reply, _ = await call_text("wecom-reply-nonmedical", ctx, cfg=config, overrides=ov,
                                   case_manager_name=g.case_manager_name or "病历经理")
        if g.case_manager_id:
            mentions = [g.case_manager_id]
            reply = f"@{g.case_manager_name or g.case_manager_id} {reply}"

    _commit(config)  # 回复即将发出：此后新消息不再打断本轮，而是进入下一轮
    actions = [await _emit(state, kind="text", content=reply, mentions=mentions,
                           extra={"route": route, "reply_to": m.id, "reply_to_ids": state["message_ids"]})]
    if push_card:
        card = {"title": s.wecom_doctor_card_title, "description": s.wecom_doctor_card_desc, "url": s.wecom_doctor_card_url,
                "avatar": s.wecom_doctor_card_avatar, "doctor_name": s.wecom_doctor_card_doctor_name}
        actions.append(await _emit(state, kind="doctor_card", content=f"{card['title']}：{card['description']}",
                                   extra={"card": card}))
        await store.update_group(g.group_id, doctor_card_pushed_at=datetime.now())
    if notify:
        content = templates.SEVERE_NOTIFY.format(sender=m.sender_name or m.sender_id,
                                                 reason=state["intent"].get("reason", ""),
                                                 content=" / ".join(x.content for x in batch if x.content)[:200])
        actions.append(await _emit(state, kind="notify", content=f"@{g.case_manager_name or '病历经理'} {content}",
                                   mentions=[g.case_manager_id] if g.case_manager_id else None))
    return {"route": route, "reply": reply, "actions": actions,
            "steps": [_step("respond", f"回复分支：{INTENT_LABEL.get(route, route)}",
                            {"route": route, "push_doctor_card": push_card, "notify_manager": notify,
                             "doctor_card_first_time": card_first}, t0)]}


async def node_finalize(state: WecomState) -> dict[str, Any]:
    if not state.get("need_reply"):
        # 之前被打断的轮次可能已登记过待回复，这里一并关闭
        await store.resolve_pending_messages(state["message_ids"], "skipped")
        return {"steps": [_step("finalize", "无需回复，结束", None)]}
    n = await store.resolve_pending_messages(
        state["message_ids"], "handover" if state.get("route") == "non_medical" else "replied")
    # 推送的卡片和系统判断消息作为系统消息带入后续上下文
    it = state.get("intent") or {}
    route = state.get("route", "")
    parts = [f"系统判断：{INTENT_LABEL.get(route, route)}"]
    if it.get("severity") and it.get("severity") != "none":
        parts.append(f"严重程度 {it['severity']}")
    kinds = [a.get("extra", {}).get("kind") for a in state.get("actions", [])]
    if "doctor_card" in kinds:
        parts.append("已推送找医生卡片")
    if "notify" in kinds:
        parts.append("已通知病历经理跟进")
    if route == "non_medical":
        parts.append("已转病历经理处理")
    await _emit(state, kind="system", content="，".join(parts), role="system")
    compress_in_background(state["group_id"])
    return {"steps": [_step("finalize", "回复完成：移除待回复记录 / 写入系统判断消息 / 后台压缩聊天记录",
                            {"pending_resolved": n})]}


# --------------------------------------------------------------------------- graph
def _retry_on(exc: Exception) -> bool:
    """LLM / 网络类错误自动重试（超时、限流、5xx、连接断开）；代码错误不重试。"""
    import openai

    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError, openai.RateLimitError,
                        openai.InternalServerError, asyncio.TimeoutError, ConnectionError)):
        return True
    return default_retry_on(exc)


def _with_crash_point(name: str, fn: Any) -> Any:
    """给每个节点加上崩溃演练点 node:<节点名>（WECOM_CRASH_AT 为空时无任何影响）。"""
    import functools
    import inspect

    if "config" in inspect.signature(fn).parameters:
        @functools.wraps(fn)
        async def wrapped(state: WecomState, config: RunnableConfig) -> dict[str, Any]:
            crash_point(f"node:{name}")
            return await fn(state, config)
    else:
        @functools.wraps(fn)
        async def wrapped(state: WecomState) -> dict[str, Any]:  # type: ignore[misc]
            crash_point(f"node:{name}")
            return await fn(state)
    return wrapped


def build_graph(checkpointer: Any = None):
    """checkpointer：LangGraph checkpoint saver（MySQL），每个 super-step 完成后保存状态，用于断点续跑。"""
    retry = RetryPolicy(max_attempts=get_settings().wecom_node_max_attempts, initial_interval=1.0, retry_on=_retry_on)
    g = StateGraph(WecomState)
    for name, fn in [
        ("need_reply", node_need_reply), ("record_pending", node_record_pending), ("parse_docs", node_parse_docs),
        ("load_context", node_load_context), ("fd_screen", node_fd_screen), ("plan", node_plan),
        ("web_search", node_web_search), ("history_search", node_history_search), ("assemble", node_assemble),
        ("clarify", node_clarify), ("intent", node_intent), ("fd_route", node_fd_route),
        ("fd_assess", node_fd_assess), ("fd_respond", node_fd_respond), ("respond", node_respond),
        ("finalize", node_finalize),
    ]:
        g.add_node(name, _with_crash_point(name, fn), retry_policy=retry)

    g.add_edge(START, "need_reply")
    g.add_conditional_edges("need_reply", lambda s: "record_pending" if s.get("need_reply") else "finalize",
                            ["record_pending", "finalize"])
    g.add_edge("record_pending", "parse_docs")
    g.add_edge("parse_docs", "load_context")
    g.add_edge("load_context", "fd_screen")  # 确定性安全门控先跑（毫秒级）
    # 命中红旗：跳过检索/意图判断，直接进入家庭医生急救路径
    g.add_conditional_edges("fd_screen", _after_fd_screen, ["plan", "fd_assess"])
    g.add_edge("plan", "web_search")          # 并行：联网检索
    g.add_edge("plan", "history_search")      # 并行：历史内容检索
    g.add_edge(["web_search", "history_search"], "assemble")
    g.add_edge("assemble", "clarify")         # 并行：是否追问
    g.add_edge("assemble", "intent")          # 并行：意图识别
    g.add_edge(["clarify", "intent"], "fd_route")
    g.add_conditional_edges("fd_route", lambda s: s.get("fd_next", "respond"), ["fd_assess", "respond"])
    g.add_edge("fd_assess", "fd_respond")
    g.add_edge("fd_respond", "finalize")
    g.add_edge("respond", "finalize")
    g.add_edge("finalize", END)
    return g.compile(name="wecom-reply-agent", checkpointer=checkpointer)


_graphs: dict[int, Any] = {}


def get_graph():
    """带 checkpointer 的图（checkpointer 在应用启动时初始化；未就绪时退化为无 checkpoint）。"""
    from app.wecom.checkpoint import get_checkpointer

    cp = get_checkpointer() if get_settings().wecom_checkpoint_enabled else None
    key = id(cp)
    if key not in _graphs:
        _graphs.clear()
        _graphs[key] = build_graph(cp)
    return _graphs[key]


# --------------------------------------------------------------------------- entry
def _run_config(run_id: str, group_id: str, message_ids: list[int], trace_id: str | None,
                overrides: dict[str, str] | None, hooks: dict[str, Any] | None, resumed: bool) -> RunnableConfig:
    handler = make_callback_handler(trace_id)
    tags = ["wecom", *(["prompt:override"] if overrides else []), *(["merged"] if len(message_ids) > 1 else []),
            *(["resumed"] if resumed else [])]
    return {
        "callbacks": [handler] if handler else [],
        "run_name": "wecom-reply-resume" if resumed else "wecom-reply",
        # thread_id = run_id：checkpoint 按执行隔离，续跑时用同一个 thread_id
        "configurable": {"thread_id": run_id, "wecom_hooks": hooks or {}},
        "metadata": {"langfuse_session_id": group_id, "langfuse_user_id": group_id, "langfuse_tags": tags,
                     "run_id": run_id, "message_ids": message_ids},
    }


async def _drive(graph_input: Any, config: RunnableConfig, *, run_id: str, trace_id: str | None,
                 t0: float, base: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    final: dict[str, Any] = dict(base)
    graph = get_graph()
    try:
        async for mode, data in graph.astream(graph_input, config=config, stream_mode=["custom", "values"],
                                              durability="sync"):  # 每一步完成后同步写 checkpoint
            if mode == "custom":
                yield data
            else:
                final = data
        crash_point("run:graph_done")  # 全部节点已完成、尚未记录完成状态
        status = "done" if final.get("need_reply") else "skipped"
        await store.finish_run(run_id, status=status, need_reply=final.get("need_reply"),
                               intent=final.get("route"), steps=final.get("steps"), error=None,
                               actions=[{"id": a["id"], "type": a["msg_type"], "content": a["content"]}
                                        for a in final.get("actions", [])],
                               context=final.get("context"), latency_ms=int((time.perf_counter() - t0) * 1000))
        yield {"type": "done", "run_id": run_id, "status": status, "need_reply": final.get("need_reply"),
               "route": final.get("route"), "reply": final.get("reply"), "actions": final.get("actions", []),
               "latency_ms": int((time.perf_counter() - t0) * 1000), "trace_url": trace_url(trace_id)}
    except asyncio.CancelledError:
        # 被新消息打断：合并到下一轮。待回复记录保留，由下一轮接管；本 run 的 checkpoint 不再需要
        await asyncio.shield(store.finish_run(run_id, status="merged", steps=final.get("steps"),
                                              latency_ms=int((time.perf_counter() - t0) * 1000)))
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("企微群回复 Agent 执行失败（checkpoint 已保存，可续跑）")
        # 节点重试耗尽后失败：状态记为 interrupted，checkpoint 保留，可通过 /runs/{id}/resume 从失败节点继续
        # 待回复记录保持 running，定时任务的超时检查仍会兜底通知病历经理
        await store.finish_run(run_id, status="interrupted", error=f"{type(e).__name__}: {e}",
                               steps=final.get("steps"), latency_ms=int((time.perf_counter() - t0) * 1000))
        yield {"type": "error", "run_id": run_id, "message": f"{type(e).__name__}: {e}", "resumable": True}
    finally:
        lf = get_langfuse()
        if lf:
            lf.flush()


async def run_stream(group_id: str, message_ids: list[int], *, overrides: dict[str, str] | None = None,
                     force_reply: bool = False, hooks: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
    """执行一次群回复流程（message_ids = 本轮合并处理的消息），产出事件：meta / step / action / done / error。

    串行、打断合并由 app/wecom/dispatcher.py 负责；被打断时本函数抛出 CancelledError，run 记为 merged。
    """
    message_ids = sorted(message_ids)
    message_id = message_ids[-1]
    run_id = uuid.uuid4().hex
    trace_id = new_trace_id()
    await store.add_run(run_id, group_id, message_id, trace_id, message_ids,
                        opts={"force_reply": force_reply, "prompt_overrides": overrides or {}})
    yield {"type": "meta", "run_id": run_id, "trace_id": trace_id, "trace_url": trace_url(trace_id)}
    config = _run_config(run_id, group_id, message_ids, trace_id, overrides, hooks, resumed=False)
    init: WecomState = {"run_id": run_id, "group_id": group_id, "message_id": message_id, "message_ids": message_ids,
                        "overrides": overrides or {}, "force_reply": force_reply, "steps": [], "actions": []}
    async for ev in _drive(init, config, run_id=run_id, trace_id=trace_id, t0=time.perf_counter(), base=dict(init)):
        yield ev


async def resume_stream(run_id: str, *, hooks: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
    """从 checkpoint 断点续跑：已完成的节点跳过，从失败 / 未完成的节点继续执行。"""
    run = await store.get_run_row(run_id)
    if run is None:
        raise ValueError(f"run 不存在：{run_id}")
    graph = get_graph()
    if graph.checkpointer is None:
        raise RuntimeError("checkpointer 未就绪，无法续跑")
    message_ids = run.message_ids or [run.message_id]
    opts = run.opts or {}
    trace_id = new_trace_id()
    config = _run_config(run_id, run.group_id, message_ids, trace_id, opts.get("prompt_overrides"), hooks, resumed=True)
    snap = await graph.aget_state(config)
    if not snap or not snap.values:
        raise RuntimeError("没有可用的 checkpoint（可能尚未执行任何节点）")
    if not snap.next:
        raise RuntimeError("该执行已经完成，无需续跑")
    await store.finish_run(run_id, status="running", error=None, resume_count=(run.resume_count or 0) + 1,
                           trace_id=trace_id)
    done_nodes = [st.get("node") for st in snap.values.get("steps", [])]
    yield {"type": "meta", "run_id": run_id, "trace_id": trace_id, "trace_url": trace_url(trace_id), "resumed": True,
           "resume_from": list(snap.next), "completed_nodes": done_nodes,
           "checkpoint_id": snap.config["configurable"].get("checkpoint_id")}
    # 输入为 None = 从最近的 checkpoint 继续
    async for ev in _drive(None, config, run_id=run_id, trace_id=trace_id, t0=time.perf_counter(),
                           base=dict(snap.values)):
        yield ev


async def checkpoint_state(run_id: str) -> dict[str, Any]:
    graph = get_graph()
    if graph.checkpointer is None:
        return {"enabled": False}
    snap = await graph.aget_state({"configurable": {"thread_id": run_id}})
    if not snap or not snap.values:
        return {"enabled": True, "exists": False}
    return {"enabled": True, "exists": True, "next": list(snap.next),
            "checkpoint_id": snap.config["configurable"].get("checkpoint_id"),
            "step": (snap.metadata or {}).get("step"),
            "completed_nodes": [st.get("node") for st in snap.values.get("steps", [])],
            "created_at": snap.created_at}
