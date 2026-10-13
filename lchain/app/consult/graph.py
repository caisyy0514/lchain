"""结构化问诊工作流（LangGraph StateGraph），一次运行 = 患者发一条消息 → 回一条。

    START
  load_state      读会话状态（没有就新建）；把患者这条消息记入对话，并补上一轮的答案
      │
  extract         判断节点 · consult-extract（JSON）——患者这句话里有哪些信息：要素/变化/红旗征
      │
  assess          判断节点 · consult-assess（JSON）——这一轮该做什么
      │           ← 结果经 gates.clamp_action 钳制：模型说错也不会跳过分诊直接下诊断
      ├─ ask_history       consult-ask-history      现病史采集（≤2 问，≤120 字）
      ├─ triage            分诊字段 → render_triage   分诊中间提示（原样发患者）
      ├─ ask_differential  consult-ask-differential  鉴别问询（≤2 问，≤140 字）
      ├─ diagnose          consult-diagnose          诊断 / 更新版诊断字段 → render_diagnosis
      ├─ ask_followup      consult-ask-followup      病情变化追问（≤2 问，≤140 字）
      └─ close             render_close              结束总结
      │
  save_state      落盘 consult.json：阶段流转、轮次、诊断版本、对话记录
      │
     END

设计要点（与 `app/wecom/graph.py` 同一套做法）：
- **流程由图决定**，不由模型决定；判断节点只输出 action，代码钳制后才执行
- **字数与问题数由配置/代码限制**：max_chars 写在提示词 config，问题数由 gates.limit_questions 兜底截断
- **安全边界由代码渲染**：分诊/诊断文本由 render.py 拼装，免责声明与急诊提示不会被模型漏掉
- 状态存 `threads/consult-<会话id>/consult.json`，服务重启不丢（对话记录也在里面，不依赖内存 checkpointer）
"""
from __future__ import annotations

import asyncio
import json
import logging
import operator
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph._internal._retry import default_retry_on
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from app.consult import render
from app.consult.gates import (
    MAX_QUESTIONS_PER_TURN,
    MIN_DIFFERENTIAL_ROUNDS,
    MIN_INTAKE_ROUNDS,
    REQUIRED_HISTORY,
    STAGES,
    clamp_action,
    is_question,
    limit_questions,
    missing_history,
    note_asked,
    one_line,
)
from app.consult.llm import call_json, model_for, stream_text
from app.consult.state import load_state, new_state, save_state
from app.core.config import get_settings
from app.core.langfuse_client import get_langfuse, make_callback_handler, new_trace_id, trace_url

logger = logging.getLogger(__name__)

URGENCIES = ("routine", "soon", "urgent", "emergency")


# --------------------------------------------------------------------------- 状态
class ConsultState(TypedDict, total=False):
    session_id: str
    user_message: str
    overrides: dict[str, str]
    trace_id: str
    state: dict[str, Any]          # consult.json 的内容（本工作流的唯一数据源）
    extracted: dict[str, Any]      # extract 节点产出
    decision: dict[str, Any]       # assess 节点产出
    action: str                    # 钳制后的本轮动作
    clamp_note: str
    reply: str
    steps: Annotated[list[dict[str, Any]], operator.add]


# --------------------------------------------------------------------------- 事件与步骤
def _emit(event: str, **data: Any) -> None:
    """向 run_stream 发一个 SSE 事件（custom 流）。非流式调用时没有 writer，忽略即可。"""
    try:
        get_stream_writer()({"event": event, **data})
    except Exception:  # noqa: BLE001
        pass


def _start(node: str) -> None:
    """通知前端：这个节点开始了（对话页渲染成一张工具卡片）。"""
    _emit("tool_start", id=node, name=node, args={})


def _done(node: str, output: str, t0: float, status: str = "success") -> None:
    _emit("tool_end", id=node, name=node, output=(output or "")[:2000], status=status)


