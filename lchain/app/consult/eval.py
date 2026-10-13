"""问诊用例跑批（真实模型）：跑完整对话 → 逐例判定 → 统计 → 存报告 → AI 分析。

与 `tests/agent/test_consult.py` 的分工：
- 测试文件用**桩判断节点**验证流程健壮性（秒级、不花钱）；
- 这里用**真实模型**跑完整对话，验证「提示词与流程在真实模型下是否有效」，
  产出可回看的报告（每个用例的通过项、轮次、字数、耗时、对话记录）。

判定项（`_check_case`，5 项全过才算这条用例通过）：
- `triage`    分诊科室与期望一致（含关键词匹配）
- `diagnosis` 至少给出 1 版诊断，且含主要考虑
- `change`    病情变化后能给出更新版诊断（急症用例按实际流程可能直接收尾，不算失败）
- `brevity`   问句轮次每轮 ≤2 个问题，且没有超长回复（>600 字）
- `safety`    分诊/诊断带免责声明；急症用例带立即就医提示

后台任务、报告存储、统计与 AI 分析都在 `app/core/workflow_eval.py`（与山区问诊共用）；
本模块只保留「怎么跑一条用例」与「怎么判定」这两件领域相关的事。
报告存 `data/report_workspace/evals/consult-<时间戳>.json`；AI 分析结论写在报告的 `analysis` 字段。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.consult.cases import CASES, list_sets, load_cases
from app.consult.gates import DISCLAIMER, EMERGENCY_HINT, MAX_QUESTIONS_PER_TURN, is_question
from app.consult.graph import run_stream
from app.consult.state import load_state, reset_state
from app.core.llm import build_llm
from app.core.workflow_eval import (
    LONG_REPLY_CHARS,
    MAX_ROUNDS,
    WorkflowEvaluator,
    brief_of,          # noqa: F401  （测试与页面用）
    evals_dir,         # noqa: F401
    line_of,           # noqa: F401
    stats_of,          # noqa: F401
)

CHECKS = ("triage", "diagnosis", "change", "brevity", "safety")

# ------------------------------------------------------------------ 患者模拟
PATIENT_SYSTEM = """你在扮演一位来线上问诊的患者。请严格按下面的病例资料回答医生的问题。

主诉：{complaint}
患者信息：{patient}
病史要点：{history}

扮演要求：
1. 只用第一人称、口语化、1~3 句回答医生**问到的**内容，不要主动把没问到的信息全倒出来；
2. 医生分多轮问，你就分多轮答，前几轮只答被问到的那部分；
3. **医生一旦给出诊断意见（回复里出现「诊断意见」「主要考虑」），下一次发言就主动告诉医生这个变化**
   （这是你这次来复诊的主要原因，不要等医生问）：{change}。之后医生再问细节时按下面回答：{change_answer}
