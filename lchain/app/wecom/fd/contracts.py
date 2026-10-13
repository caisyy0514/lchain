"""家庭医生 Agent（VBC 分级诊疗）的数据契约与服务端不变量。

对应《VBC_分级诊疗Agent_IDE完整提示词_v1.3.md》第 16–18 / 23 / 24 节：
- 枚举：Urgency（E0–E5/EU）、CareMode、PlanState、BookingState、HandoffState、ObservationState
- 对象：ActionablePlan / HomeObservationPlan / PatientPlanView / TriageOutput
- 服务端强制不变量（模型不能自证通过）：
  * 紧急程度不得低于确定性安全门控下界（`safety_floor`），模型只能升级不能降级
  * `care_mode=self_care` 必须有完整的居家观察计划（准入 + 时限 + 主动追问任务）
  * `FEASIBLE` 必须七项核验通过或有逐项豁免；没有真实资源回执时只能 `NEEDS_VERIFICATION`
  * 时间窗顺序 next_review_at <= response_due_at <= reassessment_due_at <= maximum_end_at
  * 延长观察必须有医师批准回执；`COMPLETED` 必须有真实复评证据
  * 不编造医院/号源/医生：未核实的 destination 只能给能力要求，不能给 facility_id
"""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class Urgency(StrEnum):
    """紧急程度（独立于医院等级）。EU = 暂不能安全分级。"""
    E0 = "E0"
    E1 = "E1"
    E2 = "E2"
    E3 = "E3"
    E4 = "E4"
    E5 = "E5"
    EU = "EU"


URGENCY_RANK: dict[str, int] = {u.value: i for i, u in enumerate(Urgency)}
URGENCY_LABEL: dict[str, str] = {
    "E0": "立即急救", "E1": "立即/尽快急诊", "E2": "当日就医", "E3": "限时面诊",
    "E4": "计划门诊", "E5": "稳定复诊", "EU": "暂不能安全分级（需人工/线下评估）",
}


def worst_urgency(*levels: str | None) -> str:
    """取最紧急的一个（E0 最紧急；EU 视为不确定，排在 E5 之后但不低于其本身）。"""
    valid = [x for x in levels if x in URGENCY_RANK]
    if not valid:
        return Urgency.EU.value
    return min(valid, key=lambda x: URGENCY_RANK[x])


class CareMode(StrEnum):
    INITIAL_NAVIGATION = "initial_navigation"
    IN_PERSON = "in_person"
    STABLE_FOLLOWUP = "stable_followup"
    EXPERT_SECOND_OPINION = "expert_second_opinion"
    MDT = "mdt"
    EMERGENCY = "emergency"
    SELF_CARE = "self_care"
    HEALTH_EDUCATION = "health_education"


class PlanState(StrEnum):
    DRAFT = "DRAFT"
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"
    FEASIBLE = "FEASIBLE"
    PATIENT_ACCEPTED = "PATIENT_ACCEPTED"
    BLOCKED = "BLOCKED"
    DECLINED = "DECLINED"
    SUPERSEDED = "SUPERSEDED"


class BookingState(StrEnum):
    NOT_REQUESTED = "NOT_REQUESTED"
    REQUESTED = "REQUESTED"
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    ATTENDED = "ATTENDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    ATTENDANCE_UNKNOWN = "ATTENDANCE_UNKNOWN"
    NO_SHOW = "NO_SHOW"


class HandoffState(StrEnum):
    NOT_REQUESTED = "NOT_REQUESTED"
    REQUESTED = "REQUESTED"
    QUEUED = "QUEUED"
    ACCEPTED = "ACCEPTED"
    RESPONDED = "RESPONDED"
    UNAVAILABLE = "UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"


class ObservationState(StrEnum):
    PROPOSED = "PROPOSED"
    ACTIVE = "ACTIVE"
    REVIEW_PENDING = "REVIEW_PENDING"
    CONTACT_OVERDUE = "CONTACT_OVERDUE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    ESCALATED = "ESCALATED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


