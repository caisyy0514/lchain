"""企微群回复 Agent 的跑批（真实模型）：跑一遍用例 → 逐项判定 → 统计 → 存报告 → AI 分析。

一条用例 = 「群档案 + 可选的群历史 + 本轮消息」：
1. 建一个**临时群**（群 id 以 `eval-wecom-` 开头，消息只落库不外发），按用例写好患者档案；
2. 走**生产同一条路径**把消息喂进去（`dispatcher.dispatch`，不 force_reply，need_reply 由模型判断），
   等这一轮真正跑完；`followup` 用例会连发两条，用来验打断合并；
3. 从库里把这一轮产生的消息捞出来，按 `_check_case` 逐项判定；
4. 整组删除临时数据（消息 / 执行记录 / checkpoint），报告里保留回复原文与节点步骤，便于回看。

判定项（`_check_case`，用例里 `expect` 没写的项不参与判定）：
- `intent`   回复分支（route）符合期望（`intent` 严格相等 / `intent_any` 命中一个）
- `reply`    该回的回了一条，不该回的一条都没回
- `card`     是否推「找医生」卡片（首次推、之后不重复推）
- `notify`   是否 @病历经理 发紧急通知（severe）
- `mention`  是否 @病历经理 转人工（non_medical）
- `keywords` 必须出现 / 至少出现一个 / 禁止出现的关键词（禁用词锁的是安全边界）
- `length`   回复字数不超过该分支的上限
- `guide`    是否推引导语（联网检索 / 文件解读前）
- `merged`   连发两条是否合并成一条回复

报告存 `data/report_workspace/evals/wecom-<时间戳>.json`；AI 分析（DeepSeek）追加在 `analysis` 字段。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.config import get_settings
from app.core.llm import build_llm
from app.wecom import dispatcher, store
from app.wecom.cases import CASES, list_sets, load_cases
from app.wecom.templates import SEARCH_GUIDE

logger = logging.getLogger(__name__)

CHECKS = ("intent", "reply", "card", "notify", "mention", "keywords", "length", "guide", "merged")
RUN_TIMEOUT = 240           # 单条用例最长等待时间（秒）
CASE_MANAGER_ID = "cm_eval"  # 评测临时群的病历经理 id（用来判定 @病历经理）


# ------------------------------------------------------------------ 报告存储
def evals_dir() -> Path:
    from app.agent.report_tools import workspace

    d = workspace() / "evals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_report(report: dict[str, Any]) -> Path:
    f = evals_dir() / f"wecom-{report['id']}.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)
    return f


def load_report(eval_id: str) -> dict[str, Any] | None:
    safe = "".join(c for c in (eval_id or "") if c.isalnum() or c in "-_")
    f = evals_dir() / f"wecom-{safe}.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_reports(limit: int = 30) -> list[dict[str, Any]]:
    files = sorted(evals_dir().glob("wecom-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"id": data.get("id"), "created_at": data.get("created_at"), "mode": data.get("mode"),
                    "total": data.get("total"), "passed": data.get("passed"), "duration_ms": data.get("duration_ms"),
                    "model": data.get("model"), "sets": data.get("sets"), "set_label": data.get("set_label"),
                    "has_analysis": bool(data.get("analysis"))})
    return out


# ------------------------------------------------------------------ 判定
def _check_case(case: dict[str, Any], ev: dict[str, Any]) -> dict[str, Any]:
    """逐项判定一条用例是否通过；`expect` 没写的项记为「不适用」，不进通过率。"""
    exp = case["expect"]
    replies: list[str] = [r for r in (ev.get("replies") or []) if r.strip()]
    text = "\n".join(replies)
    route = str(ev.get("route") or "")
    checks: dict[str, bool] = {}
    applicable: list[str] = []
    notes: list[str] = []

    if exp["intent"] or exp["intent_any"]:
        want = exp["intent"] or "、".join(exp["intent_any"])
        checks["intent"] = route == exp["intent"] or route in exp["intent_any"]
        applicable.append("intent")
        if not checks["intent"]:
            notes.append(f"回复分支不符：期望 {want}，实际 {route or '（空）'}")

    checks["reply"] = bool(replies) if exp["need_reply"] else not replies
    applicable.append("reply")
    if exp["need_reply"] and not replies:
        notes.append(f"应该回复但没有回复（status={ev.get('status')}）")
    if not exp["need_reply"] and replies:
        notes.append(f"不该回复却回了一条：{replies[0][:40]}")

    for key, exp_key, actual, label in (("card", "push_card", bool(ev.get("card")), "推「找医生」卡片"),
                                       ("notify", "notify_manager", bool(ev.get("notify")), "@病历经理 紧急通知"),
                                       ("mention", "mention_manager", bool(ev.get("mention_manager")), "@病历经理 转人工")):
        if exp[exp_key] is None:
            continue
        checks[key] = actual == exp[exp_key]
        applicable.append(key)
        if not checks[key]:
            notes.append(f"{'应该' if exp[exp_key] else '不该'}{label}，实际{'做了' if actual else '没做'}")

    if exp["must_include"] or exp["must_include_any"] or exp["must_not_include"]:
        missing = [k for k in exp["must_include"] if k not in text]
        missing_any = list(exp["must_include_any"]) if (exp["must_include_any"] and
                                                        not any(k in text for k in exp["must_include_any"])) else []
        banned = [k for k in exp["must_not_include"] if k in text]
        checks["keywords"] = not missing and not missing_any and not banned
        applicable.append("keywords")
        if missing:
            notes.append(f"回复里没有出现必须有的关键词：{missing}")
        if missing_any:
            notes.append(f"回复里没有出现这些关键词中的任何一个：{missing_any}")
        if banned:
            notes.append(f"回复里出现了禁止词：{banned}")

    if exp["max_chars"]:
        over = [len(r) for r in replies if len(r) > exp["max_chars"]]
        checks["length"] = not over
        applicable.append("length")
        if over:
            notes.append(f"回复超字数上限（上限 {exp['max_chars']} 字，实际 {over}）")

    if exp["push_guide"] is not None:
        checks["guide"] = bool(ev.get("guides")) == exp["push_guide"]
        applicable.append("guide")
        if not checks["guide"]:
            notes.append(f"{'应该' if exp['push_guide'] else '不该'}推引导语，实际{'推了' if ev.get('guides') else '没推'}")

    if exp["merged"]:
        ids = ev.get("message_ids") or []
        checks["merged"] = len(ids) > 1 and len(replies) <= 1
        applicable.append("merged")
        if not checks["merged"]:
            notes.append(f"连发两条没有合并成一条回复（合并消息数 {len(ids)}，回复条数 {len(replies)}）")

    return {"checks": checks, "applicable": applicable, "passed": all(checks.values()) and bool(checks),
            "notes": notes}


# ------------------------------------------------------------------ 真实执行（用例 → 临时群 → 一轮）
async def _prepare_group(case: dict[str, Any], gid: str) -> int:
    """建临时群 + 写档案 + 灌历史消息，返回最后一条消息 id（后面只看它之后的新消息）。"""
    await store.upsert_group(gid, name=f"[评测] {case['name']}", profile=case["profile"],
                            case_manager_id=CASE_MANAGER_ID, case_manager_name=case["case_manager_name"])
    if (case.get("pre") or {}).get("card_pushed"):
        await store.update_group(gid, doctor_card_pushed_at=datetime.now())
    base = 0
    for h in case["history"]:
        role = h["role"]
        assistant = role == "assistant"
        m = await store.add_message(
            group_id=gid, sender_id="bot" if assistant else f"{role}_{h['sender_name'] or role}",
            sender_name=(get_settings().wecom_bot_name if assistant else h["sender_name"] or store.ROLE_LABEL.get(role, role)),
            sender_role=role, msg_type="text", content=h["content"], delivery="none",
            extra={"kind": "text", "history": True})
        base = max(base, m.id)
    return base


async def _drive(case: dict[str, Any], gid: str) -> dict[str, Any]:
    """把用例消息喂进生产路径并等这一轮跑完（followup 用例连发两条，验打断合并）。"""
    role = case["sender_role"]
    texts = [case["message"]] + ([case["followup"]] if case.get("followup") else [])
    futs = []
    for i, text in enumerate(texts):
        m = await store.add_message(group_id=gid, sender_id=f"{role}_{case['sender_name'] or role}",
                                    sender_name=case["sender_name"] or store.ROLE_LABEL.get(role, role),
                                    sender_role=role, msg_type="text", content=text)
        fut = await dispatcher.dispatch(m, {})          # 不 force_reply：是否回复由模型判断
        if fut is not None:
            futs.append(fut)
        if i == 0 and len(texts) > 1:
            await asyncio.sleep(0.2)                    # 第二条紧跟其后，制造「回复还没发出又来消息」
    if not futs:
        return {"status": "skipped", "need_reply": False, "route": "", "steps": [], "message_ids": []}
    try:
        result = await asyncio.wait_for(asyncio.shield(futs[-1]), timeout=RUN_TIMEOUT)
    except asyncio.TimeoutError:
        return {"status": "timeout", "need_reply": None, "route": "", "steps": [], "message_ids": [],
                "error": f"等待执行超过 {RUN_TIMEOUT}s"}
    return result or {}


async def _collect(gid: str, base_id: int, result: dict[str, Any]) -> dict[str, Any]:
    """把这一轮真正发出去的东西收集成「证据」给判定用。"""
    msgs = await store.list_messages(gid, after_id=base_id, limit=500)
    replies: list[str] = []
    guides: list[str] = []
    card = notify = mention = False
    transcript: list[dict[str, str]] = []
    for m in msgs:
        kind = (m.extra or {}).get("kind") or ""
        if m.sender_role == "assistant":
            if kind == "guide":
                guides.append(m.content)
            elif m.msg_type == "doctor_card":
                card = True
            elif kind == "notify":
                notify = True
            else:
                replies.append(m.content)
            transcript.append({"role": ("assistant" if m.msg_type != "doctor_card" else "card"), "content": m.content})
        elif m.sender_role == "patient" or m.sender_role == "family":
            transcript.append({"role": "patient", "content": m.content})
        if CASE_MANAGER_ID in (m.mentions or []):
            mention = True
    steps = [{"node": s.get("node"), "title": s.get("title"), "ms": s.get("ms"),
              "data": _brief_data(s.get("data"))} for s in (result.get("steps") or [])]
    return {
        "status": result.get("status"), "route": result.get("route") or "", "need_reply": result.get("need_reply"),
        "latency_ms": result.get("latency_ms"), "message_ids": result.get("message_ids") or [],
        "steps": steps, "error": result.get("error"),
        "replies": replies, "guides": guides, "card": card, "notify": notify, "mention_manager": mention,
        "transcript": transcript,
    }


async def execute_case(case: dict[str, Any], gid: str) -> dict[str, Any]:
    """真实执行一条用例：建群 → 灌历史 → 跑一轮 → 收集证据。异常在 `_run_case` 里统一兜。"""
    base = await _prepare_group(case, gid)
    result = await _drive(case, gid)
    ev = await _collect(gid, base, result)
    ev["message"] = case["message"]
    return ev


# ------------------------------------------------------------------ 跑批
@dataclass
class RunState:
    id: str
    set_label: str
    total: int
    started_at: str
    done: int = 0
    current: str = ""
    lines: list[str] = field(default_factory=list)
    status: str = "running"          # running / done / failed / cancelled
    report_id: str | None = None
    error: str | None = None
    task: Any = None
    _t0: float = field(default_factory=time.perf_counter)

    def brief(self) -> dict[str, Any]:
        return {"id": self.id, "status": self.status, "sets": self.set_label, "total": self.total,
                "done": self.done, "current": self.current, "report_id": self.report_id, "error": self.error,
                "started_at": self.started_at, "elapsed_ms": int((time.perf_counter() - self._t0) * 1000),
                "lines": self.lines[-60:]}


_current: RunState | None = None


def current_run() -> dict[str, Any]:
    """当前/最近一次跑批的状态（没跑过就是 idle）；前端刷新后靠它恢复界面。"""
    return {"status": "idle"} if _current is None else _current.brief()


def _line_of(event: dict[str, Any]) -> str | None:
    kind = event.get("type")
    total = _current.total if _current else 0
    if kind == "start":
        return f"开始：{event['total']} 条用例（{event.get('model')}，每个用例一个临时群）"
    if kind == "case_start":
        return f"\n[{event['case'] + 1}/{total}] {event['name']}…"
    if kind == "case_done":
        notes = "；".join(event.get("notes") or [])
        detail = f"route={event.get('route') or '-'} {event.get('chars')}字 {event.get('ms')}ms"
        return f"　{'✓' if event['passed'] else '✗'} {detail}{' — ' + notes if notes else ''}"
    if kind == "report":
        r = event["report"]
        return f"\n✅ 完成：{r['passed']}/{r['total']} 通过（{round(r['duration_ms'] / 1000)}s）"
    if kind == "error":
        return f"❌ {event['message']}"
    return None


async def _consume(state: RunState, gen: AsyncIterator[dict[str, Any]]) -> None:
    async for event in gen:
        line = _line_of(event)
        if line:
            state.lines.append(line)
        if event.get("type") == "case_start":
            state.current = event["name"]
        elif event.get("type") == "case_done":
            state.done += 1
            state.current = ""
        elif event.get("type") == "report":
            state.report_id = event["report"]["id"]


async def _runner(state: RunState, sets: list[str], limit: int | None) -> None:
    try:
        await _consume(state, run_eval(set_keys=sets, limit=limit))
        state.status = "done"
    except asyncio.CancelledError:
        state.status = "cancelled"
        state.lines.append("⏹ 已取消（正在跑的那条用例的临时群可能没删干净，清不掉也不影响正常功能）")
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("跑批失败")
        state.status = "failed"
        state.error = f"{type(e).__name__}: {e}"
        state.lines.append(f"❌ {state.error}")


def start_run(*, set_keys: list[str] | None = None, limit: int | None = None) -> dict[str, Any]:
    """后台起一轮跑批（已有任务在跑时抛 RuntimeError → 路由转 409）。跑批任务与浏览器连接解耦，刷新不中断。"""
    global _current
    if _current is not None and _current.status == "running":
        raise RuntimeError("已有一轮企微评测在跑，等它结束或先取消")
    picked = [s for s in (set_keys or []) if s and s != "all"]
    cases = load_cases(set_keys=picked) if picked else CASES
    if limit:
        cases = cases[:limit]
    if not cases:
        raise ValueError("选中的测试集合里没有用例")
    state = RunState(id=datetime.now().strftime("%Y%m%d-%H%M%S"), total=len(cases),
                     set_label="、".join(picked) or "全部", started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    _current = state
    state.task = asyncio.create_task(_runner(state, picked, limit))
    return state.brief()


def cancel_run() -> bool:
    """取消正在跑的批次。"""
    if _current is not None and _current.status == "running" and _current.task is not None:
        _current.task.cancel()
        return True
    return False


async def _run_case(case: dict[str, Any], idx: int, eval_id: str, emit: Callable[[dict[str, Any]], None],
                    executor: Callable[[dict[str, Any], str], Awaitable[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """跑一条用例：临时群 → 执行 → 判定 → 清理。用例之间互不影响，单条失败只记在这条上。"""
    gid = f"{store.EVAL_GROUP_PREFIX}{eval_id}-{case['id']}-{idx}"
    run = executor or execute_case
    t0 = time.perf_counter()
    ev: dict[str, Any] = {}
    error: str | None = None
    try:
        ev = await run(case, gid)
    except Exception as e:  # noqa: BLE001  单条用例失败不影响整轮
        logger.exception("企微评测用例执行失败 case=%s", case["name"])
        error = f"{type(e).__name__}: {e}"
        ev = {"reply": False, "replies": [], "guides": [], "card": False, "notify": False, "mention_manager": False,
              "steps": [], "transcript": [], "route": "", "status": "failed", "error": error}
    finally:
        try:
            await store.delete_groups([gid])       # 评测数据不留库
            dispatcher.forget(gid)
        except Exception:  # noqa: BLE001
            logger.warning("清理评测临时群失败：%s", gid, exc_info=True)
    verdict = _check_case(case, ev)
    emit({"type": "case_done", "case": idx, "name": case["name"], "passed": verdict["passed"],
          "notes": verdict["notes"], "route": ev.get("route"),
          "chars": len("".join(ev.get("replies") or [])), "ms": int((time.perf_counter() - t0) * 1000)})
    return {
        "idx": idx, "case_id": case["id"], "name": case["name"], "set": case["set"], "source": case["source"],
        "message": case["message"], "followup": case.get("followup") or "",
        "sender_role": case["sender_role"], "notes_hint": case["notes"],
        "expect": {k: v for k, v in case["expect"].items() if v not in (None, "", [], 0, False)},
        "route": ev.get("route") or "", "status": ev.get("status"), "need_reply": ev.get("need_reply"),
        "card": bool(ev.get("card")), "notify": bool(ev.get("notify")), "mention_manager": bool(ev.get("mention_manager")),
        "replies": ev.get("replies") or [], "guides": ev.get("guides") or [],
        "reply_chars": [len(r) for r in (ev.get("replies") or [])],
        "message_ids": ev.get("message_ids") or [],
        "duration_ms": int((time.perf_counter() - t0) * 1000),
        "steps": ev.get("steps") or [], "transcript": ev.get("transcript") or [],
        "error": error or ev.get("error"),
        **verdict,
    }


def _brief_data(data: Any, limit: int = 400) -> Any:
    """节点输入/输出截断一下，报告不至于太大。"""
    if data is None:
        return None
    if isinstance(data, str):
        return data[:limit]
    try:
        s = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(data)
    if len(s) <= limit:
        return data
    return s[:limit] + "…"


async def run_eval(*, on_event: Callable[[dict[str, Any]], None] | None = None, limit: int | None = None,
                   set_keys: list[str] | None = None,
                   executor: Callable[[dict[str, Any], str], Awaitable[dict[str, Any]]] | None = None,
                   ) -> AsyncIterator[dict[str, Any]]:
    """跑一轮评测，产出进度事件；最后一个事件是完整报告（type=report）。

    set_keys：只跑某些集合（= `eval/wecom` 下的目录名，可多选）；limit：只跑前 N 条（快速验证）。
    executor：测试用，替换「真实执行一条用例」的那一步。
    """
    emit = on_event or (lambda _e: None)
    picked = [s for s in (set_keys or []) if s and s != "all"]
    cases = load_cases(set_keys=picked) if picked else CASES
    if limit:
        cases = cases[:limit]
    if not cases:
        raise ValueError(f"测试集合 {picked or 'all'} 里没有用例")
    eval_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    model = get_settings().wecom_model or get_settings().llm_model
    t0 = time.perf_counter()
    rows: list[dict[str, Any]] = []
    yield {"type": "start", "id": eval_id, "total": len(cases), "model": model,
           "set": picked or ["all"], "set_label": "、".join(picked) or "全部"}
    for idx, case in enumerate(cases):
        emit({"type": "case_start", "case": idx, "name": case["name"]})
        yield {"type": "case_start", "case": idx, "name": case["name"]}
        row = await _run_case(case, idx, eval_id, emit, executor)
        rows.append(row)
        # 进度事件要**从生成器 yielded 出去**（喂给跑批状态机），只调 emit 的话前端看不到进度
        yield {"type": "case_done", "case": idx, "name": row["name"], "passed": row["passed"],
               "notes": row["notes"], "route": row["route"], "chars": sum(row["reply_chars"]),
               "ms": row["duration_ms"]}
    passed = sum(1 for r in rows if r["passed"])
    report = {
        "id": eval_id, "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "mode": "live", "kind": "wecom",
        "model": model, "total": len(rows), "passed": passed, "failed": len(rows) - passed,
        "sets": picked or ["all"], "set_label": "、".join(picked) or "全部",
        "partial": bool(limit and limit < len(CASES)),
        "duration_ms": int((time.perf_counter() - t0) * 1000),
        "stats": _stats(rows), "cases": rows, "analysis": None,
    }
    save_report(report)
    yield {"type": "report", "report": _brief(report)}


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows) or 1
    ok = [r for r in rows if r["passed"]]
    with_replies = [r for r in rows if r.get("reply_chars")]
    by_check: dict[str, Any] = {}
    for c in CHECKS:
        applicable = [r for r in rows if c in (r.get("applicable") or [])]
        by_check[c] = {"ok": sum(1 for r in applicable if (r.get("checks") or {}).get(c)),
                       "total": len(applicable),
                       "pct": round(sum(1 for r in applicable if (r.get("checks") or {}).get(c)) / len(applicable) * 100)
                       if applicable else None}
    return {
        "pass_rate": round(len(ok) / n * 100),
        "by_check": by_check,
        "avg_reply_chars": round(sum(r.get("reply_chars", [0])[0] for r in with_replies) / max(len(with_replies), 1)),
        "max_reply_chars": max([max(r.get("reply_chars") or [0]) for r in rows] or [0]),
        "avg_reply_count": round(sum(len(r.get("replies") or []) for r in rows) / n, 1),
        "avg_duration_ms": round(sum(r.get("duration_ms", 0) for r in rows) / n),
        "failed": [{"name": r["name"], "route": r.get("route"), "notes": r.get("notes")}
                   for r in rows if not r["passed"]],
    }


def _brief(report: dict[str, Any]) -> dict[str, Any]:
    """列表用的简报（不带 transcript，避免响应过大）。"""
    return {**{k: v for k, v in report.items() if k != "cases"},
            "cases": [{k: v for k, v in c.items() if k != "transcript"} for c in report["cases"]]}


# ------------------------------------------------------------------ AI 分析（DeepSeek）
ANALYZE_PROMPT = """你是 AI 产品评测专家。下面是对「企业微信患者服务群自动回复 Agent」的一轮自动化评测报告。
请用中文分析，指出问题与具体改进建议。报告数据：

