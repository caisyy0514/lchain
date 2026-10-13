"""山区问诊工作流真实端到端冒烟：用真实模型扮演「医生工作流 + 山区村民」，把用例跑完整流程。

`tests/agent/test_rural.py` 用桩判断节点验证流程健壮性（不起模型、秒级完成）；
本脚本是最轻的命令行版本，用来快速看某个用例的阶段推进、处置层级与每轮字数。

依赖真实 LLM（消耗 token），默认只跑第 1 个用例：
    PYTHONPATH=. .venv/bin/python -m scripts.rural_smoke            # 跑用例 0
    PYTHONPATH=. .venv/bin/python -m scripts.rural_smoke --case 3   # 跑指定用例
    PYTHONPATH=. .venv/bin/python -m scripts.rural_smoke --all      # 跑全部用例

用例库在 `eval/rural/**/*.md`（单一数据源，见 app/rural/cases.py）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.llm import build_llm
from app.rural import load_state, reset_state
from app.rural.cases import CASES
from app.rural.graph import run_stream

PATIENT_SYSTEM = """你在扮演一位住在偏远山区的村民／患者家属，用手机向医生问病。
请严格按下面的资料回答医生的问题。

主诉：{complaint}
患者信息：{patient}
病情要点：{history}
家里和村里的情况（医生问到就医条件时按这个答）：{access}

扮演要求：
1. 只用第一人称、口语化、1~3 句话回答医生**问到的**内容，不要把没问到的信息全倒出来；
2. 说话像山里人：短句、大白话、偶尔有错别字；医生一次只问一个问题，你就只答那一个；
3. 医生问「到能看病的地方要走多久、有没有车、有没有人陪」这类问题时，按上面的情况如实回答；
4. 医生一旦给出诊断意见（回复里出现「诊断意见」「主要考虑」），下一次发言就主动告诉医生这个变化：
   {change}。之后医生再问细节时按下面回答：{change_answer}
5. 不要承认自己是 AI、不要给诊断、不要说"建议就医"这类医生的话；
6. 医生问的内容资料里没有的，就答"这个我不太清楚"或"没注意过"。
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
    session_id = f"rural-smoke-{idx}-{int(time.time())}"
    reset_state(session_id)
    patient = build_llm(temperature=0.4)
    history = "；".join(f"{k}={v}" for k, v in case["history"].items())
    access = "；".join(f"{k}={v}" for k, v in case["access"].items())
    sys_prompt = PATIENT_SYSTEM.format(complaint=case["complaint"], patient=case["patient"],
                                       history=history, access=access, change=case["change"],
                                       change_answer=case["change_answer"])

    message = case["complaint"]
    stages: list[str] = []
    actions: list[str] = []
    lengths: list[int] = []
    for r in range(max_rounds):
        try:
            doctor, done = await one_turn(session_id, message)
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ 第 {r + 1} 轮失败：{type(e).__name__}: {e}")
            break
        data = load_state(session_id) or {}
        stage = data.get("stage", "?")
        action = done.get("action") or "?"
        actions.append(action)
        if not stages or stages[-1] != stage:
            stages.append(stage)
        lengths.append(len(doctor))
        triage = data.get("triage") or {}
        disp = triage.get("disposition") or "?"
        diag = len(data.get("diagnoses") or [])
        print(f"    round {r + 1:2d} | {stage:<10} | {action:<16} | {disp:<10} | 诊断 v{diag} | "
              f"{len(doctor):4d} 字 | {doctor[:44]!r}")
        if (stage == "diagnosis" and diag >= 2) or (stage == "closed" and diag >= 1):
            break
        if stage == "closed" and lengths[-1] == (lengths[-2] if len(lengths) > 1 else None):
            break   # 已经在重复收尾，不再烧 token
        answer = (await patient.ainvoke([SystemMessage(content=sys_prompt),
                                         HumanMessage(content=doctor[-1500:])])).content.strip()
        message = answer or "这个我不太清楚"

    data = load_state(session_id) or {}
    triage = data.get("triage") or {}
    return {"session": session_id, "stages": stages, "actions": actions, "lengths": lengths,
            "triage": triage.get("department"), "disposition": triage.get("disposition"),
            "diagnoses": [d["primary"] for d in data.get("diagnoses") or []],
            "rounds": {"intake": data.get("record_rounds"),
                       "access_asked": bool(data.get("access_asked")),
                       "differential": len(data.get("differential") or [])}}


async def main() -> int:
    ap = argparse.ArgumentParser(description="山区问诊工作流真实端到端冒烟")
    ap.add_argument("--case", type=int, default=0, help="用例序号（0 起）")
    ap.add_argument("--all", action="store_true", help="跑全部用例")
    ap.add_argument("--max-rounds", type=int, default=16, help="单个用例最多对话轮数")
    args = ap.parse_args()

    cases = CASES
    picked = list(range(len(cases))) if args.all else [args.case % len(cases)]
    disp_ok = diag_ok = 0
    for i in picked:
        case = cases[i]
        print(f"\n=== [{i}] {case['name']}：{case['complaint']} ===")
        print(f"  期望：处置 {case['disposition']} / 科室 {case['department']} / 紧急度 {case['urgency']}")
        try:
            r = await run_case(case, i, args.max_rounds)
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ 异常：{type(e).__name__}: {e}")
            continue
        over = [n for n in r["lengths"] if n > 600]
        print(f"  阶段推进：{' → '.join(r['stages'])}")
        print(f"  动作序列：{r['actions']}")
        print(f"  就医建议：{r['triage']}｜处置层级 {r['disposition']}（期望 {case['disposition']}）")
        print(f"  诊断：{r['diagnoses']}")
        print(f"  轮次：{r['rounds']}；长文本轮次（>600 字）：{over or '无'}")
        disp_ok += 1 if r["disposition"] == case["disposition"] else 0
        diag_ok += 1 if r["diagnoses"] else 0
    print(f"\n完成：处置层级命中 {disp_ok}/{len(picked)}；至少给出 1 版诊断 {diag_ok}/{len(picked)}")
    return 0 if disp_ok == len(picked) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