# 放行状态：未经批准不得通过环境变量或 UI 绕过临床放行门禁
RELEASE_STAGES = ("DEMO", "SHADOW", "CLINICIAN_ASSISTED", "PRODUCTION_APPROVED")

# 患者端显示：不出现 E0/E2 等内部代码
URGENCY_PATIENT_WORD: dict[str, str] = {
    "E0": "现在立即", "E1": "现在尽快", "E2": "今天内", "E3": "按下面时限",
    "E4": "按计划", "E5": "复诊时", "EU": "尽快（需要人工/线下确认）",
}


# --------------------------------------------------------------------------- 基础对象
class TimeWindow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    anchor_time: str | None = None          # 医学锚点（起病/最后正常/识别时间），带时区 ISO8601
    anchor_source: str = "unknown"          # symptom_onset / last_known_well / first_observed / report / clinician
    timezone: str = "Asia/Shanghai"
    deadline: str | None = None             # 临床截止时间（带时区）
    immediately: bool = False
    display_window: str = ""                # 患者可见的具体时间窗
    unknown_reason: str | None = None       # 未知时必须说明原因


class Destination(BaseModel):
    model_config = ConfigDict(extra="ignore")

    care_setting: str = "in_person"         # emergency / in_person / online / self_care
    required_capabilities: list[str] = Field(default_factory=list)
    facility_id: str | None = None          # 只有真实核验回执才允许填写
    facility_name: str | None = None
    campus_id: str | None = None
    department: str | None = None
    entry_method: str | None = None
    verification_receipt_id: str | None = None
    valid_until: str | None = None
    verified: bool = False


class HomeObservationPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    observation_id: str
    case_id: str
    plan_version: int = 1
    state: str = ObservationState.PROPOSED.value
    approved_policy: dict[str, Any] = Field(default_factory=dict)
    admission: dict[str, Any] = Field(default_factory=dict)
    started_at: str | None = None
    anchor_source: str = "unknown"
    maximum_end_at: str | None = None
    next_review_at: str | None = None
    response_due_at: str | None = None
    reassessment_due_at: str | None = None
    contact_policy_id: str | None = None
    owner_queue_id: str | None = None
    scheduler_receipt_id: str | None = None
    channel_authorization_receipt_id: str | None = None
    followup_task_ids: list[str] = Field(default_factory=list)
    observation_items: list[dict[str, Any]] = Field(default_factory=list)
    escalation_rules: list[dict[str, Any]] = Field(default_factory=list)
    current_action: str = ""                 # 观察期间现在怎么做（来自已批准路径）
    reviews: list[dict[str, Any]] = Field(default_factory=list)
    extensions: list[dict[str, Any]] = Field(default_factory=list)
    closure: dict[str, Any] | None = None

    def active(self) -> bool:
        return self.state in (ObservationState.ACTIVE.value, ObservationState.REVIEW_PENDING.value)


class ActionablePlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    case_id: str
    plan_version: int = 1
    state: str = PlanState.NEEDS_VERIFICATION.value
    urgency: str = Urgency.EU.value
    urgency_assessment: str | None = None   # 重新评估后的紧急程度（与 urgency 一致时可为空）
    care_mode: str = CareMode.INITIAL_NAVIGATION.value
    primary_problem: str = ""
    primary_action: str = ""
    rule_id: str | None = None
    rule_version: str | None = None
    time: TimeWindow = Field(default_factory=TimeWindow)
    destination: Destination = Field(default_factory=Destination)
    feasible_checks: dict[str, bool | None] = Field(default_factory=dict)
    check_exemptions: list[dict[str, Any]] = Field(default_factory=list)
    null_reasons: dict[str, str] = Field(default_factory=dict)
    observation_review_at: str | None = None
    home_observation: HomeObservationPlan | None = None
    unresolved_dependencies: list[str] = Field(default_factory=list)
    booking: dict[str, Any] = Field(default_factory=lambda: {"state": BookingState.NOT_REQUESTED.value,
                                                             "request_id": None, "receipt_id": None,
                                                             "appointment_at": None})
    handoff: dict[str, Any] = Field(default_factory=lambda: {"state": HandoffState.NOT_REQUESTED.value,
                                                             "task_id": None, "assignee_id": None})
    followup: dict[str, Any] = Field(default_factory=lambda: {"required": False, "task_id": None, "due_at": None,
                                                              "owner_queue_id": None, "assignee_id": None,
                                                              "unavailable_reason": None})
    preparation: list[str] = Field(default_factory=list)
    escalation_triggers: list[dict[str, Any]] = Field(default_factory=list)
    fallback_action: str | None = None
    safety_flags: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    # ---- 临床决策依据（第 9 / 23 节：就医规划必须讲清楚）----
    condition_summary: str = ""                       # 病情小结（患者语言）
    possible_conditions: list[dict[str, Any]] = Field(default_factory=list)  # [{name, likelihood, evidence}]
    risks_to_exclude: list[str] = Field(default_factory=list)   # 需要优先排除的风险
    rationale: str = ""                              # 为什么这样安排
    facility_level: str = ""                         # 需要的机构级别（急救/急诊/三级/二级/基层/线上复诊）
    tests_needed: list[str] = Field(default_factory=list)       # 建议检查
    confirm_method: str = ""                         # 最终如何确诊
    doctor_review_note: str | None = None             # 真人医生审核说明（第 3 条要求：不引导线上问诊）
    missing_critical_data: list[str] = Field(default_factory=list)
    clinician_review_receipt_id: str | None = None
    human_review_required: bool = False
    release_stage: str = "DEMO"
    patient_visible_note: str | None = None
    consistency_warnings: list[str] = Field(default_factory=list)
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class PlanTask(BaseModel):
    """患者卡片上的一条任务（就医规划的清单化投影，由服务端从结构化字段确定性生成）。

    每条任务必须说清四要素：任务（title）、时间（when）、地点（where）、花费（cost）。
    """
    model_config = ConfigDict(extra="ignore")

    title: str = ""                  # 任务（一句话，动词开头、尽量短）
    detail: str = ""                 # 补充说明（入口 / 材料 / 检查项）
    kind: str = "action"             # go | prepare | check | book | observe（前端配图标）
    status: str = ""                 # "" 普通 | todo 待办 | confirmed 已确认 | active 进行中
    when: str = ""                   # 时间（什么时候做）
    where: str = ""                  # 地点（在哪里做）
    cost: str = ""                   # 花费（患者视角的金额/是否免费）


class PlanCondition(BaseModel):
    """疑似诊断（患者卡片"医生的判断"里的一条）。"""
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    likelihood: str = ""              # 较可能 | 需优先排除 | 待明确
    evidence: str = ""                # 依据（患者听得懂的表述）


class PlanRisk(BaseModel):
    """重点排除的风险 + 对应危险信号（患者能直接对照的症状）。"""
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    signals: list[str] = Field(default_factory=list)   # 出现任一信号就要立即行动
    action: str = ""                                   # 对应行动


class SafetyGroup(BaseModel):
    """患者安全提醒：按行动紧急程度合并、去重后的危险信号（首屏直接可见）。"""
    model_config = ConfigDict(extra="ignore")

    level: str = ""        # emergency 立即 | today 今天内 | soon 尽快
    action: str = ""       # 对应行动（一句）
    signals: list[str] = Field(default_factory=list)


class FacilityOption(BaseModel):
    """一个可选的具体机构及其估算费用。"""
    model_config = ConfigDict(extra="ignore")

    facility_id: str = ""
    name: str = ""
    level: str = ""
    level_rank: int = 0               # 1 基层 / 2 二级 / 3 三级
    district: str = ""
    department: str = ""
    address: str = ""
    entry_method: str = ""
    insurance: str = ""
    travel_minutes: int | None = None
    reason: str = ""                  # 为什么推荐它
    recommended: bool = False
    covers_needs: bool = False        # 是否具备本次所需能力（医学安全性对照）
    total: float = 0                  # 估算总费用（元）
    reimbursed: float = 0             # 估算医保报销
    out_of_pocket: float = 0          # 估算个人自付


