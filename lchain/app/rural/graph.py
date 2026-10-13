"""山区远程问诊工作流（LangGraph StateGraph），一次运行 = 村民发一条消息 → 回一条。

    START
  load_state      读会话状态（没有就新建）；记录患者这条消息，并补上一轮的答案
      │
      ├────────────────┬────────────────┐   ← 两个判断节点**并发**（与 app/consult 同一套做法）
  extract            assess
  「这句话里有什么」   「这一轮该做什么」
      └────────────────┴────────────────┘
      │
  decide          合并提取结果 → clamp_action 钳制动作   ← 纯代码节点，不调 LLM
      │
      ├─ ask_history       采集现病史（一次只问一个问题，≤60 字）
      ├─ ask_access        问就医条件（到最近能看病的地方要走多久 / 有没有车 / 有没有人陪）
      ├─ triage            就医建议字段 → render_triage（科室 + 去哪一级 + 怎么去 + 急救要点）
      ├─ ask_differential  鉴别问询（1 个问题）
      ├─ diagnose          诊断字段 → render_diagnosis（就地能做的处理 + 转诊安排）
      ├─ ask_followup      病情变化追问（1 个问题）
      └─ close             结束总结
      │
  save_state      落盘 rural.json：阶段流转、轮次、就医条件、诊断版本、对话记录
      │
     END

为什么和城市问诊分成两个模块而不是加开关：服务对象不同（山区老人 vs 城市患者），
要问的东西（就医条件）、要给出的结论（去哪一级医院、路上注意什么）、
说话方式（一次只问一个、更短、更口语）都不一样，混在一个流程里两边都会变差。

设计要点（与 `app/consult/graph.py` / `app/wecom/graph.py` 同一套）：
- **流程由图决定**，模型只输出 action，由 `gates.clamp_action` 钳制后才执行
- **字数与问题数由配置/代码限制**：max_chars 在提示词 config，问题数由 `limit_questions(text, 1)` 兜底
- **安全边界由代码渲染**：就医建议/诊断文本由 `render.py` 拼装，
  免责声明、急诊提示、急救要点、无药物剂量（`gates.strip_dosage`）都不依赖模型自觉
- 状态存 `threads/rural-<会话id>/rural.json`，服务重启不丢（对话记录也在里面）
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

from app.core.config import get_settings
from app.core.langfuse_client import get_langfuse, make_callback_handler, new_trace_id, trace_url
from app.rural import render
from app.rural.gates import (
    ACCESS_KEYS,
    ACCESS_LABEL,
    DISPOSITIONS,
    MAX_QUESTIONS_PER_TURN,
    MIN_DIFFERENTIAL_ROUNDS,
    MIN_INTAKE_ROUNDS,
    REQUIRED_HISTORY,
    RURAL_RED_FLAGS,
    STAGES,
    URGENCY,
    _bullets,
    clamp_action,
    escalate,
    is_question,
    limit_questions,
    missing_access,
    missing_history,
    note_access_asked,
    note_asked,
    one_line,
    strip_dosage,
)
from app.rural.llm import call_json, model_for, stream_text
from app.rural.state import load_state, new_state, save_state

logger = logging.getLogger(__name__)

URGENCIES = ("routine", "soon", "urgent", "emergency")
FIRST_AID_NAMES = ("蛇咬", "蜂蜇", "农药", "煤气", "烫伤", "出血", "骨折", "惊厥", "临产", "胸痛", "呼吸困难", "意识")


# --------------------------------------------------------------------------- 状态
class RuralState(TypedDict, total=False):
    session_id: str
    user_message: str
    overrides: dict[str, str]
    trace_id: str
    state: dict[str, Any]          # rural.json 的内容（本工作流的唯一数据源）
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
    _emit("tool_start", id=node, name=node, args={})


def _done(node: str, output: str, t0: float, status: str = "success") -> None:
    _emit("tool_end", id=node, name=node, output=(output or "")[:2000], status=status)


def _rec(node: str, out: str = "", t0: float | None = None) -> dict[str, Any]:
    """记录一步（给测试、排查与历史回看用，不发事件）。"""
    step: dict[str, Any] = {"node": node, "out": str(out or "")[:500],
                            "at": datetime.now().strftime("%H:%M:%S")}
    if t0 is not None:
        step["ms"] = int((time.perf_counter() - t0) * 1000)
    return step


def turn_steps(graph_state: RuralState) -> list[dict[str, Any]]:
    """把本轮的节点步骤转成前端工具卡片的统一格式（带耗时）。"""
    return [{"name": s.get("node", ""), "args": {}, "output": s.get("out", ""),
             "status": "success", "ms": s.get("ms")}
            for s in (graph_state.get("steps") or []) if s.get("node")]


# --------------------------------------------------------------------------- 提示词变量
def _fmt_history(history: dict[str, Any]) -> str:
    return "\n".join(f"- {label.split('（')[0]}：{history[key]}"
                     for key, label in REQUIRED_HISTORY if history.get(key))


def _fmt_access(access: dict[str, Any]) -> str:
    return "\n".join(f"- {ACCESS_LABEL[key].split('（')[0]}：{access[key]}"
                     for key in ACCESS_KEYS if access.get(key)) or "（还没问到就医条件）"


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


def _first_aid(name: Any) -> list[dict[str, Any]]:
    """把模型给的急救场景词转成固定的急救要点（内容只来自 gates.FIRST_AID，模型说了不算）。"""
    from app.rural.gates import first_aid_for

    key = str(name or "").strip()
    return first_aid_for([key]) if key in FIRST_AID_NAMES else []


def _base_vars(data: dict[str, Any]) -> dict[str, Any]:
    """所有提示词变量（判断节点与生成节点共用；用不到的变量不影响模板编译）。"""
    s = get_settings()
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
        "missing": "、".join(x.split("（")[0] for x in missing) or "（已采集齐全）",
        "intake_rounds": data.get("record_rounds") or 0,
        "min_intake_rounds": MIN_INTAKE_ROUNDS,
        "access_summary": _fmt_access(data.get("access") or {}),
        "missing_access": "、".join(x.split("（")[0] for x in missing_access(data)) or "（已问齐）",
        "has_triage": "是" if triage else "否",
        "triage_department": triage.get("department") or "（尚未给出就医建议）",
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
        "recent_dialogue": _fmt_turns(data.get("turns") or [], s.rural_recent_turns),
        "max_questions": MAX_QUESTIONS_PER_TURN,
        # ---- 山区特有：当地条件与转运提示，直接写进提示词，让建议落在"做得到"的范围里
        "local_resources": s.rural_local_resources,
        "transport_hint": s.rural_transport_hint,
        "referral_target": s.rural_referral_target,
        "county_hospital": s.rural_county_hospital,
        "emergency_number": s.rural_emergency_number,
        "rural_red_flags": "\n".join(f"  · {x}" for x in RURAL_RED_FLAGS),
    }


# --------------------------------------------------------------------------- 节点
async def node_load_state(graph_state: RuralState, config: RunnableConfig) -> dict[str, Any]:
    """读会话状态（没有就新建）；记录患者这条消息，并补上一轮问题的答案。"""
    t0 = time.perf_counter()
    _start("load_state")
    sid = graph_state["session_id"]
    data = load_state(sid) or new_state(sid)
    message = (graph_state.get("user_message") or "").strip()

    # 第一次发言就是主诉（历史列表与判断提示词都要用），只写一次，不被后续病情变化覆盖
    if not str(data.get("chief_complaint") or "").strip():
        data["chief_complaint"] = message[:80]

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


async def node_extract(graph_state: RuralState, config: RunnableConfig) -> dict[str, Any]:
    """判断节点：从患者这句话里提取现病史要素 / 就医条件 / 危险信号。

    **与 assess 并发**：这里只调 LLM 拿结果，不动状态（合并放在 decide）。
    """
    t0 = time.perf_counter()
    _start("extract")
    extracted, _ = await call_json("rural-extract", graph_state["user_message"], cfg=config,
                                   overrides=graph_state.get("overrides"), **_base_vars(graph_state["state"]))
    new_items = extracted.get("new_items") or {}
    valid = {key for key, _ in REQUIRED_HISTORY} | set(ACCESS_KEYS)
    keys = [k for k in new_items if k in valid] if isinstance(new_items, dict) else []
    flags = [str(f)[:60] for f in (extracted.get("red_flags") or []) if str(f).strip()]
    output = f"要素 {'、'.join(keys) or '无'}；新变化 {'有' if extracted.get('is_new_change') else '无'}"
    if flags:
        output += f"；危险信号 {'、'.join(flags)}"
    _done("extract", output, t0)
    return {"extracted": extracted, "steps": [_rec("extract", output, t0)]}


async def node_assess(graph_state: RuralState, config: RunnableConfig) -> dict[str, Any]:
    """判断节点：这一轮该做什么（**与 extract 并发**，因此看到的是合并前的状态）。"""
    t0 = time.perf_counter()
    _start("assess")
    decision, _ = await call_json("rural-assess", graph_state["user_message"], cfg=config,
                                  overrides=graph_state.get("overrides"), **_base_vars(graph_state["state"]))
    output = f"{decision.get('action') or '?'}｜{decision.get('reason') or '—'}"
    _done("assess", output, t0)
    return {"decision": decision, "steps": [_rec("assess", output, t0)]}


async def node_decide(graph_state: RuralState, config: RunnableConfig) -> dict[str, Any]:
    """extract 与 assess 的汇合点：合并提取结果 → 钳制动作 → 定下本轮动作。"""
    t0 = time.perf_counter()
    _start("decide")
    data = graph_state["state"]
    extracted = graph_state.get("extracted") or {}
    decision = graph_state.get("decision") or {}

    # ---------- 1) 合并 extract 的提取结果（现病史要素 + 就医条件） ----------
    valid = {key for key, _ in REQUIRED_HISTORY}
    new_items = extracted.get("new_items") or {}
    if isinstance(new_items, dict):
        for key, value in new_items.items():
            value = str(value or "").strip()
            if not value:
                continue
            if key in valid:
                data.setdefault("history", {})[key] = value[:300]
            elif key in ACCESS_KEYS:
                data.setdefault("access", {})[key] = value[:200]

    flags = [str(f)[:60] for f in (extracted.get("red_flags") or []) if str(f).strip()]
    if flags:
        data["found_red_flags"] = list(dict.fromkeys([*(data.get("found_red_flags") or []), *flags]))[:10]
    # 意外伤害/中毒现场：把急救场景记下来，后面由 render 追加正确急救做法（模型不算数）
    scene = str(extracted.get("first_aid_scene") or "").strip()
    if scene:
        data["first_aid_scene"] = scene[:20]

    fresh_change = False
    if extracted.get("is_new_change"):
        summary = str(extracted.get("change_summary") or graph_state["user_message"])[:400]
        if data.get("stage") == "followup" and data.get("last_action") == "ask_followup":
            # 这是对上一轮追问的回答：合并进同一次变化，不开启新一轮追问（否则无限追问）
            last = (data.setdefault("changes", []) or [{}])[-1]
            if last:
                last["summary"] = f"{last.get('summary', '')}；{summary}"[:600].strip("；")
        else:
            data.setdefault("changes", []).append({"summary": summary,
                                                   "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            data["followup_answered"] = False
            fresh_change = True
        logger.info("山区问诊记录到病情变化 session=%s：%s", graph_state["session_id"], summary[:60])

    # ---------- 2) 用合并后的状态钳制 assess 给出的动作 ----------
    action, note = clamp_action(data, decision.get("action"),
                                wants_to_stop=bool(extracted.get("wants_to_stop")),
                                fresh_change=fresh_change)
    if note:
        logger.info("山区问诊动作被钳制 session=%s：%s → %s（%s）", graph_state["session_id"],
                    decision.get("action"), action, note)
    output = f"{action}｜{decision.get('reason') or '—'}" + (f"｜已钳制：{note}" if note else "")
    _done("decide", output, t0)
    return {"action": action, "clamp_note": note, "state": data,
            "steps": [_rec("decide", output, t0)]}


async def _stream_reply(prompt_name: str, node: str, graph_state: RuralState, config: RunnableConfig,
                        **extra_vars: Any) -> dict[str, Any]:
    """生成类节点公共部分：流式产出 → 截到 1 个问题 → 合并成一行 → 返回回复文本。"""
    t0 = time.perf_counter()
    _start(node)
    variables = {**_base_vars(graph_state["state"]), **extra_vars}
    parts: list[str] = []
    async for chunk in stream_text(prompt_name, graph_state["user_message"], cfg=config,
                                   overrides=graph_state.get("overrides"), **variables):
        parts.append(chunk)
        _emit("token", content=chunk)
    text = "".join(parts).strip()
    final = one_line(limit_questions(text, MAX_QUESTIONS_PER_TURN))
    if final != text:
        _emit("replace", content=final)   # 已流出前端的文本不对，让前端重渲染
        text = final
    _done(node, text, t0)
    return {"reply": text, "steps": [_rec(node, text, t0)]}


async def node_ask_history(graph_state: RuralState, config: RunnableConfig) -> dict[str, Any]:
    focus = (graph_state.get("decision") or {}).get("focus") or "最关键的缺失要素"
    return await _stream_reply("rural-ask-history", "ask_history", graph_state, config, focus=focus)


async def node_ask_access(graph_state: RuralState, config: RunnableConfig) -> dict[str, Any]:
    """问就医条件（距离/交通/陪同/本地可用）：决定去村卫生室还是直接送县医院。"""
    return await _stream_reply("rural-ask-access", "ask_access", graph_state, config)


async def node_ask_differential(graph_state: RuralState, config: RunnableConfig) -> dict[str, Any]:
    return await _stream_reply("rural-ask-differential", "ask_differential", graph_state, config)


async def node_ask_followup(graph_state: RuralState, config: RunnableConfig) -> dict[str, Any]:
    return await _stream_reply("rural-ask-followup", "ask_followup", graph_state, config)


def _escalate(structured: dict[str, Any], data: dict[str, Any]) -> None:
    """安全兜底：患者说过危险信号、但这步给了轻处置 → 抬紧急程度 + 抬处置层级 + 补急救要点。"""
    found = list(data.get("found_red_flags") or [])
    scene = data.get("first_aid_scene")
    flags = [*found, *([scene] if scene else [])]
    escalate(structured, flags, all_flags=found)
    if found and structured.get("urgency") in ("routine", "soon"):
        structured["urgency"] = "urgent"


async def node_triage(graph_state: RuralState, config: RunnableConfig) -> dict[str, Any]:
    """就医建议：判断节点出字段，render 拼文本（免责声明、急诊提示、急救要点由代码保证）。"""
    t0 = time.perf_counter()
    _start("triage")
    data = graph_state["state"]
    fields, _ = await call_json("rural-triage", graph_state["user_message"], cfg=config,
                                overrides=graph_state.get("overrides"), **_base_vars(data))
    level = str(fields.get("disposition") or "home")
    triage = {
        "department": str(fields.get("department") or "").strip()[:40] or "全科医学科",
        "urgency": fields.get("urgency") if fields.get("urgency") in URGENCIES else "routine",
        "disposition": level if level in DISPOSITIONS else "home",
        "referral_target": str(fields.get("referral_target") or "").strip()[:80],
        "transfer_notes": str(fields.get("transfer_notes") or "").strip()[:120],
        "access_notes": str(fields.get("access_notes") or "").strip()[:120],
        "local_actions": strip_dosage(fields.get("local_actions"), "triage.local_actions")[:3],
        "reasoning": str(fields.get("reasoning") or "").strip()[:600],
        "red_flags": [str(x)[:60] for x in (fields.get("red_flags") or [])][:8],
        "suggested_tests": [str(x)[:60] for x in (fields.get("suggested_tests") or [])][:6],
        "first_aid": _first_aid(fields.get("first_aid")) or _first_aid(data.get("first_aid_scene")),
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _escalate(triage, data)
    data["triage"] = triage
    data["red_flag_screening"] = "；".join(triage["red_flags"]) or "已询问，未发现明显危险信号"
    text = render.render_triage(triage)
    _emit("token", content=text)
    _done("triage", f"{triage['department']}｜{triage['disposition']}", t0)
    return {"reply": text, "state": data,
            "steps": [_rec("triage", f"{triage['department']}｜{DISPOSITIONS[triage['disposition']]}", t0)]}


async def node_diagnose(graph_state: RuralState, config: RunnableConfig) -> dict[str, Any]:
    """诊断 / 更新版诊断：字段由判断节点出，文本由 render 拼装（含就地处理与转诊安排）。"""
    t0 = time.perf_counter()
    data = graph_state["state"]
    is_update = graph_state.get("action") == "re_diagnose"
    node = "re_diagnose" if is_update else "diagnose"
    _start(node)
    version = len(data.get("diagnoses") or []) + 1
    fields, _ = await call_json("rural-diagnose", graph_state["user_message"], cfg=config,
                                overrides=graph_state.get("overrides"),
                                **{**_base_vars(data), "version": version,
                                   "is_update": "是" if is_update else "否"})
    level = str(fields.get("disposition") or "home")
    entry = {
        "version": version,
        "primary": str(fields.get("primary") or "").strip()[:40] or "目前还不能明确，需结合检查判断",
        "differentials": [str(x)[:40] for x in (fields.get("differentials") or [])][:4],
        "evidence": [str(x)[:60] for x in (fields.get("evidence") or [])][:4],
        "severity": str(fields.get("severity") or "").strip()[:40],
        "urgency": fields.get("urgency") if fields.get("urgency") in URGENCIES else "routine",
        "disposition": level if level in DISPOSITIONS else "home",
        "disposition_reason": str(fields.get("disposition_reason") or "").strip()[:60],
        "referral_target": str(fields.get("referral_target") or "").strip()[:60],
        "referral_plan": str(fields.get("referral_plan") or "").strip()[:110],
        "tests": [str(x)[:40] for x in (fields.get("tests") or [])][:4],
        # 只保留**不带剂量**的方向性建议：药名剂量一律不给（strip_dosage 会剔除并记日志）
        "local_management": strip_dosage(fields.get("local_management"), "diagnose.local_management")[:4],
        "red_flags": [str(x)[:40] for x in (fields.get("red_flags") or [])][:4],
        "first_aid": _first_aid(fields.get("first_aid")) or _first_aid(data.get("first_aid_scene")),
        "followup_plan": str(fields.get("followup_plan") or "").strip()[:70] or "症状无缓解或加重请联系村医",
        "confidence": fields.get("confidence") if fields.get("confidence") in ("低", "中", "高") else "中",
        "after_change": is_update,
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _escalate(entry, data)
    data.setdefault("diagnoses", []).append(entry)
    if entry["red_flags"]:
        data["red_flag_screening"] = "；".join(entry["red_flags"])
    text = render.render_diagnosis(entry)
    _emit("token", content=text)
    _done(node, f"v{version}｜{entry['primary']}", t0)
    return {"reply": text, "state": data,
            "steps": [_rec(node, f"第 {version} 版：{entry['primary']}｜{DISPOSITIONS[entry['disposition']]}", t0)]}


async def node_close(graph_state: RuralState, config: RunnableConfig) -> dict[str, Any]:
    t0 = time.perf_counter()
    _start("close")
    text = render.render_close(graph_state["state"])
    _emit("token", content=text)
    _done("close", "问诊已结束", t0)
    return {"reply": text, "steps": [_rec("close", "closed", t0)]}


async def node_save_state(graph_state: RuralState, config: RunnableConfig) -> dict[str, Any]:
    """落盘：阶段流转、轮次、就医条件、诊断版本、对话记录。"""
    t0 = time.perf_counter()
    _start("save_state")
    data = graph_state["state"]
    action = graph_state.get("action") or "ask_history"
    reply = graph_state.get("reply") or ""

    if action == "ask_history":
        data["stage"] = "intake"
        data["record_rounds"] = int(data.get("record_rounds") or 0) + 1
        note_asked(data)   # 同一要素问太多次就记「未明确」，不让流程卡在某个要素上
    elif action == "ask_access":
        data["stage"] = "access"
        note_access_asked(data)   # 就医条件问够次数就按最坏情况考虑，不再纠缠
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
              "trace_id": graph_state.get("trace_id"),
              "trace_url": trace_url(graph_state.get("trace_id")) if graph_state.get("trace_id") else None,
              "steps": [*turn_steps(graph_state),
                        {"name": "save_state", "args": {}, "output": output, "status": "success"}]}]
    data["turns"] = turns[-get_settings().rural_max_turns:]
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_state(graph_state["session_id"], data)
    _done("save_state", output, t0)
    return {"state": data, "steps": [_rec("save_state", output, t0)]}


def questions_of(reply: str) -> list[str]:
    """从回复里抽出问题行，存起来给下一轮判断节点看。"""
    out = [ln.strip()[:200] for ln in (reply or "").split("\n") if ln.strip() and is_question(ln)]
    if not out and reply.strip():
        out = [reply.strip()[:200]]
    return out[:MAX_QUESTIONS_PER_TURN + 1]


# --------------------------------------------------------------------------- 图
def _route(graph_state: RuralState) -> str:
    action = graph_state.get("action") or "ask_history"
    return "diagnose" if action == "re_diagnose" else action


NODES = [
    ("load_state", node_load_state), ("extract", node_extract), ("assess", node_assess),
    ("decide", node_decide),
    ("ask_history", node_ask_history), ("ask_access", node_ask_access), ("triage", node_triage),
    ("ask_differential", node_ask_differential), ("diagnose", node_diagnose),
    ("ask_followup", node_ask_followup), ("close", node_close),
    ("save_state", node_save_state),
]
BRANCHES = ["ask_history", "ask_access", "triage", "ask_differential", "diagnose", "ask_followup", "close"]


def build_graph():
    retry = RetryPolicy(max_attempts=get_settings().rural_node_max_attempts, initial_interval=1.0,
                        retry_on=default_retry_on)
    g = StateGraph(RuralState)
    for name, fn in NODES:
        g.add_node(name, fn, retry_policy=retry)
    g.add_edge(START, "load_state")
    # extract 与 assess 并发（同 app/consult）：评估节点看不到本轮刚提取的要素与新变化，
    # 这两处偏差由 decide 的 clamp_action（fresh_change / 就医条件）补回来。
    g.add_edge("load_state", "extract")
    g.add_edge("load_state", "assess")
    g.add_edge(["extract", "assess"], "decide")
    g.add_conditional_edges("decide", _route, BRANCHES)
    for name in BRANCHES:
        g.add_edge(name, "save_state")
    g.add_edge("save_state", END)
    return g.compile(name="rural-workflow")


_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


# --------------------------------------------------------------------------- 执行入口
# 同一会话的并发保护：两个标签页同时发消息时，避免两个工作流交错读写同一份 rural.json
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(session_id: str) -> asyncio.Lock:
    return _locks.setdefault(session_id, asyncio.Lock())


def _sse(event: str, data: Any) -> dict[str, str]:
    return {"event": event, "data": json.dumps(data, ensure_ascii=False, default=str)}


async def run_stream(*, session_id: str, message: str, trace_id: str | None = None,
                     overrides: dict[str, str] | None = None,
                     user_id: str = "web-user") -> AsyncIterator[dict[str, str]]:
    """跑一轮山区问诊，产出与对话页一致的 SSE 事件：meta / token / replace / tool_start / tool_end / done / error。"""
    trace_id = trace_id or new_trace_id()
    handler = make_callback_handler(trace_id)
    config: RunnableConfig = {
        "configurable": {"thread_id": session_id},
        "callbacks": [handler] if handler else [],
        "run_name": "rural-turn",
        "recursion_limit": 30,
        "metadata": {"langfuse_session_id": session_id, "langfuse_user_id": user_id,
                     "langfuse_tags": ["agent", "rural"]},
    }
    model, _ = model_for("rural-ask-history", None)
    yield _sse("meta", {"trace_id": trace_id, "trace_url": trace_url(trace_id), "model": model,
                        "temperature": 0.3,
                        "prompt": {"name": "rural-workflow", "version": None, "source": "workflow"},
                        "system_prompt": "", "tools": [], "skills": ["rural"]})

    init: RuralState = {"session_id": session_id, "user_message": message,
                        "overrides": overrides or {}, "trace_id": trace_id, "steps": []}
    final: dict[str, Any] = dict(init)
    t0 = time.perf_counter()
    # 同一会话串行（状态文件是读-改-写）；排队时先告知前端，否则用户以为卡死
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
        logger.exception("山区问诊工作流执行失败 session=%s", session_id)
        yield _sse("error", {"message": f"{type(e).__name__}: {e}", "trace_id": trace_id})
    finally:
        lf = get_langfuse()
        if lf:
            await asyncio.to_thread(lf.flush)   # 同步等待上报，放线程里