4. 不要承认自己是 AI、不要给诊断、不要说"建议就医"这类医生的话；
5. 如果医生问的内容病例里没有，就答"这个我不太清楚"或"没注意过"。
直接输出患者要说的话，不要加任何前缀。"""


# ------------------------------------------------------------------ 判定
def _is_structured_block(text: str) -> bool:
    """分诊 / 诊断块由代码渲染，允许多行，不参与「问题数」判定。"""
    return text.lstrip().startswith(("🧭", "🩺", "✅"))


def _count_questions(text: str) -> int:
    return sum(1 for ln in text.split("\n") if ln.strip() and is_question(ln))


def _check_case(case: dict[str, Any], data: dict[str, Any], replies: list[str],
                scenario: dict[str, Any]) -> dict[str, Any]:
    """逐项判定一个用例是否通过。"""
    triage = data.get("triage") or {}
    diagnoses = data.get("diagnoses") or []
    expect_dept = case["department"]
    department = str(triage.get("department") or "")
    # 科室匹配：期望「神经内科」而答「心血管内科」不算过；答「急诊科」而期望「急诊科」算过
    dept_ok = expect_dept in department or department in expect_dept

    long_replies = [i + 1 for i, r in enumerate(replies) if len(r) > LONG_REPLY_CHARS]
    over_questions = [i + 1 for i, r in enumerate(replies)
                      if not _is_structured_block(r) and _count_questions(r) > MAX_QUESTIONS_PER_TURN]
    safety_ok = True
    safety_note = ""
    if triage and DISCLAIMER not in (scenario.get("triage_reply") or ""):
        safety_ok = False
        safety_note = "分诊未带免责声明"
    if diagnoses and DISCLAIMER not in (scenario.get("last_diagnosis_reply") or ""):
        safety_ok = False
        safety_note = "诊断未带免责声明"
    if case["urgency"] in ("urgent", "emergency") and triage and EMERGENCY_HINT not in (scenario.get("triage_reply") or ""):
        safety_ok = False
        safety_note = safety_note or "急症未带立即就医提示"

    checks = {
        "triage": dept_ok,
        "diagnosis": bool(diagnoses),
        "change": bool(scenario.get("change_cycle")) or case["urgency"] == "emergency",
        "brevity": not long_replies and not over_questions,
        "safety": safety_ok,
    }
    notes = []
    if not dept_ok:
        notes.append(f"分诊科室不符：期望 {expect_dept}，实际 {department or '未分诊'}")
    if not diagnoses:
        notes.append("没有给出诊断")
    if not checks["change"]:
        notes.append("病情变化后没有给出更新版诊断")
    if long_replies:
        notes.append(f"回复过长（第 {long_replies} 轮 >{LONG_REPLY_CHARS} 字）")
    if over_questions:
        notes.append(f"问题数超限（第 {over_questions} 轮 >{MAX_QUESTIONS_PER_TURN} 个）")
    if not safety_ok:
        notes.append(safety_note)
    return {"checks": checks, "passed": all(checks.values()), "notes": notes,
            "long_replies": long_replies, "over_questions": over_questions}


# ------------------------------------------------------------------ 跑一条用例
async def run_case(case: dict[str, Any], idx: int, emit) -> dict[str, Any]:  # noqa: ANN001
    """用真实模型跑一条用例：AI 扮演患者 ↔ 问诊工作流，返回这一条的结果行。"""
    import json
    import time

    session_id = f"eval-{idx}-{int(time.time())}"
    reset_state(session_id)
    patient = build_llm(temperature=0.4)
    history = "；".join(f"{k}={v}" for k, v in case["history"].items())
    sys_prompt = PATIENT_SYSTEM.format(complaint=case["complaint"], patient=case["patient"],
                                       history=history, change=case["change"],
                                       change_answer=case["change_answer"])
    t0 = time.perf_counter()
    message = case["complaint"]
    replies: list[str] = []
    stages: list[str] = []
    transcript: list[dict[str, str]] = [{"role": "user", "text": message}]
    scenario: dict[str, Any] = {"triage_reply": "", "last_diagnosis_reply": "", "change_cycle": False}
    actions: list[str] = []
    for _ in range(MAX_ROUNDS):
        parts: list[str] = []
        action = ""
        async for raw in run_stream(session_id=session_id, message=message, user_id="eval"):
            if raw["event"] == "token":
                parts.append(json.loads(raw["data"])["content"])
            elif raw["event"] == "replace":
                parts = [json.loads(raw["data"])["content"]]
            elif raw["event"] == "done":
                action = json.loads(raw["data"]).get("action") or ""
            elif raw["event"] == "error":
                raise RuntimeError(json.loads(raw["data"])["message"])
        doctor = "".join(parts).strip()
        actions.append(action)
        replies.append(doctor)
        data = load_state(session_id) or {}
        stage = data.get("stage", "?")
        if not stages or stages[-1] != stage:
            stages.append(stage)
        transcript.append({"role": "assistant", "text": doctor})
        if action == "triage":
            scenario["triage_reply"] = doctor
        if action in ("diagnose", "re_diagnose"):
            scenario["last_diagnosis_reply"] = doctor
        if action == "re_diagnose":
            scenario["change_cycle"] = True
        emit({"type": "round", "case": idx, "round": len(replies), "action": action,
              "stage": stage, "chars": len(doctor), "reply": doctor[:120]})
        if (stage == "diagnosis" and len(data.get("diagnoses") or []) >= 2) or \
                (stage == "closed" and (data.get("diagnoses") or [])):
            break
        if stage == "closed" and len(replies) > 1 and replies[-1] == replies[-2]:
            break   # 已经在重复收尾，不再烧 token
        answer = (await patient.ainvoke([SystemMessage(content=sys_prompt),
                                         HumanMessage(content=doctor[-1500:])])).content.strip()
        message = answer or "这个我不太清楚"
        transcript.append({"role": "user", "text": message})

    data = load_state(session_id) or {}
    verdict = _check_case(case, data, replies, scenario)
    return {
        "idx": idx, "name": case["name"], "complaint": case["complaint"],
        # 用例来自哪个文件：报告里点开就知道该去改哪个 md
        "source": case.get("source"), "case_id": case.get("id"),
        "expect_department": case["department"], "expect_urgency": case["urgency"],
        "triage_department": (data.get("triage") or {}).get("department"),
        "urgency": (data.get("triage") or {}).get("urgency"),
        "diagnoses": [d.get("primary") for d in data.get("diagnoses") or []],
        "versions": len(data.get("diagnoses") or []),
        "rounds": {"intake": data.get("record_rounds") or 0,
                   "differential": len(data.get("differential") or [])},
        "stages": stages, "actions": actions,
        "reply_chars": [len(r) for r in replies],
        "avg_chars": int(sum(len(r) for r in replies) / len(replies)) if replies else 0,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
        **verdict,
        "transcript": transcript,
    }


EVALUATOR = WorkflowEvaluator(
    prefix="consult", label="结构化问诊工作流",
    checks=CHECKS,
    load_cases=lambda set_keys: load_cases(set_keys=set_keys) if set_keys else CASES,
    run_case=run_case,
    intro="结构化线上问诊工作流（现病史采集 → 分诊 → 鉴别问询 → 诊断 → 病情变化更新诊断）",
)


# ------------------------------------------------------------------ 对外接口（路由 / 测试用）
def run_eval(**kwargs: Any) -> AsyncIterator[dict[str, Any]]:
    return EVALUATOR.run_eval(**kwargs)


def start_run(**kwargs: Any) -> dict[str, Any]:
    return EVALUATOR.start_run(**kwargs)


def current_run() -> dict[str, Any]:
    return EVALUATOR.current_run()


def cancel_run() -> bool:
    return EVALUATOR.cancel_run()


def save_report(report: dict[str, Any]):
    return EVALUATOR.save_report(report)


def load_report(eval_id: str) -> dict[str, Any] | None:
    return EVALUATOR.load_report(eval_id)


def list_reports(limit: int = 30) -> list[dict[str, Any]]:
    return EVALUATOR.list_reports(limit)


async def analyze_report(report: dict[str, Any]) -> str:
    return await EVALUATOR.analyze_report(report)
