"""问诊工作流真实端到端冒烟：用真实模型扮演「医生工作流 + 患者」，把病例跑完整流程。

`tests/agent/test_consult.py` 用桩判断节点验证流程健壮性（不起模型、秒级完成）；
`app/consult/eval.py` 是同一思路的完整评测（带通过判定与报告，在对话页右侧面板跑）；
本脚本是最轻的命令行版本，用来快速看某个病例的阶段推进与每轮字数。

依赖真实 LLM（消耗 token），默认只跑第 1 个病例：
    PYTHONPATH=. .venv/bin/python -m scripts.consult_smoke            # 跑病例 0
    PYTHONPATH=. .venv/bin/python -m scripts.consult_smoke --case 3   # 跑指定病例
    PYTHONPATH=. .venv/bin/python -m scripts.consult_smoke --all      # 跑全部 10 个病例

病例库在 `app/consult/cases.py`（单一数据源）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from langchain_core.messages import HumanMessage, SystemMessage

from app.consult import load_state, reset_state
from app.consult.cases import CASES
from app.consult.graph import run_stream
from app.core.llm import build_llm


PATIENT_SYSTEM = """你在扮演一位来线上问诊的患者。请严格按下面的病例资料回答医生的问题。

主诉：{complaint}
患者信息：{patient}
病史要点：{history}

扮演要求：
1. 只用第一人称、口语化、1~3 句回答医生**问到的**内容，不要主动把没问到的信息全倒出来；
2. 医生分多轮问，你就分多轮答，前几轮只答被问到的那部分；
3. 病情变化要按这个来：{change}。当医生问起变化时，回答：{change_answer}
4. 不要承认自己是 AI、不要给诊断、不要说"建议就医"这类医生的话；
5. 如果医生问的内容病例里没有，就答"这个我不太清楚"或"没注意过"。
直接输出患者要说的话，不要加任何前缀。"""


async def one_turn(session_id: str, message: str) -> tuple[str, dict]:
    """跑一轮工作流，返回 (医生的回复文本, done 事件数据)。"""
    parts: list[str] = []
    done: dict = {}
    async for raw in run_stream(session_id=session_id, message=message):
        if raw["event"] == "token":
            parts.append(json.loads(raw["data"])["content"])
        elif raw["event"] == "replace":      # 问题数超限时工作流会发截断后的文本
            parts = [json.loads(raw["data"])["content"]]
        elif raw["event"] == "done":
            done = json.loads(raw["data"])
        elif raw["event"] == "error":
            raise RuntimeError(json.loads(raw["data"])["message"])
    return "".join(parts), done


async def run_case(case: dict, idx: int, max_rounds: int) -> dict:
    session_id = f"consult-smoke-{idx}-{int(time.time())}"
    reset_state(session_id)
    patient = build_llm(temperature=0.4)
    history = "；".join(f"{k}={v}" for k, v in case["history"].items())
    sys_prompt = PATIENT_SYSTEM.format(complaint=case["complaint"], patient=case["patient"],
                                       history=history, change=case["change"],
                                       change_answer=case["change_answer"])

    message = case["complaint"]
    stages: list[str] = []
    lengths: list[int] = []
    for r in range(max_rounds):
        try:
            doctor, done = await one_turn(session_id, message)
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ 第 {r + 1} 轮失败：{type(e).__name__}: {e}")
            break
        data = load_state(session_id) or {}
        stage = data.get("stage", "?")
        if not stages or stages[-1] != stage:
            stages.append(stage)
        lengths.append(len(doctor))
        diag = len(data.get("diagnoses") or [])
        print(f"    round {r + 1:2d} | {stage:<12} | 诊断 v{diag} | {len(doctor):4d} 字 | {doctor[:56]!r}")
        if (stage == "diagnosis" and diag >= 2) or (stage == "closed" and diag >= 1):
            break
        if stage == "closed" and lengths[-1] == (lengths[-2] if len(lengths) > 1 else None):
            break   # 已经在重复收尾了，没必要继续烧 token
        answer = (await patient.ainvoke([SystemMessage(content=sys_prompt),
                                         HumanMessage(content=doctor[-1500:])])).content.strip()
        message = answer or "这个我不太清楚"

    data = load_state(session_id) or {}
    return {"session": session_id, "stages": stages, "lengths": lengths,
            "triage": (data.get("triage") or {}).get("department"),
            "diagnoses": [d["primary"] for d in data.get("diagnoses") or []],
            "rounds": {"intake": data.get("record_rounds"), "differential": len(data.get("differential") or [])}}


async def main() -> int:
    ap = argparse.ArgumentParser(description="问诊工作流真实端到端冒烟")
    ap.add_argument("--case", type=int, default=0, help="病例序号（0~9）")
    ap.add_argument("--all", action="store_true", help="跑全部病例")
    ap.add_argument("--max-rounds", type=int, default=16, help="单个病例最多对话轮数")
    args = ap.parse_args()

    cases = CASES
    picked = list(range(len(cases))) if args.all else [args.case % len(cases)]
    ok = 0
    for i in picked:
        case = cases[i]
        print(f"\n=== [{i}] {case['name']}：{case['complaint']} ===")
        try:
            r = await run_case(case, i, args.max_rounds)
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ 异常：{type(e).__name__}: {e}")
            continue
        over = [n for n in r["lengths"] if n > 600]
        print(f"  阶段推进：{' → '.join(r['stages'])}")
        print(f"  分诊：{r['triage']}（期望 {case['department']}）；诊断：{r['diagnoses']}")
        print(f"  轮次：{r['rounds']}；长文本轮次（>600 字）：{over or '无'}")
        ok += 1 if r["diagnoses"] else 0
    print(f"\n完成：{ok}/{len(picked)} 个病例至少给出了 1 版诊断")
    return 0 if ok == len(picked) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