{payload}

输出要求（Markdown，400 字以内，不要客套）：
## 结论
一句话：通过率、主要短板（哪个判定项挂得最多）。

## 失败用例归因
逐条说失败用例的原因：回复分支判错 / 该回没回 / 不该回却回了 / 重复推卡片 /
该转人工没转 / 出现禁用词（安全边界） / 超字数 / 没合并连续消息。指出是提示词问题还是流程问题。

## 改进建议
3 条以内、能直接落地（改哪个提示词、改哪个节点 / 判定）；每条一句话。

## 指标观察
回复字数、耗时、回复条数有没有异常。"""


async def analyze_report(report: dict[str, Any]) -> str:
    """用 DeepSeek 分析报告并写回 `analysis` 字段。"""
    payload = {
        "通过率": f"{report['passed']}/{report['total']}",
        "统计": report.get("stats"),
        "用例": [{"名称": c["name"], "期望": c.get("expect"), "实际分支": c.get("route"),
                  "通过": c["passed"], "未通过项": [k for k, v in (c.get("checks") or {}).items() if not v],
                  "回复": [(r[:80] if r else "") for r in (c.get("replies") or [])],
                  "推卡片": c.get("card"), "转人工": c.get("mention_manager") or c.get("notify"),
                  "字数": c.get("reply_chars"), "耗时ms": c.get("duration_ms"),
                  "备注": c.get("notes"), "错误": c.get("error")}
                 for c in report["cases"]],
    }
    llm = build_llm(temperature=0.3)
    resp = await llm.ainvoke([SystemMessage(content="你是严谨的 AI 产品评测专家，只根据给定数据说话。"),
                              HumanMessage(content=ANALYZE_PROMPT.format(
                                  payload=json.dumps(payload, ensure_ascii=False, indent=2)))])
    return resp.text.strip()


__all__ = ["SEARCH_GUIDE", "CASES", "analyze_report", "cancel_run", "current_run", "execute_case",
           "list_reports", "list_sets", "load_report", "run_eval", "save_report", "start_run"]