class CareOptions(BaseModel):
    """推荐机构 + 医保费用测算（示例政策，供患者比较）。"""
    model_config = ConfigDict(extra="ignore")

    region: str = ""
    insurance: str = ""
    options: list[FacilityOption] = Field(default_factory=list)  # 推荐在前，高级别对照在后
    needs: list[str] = Field(default_factory=list)   # 本次需要的科室/检查（能力对照）
    saving: float | None = None       # 相比升级到高级别机构，个人少付多少
    plan_note: str = ""               # 有效性说明：升级不改变检查与处理方案
    disclaimer: str = ""


class PatientPlanView(BaseModel):
    """患者视图（计划卡的投影，不重新生成临床方案）。"""
    model_config = ConfigDict(extra="ignore")

    case_id: str
    plan_version: int
    rendered_at: str
    channel: str = "plain_text"
    urgency_word: str = ""
    headline: str = ""
    steps: list[str] = Field(default_factory=list)         # 纯文本通道用；卡片优先用 tasks
    tasks: list[PlanTask] = Field(default_factory=list)     # 任务清单（就医规划 → 待办）
    conditions: list[PlanCondition] = Field(default_factory=list)  # 疑似诊断（结构）
    risk_signals: list[PlanRisk] = Field(default_factory=list)      # 重点排除风险 + 危险信号
    safety_groups: list[SafetyGroup] = Field(default_factory=list)  # 合并去重后的安全提醒（首屏）
    care_options: CareOptions | None = None                        # 推荐机构与费用测算
    time_window: str = ""                                           # 患者可见时间窗（首屏事实标签）
    alerts: list[str] = Field(default_factory=list)
    # ---- 病情说明与决策依据（让患者听得懂、信得过）----
    condition_summary: str = ""                       # 目前了解到的情况（患者语言）
    possible_conditions: list[str] = Field(default_factory=list)   # 可能的方向（按可能性排序）
    risks_to_exclude: list[str] = Field(default_factory=list)      # 本次需要优先排除的风险
    rationale: str = ""                              # 为什么这样安排
    facility_level: str = ""                         # 需要的机构级别
    tests_needed: list[str] = Field(default_factory=list)          # 建议检查
    confirm_method: str = ""                         # 最终如何确诊
    doctor_review_note: str | None = None             # 真人医生审核状态
    arrangement_note: str | None = None
    observation_review_note: str | None = None
    revision_notice: str | None = None
    must_show_bindings: dict[str, str] = Field(default_factory=dict)
    details: list[dict[str, str]] = Field(default_factory=list)

    def text(self) -> str:
        """企业微信纯文本通道的呈现（病情说明 + 决策依据 + 一句建议 + 少量步骤 + 必要警示）。"""
        return self.chat_text()

    def chat_text(self) -> str:
        """纯文本通道：行动优先（一句建议 → 步骤 → 警示 → 安排），病情与依据放后面，避免首屏全是背景信息。"""
        lines: list[str] = []
        if self.revision_notice:
            lines.append(f"【计划更新】{self.revision_notice}")
        if self.headline:
            lines.append(self.headline)
        if self.steps:
            lines.append("现在要做：")
            for i, st in enumerate(self.steps, 1):
                lines.append(f"{i}. {st}")
        if self.alerts:
            lines.append("出现以下情况请立即行动：")
            for a in self.alerts:
                lines.append(f"· {a}")
        if self.observation_review_note:
            lines.append(self.observation_review_note)
        if self.arrangement_note:
            lines.append(self.arrangement_note)
        if self.doctor_review_note:
            lines.append(self.doctor_review_note)
        if self.condition_summary:
            lines.append(f"病情了解：{self.condition_summary}")
        if self.possible_conditions:
            lines.append("可能的方向：" + "；".join(self.possible_conditions))
        if self.risks_to_exclude:
            lines.append("需要优先排除：" + "、".join(self.risks_to_exclude))
        if self.rationale:
            lines.append(f"为什么这样安排：{self.rationale}")
        return "\n".join(x for x in lines if x)