def _rec(node: str, out: str = "", t0: float | None = None) -> dict[str, Any]:
    """记录一步（给测试、排查与**历史回看**用，不发事件）。

    `out` 是这一步的简短说明（节点卡片展开后显示的内容），会随对话记录一起落盘，
    所以点历史会话时能把卡片和 trace 入口一起还原出来。
    """
    step: dict[str, Any] = {"node": node, "out": str(out or "")[:500],
                            "at": datetime.now().strftime("%H:%M:%S")}
    if t0 is not None:
        step["ms"] = int((time.perf_counter() - t0) * 1000)
    return step


def turn_steps(graph_state: ConsultState) -> list[dict[str, Any]]:
    """把本轮的节点步骤转成前端工具卡片的统一格式（与通用对话的 tool 事件一致，带耗时）。"""
    return [{"name": s.get("node", ""), "args": {}, "output": s.get("out", ""),
             "status": "success", "ms": s.get("ms")}
            for s in (graph_state.get("steps") or []) if s.get("node")]


# --------------------------------------------------------------------------- 提示词变量
def _fmt_history(history: dict[str, Any]) -> str:
    return "\n".join(f"- {label.split('（')[0]}：{history[key]}"
                     for key, label in REQUIRED_HISTORY if history.get(key))


def _fmt_turns(turns: list[dict[str, Any]], limit: int) -> str:
    rows = [f"{'患者' if t.get('role') == 'user' else '医生'}：{t.get('text', '')}" for t in (turns or [])[-limit:]]
    return "\n".join(rows) or "（这是本次问诊的第一条消息）"


def _fmt_differential(rounds: list[dict[str, Any]]) -> str:
    rows = []
    for r in rounds or []:
        rows.append(f"- 第 {r.get('round')} 轮问：{'；'.join(r.get('questions') or []) or '（已问）'}")
        if r.get("answers"):
            rows.append(f"  患者答：{r['answers']}")
    return "\n".join(rows) or "（还没有鉴别问询记录）"


def _base_vars(data: dict[str, Any]) -> dict[str, Any]:
    """所有提示词变量（判断节点与生成节点共用；用不到的变量不影响模板编译）。"""
    missing = missing_history(data)
    triage = data.get("triage") or {}
    diagnoses = data.get("diagnoses") or []
    changes = data.get("changes") or []
    asked = [q for r in (data.get("differential") or []) for q in (r.get("questions") or [])]
    return {
        "stage": STAGES.get(data.get("stage", "intake"), ""),
        "chief_complaint": data.get("chief_complaint") or "（未记录）",
        "patient_info": data.get("patient") or "（未提供）",
        "known_history": _fmt_history(data.get("history") or {}) or "（还没有采集到）",
        "missing": "、".join(missing) or "（已采集齐全）",
        "intake_rounds": data.get("record_rounds") or 0,
        "min_intake_rounds": MIN_INTAKE_ROUNDS,
        "has_triage": "是" if triage else "否",
        "triage_department": triage.get("department") or "（尚未分诊）",
        "differential_rounds": len(data.get("differential") or []),
        "min_differential_rounds": MIN_DIFFERENTIAL_ROUNDS,
        "red_flag_screening": data.get("red_flag_screening") or "尚未筛查",
        "diagnosis_versions": len(diagnoses),
        "has_new_change": "是" if changes else "否",
        "followup_answered": "是" if data.get("followup_answered") else "否",
        "version": len(diagnoses) + 1,
        "is_update": "是" if data.get("last_action") in ("ask_followup", "re_diagnose") and diagnoses else "否",
        "change_summary": (changes[-1].get("summary") if changes else "") or "（无）",
        "last_diagnosis": (diagnoses[-1].get("primary") if diagnoses else "") or "（尚未给出诊断）",
        "differential_notes": _fmt_differential(data.get("differential") or []),
        "asked_already": "；".join(asked) or "（还没有问过）",
        "last_questions": "；".join(data.get("last_questions") or []) or "（这是第一轮）",
        "recent_dialogue": _fmt_turns(data.get("turns") or [], get_settings().consult_recent_turns),
        "max_questions": MAX_QUESTIONS_PER_TURN,
    }


