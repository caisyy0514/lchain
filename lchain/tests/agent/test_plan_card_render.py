"""前端计划卡渲染器（app/web/plan-card.js）的回归测试：去重与旧消息回退。

仓库没有 JS 测试链，这里用 node 直接调用 planCardHTML；node 不可用时跳过。
覆盖：
- 新消息：任务清单 + 危险信号 + 推荐机构与花费；风险不再以标签重复出现；
- 旧消息（无 tasks/conditions/risk_signals/care_options）：回退到编号步骤 + 风险标签 + 安排备注；
- 去重：结构化风险信号优先，不与“重点排除”标签重复。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path(__file__).parent / "plan_card_harness.js"
RENDERER = ROOT / "app" / "web" / "plan-card.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node 不可用，跳过前端渲染器测试")


def _render(cases: list[dict]) -> dict[str, dict]:
    proc = subprocess.run([NODE, str(HARNESS), str(RENDERER), json.dumps(cases, ensure_ascii=False)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 渲染失败：{proc.stderr}"
    rows = json.loads(proc.stdout)
    return {row["key"]: row for row in rows}


_NEW_EXTRA = {
    "kind": "plan_card", "urgency": "E2", "care_mode": "in_person", "release_stage": "DEMO",
    "view": {
        "plan_version": 1, "headline": "今天内到二级医院·呼吸内科就诊。",
        "tasks": [
            {"kind": "book", "status": "todo", "title": "先完成预约挂号", "detail": "官方渠道",
             "when": "今天内前", "where": "示例市第三医院·呼吸内科", "cost": "挂号费约 ¥30"},
            {"kind": "prepare", "status": "", "title": "身份证、医保卡", "detail": "",
             "when": "出发前", "where": "随身携带", "cost": "无"},
        ],
        "conditions": [
            {"name": "细菌性肺炎", "likelihood": "需优先排除", "evidence": "发热3天伴黄痰"},
            {"name": "急性支气管炎", "likelihood": "较可能", "evidence": ""},
        ],
        "risk_signals": [
            {"name": "细菌性肺炎", "signals": ["持续高热不退", "呼吸急促"], "action": "立即拨打120或急诊"},
            {"name": "低氧血症", "signals": ["口唇发紫", "意识模糊"], "action": "立即拨打120或急诊"},
        ],
        "care_options": {
            "region": "示例市朝阳区", "insurance": "示例市职工医保（演示参数）", "saving": 129,
            "needs": ["呼吸内科", "血常规", "胸部X线"],
            "plan_note": "升级到更高级别医院不会改变本次检查与处理方案，但个人自付更高。",
            "disclaimer": "费用为估算，实际以医院结算为准。",
            "options": [
                {"name": "示例市第三医院", "level": "二级", "district": "朝阳区", "department": "呼吸内科",
                 "insurance": "医保定点", "travel_minutes": 15, "recommended": True, "covers_needs": True,
                 "total": 215, "reimbursed": 69, "out_of_pocket": 146, "reason": "与您同区"},
                {"name": "示例市朝阳医院", "level": "三级", "district": "朝阳区", "department": "呼吸内科",
                 "insurance": "医保定点", "travel_minutes": 25, "recommended": False, "covers_needs": True,
                 "total": 275, "reimbursed": 0, "out_of_pocket": 275, "reason": "三级具备所需能力"},
            ],
        },
        "safety_groups": [
            {"level": "emergency", "action": "出现以下任一情况，立即拨打120或去最近急诊",
             "signals": ["呼吸困难、口唇发紫", "意识模糊"]},
            {"level": "today", "action": "出现以下任一情况，今天内就医",
             "signals": ["发热持续不退"]},
        ],
        "alerts": ["如果出现呼吸困难，请立即拨打120"],
        "risks_to_exclude": ["细菌性肺炎", "低氧血症"],
    },
}

_OLD_EXTRA = {
    "kind": "plan_card", "urgency": "E3", "care_mode": "in_person", "release_stage": "DEMO",
    "view": {   # 旧消息：没有 tasks / conditions / risk_signals / care_options
        "plan_version": 1, "headline": "按下面时限就医。",
        "steps": ["二级医院·消化内科（需到具备能力：消化内科门诊）", "医生可能会安排检查：血常规"],
        "alerts": ["如果出现呕血或黑便，请立即拨打120"],
        "possible_conditions": ["胆管炎（需要优先排除）", "胆囊结石（较可能）"],
        "risks_to_exclude": ["胆管炎", "胰腺炎"],
        "arrangement_note": "当前尚未预约（仍需你或病历经理按上面的方式落实）",
        "rationale": "需要面诊评估。",
    },
}


def test_new_card_prefers_tasks_risks_and_care_options() -> None:
    rows = _render([{"key": "new", "extra": _NEW_EXTRA}])
    r = rows["new"]
    assert r["hasTasks"] and not r["hasSteps"], "新消息应渲染任务清单而不是编号步骤"
    assert r["taskCount"] == 2 and r["diagnosisCount"] == 2
    assert r["riskCount"] == 2 and r["riskSignalCount"] == 2
    assert r["safetyGroupCount"] == 2, "安全提醒应按紧急程度分组渲染"
    assert r["riskChipCount"] == 0, "有结构化危险信号时不应再渲染重复的风险标签"
    assert r["taskFactCount"] == 2, "每条任务都要有四要素（时间/地点/花费）"
    assert r["compareCount"] == 2 and r["barCount"] == 2, "费用对比要有多维对照与自付对比条"
    assert r["hasConclusion"], "费用对比要先给结论（本次安排最合理）"
    assert r["hasCare"] and r["careOptionCount"] == 2 and r["hasSaving"]
    assert not r["hasNote"], "新消息的预约/观察已并入任务清单，不再单独渲染备注"


def test_old_card_falls_back_to_steps_and_risk_chips() -> None:
    rows = _render([{"key": "old", "extra": _OLD_EXTRA}])
    r = rows["old"]
    assert r["hasSteps"] and not r["hasTasks"], "旧消息没有 tasks，应回退到编号步骤"
    assert r["taskCount"] == 0 and r["riskSignalCount"] == 0 and r["safetyGroupCount"] == 0
    assert r["taskFactCount"] == 0 and r["compareCount"] == 0, "旧消息不渲染四要素与费用对比"
    assert r["riskChipCount"] >= 1, "旧消息没有危险信号，应回退成风险标签"
    assert not r["hasCare"], "旧消息没有 care_options，不渲染费用测算"
    assert r["hasNote"], "旧消息应回退渲染安排备注"


def test_duplicate_risks_are_not_rendered_twice() -> None:
    """结构化风险与“重点排除”标签同名时，只以危险信号形式出现一次。"""
    rows = _render([{"key": "dup", "extra": _NEW_EXTRA}])
    r = rows["dup"]
    assert r["riskCount"] == 2, "两个风险各渲染一次"
    assert r["riskChipCount"] == 0, "同名风险不得再以标签重复出现"