# --------------------------------------------------------------------------- LLM 输出契约
class _NoneSafe(BaseModel):
    """容错基类：LLM 常对字符串/列表字段返回 null，统一转成空值，避免整次分诊失败。"""
    model_config = ConfigDict(extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _coerce_none(cls, v: Any, info: ValidationInfo) -> Any:
        if v is not None:
            return v
        field = cls.model_fields.get(info.field_name)
        ann = field.annotation if field is not None else None
        if ann is str:
            return ""
        if getattr(ann, "__origin__", None) is list:
            return []
        if getattr(ann, "__origin__", None) is dict:
            return {}
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            return ann()
        return v


class EscalationOut(_NoneSafe):
    model_config = ConfigDict(extra="ignore")
    change: str = ""
    action: str = ""
    urgency: str = Urgency.E1.value


class SelfCareProposal(_NoneSafe):
    model_config = ConfigDict(extra="ignore")
    eligible: bool = False
    pathway_id: str | None = None       # 只能引用已批准的低风险路径 ID
    reason: str = ""


class ConditionOut(_NoneSafe):
    """可能的方向（患者语言，最多 3 条）。"""
    model_config = ConfigDict(extra="ignore")
    name: str = ""
    likelihood: str = ""      # 可能 / 需优先排除 / 待明确
    evidence: str = ""


class TriageOutput(_NoneSafe):
    """`fd-triage` 节点的结构化输出（LLM），服务端再做合并与门控。"""
    model_config = ConfigDict(extra="ignore")

    # 多轮问诊
    status: str = "concluded"                     # need_more_info / concluded
    questions: list[str] = Field(default_factory=list)   # 需要继续追问（1–2 个）
    collected_facts: dict[str, str] = Field(default_factory=dict)  # 本轮新增的已确认事实

    # 病情说明与决策依据
    condition_summary: str = ""                   # 目前了解到的情况（患者语言）
    possible_conditions: list["ConditionOut"] = Field(default_factory=list)
    risks_to_exclude: list[str] = Field(default_factory=list)
    rationale: str = ""                           # 为什么这样安排
    facility_level: str = ""                      # 需要的机构级别
    tests_needed: list[str] = Field(default_factory=list)
    confirm_method: str = ""                      # 最终如何确诊

    primary_problem: str = ""
    urgency: str = Urgency.EU.value
    care_mode: str = CareMode.INITIAL_NAVIGATION.value
    confidence_status: str = "uncertain"          # qualified / uncertain
    required_capabilities: list[str] = Field(default_factory=list)
    care_setting: str = "in_person"
    department: str | None = None
    time_window: str = ""                         # 患者可见的时间窗描述
    deadline_hours: float | None = None           # 距锚点的小时数（可选，服务端换算）
    action_now: str = ""                          # 一句首要行动
    actions: list[str] = Field(default_factory=list)
    escalation_triggers: list[EscalationOut] = Field(default_factory=list)
    missing_critical_data: list[str] = Field(default_factory=list)
    safety_flags: list[str] = Field(default_factory=list)
    preparation: list[str] = Field(default_factory=list)
    self_care: SelfCareProposal = Field(default_factory=SelfCareProposal)
    explanation: str = ""                         # 非关键解释
    question: str = ""                            # 兼容旧字段（合并进 questions）
    red_flags: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- 不变量
def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


ALL_FEASIBLE_CHECKS = ("medical_fit", "within_deadline", "resource_verified", "entry_known",
                       "patient_can_reach", "support_ready", "fallback_ready")


def validate_plan(plan: ActionablePlan) -> list[str]:
    """返回违反的不变量列表；空 = 通过。模型输出后、发布前必须调用。"""
    v: list[str] = []
    urgency, mode = plan.urgency, plan.care_mode

    if urgency not in URGENCY_RANK:
        v.append(f"非法紧急程度：{urgency}")
    if mode not in {m.value for m in CareMode}:
        v.append(f"非法照护模式：{mode}")

    # 就医规划必须讲清楚：病情说明 / 可能方向 / 需排除风险 / 决策依据 / 确诊方式 / 机构级别
    clinical_modes = {CareMode.EMERGENCY.value, CareMode.IN_PERSON.value, CareMode.STABLE_FOLLOWUP.value,
                      CareMode.EXPERT_SECOND_OPINION.value, CareMode.MDT.value, CareMode.SELF_CARE.value}
    if mode in clinical_modes:
        if not plan.condition_summary:
            v.append("结论缺少病情说明（condition_summary）")
        if not plan.possible_conditions:
            v.append("结论缺少可能的方向（possible_conditions）")
        if not plan.risks_to_exclude and urgency != "E5":
            v.append("结论缺少需要优先排除的风险（risks_to_exclude）")
        if not plan.rationale:
            v.append("结论缺少决策依据（rationale）")
        if mode != CareMode.SELF_CARE.value and not plan.confirm_method:
            v.append("结论缺少确诊方式（confirm_method）")
        if mode in (CareMode.EMERGENCY.value, CareMode.IN_PERSON.value) and not plan.facility_level:
            v.append("结论缺少机构级别（facility_level）")

    # E0/E1：立即行动，不能是居家观察/健康教育
    if urgency in ("E0", "E1"):
        if mode in (CareMode.SELF_CARE.value, CareMode.HEALTH_EDUCATION.value):
            v.append("E0/E1 不允许居家观察或仅健康教育")
        if not plan.time.immediately:
            v.append("E0/E1 必须标记为立即行动")
        if not plan.primary_action:
            v.append("E0/E1 必须有明确的当前行动")
        if not plan.escalation_triggers:
            v.append("E0/E1 必须给出升级触发条件")

    # 信息缺失 + 低风险 → 必须保守（EU 或更紧急）；已确诊稳定复诊与居家观察除外
    if (plan.missing_critical_data and urgency in ("E4", "E5")
            and mode not in (CareMode.STABLE_FOLLOWUP.value, CareMode.SELF_CARE.value)):
        v.append("存在关键缺失信息时不得判为 E4/E5，应为 EU 或更紧急")

    # 未核实的机构：不能有 facility_id，也不能声称已落实
    if plan.destination.facility_id and not plan.destination.verified:
        v.append("未核实的机构不得写入 facility_id")
    if plan.destination.verified and not plan.destination.verification_receipt_id:
        v.append("已核实机构必须有 verification_receipt_id")

    # FEASIBLE / PATIENT_ACCEPTED：七项核验必须通过或有逐项豁免
    if plan.state in (PlanState.FEASIBLE.value, PlanState.PATIENT_ACCEPTED.value):
        exempt = {e.get("check") for e in plan.check_exemptions}
        missing = [c for c in ALL_FEASIBLE_CHECKS
                   if plan.feasible_checks.get(c) is not True and c not in exempt]
        if missing:
            v.append(f"计划标记 {plan.state} 但核验项未通过：{', '.join(missing)}")

    # 居家观察：必须有完整闭环
    if mode == CareMode.SELF_CARE.value:
        obs = plan.home_observation
        if obs is None:
            v.append("care_mode=self_care 必须有 home_observation")
        else:
            if not obs.active():
                v.append(f"self_care 的观察状态必须为 ACTIVE/REVIEW_PENDING，当前 {obs.state}")
            if not obs.approved_policy.get("rule_id"):
                v.append("居家观察必须有已批准的路径（approved_policy.rule_id）")
            if not obs.admission.get("eligible"):
                v.append("居家观察准入未通过")
            if not plan.followup.get("required") or not plan.followup.get("task_id"):
                v.append("居家观察必须创建主动追问任务（followup.required + task_id）")
            if not obs.next_review_at or not obs.maximum_end_at:
                v.append("居家观察必须有下一追问时间与最长观察截止")
            else:
                nxt, due, rea, end = (_dt(obs.next_review_at), _dt(obs.response_due_at),
                                      _dt(obs.reassessment_due_at), _dt(obs.maximum_end_at))
                if nxt and due and nxt > due:
                    v.append("next_review_at 不能晚于 response_due_at")
                if due and rea and due > rea:
                    v.append("response_due_at 不能晚于 reassessment_due_at")
                if rea and end and rea > end:
                    v.append("reassessment_due_at 不能晚于 maximum_end_at")
                if nxt and end and nxt > end:
                    v.append("next_review_at 不能晚于 maximum_end_at")

    # 观察状态不变量（任何模式）
    obs = plan.home_observation
    if obs is not None:
        order = [("next_review_at", "response_due_at"), ("response_due_at", "reassessment_due_at"),
                 ("reassessment_due_at", "maximum_end_at")]
        for a, b in order:
            da, db = _dt(getattr(obs, a)), _dt(getattr(obs, b))
            if da and db and da > db:
                v.append(f"{a} 不能晚于 {b}")
        for ext in obs.extensions:
            if not ext.get("clinician_approval_receipt_id"):
                v.append("延长观察必须有医师批准回执（clinician_approval_receipt_id）")
        if obs.state == ObservationState.COMPLETED.value and not obs.closure:
            v.append("COMPLETED 必须有 closure 证据（真实复评）")

    # 时间未知必须有理由
    if not plan.time.display_window and not plan.time.unknown_reason and urgency not in ("E0",):
        v.append("时间窗未知时必须给出 unknown_reason")
    return v


def clamp_urgency(plan: ActionablePlan, floor: str | None) -> ActionablePlan:
    """安全门控下界只能升不能降（模型输出不能低于确定性规则命中的等级）。"""
    if floor and floor in URGENCY_RANK:
        final = worst_urgency(plan.urgency, floor)
        if final != plan.urgency:
            plan.urgency_assessment = plan.urgency
            plan.urgency = final
            plan.consistency_warnings.append(f"紧急程度按安全门控下界提升为 {final}（模型原本 {plan.urgency_assessment}）")
    return plan


def make_eu_plan(case_id: str, reason: str, *, safety_flags: list[str] | None = None,
                 rule_id: str | None = None, rule_version: str | None = None,
                 primary_problem: str = "", stage: str = "DEMO") -> ActionablePlan:
    """保守兜底：无法安全分级时的人工/线下评估路径（绝不返回“没事/居家”）。"""
    return ActionablePlan(
        case_id=case_id, urgency=Urgency.EU.value, care_mode=CareMode.INITIAL_NAVIGATION.value,
        primary_problem=primary_problem or "暂不能安全判断",
        primary_action="尽快联系病历经理安排人工/线下评估，不要只在家等待",
        rule_id=rule_id, rule_version=rule_version,
        time=TimeWindow(immediately=False, display_window="尽快（今天内联系人工/线下评估）",
                        unknown_reason=reason),
        destination=Destination(care_setting="in_person",
                                required_capabilities=["可进行线下评估的医疗机构（含急诊）"],
                                entry_method="联系群内病历经理协助，或直接前往就近有急诊的医院"),
        escalation_triggers=[{"change": "症状加重或出现呼吸困难、意识改变、大出血等",
                              "action": "立即拨打急救电话或前往急诊", "urgency": "E0"}],
        safety_flags=safety_flags or [],
        missing_critical_data=[reason],
        condition_summary="目前掌握的信息还不足以安全判断病情方向，先安排人工/线下评估",
        possible_conditions=[{"name": "需要医生面诊/检查后才能判断", "likelihood": "待明确", "evidence": reason}],
        risks_to_exclude=["不能排除的危险情况（已按红旗规则优先筛查）"],
        rationale="关键信息不足或存在矛盾，先由医生做线下评估比在家猜测更安全",
        facility_level="可线下评估的医疗机构（含急诊）",
        confirm_method="由接诊医生结合查体和检查结果确认",
        human_review_required=True,
        fallback_action="若无法联系到人工，直接前往就近医院急诊评估",
        release_stage=stage,
    )


def deadline_from(anchor: datetime | None, hours: float | None) -> datetime | None:
    if anchor is None or hours is None:
        return None
    return anchor + timedelta(hours=hours)
