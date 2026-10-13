"""分诊 / 诊断 / 结束语的代码渲染。

为什么不让模型直接写这两段：它们是**要原样发给患者的结论**，必须带免责声明、
紧急情况必须带立即就医提示、绝不能出现药物剂量。模型只负责产出**字段值**
（科室、依据、红旗征…），文本由这里拼——这样安全边界不依赖模型自觉。

用 Markdown 列表（`- `）+ 空行：模型转发时不容易压成一大段，前端 md() 也能渲染成列表。
"""
from __future__ import annotations

from typing import Any

from app.consult.gates import DISCLAIMER, EMERGENCY_HINT, URGENCY, _bullets


def _join(items: list[str]) -> str:
    return "、".join(str(x) for x in items if str(x).strip())


def render_triage(triage: dict[str, Any]) -> str:
    """分诊中间提示：给患者的「建议看哪个科」，原样发出。"""
    urgency = triage.get("urgency", "routine")
    lines = ["🧭 分诊建议（中间提示）", "",
             f"- 建议就诊：**{triage.get('department') or '待定'}**",
             f"- 紧急程度：{URGENCY.get(urgency, urgency)}"]
    if triage.get("reasoning"):
        lines.append(f"- 判断依据：{triage['reasoning']}")
    if triage.get("suggested_tests"):
        lines.append("- 建议完善检查：" + _join(triage["suggested_tests"]))
    if triage.get("red_flags"):
        lines.append("- 需要警惕：" + _join(triage["red_flags"]))
    if urgency in ("urgent", "emergency"):
        lines += ["", EMERGENCY_HINT]
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


def render_diagnosis(entry: dict[str, Any]) -> str:
    """诊断意见（含更新版）：给患者的结论，原样发出。"""
    urgency = entry.get("urgency", "routine")
    version = entry.get("version", 1)
    lines = [f"🩺 诊断意见（第 {version} 版）", "",
             f"- 主要考虑：**{entry.get('primary') or '待明确'}**"
             + (f"（把握程度：{entry['confidence']}）" if entry.get("confidence") else "")]
    if entry.get("differentials"):
        lines.append("- 需要鉴别：" + _join(entry["differentials"]))
    if entry.get("severity"):
        lines.append(f"- 严重程度：{entry['severity']}")
    lines.append(f"- 处理建议：{URGENCY.get(urgency, urgency)}"
                 + ("（见下方警示）" if urgency in ("urgent", "emergency") else ""))
    if entry.get("tests"):
        lines.append("- 建议检查：" + _join(entry["tests"]))
    if entry.get("evidence"):
        lines += ["", "依据：", "", _bullets(entry["evidence"])]
    if entry.get("management"):
        lines += ["", "可以这样做：", "", _bullets(entry["management"])]
    if entry.get("red_flags"):
        lines += ["", "出现以下情况请立即就医：", "", _bullets(entry["red_flags"])]
    if urgency in ("urgent", "emergency"):
        lines += ["", EMERGENCY_HINT]
    if entry.get("followup_plan"):
        lines += ["", f"随访计划：{entry['followup_plan']}"]
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


def render_close(state: dict[str, Any]) -> str:
    """结束语：简短总结，不重新展开诊断内容。"""
    last = (state.get("diagnoses") or [{}])[-1]
    lines = ["✅ 本次问诊到这里就结束了。"]
    if last.get("primary"):
        lines.append(f"主要考虑：**{last['primary']}**。")
    if last.get("tests"):
        lines.append("需要做的检查：" + _join(last["tests"]) + "。")
    if last.get("followup_plan"):
        lines.append(f"随访：{last['followup_plan']}")
    lines += ["", DISCLAIMER]
    return "\n".join(lines)