# --------------------------------------------------------------------------- 节点
async def node_load_state(graph_state: ConsultState, config: RunnableConfig) -> dict[str, Any]:
    """读会话状态（没有就新建）；把患者这条消息记入对话，并补上一轮问题的答案。"""
    t0 = time.perf_counter()
    _start("load_state")
    sid = graph_state["session_id"]
    data = load_state(sid) or new_state(sid)
    message = (graph_state.get("user_message") or "").strip()

    # 第一次发言就是主诉（历史列表、分诊/诊断提示词都要用），只写一次，不要被后续病情变化覆盖
    if not str(data.get("chief_complaint") or "").strip():
        data["chief_complaint"] = message[:80]

    # 补上一轮问题的答案：让判断节点看到「问了什么 + 答了什么」
    rounds = data.get("differential") or []
    if data.get("stage") == "differential" and rounds and not rounds[-1].get("answers"):
        rounds[-1]["answers"] = message[:800]
    if data.get("stage") == "followup" and not data.get("followup_answered"):
        data["followup_answered"] = True   # 变化已经追问过一轮
    data.setdefault("turns", []).append({"role": "user", "text": message[:1000],
                                         "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

    output = f"当前阶段：{STAGES.get(data.get('stage', 'intake'), '')}"
    _done("load_state", output, t0)
    return {"state": data, "steps": [_rec("load_state", output, t0)]}


async def node_extract(graph_state: ConsultState, config: RunnableConfig) -> dict[str, Any]:
    """判断节点：从患者这句话里提取要素 / 变化 / 红旗征。

    **与 assess 并发执行**：这里只调 LLM 拿结果，不动状态（合并放在 decide 节点）。
    """
    t0 = time.perf_counter()
    _start("extract")
    extracted, _ = await call_json("consult-extract", graph_state["user_message"], cfg=config,
                                   overrides=graph_state.get("overrides"), **_base_vars(graph_state["state"]))
    new_items = extracted.get("new_items") or {}
    valid = {key for key, _ in REQUIRED_HISTORY}
    keys = [k for k in new_items if k in valid] if isinstance(new_items, dict) else []
    flags = [str(f)[:60] for f in (extracted.get("red_flags") or []) if str(f).strip()]
    output = f"要素 {'、'.join(keys) or '无'}；新变化 {'有' if extracted.get('is_new_change') else '无'}"
    if flags:
        output += f"；危险信号 {'、'.join(flags)}"
    _done("extract", output, t0)
    return {"extracted": extracted, "steps": [_rec("extract", output, t0)]}


async def node_assess(graph_state: ConsultState, config: RunnableConfig) -> dict[str, Any]:
    """判断节点：这一轮该做什么。

    **与 extract 并发执行**：因此它看到的是「合并前」的状态（可能少 1 项要素、不知道本轮的新变化）。
    这两个偏差由 decide 节点的 clamp_action 补回来，流程不会跑偏。
    """
    t0 = time.perf_counter()
    _start("assess")
    decision, _ = await call_json("consult-assess", graph_state["user_message"], cfg=config,
                                  overrides=graph_state.get("overrides"), **_base_vars(graph_state["state"]))
    output = f"{decision.get('action') or '?'}｜{decision.get('reason') or '—'}"
    _done("assess", output, t0)
    return {"decision": decision, "steps": [_rec("assess", output, t0)]}


async def node_decide(graph_state: ConsultState, config: RunnableConfig) -> dict[str, Any]:
    """等 extract 与 assess 都回来之后：合并提取结果 → 钳制动作 → 定下本轮动作。

    这是两个并发判断节点的**汇合点**，也是「内容由判断提示词决定、流程安全由代码决定」的落地点。
    """
    t0 = time.perf_counter()
    _start("decide")
    data = graph_state["state"]
    extracted = graph_state.get("extracted") or {}
    decision = graph_state.get("decision") or {}

    # ---------- 1) 合并 extract 的提取结果 ----------
    valid = {key for key, _ in REQUIRED_HISTORY}
    new_items = extracted.get("new_items") or {}
    if isinstance(new_items, dict):
        for key, value in new_items.items():
            value = str(value or "").strip()
            if value and key in valid:
                data.setdefault("history", {})[key] = value[:300]

    flags = [str(f)[:60] for f in (extracted.get("red_flags") or []) if str(f).strip()]
    if flags:
        data["found_red_flags"] = list(dict.fromkeys([*(data.get("found_red_flags") or []), *flags]))[:10]

    fresh_change = False
    if extracted.get("is_new_change"):
        summary = str(extracted.get("change_summary") or graph_state["user_message"])[:400]
        if data.get("stage") == "followup" and data.get("last_action") == "ask_followup":
            # 这是对上一轮追问的**回答**，属于同一次变化：合并进去，不开启新一轮追问（否则会无限追问）
            last = (data.setdefault("changes", []) or [{}])[-1]
            if last:
                last["summary"] = f"{last.get('summary', '')}；{summary}"[:600].strip("；")
        else:
            data.setdefault("changes", []).append({"summary": summary,
                                                   "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            data["followup_answered"] = False   # 新变化需要重新追问一轮
            fresh_change = True
        logger.info("问诊记录到病情变化 session=%s：%s", graph_state["session_id"], summary[:60])

    # ---------- 2) 用合并后的状态钳制 assess 给出的动作 ----------
    action, note = clamp_action(data, decision.get("action"),
                                wants_to_stop=bool(extracted.get("wants_to_stop")),
                                fresh_change=fresh_change)
    if note:
        logger.info("问诊动作被钳制 session=%s：%s → %s（%s）", graph_state["session_id"],
                    decision.get("action"), action, note)
    output = f"{action}｜{decision.get('reason') or '—'}" + (f"｜已钳制：{note}" if note else "")
    _done("decide", output, t0)
    return {"action": action, "clamp_note": note, "state": data,
            "steps": [_rec("decide", output, t0)]}


async def _stream_reply(prompt_name: str, node: str, graph_state: ConsultState, config: RunnableConfig,
                        **extra_vars: Any) -> dict[str, Any]:
    """生成类节点公共部分：流式产出 → 按上限截断问题数 → 返回回复文本。"""
    t0 = time.perf_counter()
    _start(node)
    variables = {**_base_vars(graph_state["state"]), **extra_vars}
    parts: list[str] = []
    async for chunk in stream_text(prompt_name, graph_state["user_message"], cfg=config,
                                   overrides=graph_state.get("overrides"), **variables):
        parts.append(chunk)
        _emit("token", content=chunk)
    text = "".join(parts).strip()
    # 问句节点专用：截到最多 2 个问题，并把多行合并成一行（产品要求：两个问题写在同一行）
    final = one_line(limit_questions(text))
    if final != text:
        # 流出前端的已经不对了，发一个 replace 让前端用修正后的文本重渲染
        _emit("replace", content=final)
        text = final
    _done(node, text, t0)
    return {"reply": text, "steps": [_rec(node, text, t0)]}


async def node_ask_history(graph_state: ConsultState, config: RunnableConfig) -> dict[str, Any]:
    focus = (graph_state.get("decision") or {}).get("focus") or "最关键的缺失要素"
    return await _stream_reply("consult-ask-history", "ask_history", graph_state, config, focus=focus)


async def node_ask_differential(graph_state: ConsultState, config: RunnableConfig) -> dict[str, Any]:
    return await _stream_reply("consult-ask-differential", "ask_differential", graph_state, config)


async def node_ask_followup(graph_state: ConsultState, config: RunnableConfig) -> dict[str, Any]:
    return await _stream_reply("consult-ask-followup", "ask_followup", graph_state, config)


def _escalate(structured: dict[str, Any], found_flags: list[str]) -> None:
    """患者前面说过危险信号、但这一步给了常规就诊：抬到 urgent，避免漏掉急症。"""
    if found_flags and structured.get("urgency") in ("routine", "soon"):
        structured["urgency"] = "urgent"
        structured["red_flags"] = list(dict.fromkeys([*found_flags, *(structured.get("red_flags") or [])]))[:8]


async def node_triage(graph_state: ConsultState, config: RunnableConfig) -> dict[str, Any]:
    """分诊：判断节点出字段，render.py 拼文本（免责声明与急诊提示由代码保证）。"""
    t0 = time.perf_counter()
    _start("triage")
    data = graph_state["state"]
    fields, _ = await call_json("consult-triage", graph_state["user_message"], cfg=config,
                                overrides=graph_state.get("overrides"), **_base_vars(data))
    triage = {
        "department": str(fields.get("department") or "").strip()[:40] or "全科医学科",
        "reasoning": str(fields.get("reasoning") or "").strip()[:600],
        "urgency": fields.get("urgency") if fields.get("urgency") in URGENCIES else "routine",
        "red_flags": [str(x)[:60] for x in (fields.get("red_flags") or [])][:8],
        "suggested_tests": [str(x)[:60] for x in (fields.get("suggested_tests") or [])][:6],
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _escalate(triage, data.get("found_red_flags") or [])
    data["triage"] = triage
    data["red_flag_screening"] = "；".join(triage["red_flags"]) or "已询问，未发现明显红旗征"
    text = render.render_triage(triage)
    _emit("token", content=text)
    _done("triage", f"{triage['department']}｜{triage['urgency']}", t0)
    return {"reply": text, "state": data, "steps": [_rec("triage", f"科室 {triage['department']}｜{triage['urgency']}", t0)]}


async def node_diagnose(graph_state: ConsultState, config: RunnableConfig) -> dict[str, Any]:
    """诊断 / 更新版诊断：判断节点出字段，render.py 拼文本；安全边界由代码保证。"""
    t0 = time.perf_counter()
    data = graph_state["state"]
    is_update = graph_state.get("action") == "re_diagnose"
    node = "re_diagnose" if is_update else "diagnose"
    _start(node)
    version = len(data.get("diagnoses") or []) + 1
    fields, _ = await call_json("consult-diagnose", graph_state["user_message"], cfg=config,
                                overrides=graph_state.get("overrides"),
                                **{**_base_vars(data), "version": version,
                                   "is_update": "是" if is_update else "否"})
    entry = {
        "version": version,
        "primary": str(fields.get("primary") or "").strip()[:80] or "目前还不能明确，需结合检查判断",
        "differentials": [str(x)[:40] for x in (fields.get("differentials") or [])][:4],
        "evidence": [str(x)[:60] for x in (fields.get("evidence") or [])][:4],
        "severity": str(fields.get("severity") or "").strip()[:60],
        "urgency": fields.get("urgency") if fields.get("urgency") in URGENCIES else "routine",
        "tests": [str(x)[:40] for x in (fields.get("tests") or [])][:4],
        "management": [str(x)[:50] for x in (fields.get("management") or [])][:4],
        "red_flags": [str(x)[:40] for x in (fields.get("red_flags") or [])][:4],
        "followup_plan": str(fields.get("followup_plan") or "").strip()[:120] or "症状无缓解或加重请及时复诊",
        "confidence": fields.get("confidence") if fields.get("confidence") in ("低", "中", "高") else "中",
        "after_change": is_update,
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _escalate(entry, data.get("found_red_flags") or [])
    data.setdefault("diagnoses", []).append(entry)
    if entry["red_flags"]:
        data["red_flag_screening"] = "；".join(entry["red_flags"])
    text = render.render_diagnosis(entry)
    _emit("token", content=text)
    _done(node, f"v{version}｜{entry['primary']}", t0)
    return {"reply": text, "state": data,
            "steps": [_rec(node, f"第 {version} 版：{entry['primary']}", t0)]}


async def node_close(graph_state: ConsultState, config: RunnableConfig) -> dict[str, Any]:
    t0 = time.perf_counter()
    _start("close")
    text = render.render_close(graph_state["state"])
    _emit("token", content=text)
    _done("close", "问诊已结束", t0)
    return {"reply": text, "steps": [_rec("close", "closed", t0)]}


async def node_save_state(graph_state: ConsultState, config: RunnableConfig) -> dict[str, Any]:
    """落盘：阶段流转、轮次、对话记录。"""
    t0 = time.perf_counter()
    _start("save_state")
    data = graph_state["state"]
    action = graph_state.get("action") or "ask_history"
    reply = graph_state.get("reply") or ""

    if action == "ask_history":
        data["stage"] = "intake"
        data["record_rounds"] = int(data.get("record_rounds") or 0) + 1
        note_asked(data)   # 同一要素问太多次就记「未明确」，不让流程卡在某个要素上
    elif action == "triage":
        data["stage"] = "differential"
    elif action == "ask_differential":
        data["stage"] = "differential"
        rounds = data.setdefault("differential", [])
        rounds.append({"round": len(rounds) + 1, "questions": questions_of(reply),
                       "reason": (graph_state.get("decision") or {}).get("reason", ""),
                       "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    elif action in ("diagnose", "re_diagnose"):
        data["stage"] = "diagnosis"
    elif action == "ask_followup":
        data["stage"] = "followup"
        data["followup_answered"] = False
    elif action == "close":
        data["stage"] = "closed"

    data["last_action"] = action
    data["last_questions"] = questions_of(reply)
    output = f"阶段 {STAGES.get(data.get('stage'), '')}｜动作 {action}"
    turns = [*(data.get("turns") or []),
             {"role": "assistant", "text": reply[:2000], "action": action,
              "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              # trace 入口 + 本轮节点步骤：点历史会话时能把卡片与 Langfuse 链接一起还原
              "trace_id": graph_state.get("trace_id"),
              "trace_url": trace_url(graph_state.get("trace_id")) if graph_state.get("trace_id") else None,
              # 末尾补上 save_state 自己（它还没返回，不补就与实时看到的卡片不一致）
              "steps": [*turn_steps(graph_state),
                        {"name": "save_state", "args": {}, "output": output, "status": "success"}]}]
    data["turns"] = turns[-get_settings().consult_max_turns:]
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_state(graph_state["session_id"], data)
    _done("save_state", output, t0)
    return {"state": data, "steps": [_rec("save_state", output, t0)]}


def questions_of(reply: str) -> list[str]:
    """从回复里抽出问题行（一行一个问题），存起来给下一轮判断节点看。"""
    out = [ln.strip()[:200] for ln in (reply or "").split("\n") if ln.strip() and is_question(ln)]
    if not out and reply.strip():
        out = [reply.strip()[:200]]
    return out[:MAX_QUESTIONS_PER_TURN + 1]


# --------------------------------------------------------------------------- 图
def _route(graph_state: ConsultState) -> str:
    action = graph_state.get("action") or "ask_history"
    return "diagnose" if action == "re_diagnose" else action


NODES = [
    ("load_state", node_load_state), ("extract", node_extract), ("assess", node_assess),
    ("decide", node_decide),
    ("ask_history", node_ask_history), ("triage", node_triage), ("ask_differential", node_ask_differential),
    ("diagnose", node_diagnose), ("ask_followup", node_ask_followup), ("close", node_close),
    ("save_state", node_save_state),
]
BRANCHES = ["ask_history", "triage", "ask_differential", "diagnose", "ask_followup", "close"]


def build_graph():
    retry = RetryPolicy(max_attempts=get_settings().consult_node_max_attempts, initial_interval=1.0,
                        retry_on=default_retry_on)
    g = StateGraph(ConsultState)
    for name, fn in NODES:
        g.add_node(name, fn, retry_policy=retry)
    g.add_edge(START, "load_state")
    # extract 与 assess **并发**：assess 只需要“阶段/轮次/分诊/诊断”这些代码算好的信号，
    # 它对 extract 的依赖（要素齐否、本轮有无新变化）在 decide 里用 clamp_action 补回来。
    # 两个判断节点占单轮耗时的 ~73%，并发后省掉其中较短的一个（实测 ~1.4s/轮）。
    g.add_edge("load_state", "extract")
    g.add_edge("load_state", "assess")
    g.add_edge(["extract", "assess"], "decide")
    g.add_conditional_edges("decide", _route, BRANCHES)
    for name in BRANCHES:
        g.add_edge(name, "save_state")
    g.add_edge("save_state", END)
    return g.compile(name="consult-workflow")


_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


# --------------------------------------------------------------------------- 执行入口
# 同一会话的并发保护：两个标签页同时发消息时，避免两个工作流交错读写同一份 consult.json
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(session_id: str) -> asyncio.Lock:
    return _locks.setdefault(session_id, asyncio.Lock())


def _sse(event: str, data: Any) -> dict[str, str]:
    return {"event": event, "data": json.dumps(data, ensure_ascii=False, default=str)}


async def run_stream(*, session_id: str, message: str, trace_id: str | None = None,
                     overrides: dict[str, str] | None = None,
                     user_id: str = "web-user") -> AsyncIterator[dict[str, str]]:
    """跑一轮问诊，产出与对话页一致的 SSE 事件：meta / token / replace / tool_start / tool_end / done / error。"""
    trace_id = trace_id or new_trace_id()
    handler = make_callback_handler(trace_id)
    config: RunnableConfig = {
        "configurable": {"thread_id": session_id},
        "callbacks": [handler] if handler else [],
        "run_name": "consult-turn",
        "recursion_limit": 30,
        "metadata": {"langfuse_session_id": session_id, "langfuse_user_id": user_id,
                     "langfuse_tags": ["agent", "consult"]},
    }
    model, _ = model_for("consult-ask-history", None)
    yield _sse("meta", {"trace_id": trace_id, "trace_url": trace_url(trace_id), "model": model,
                        "temperature": 0.3,
                        "prompt": {"name": "consult-workflow", "version": None, "source": "workflow"},
                        "system_prompt": "", "tools": [], "skills": ["consult"]})

    init: ConsultState = {"session_id": session_id, "user_message": message,
                          "overrides": overrides or {}, "trace_id": trace_id, "steps": []}
    final: dict[str, Any] = dict(init)
    t0 = time.perf_counter()
    # 同一会话串行（状态文件是读-改-写，不能并发）；若上一个请求还在跑，先告知前端在排队，
    # 否则用户会觉得“卡死了”（多个标签页共用同一个 session_id 时会遇到）。
    lock = _lock_for(session_id)
    if lock.locked():
        yield _sse("queued", {"message": "这个会话上一条消息还在处理中，你在排队，稍等一下…"})
    try:
        async with lock:
            async for mode, data in get_graph().astream(init, config=config, stream_mode=["custom", "values"]):
                if mode == "custom" and isinstance(data, dict):
                    payload = dict(data)
                    event = payload.pop("event", None)
                    if event:
                        yield _sse(event, payload)
                elif mode == "values" and isinstance(data, dict):
                    final = data
        yield _sse("done", {"trace_id": trace_id, "trace_url": trace_url(trace_id), "usage": None,
                            "action": final.get("action"),
                            "stage": (final.get("state") or {}).get("stage"),
                            "latency_ms": int((time.perf_counter() - t0) * 1000)})
    except Exception as e:  # noqa: BLE001
        logger.exception("问诊工作流执行失败 session=%s", session_id)
        yield _sse("error", {"message": f"{type(e).__name__}: {e}", "trace_id": trace_id})
    finally:
        lf = get_langfuse()
        if lf:
            await asyncio.to_thread(lf.flush)   # 同步等待上报，放线程里
