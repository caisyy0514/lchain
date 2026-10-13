"""已批准的低风险居家观察路径注册表（演示版）。

《VBC…v1.3.md》第 16.2 / 24 节要求：`care_mode=self_care` 必须有本机构批准、当前有效、
适用于年龄/病程/基础病/治疗阶段的低风险路径，并且：
- 观察截止、下一追问时间、回应窗口、复评节点由已审路径决定，**不由模型猜数字**
- 升级指征是「具体变化 → 多久内 → 去哪里/联系谁」的结构化条件
- 没有匹配路径时不允许居家观察，只能给线下评估或人工复核（EU）

当前仓库没有真实机构审批的临床规则，因此：
- 这些路径仅用于 **DEMO / SHADOW** 放行状态下的本地演示，标记为 DEMO_PENDING_CLINICAL_REVIEW
- `PRODUCTION_APPROVED` 放行时 `find_pathway` 一律返回 None（禁止绕过临床放行门禁）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.wecom.fd.red_flags import REVIEW_STATUS


@dataclass(frozen=True)
class ObservationPathway:
    pathway_id: str
    version: str
    title: str
    applies: tuple[str, ...]            # 主诉关键词（任一命中）
    excludes: tuple[str, ...]           # 命中任一排除条件则不适用
    current_action: str                 # 现在怎么做
    observation_items: tuple[dict[str, Any], ...]   # 需要观察的指标/问题
    max_hours: float                    # 最长观察截止（小时）
    next_review_minutes: float          # 下一次主动追问（分钟）
    response_due_minutes: float         # 患者回应窗口（分钟）
    reassessment_due_hours: float       # 完成复评的最晚时间（小时）
    escalation_rules: tuple[dict[str, Any], ...]
    approval_receipt_id: str = ""
    review_status: str = REVIEW_STATUS
    owner_queue_id: str = "case_manager_queue"


# --------------------------------------------------------------------------- 路径表
PATHWAYS: tuple[ObservationPathway, ...] = (
    ObservationPathway(
        pathway_id="PATHWAY-DEMO-URI-MILD",
        version="demo-2026.10-v1",
        title="普通上呼吸道感染（轻症、无危险信号）",
        applies=(r"流鼻涕|鼻塞|打喷嚏|嗓子(疼|干|痒)|轻咳|低烧|37\.[5-9]|38\.[0-3]",),
        excludes=(r"呼吸困难|喘不上气|胸痛|意识|抽搐|血氧|小孩|婴儿|宝宝|新生儿|孕妇|怀孕|免疫|化疗|"
                  r"糖尿病|心衰|慢阻肺|哮喘|体温\s*3[89]",),
        current_action="多喝温水、充分休息，按需物理降温；不要自行加用抗生素",
        observation_items=(
            {"field": "体温", "method": "每天早晚各测一次并记录", "required": True},
            {"field": "呼吸情况", "method": "是否出现气短、喘不上气", "required": True},
            {"field": "饮水与尿量", "method": "能否正常喝水、尿量是否明显减少", "required": True},
        ),
        max_hours=72, next_review_minutes=24 * 60, response_due_minutes=36 * 60, reassessment_due_hours=48,
        escalation_rules=(
            {"rule_id": "ESC-URI-DEMO-1", "version": "demo-2026.10-v1",
             "patient_condition": "出现呼吸困难、胸痛、意识模糊、抽搐或血氧低于 94%",
             "urgency": "E0", "response_window": "立即",
             "destination_capability": "急诊/急救", "action": "立即拨打{emergency_number}或前往急诊"},
            {"rule_id": "ESC-URI-DEMO-2", "version": "demo-2026.10-v1",
             "patient_condition": "发热超过 3 天不退、症状明显加重或出现新症状",
             "urgency": "E2", "response_window": "当天",
             "destination_capability": "有急诊/呼吸科能力的医院", "action": "当天线下就诊"},
            {"rule_id": "ESC-URI-DEMO-3", "version": "demo-2026.10-v1",
             "patient_condition": "超过 48 小时仍未改善或反复",
             "urgency": "E3", "response_window": "24–72 小时内",
             "destination_capability": "门诊", "action": "安排线下门诊评估"},
        ),
    ),
    ObservationPathway(
        pathway_id="PATHWAY-DEMO-GASTRO-MILD",
        version="demo-2026.10-v1",
        title="轻度腹泻/胃肠不适（无脱水与危险信号）",
        applies=(r"拉肚子|腹泻|大便(稀|不成形)|肚子(有点)?不舒服|吃坏肚子",),
        excludes=(r"脱水|口干.*尿少|血便|黑便|呕血|吐血|高热|39|剧烈腹痛|肚子疼得|怀孕|孕妇|婴儿|宝宝|老人|"
                  r"糖尿病|免疫|化疗|抗凝",),
        current_action="少量多次补充口服补液盐或淡盐水，清淡饮食；不要自行吃止泻药掩盖症状",
        observation_items=(
            {"field": "大便次数与性状", "method": "每次记录次数、是否带血/黑便", "required": True},
            {"field": "尿量与口渴", "method": "尿量明显减少、口干、站起来头晕需就医", "required": True},
            {"field": "能否进食饮水", "method": "是否频繁呕吐、喝不进水", "required": True},
        ),
        max_hours=48, next_review_minutes=12 * 60, response_due_minutes=18 * 60, reassessment_due_hours=24,
        escalation_rules=(
            {"rule_id": "ESC-GI-DEMO-1", "version": "demo-2026.10-v1",
             "patient_condition": "出现血便/黑便、呕血、剧烈腹痛、晕厥",
             "urgency": "E0", "response_window": "立即",
             "destination_capability": "急诊/急救", "action": "立即拨打{emergency_number}或前往急诊"},
            {"rule_id": "ESC-GI-DEMO-2", "version": "demo-2026.10-v1",
             "patient_condition": "明显脱水（口干、尿少、站起头晕）或频繁呕吐喝不进水",
             "urgency": "E2", "response_window": "当天",
             "destination_capability": "有急诊/消化科能力的医院", "action": "当天线下就诊"},
        ),
    ),
    ObservationPathway(
        pathway_id="PATHWAY-DEMO-SOFT-TISSUE",
        version="demo-2026.10-v1",
        title="轻度软组织损伤/蚊虫叮咬局部反应（无骨折与感染征象）",
        applies=(r"扭(了|到)|崴脚|轻微(拉伤|碰伤)|蚊虫叮咬|被咬|局部(红肿|痒)",),
        excludes=(r"骨折|变形|不能动|大出血|伤口(很深|很大)|发热|高热|红线|化脓|抗凝|糖尿病足",),
        current_action="休息、抬高患处，早期冷敷；保持伤口清洁，不要抓挠",
        observation_items=(
            {"field": "肿痛变化", "method": "肿胀/疼痛是否持续加重，能否负重", "required": True},
            {"field": "伤口情况", "method": "是否化脓、发红扩散、出现红线", "required": True},
            {"field": "全身症状", "method": "是否发热、寒战", "required": True},
        ),
        max_hours=72, next_review_minutes=24 * 60, response_due_minutes=36 * 60, reassessment_due_hours=48,
        escalation_rules=(
            {"rule_id": "ESC-ST-DEMO-1", "version": "demo-2026.10-v1",
             "patient_condition": "伤口化脓、红线扩散、发热寒战或无法负重",
             "urgency": "E2", "response_window": "当天",
             "destination_capability": "有急诊/外科能力的医院", "action": "当天线下就诊"},
            {"rule_id": "ESC-ST-DEMO-2", "version": "demo-2026.10-v1",
             "patient_condition": "肿痛明显加重、肢体麻木或变形",
             "urgency": "E1", "response_window": "立即/尽快",
             "destination_capability": "急诊", "action": "尽快到急诊评估"},
        ),
    ),
)

ALLOWED_STAGES = {"DEMO", "SHADOW"}


def find_pathway(text: str, *, release_stage: str = "DEMO") -> ObservationPathway | None:
    """匹配低风险路径；放行状态不允许时返回 None（不允许用演示规则上线）。"""
    if release_stage not in ALLOWED_STAGES:
        return None
    import re

    from app.wecom.fd.red_flags import pattern_hit

    for p in PATHWAYS:
        if pattern_hit(text, p.excludes):   # “没有血便/没有剧烈腹痛”等否定句不算排除项
            continue
        if any(re.search(x, text) for x in p.applies):
            return p
    return None


def pathway_count() -> int:
    return len(PATHWAYS)


def pathway_by_id(pathway_id: str, *, release_stage: str = "DEMO") -> ObservationPathway | None:
    if release_stage not in ALLOWED_STAGES:
        return None
    return next((p for p in PATHWAYS if p.pathway_id == pathway_id), None)
