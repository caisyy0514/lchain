"""山区问诊的结论文本渲染：就医建议、诊断意见、急救要点、结束语。

为什么不让模型直接写这几段：它们是要**原样发到患者手机上**的结论，必须做到
- 一定带免责声明；
- 急症一定带「立即就医 / 打 120」和去哪一级医院；
- 对话里出现过蛇咬、农药、煤气、摔伤等场景时，一定给**正确**的急救处理（模型容易教出
  绑扎、切开吸毒、涂牙膏这类有害做法）；
- 绝不出现具体药物剂量。

模型只产出**字段值**（科室、处置层级、依据、红旗征…），文本在这里拼——安全边界不依赖模型自觉。
渲染出的块**不含问号**，所以不会被 `limit_questions` 当成提问截断。
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.config import get_settings
from app.rural.gates import (
    DISCLAIMER,
    emergency_hint,
    URGENCY,
    _bullets,
    disposition_label,
)

logger = logging.getLogger(__name__)


def _join(items: list[str]) -> str:
    return "、".join(str(x) for x in items if str(x).strip())


# 文本块裁剪阈值：老人看不了长文，超过这个长度就先丢「依据」这类可以省掉的段落（按顺序）。
# 注意这不是硬截断——就医方式 / 去哪一级 / 路上注意 / 急救要点 / 急诊提示 / 免责声明永远保留，
# 所以急症块（三段急救要点 + 120 提示）仍可能到 800 字左右：安全内容优先于精简。
TRIM_OVER_CHARS = 700


def _assemble(core_head: list[str], optional: list[list[str]], core_tail: list[str]) -> str:
    """拼接文本块：全量超上限时，按 optional 的顺序依次丢掉最不要紧的段落。"""
    kept = list(optional)
    while True:
        text = "\n".join([*core_head, *[ln for block in kept for ln in block], *core_tail])
        if len(text) <= TRIM_OVER_CHARS:
            return text
        drop = next((i for i in range(len(kept)) if kept[i]), None)
        if drop is None:
            return text
        logger.info("山区问诊文本块过长（%d 字），省掉一段可选内容", len(text))
        kept[drop] = []


def render_first_aid(first_aid: list[dict[str, Any]] | None) -> list[str]:
    """急救要点块：命中场景时逐条列出正确做法（内容全部来自 gates.FIRST_AID，不经过模型）。"""
    blocks: list[str] = []
    for item in first_aid or []:
        steps = [str(s) for s in (item.get("steps") or []) if str(s).strip()]
        if not steps:
            continue
        blocks += [f"**马上这样做（{item.get('name')}）**", "", _bullets(steps)]
    return blocks


def render_triage(triage: dict[str, Any]) -> str:
    """就医建议（中间提示）：科室 + 紧急程度 + 去哪一级医院 / 怎么去 + 急救要点。"""
    urgency = triage.get("urgency", "routine")
    level = triage.get("disposition", "home")
    lines = ["🏔 就医建议（中间提示）", "",
             f"- 建议就诊：**{triage.get('department') or '待定'}**",
             f"- 紧急程度：{URGENCY.get(urgency, urgency)}",
             f"- 就医方式：**{disposition_label(level)}**"]
    if triage.get("referral_target"):
        lines.append(f"- 去哪里：{triage['referral_target']}")
    if triage.get("transfer_notes"):
        lines.append(f"- 路上注意：{triage['transfer_notes']}")
    optional = []
    if triage.get("reasoning"):
        optional.append([f"- 判断依据：{triage['reasoning']}"])
    if triage.get("access_notes"):
        optional.append([f"- 就医条件：{triage['access_notes']}"])
    if triage.get("suggested_tests"):
        optional.append(["- 到那儿要做的检查：" + _join(triage["suggested_tests"])])
    if triage.get("red_flags"):
        optional.append(["- 需要警惕：" + _join(triage["red_flags"])])
    tail = render_first_aid(triage.get("first_aid"))
    if urgency in ("urgent", "emergency") or level in ("county", "emergency"):
        tail += ["", emergency_hint()]
    tail += ["", DISCLAIMER]
    return _assemble(lines, optional, tail)


def render_diagnosis(entry: dict[str, Any]) -> str:
    """诊断意见（含更新版）：结论 + 就地能做的处理 + 转诊建议 + 急救要点。"""
    urgency = entry.get("urgency", "routine")
    level = entry.get("disposition", "home")
    version = entry.get("version", 1)
    lines = [f"🩺 诊断意见（第 {version} 版）", "",
             f"- 主要考虑：**{entry.get('primary') or '待明确'}**"
             + (f"（把握程度：{entry['confidence']}）" if entry.get("confidence") else "")]
    if entry.get("differentials"):
        lines.append("- 需要鉴别：" + _join(entry["differentials"]))
    if entry.get("severity"):
        lines.append(f"- 严重程度：{entry['severity']}")
    lines.append(f"- 就医建议：**{disposition_label(level)}**"
                 + (f"（{entry['disposition_reason']}）" if entry.get("disposition_reason") else ""))
    if entry.get("referral_target"):
        lines.append(f"- 去哪里：{entry['referral_target']}")
    if entry.get("referral_plan"):
        lines.append(f"- 什么时候出发 / 怎么去：{entry['referral_plan']}")
    if entry.get("tests"):
        lines.append("- 建议检查（尽量选当地能做的）：" + _join(entry["tests"]))
    # 「依据」放在可选段落：整段太长时优先省掉它（患者更关心怎么看病、什么时候走）
    optional = [["", "依据：", "", _bullets(entry["evidence"])]] if entry.get("evidence") else []
    tail: list[str] = []
    if entry.get("local_management"):
        tail += ["", "在村卫生室 / 家里可以先这样做：", "", _bullets(entry["local_management"])]
    if entry.get("red_flags"):
        tail += ["", "出现以下情况请立即就医：", "", _bullets(entry["red_flags"])]
    tail += render_first_aid(entry.get("first_aid"))
    if urgency in ("urgent", "emergency") or level in ("county", "emergency"):
        tail += ["", emergency_hint()]
    if entry.get("followup_plan"):
        tail += ["", f"随访计划：{entry['followup_plan']}"]
    tail += ["", DISCLAIMER]
    return _assemble(lines, optional, tail)


def render_close(state: dict[str, Any]) -> str:
    """结束语：简短总结，不重新展开诊断内容；把「去哪一级」再强调一次。"""
    last = (state.get("diagnoses") or [{}])[-1]
    lines = ["✅ 本次问诊到这里就结束了。"]
    if last.get("primary"):
        lines.append(f"主要考虑：**{last['primary']}**。")
    if last.get("disposition"):
        lines.append(f"就医安排：{disposition_label(last['disposition'])}"
                     + (f"（{last['referral_target']}）" if last.get("referral_target") else "") + "。")
    if last.get("tests"):
        lines.append("需要做的检查：" + _join(last["tests"]) + "。")
    if last.get("followup_plan"):
        lines.append(f"随访：{last['followup_plan']}")
    lines += ["", f"村里路远、出门不方便，路上注意保暖、尽量有人陪同；症状加重随时联系村医或打 "
              f"{get_settings().rural_emergency_number}。"]
    lines += ["", DISCLAIMER]
    return "\n".join(lines)
