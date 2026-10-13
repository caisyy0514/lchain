"""家庭医生 Agent 引擎。

流水线（每一步都可单测）：
  1. 确定性安全门控（red_flags.screen）—— 已知危险信号立即产出已审行动，不做完整推理
  2. 非危急病例调用 fd-triage（LLM 结构化输出）
  3. 服务端合并与门控：紧急程度只能升不能降；缺关键信息 → EU；self_care 必须有已批准路径 + 观察闭环
  4. 构建 ActionablePlan → 校验不变量 → 持久化 → 确定性渲染患者计划卡
  5. 居家观察：创建真实观察任务（调度器扫描到期）或按复评结果推进状态

模型不可用 / 输出非法 / 规则缺失时一律走保守兜底（EU + 人工/线下评估），不返回“没事/居家”。
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.core.config import get_settings
from app.wecom.fd import render as fd_render
from app.wecom.fd import store as fd_store
from app.wecom.fd.contracts import (
    ActionablePlan,
    BookingState,
    CareMode,
    Destination,
    HandoffState,
    HomeObservationPlan,
    ObservationState,
    PlanState,
    TriageOutput,
    TimeWindow,
    Urgency,
    clamp_urgency,
    make_eu_plan,
    validate_plan,
    worst_urgency,
)
from app.wecom.fd.observation import (
    admission_checks,
    apply_review,
    build_observation_plan,
    mark_contact_overdue,
    patient_urgency_for_decision,
)
from app.wecom.fd.pathways import find_pathway, pathway_by_id
from app.wecom.fd.prompts import FD_PROMPT_TIER  # noqa: F401  注册模型档位
from app.wecom.fd.red_flags import GENERAL_ESCALATION_HINTS, ScreenResult, screen

logger = logging.getLogger(__name__)

_CATEGORY_CAPABILITIES: dict[str, list[str]] = {
    "cardio": ["急诊/急救", "24 小时胸痛中心/心内科"],
    "neuro": ["急诊/急救", "卒中中心（可溶栓/取栓）"],
    "resp": ["急诊/急救", "呼吸科（可吸氧/监护）"],
    "infection": ["急诊/急救（儿童需儿科急诊）"],
    "abdomen": ["急诊外科"],
    "metabolism": ["急诊/内分泌（可查血糖血气）"],
    "obstetric": ["产科急诊（有分娩与手术条件）"],
    "pediatric": ["儿科急诊"],
    "trauma": ["急诊/创伤中心"],
    "vascular": ["急诊/血管外科"],
    "oncology": ["肿瘤科/急诊（可处理粒缺发热）"],
    "transplant": ["移植中心/急诊"],
    "hematology": ["急诊/血液科"],
    "mental": ["精神科急诊/危机干预"],
    "eye": ["眼科急诊"],
    "ent": ["急诊/耳鼻喉科"],
    "generic": ["有急诊能力的医院"],
}
_EMERGENCY_KINDS = {"E0", "E1"}


@dataclass
class FdOutcome:
    plan: ActionablePlan | None = None
    view: fd_render.PatientPlanView | None = None
    text: str = ""
    screen: ScreenResult | None = None
    emergency: bool = False
    notify_manager: bool = False
    push_doctor_card: bool = False
    observation_id: str | None = None
    followup: dict[str, Any] | None = None
    triage: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    # 多轮问诊
    kind: str = "plan"                       # plan / question / emergency
    questions: list[str] = field(default_factory=list)
    consultation: dict[str, Any] | None = None
    case_id: str | None = None


def _capabilities(categories: list[str]) -> list[str]:
    out: list[str] = []
    for c in categories:
        for x in _CATEGORY_CAPABILITIES.get(c, _CATEGORY_CAPABILITIES["generic"]):
            if x not in out:
                out.append(x)
    return out or list(_CATEGORY_CAPABILITIES["generic"])


def _emergency_triggers(screen_res: ScreenResult) -> list[dict[str, Any]]:
    triggers: list[dict[str, Any]] = []
    for m in screen_res.matched[:3]:
        triggers.append({"change": m["title"], "action": m["action"].format(emergency_number=_num()),
                         "urgency": m["urgency"], "rule_id": m["rule_id"]})
    if len(triggers) < 2:
        for hint in GENERAL_ESCALATION_HINTS[:2]:
            triggers.append({"change": hint, "action": "立即拨打急救电话或前往急诊", "urgency": "E0"})
    return triggers[:3]


def _num() -> str:
    return get_settings().wecom_fd_emergency_number or "120"


def emergency_plan(case_id: str, screen_res: ScreenResult, *, primary_problem: str = "") -> ActionablePlan:
    """确定性急救计划：直接用红旗规则里的已审行动，不等待 LLM。"""
    worst = min(screen_res.matched, key=lambda m: {"E0": 0, "E1": 1}.get(m["urgency"], 2))
    cats = [m["category"] for m in screen_res.matched]
    action = worst["action"].format(emergency_number=_num())
    return ActionablePlan(
        case_id=case_id,
        state=PlanState.NEEDS_VERIFICATION.value,
        urgency=worst["urgency"],
        care_mode=CareMode.EMERGENCY.value,
        primary_problem=primary_problem or worst["title"],
        primary_action=action,
        rule_id=worst["rule_id"], rule_version=screen_res.version,
        time=TimeWindow(immediately=True, anchor_source="first_observed",
                        display_window="立即（现在就拨打急救电话或前往急诊）"),
        destination=Destination(care_setting="emergency", required_capabilities=_capabilities(cats),
                                entry_method="拨打急救电话 / 直接前往有急诊能力的医院"),
        feasible_checks={"medical_fit": True, "within_deadline": True, "resource_verified": None,
                         "entry_known": True, "patient_can_reach": None, "support_ready": True,
                         "fallback_ready": True},
        escalation_triggers=_emergency_triggers(screen_res),
        safety_flags=[f"红旗:{m['rule_id']}" for m in screen_res.matched],
        evidence_ids=[m["rule_id"] for m in screen_res.matched],
        fallback_action="如果无法自行前往，请旁人或邻居协助拨打急救电话",
        human_review_required=True,
        condition_summary=f"你描述的情况符合「{worst['title']}」的紧急表现，需要立即处理",
        possible_conditions=[{"name": m["title"], "likelihood": "需立即处理",
                              "evidence": "、".join(m.get("evidence") or [])} for m in screen_res.matched[:3]],
        risks_to_exclude=[m["title"] for m in screen_res.matched[:3]],
        rationale="这类情况每一分钟都可能影响结果，先把人送到能急救的地方，比继续分析原因更重要，所以不需要等问诊、审核或预约。",
        facility_level="急救/急诊（有抢救条件的医院）",
        tests_needed=[],
        confirm_method="由急诊医生根据心电图、影像、化验等结果确认",
    )


def _build_plan_from_triage(case_id: str, t: TriageOutput, screen_res: ScreenResult) -> ActionablePlan:
    urgency = t.urgency if t.urgency in {u.value for u in Urgency} else Urgency.EU.value
    care_mode = t.care_mode if t.care_mode in {m.value for m in CareMode} else CareMode.INITIAL_NAVIGATION.value
    deadline = None
    if t.deadline_hours is not None:
        try:
            deadline = (datetime.now() + timedelta(hours=float(t.deadline_hours))).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            deadline = None
    plan = ActionablePlan(
        case_id=case_id,
        state=PlanState.NEEDS_VERIFICATION.value,
        urgency=urgency,
        care_mode=care_mode,
        primary_problem=t.primary_problem or "（未提供）",
        primary_action=t.action_now or "",
        rule_id=(screen_res.matched[0]["rule_id"] if screen_res.matched else None),
        rule_version=screen_res.version if screen_res.matched else None,
        time=TimeWindow(immediately=urgency in _EMERGENCY_KINDS,
                        deadline=deadline, display_window=t.time_window,
                        unknown_reason=None if t.time_window else "模型未给出明确时间窗，需人工确认"),
        destination=Destination(care_setting=t.care_setting if t.care_setting in
                                ("emergency", "in_person", "online", "self_care") else "in_person",
                                required_capabilities=t.required_capabilities
                                or _capabilities([m["category"] for m in screen_res.matched]),
                                department=t.department or None,
                                entry_method=None),
        escalation_triggers=[{"change": e.change, "action": e.action, "urgency": e.urgency}
                             for e in t.escalation_triggers if e.change and e.action]
                             or _emergency_triggers(screen_res)[-2:],
        safety_flags=t.safety_flags,
        evidence_ids=[m["rule_id"] for m in screen_res.matched],
        missing_critical_data=t.missing_critical_data,
        preparation=t.preparation,
        fallback_action="症状加重或无法按计划就诊时，立即联系病历经理或前往急诊",
        condition_summary=t.condition_summary,
        possible_conditions=[{"name": c.name, "likelihood": c.likelihood, "evidence": c.evidence}
                             for c in t.possible_conditions if c.name][:3],
        risks_to_exclude=[r for r in t.risks_to_exclude if r][:4],
        rationale=t.rationale,
        facility_level=t.facility_level,
        tests_needed=[x for x in t.tests_needed if x][:6],
        confirm_method=t.confirm_method,
    )
    return plan


def _apply_server_gates(plan: ActionablePlan, t: TriageOutput, screen_res: ScreenResult,
                        *, pathway: Any, admission: dict[str, Any], observation_ok: bool,
                        stage: str) -> tuple[ActionablePlan, list[str], HomeObservationPlan | None]:
    """服务端门控：返回 (计划, 警告, 观察计划或 None)。"""
    warnings: list[str] = []
    obs: HomeObservationPlan | None = None

    plan = clamp_urgency(plan, screen_res.urgency_floor)
    if plan.urgency in _EMERGENCY_KINDS:
        plan.care_mode = CareMode.EMERGENCY.value
        plan.time.immediately = True
        plan.destination.care_setting = "emergency"
    elif plan.care_mode == CareMode.EMERGENCY.value:
        # 模式与紧急程度矛盾（如 EU + emergency）：按线下评估处理，避免患者端显示自我矛盾的计划
        plan.care_mode = CareMode.IN_PERSON.value
        warnings.append("emergency 模式与紧急程度不一致，已按线下评估处理")

    # 信息不足 + 低风险 → EU（不得 E4/E5）；已确诊稳定复诊与居家观察除外：
    # ・稳定复诊：缺常规复查资料不改变路径；・居家观察：待测观察项正是计划的一部分，由准入检查把关
    if (plan.missing_critical_data and plan.urgency in ("E4", "E5")
            and plan.care_mode not in (CareMode.STABLE_FOLLOWUP.value, CareMode.SELF_CARE.value)):
        warnings.append("存在关键缺失信息，降级为 EU（需人工/线下评估）")
        plan.urgency = Urgency.EU.value
    if (t.confidence_status == "uncertain" and plan.urgency in ("E4", "E5")
            and plan.care_mode not in (CareMode.STABLE_FOLLOWUP.value, CareMode.SELF_CARE.value)):
        warnings.append("模型自评不确定，降级为 EU")
        plan.urgency = Urgency.EU.value

    # 居家观察门控：self_care 只属于非紧急计划路径
    if plan.care_mode == CareMode.SELF_CARE.value:
        if plan.urgency in ("E0", "E1", "E2", "E3"):
            warnings.append(f"{plan.urgency} 与居家观察冲突，改为线下评估")
            plan.care_mode = CareMode.IN_PERSON.value
        elif plan.urgency in ("E5", "EU"):
            warnings.append(f"self_care 与 {plan.urgency} 冲突，按非紧急居家观察归一到 E4")
            plan.urgency = "E4"
    if plan.care_mode == CareMode.SELF_CARE.value:
        if not observation_ok:
            warnings.append("主动追问/调度不可用，居家观察不成立，改为线下评估")
            plan.care_mode = CareMode.IN_PERSON.value
            plan.urgency = worst_urgency(plan.urgency, "E3")
            plan.unresolved_dependencies.append("平台主动追问能力不可用")
        elif pathway is None:
            warnings.append("没有匹配的已批准低风险路径，居家观察不成立，改为线下评估")
            plan.care_mode = CareMode.IN_PERSON.value
            plan.urgency = worst_urgency(plan.urgency, "E3")
            plan.unresolved_dependencies.append("无已批准居家观察路径，需线下评估")
        elif not admission.get("eligible"):
            warnings.append("居家观察准入未通过：" + "；".join(admission.get("unresolved_items") or []))
            plan.care_mode = CareMode.IN_PERSON.value
            plan.urgency = worst_urgency(plan.urgency, "E3")
        else:
            obs = build_observation_plan(plan.case_id, plan.plan_version, pathway,
                                         task_id=f"fd-task-{uuid.uuid4().hex[:10]}",
                                         emergency_number=_num())
            obs.admission = admission
            plan.home_observation = obs
            plan.observation_review_at = obs.next_review_at
            plan.followup = {"required": True, "task_id": obs.followup_task_ids[0],
                             "due_at": obs.next_review_at, "owner_queue_id": obs.owner_queue_id,
                             "assignee_id": None, "unavailable_reason": None}
            plan.time.display_window = plan.time.display_window or f"居家观察到 {obs.maximum_end_at[:16]}"
            plan.destination = Destination(care_setting="self_care", required_capabilities=[],
                                           entry_method="在本群按观察计划执行，出现升级指征立即就医")

    # 非就诊场景清理：不能让患者看到空的医院字段
    if plan.care_mode in (CareMode.SELF_CARE.value, CareMode.HEALTH_EDUCATION.value):
        plan.booking = {"state": BookingState.NOT_REQUESTED.value, "request_id": None,
                        "receipt_id": None, "appointment_at": None}
    elif plan.care_mode in (CareMode.EMERGENCY.value, CareMode.IN_PERSON.value) \
            and plan.facility_level in ("", "居家观察", "线上复诊"):
        # 线下/急诊计划不能把“机构级别”写成居家或线上（模型输出不一致时服务端纠正）
        plan.facility_level = ("急救/急诊（有抢救条件的医院）" if plan.care_mode == CareMode.EMERGENCY.value
                               else "可线下评估的医疗机构（含急诊）")
        warnings.append("机构级别与照护模式不一致，已按线下/急诊纠正")

    if plan.urgency == Urgency.EU.value:
        plan.human_review_required = True
        plan.handoff = {"state": HandoffState.REQUESTED.value, "task_id": None, "assignee_id": None}
        plan.fallback_action = plan.fallback_action or "无法联系人工时，直接前往就近医院急诊评估"

    plan.release_stage = stage
    return plan, warnings, obs


async def assess(*, group_id: str, profile: str, user_text: str, context: str,
                 cfg: RunnableConfig | None = None, overrides: dict[str, str] | None = None,
                 existing_case_id: str | None = None, idem_key: str | None = None,
                 consultation: dict[str, Any] | None = None) -> FdOutcome:
    """非观察场景：多轮问诊 → 分级诊疗结论。

    - 信息不足：只输出 1–2 个最能改变决策的追问，不急着下结论（普通病例至少 min_rounds 轮）
    - 急症：直接给已审行动，不等待问诊/审核
    - 结论：包含病情说明、可能方向、需排除风险、机构级别/科室/检查/确诊方式，并提交真人医生审核
    """
    from app.wecom.llm import call_json  # 延迟导入，避免循环依赖

    s = get_settings()
    stage = s.wecom_fd_release_stage.upper()
    case_id = existing_case_id or uuid.uuid4().hex[:16]
    screen_res = screen(user_text, profile=profile, emergency_number=_num())
    plan_idem = f"{idem_key}:plan" if idem_key else None
    conf = dict(consultation or {})
    rounds = int(conf.get("rounds") or 0)
    now = datetime.now().isoformat(timespec="seconds")

    # 1) 已知危险信号：立即给已审行动，不调用模型、不等待问诊
    if screen_res.emergency:
        plan = emergency_plan(case_id, screen_res)
        plan.release_stage = stage
        await fd_store.save_plan(group_id, plan, idem_key=plan_idem)
        view = fd_render.render(plan, emergency_number=_num(), profile=profile)
        view_issues = fd_render.check_patient_text(view, plan)
        _log_issues(view_issues)
        conf.update({"status": "concluded", "rounds": rounds + 1, "concluded_at": now,
                     "possible_conditions": [c["name"] for c in plan.possible_conditions],
                     "risks_to_exclude": plan.risks_to_exclude, "updated_at": now})
        return FdOutcome(plan=plan, view=view, text=view.chat_text(), screen=screen_res, emergency=True,
                         notify_manager=True, kind="emergency", triage={"source": "deterministic"},
                         consultation=conf, case_id=case_id, warnings=view_issues)

    # 2) LLM 结构化分诊（失败 → 保守兜底）
    digest = screen_res.prompt_digest(_num())
    known = conf.get("facts") or {}
    asked = conf.get("questions_asked") or []
    consult_digest = (f"已完成问诊轮次：{rounds}\n"
                      f"已确认信息：{json.dumps(known, ensure_ascii=False) if known else '（无）'}\n"
                      f"已问过的问题：{'；'.join(asked[-6:]) if asked else '（无）'}")
    user_content = (f"## 确定性安全初筛（服务端，不得降级）\n{digest}\n\n"
                    f"## 问诊进度\n{consult_digest}\n\n"
                    f"## 对话与病例上下文\n{context}\n\n"
                    f"## 患者当前消息\n{user_text}")
    try:
        raw, _ = await call_json("fd-triage", user_content, cfg=cfg, overrides=overrides)
        t = TriageOutput.model_validate(raw)
    except Exception as e:  # noqa: BLE001
        logger.exception("fd-triage 失败，走保守兜底：%s", e)
        plan = make_eu_plan(case_id, f"分诊模型调用/解析失败：{type(e).__name__}", stage=stage,
                            safety_flags=[f"红旗:{m['rule_id']}" for m in screen_res.matched])
        plan.doctor_review_note = _review_note()
        await fd_store.save_plan(group_id, plan, idem_key=plan_idem)
        view = fd_render.render(plan, emergency_number=_num(), profile=profile)
        conf.update({"status": "concluded", "rounds": rounds + 1, "concluded_at": now, "updated_at": now})
        return FdOutcome(plan=plan, view=view, text=view.chat_text(), screen=screen_res, kind="plan",
                         notify_manager=bool(screen_res.matched), triage={"source": "fallback", "error": str(e)},
                         consultation=conf, case_id=case_id)

    # 2.5) 多轮问诊：信息不足时先追问，不急着下结论
    warnings_prefix: list[str] = []
    questions = [q.strip() for q in (t.questions or []) if q and q.strip()]
    if not questions and t.question:
        questions = [t.question.strip()]
    questions = questions[:2]
    rounds_after = rounds + 1
    is_question = t.status == "need_more_info" and bool(questions)
    min_rounds = max(0, int(s.wecom_fd_min_rounds))
    if (not is_question and rounds < min_rounds and t.urgency not in _EMERGENCY_KINDS
            and t.care_mode != CareMode.STABLE_FOLLOWUP.value):
        # 普通病例：前 min_rounds 轮不因模型“想直接结伦”而过早给结论（急症/稳定复诊除外）
        is_question = True
        warnings_prefix.append(f"服务端要求至少 {min_rounds} 轮问诊，本轮先追问")
        if not questions:
            questions = _fallback_questions(t)
    if is_question and rounds_after > max(1, int(s.wecom_fd_max_rounds)):
        is_question = False
        warnings_prefix.append("已达到最大问诊轮次，按现有信息给保守结论")

    if is_question:
        facts = {**(conf.get("facts") or {}), **(t.collected_facts or {})}
        conf2 = {**conf, "status": "in_progress", "rounds": rounds_after, "facts": facts,
                 "raw_text": ((conf.get("raw_text") or "") + " " + user_text)[-2000:],
                 "questions_asked": [*(conf.get("questions_asked") or []), *questions][-12:],
                 "preliminary": [c.name for c in t.possible_conditions if c.name][:3],
                 "updated_at": now}
        return FdOutcome(kind="question", text=_questions_text(questions, t), screen=screen_res,
                         questions=questions, triage=t.model_dump(), consultation=conf2,
                         case_id=case_id, warnings=warnings_prefix)

    # 3) 服务端门控
    observation_ok = bool(s.wecom_fd_observation_enabled)
    pathway = None
    admission: dict[str, Any] = {}
    if t.self_care.eligible:
        # 路径匹配只用「患者原始描述 + 已确认事实」；不能用模型总结（会出现“需排除脱水”之类措辞误触排除项）
        facts_text = " ".join(str(x) for x in {**known, **(t.collected_facts or {})}.values())
        pathway_text = " ".join([conf.get("raw_text") or "", facts_text, t.primary_problem, user_text])
        pathway = pathway_by_id(t.self_care.pathway_id or "", release_stage=stage) or \
            find_pathway(pathway_text, release_stage=stage)
        admission = admission_checks(pathway, screen_res, task_ready=observation_ok)

    plan = _build_plan_from_triage(case_id, t, screen_res)
    plan, warnings, obs = _apply_server_gates(plan, t, screen_res, pathway=pathway, admission=admission,
                                              observation_ok=observation_ok, stage=stage)
    warnings = [*warnings_prefix, *warnings]
    # 结论统一提交真人医生审核（标准服务）；患者端不引导去找医生问诊
    plan.human_review_required = True
    if plan.urgency not in _EMERGENCY_KINDS:
        plan.handoff = {"state": HandoffState.REQUESTED.value, "task_id": None, "assignee_id": None}
        plan.doctor_review_note = _review_note()

    # 4) 先做不变量校验（在创建观察任务/落库之前），不通过就保守兜底，避免留下“没有有效计划的观察任务”
    if obs is not None:
        obs.followup_task_ids = ["pending"]
        plan.home_observation = obs
        plan.followup = {"required": True, "task_id": "pending", "due_at": obs.next_review_at,
                         "owner_queue_id": obs.owner_queue_id, "assignee_id": None, "unavailable_reason": None}
    violations = validate_plan(plan)
    if violations:
        logger.warning("家庭医生计划不变量校验失败：%s", violations)
        plan = make_eu_plan(case_id, "计划校验未通过：" + "；".join(violations[:3]),
                            safety_flags=plan.safety_flags, rule_id=plan.rule_id,
                            rule_version=plan.rule_version, primary_problem=plan.primary_problem, stage=stage)
        obs = None
        warnings.extend(violations)

    # 5) 创建观察任务（幂等）→ 回填真实 task_id → 保存计划；回填后仍异常时取消观察任务
    if obs is not None:
        row = await fd_store.create_observation(group_id, obs,
                                                idem_key=f"{idem_key}:obs" if idem_key else None)
        oid = row["observation_id"]
        obs.observation_id = oid
        obs.followup_task_ids = [oid]
        obs.scheduler_receipt_id = oid
        plan.home_observation = obs
        plan.followup = {"required": True, "task_id": oid, "due_at": obs.next_review_at,
                         "owner_queue_id": obs.owner_queue_id, "assignee_id": None, "unavailable_reason": None}
    saved = await fd_store.save_plan(group_id, plan, idem_key=plan_idem)
    plan.plan_version = saved["plan_version"]
    after = validate_plan(plan)
    if after:
        logger.warning("回填观察任务后校验失败：%s", after)
        if obs is not None:
            obs.state = ObservationState.CANCELLED.value
            await fd_store.update_observation(obs.observation_id, obs)
        plan = make_eu_plan(case_id, "计划校验未通过：" + "；".join(after[:3]),
                            safety_flags=plan.safety_flags, rule_id=plan.rule_id,
                            rule_version=plan.rule_version, primary_problem=plan.primary_problem, stage=stage)
        saved = await fd_store.save_plan(group_id, plan, idem_key=f"{idem_key}:fallback" if idem_key else None)
        plan.plan_version = saved["plan_version"]
        warnings.extend(after)

    view = fd_render.render(plan, emergency_number=_num(), profile=profile)
    issues = fd_render.check_patient_text(view, plan)
    _log_issues(issues)
    warnings.extend(issues)
    plan.consistency_warnings = [*plan.consistency_warnings, *issues]
    verdict = {}
    if plan.care_mode == CareMode.SELF_CARE.value and plan.home_observation is not None:
        verdict = {"observation_id": plan.home_observation.observation_id,
                   "next_review_at": plan.home_observation.next_review_at}
    facts = {**(conf.get("facts") or {}), **(t.collected_facts or {})}
    conf2 = {**conf, "status": "concluded", "rounds": rounds_after, "facts": facts,
             "raw_text": ((conf.get("raw_text") or "") + " " + user_text)[-2000:],
             "possible_conditions": [c["name"] for c in plan.possible_conditions][:3],
             "risks_to_exclude": plan.risks_to_exclude[:4], "concluded_at": now, "updated_at": now}
    return FdOutcome(plan=plan, view=view, text=view.chat_text(), screen=screen_res, kind="plan",
                     emergency=plan.urgency in _EMERGENCY_KINDS,
                     notify_manager=plan.urgency in _EMERGENCY_KINDS,
                     push_doctor_card=False,
                     observation_id=verdict.get("observation_id"),
                     followup=None if not verdict else {"observation_id": verdict["observation_id"]},
                     triage=t.model_dump(), warnings=warnings, consultation=conf2, case_id=case_id)


async def review_observation(*, group_id: str, obs_row: dict[str, Any], user_text: str, context: str,
                             profile: str = "",
                             cfg: RunnableConfig | None = None,
                             overrides: dict[str, str] | None = None,
                             idem_key: str | None = None) -> FdOutcome:
    """居家观察到时/患者主动报告时的复评（第 24.4–24.6 节）。"""
    from app.wecom.llm import call_json

    s = get_settings()
    stage = s.wecom_fd_release_stage.upper()
    obs = HomeObservationPlan.model_validate(obs_row["plan"])
    case_id = obs.case_id
    screen_res = screen(user_text, profile=profile, emergency_number=_num())

    # 已知危险信号：立即升级，不等待复评问卷
    if screen_res.emergency:
        plan = emergency_plan(case_id, screen_res, primary_problem="居家观察期间出现危险信号")
        plan.release_stage = stage
        obs = apply_review(obs, decision="ESCALATE", review_id=f"{idem_key}:review" if idem_key else None,
                           facts={"screen": screen_res.to_dict()}, reason="确定性红旗升级")
        await fd_store.update_observation(obs.observation_id, obs)
        await fd_store.save_plan(group_id, plan, idem_key=f"{idem_key}:emergency" if idem_key else None)
        view = fd_render.render(plan, emergency_number=_num(), profile=profile)
        return FdOutcome(plan=plan, view=view, text=view.text(), screen=screen_res, emergency=True,
                         notify_manager=True, observation_id=obs.observation_id,
                         review={"decision": "ESCALATE", "source": "deterministic"}, triage={"source": "deterministic"})

    obs_items = "；".join(str(i.get("field")) for i in obs.observation_items)
    esc = "；".join(str(r.get("patient_condition")) for r in obs.escalation_rules)
    digest = screen_res.prompt_digest(_num())
    user_content = (f"## 观察计划\n最长观察截止：{obs.maximum_end_at}\n下一追问时间：{obs.next_review_at}\n"
                    f"现在怎么做：{obs.current_action}\n观察项：{obs_items}\n升级指征：{esc}\n"
                    f"此前复评次数：{len(obs.reviews)}\n\n## 确定性安全初筛\n{digest}\n\n"
                    f"## 对话上下文\n{context}\n\n## 患者本次回复\n{user_text}")
    try:
        raw, _ = await call_json("fd-followup", user_content, cfg=cfg, overrides=overrides)
    except Exception as e:  # noqa: BLE001
        logger.exception("fd-followup 失败：%s", e)
        raw = {"decision": "NEEDS_REVIEW", "urgency": "E2", "assessment": "复评模型不可用，需要人工复核",
               "action_now": "尽快联系病历经理安排线下评估", "unresolved": ["模型调用失败"]}
    decision = str(raw.get("decision") or "NEEDS_REVIEW").upper()
    if decision not in ("CONTINUE", "ESCALATE", "NEEDS_REVIEW", "END"):
        decision = "NEEDS_REVIEW"

    # 超过最长截止：不允许继续观察
    from app.wecom.fd.observation import is_expired
    if is_expired(obs) and decision == "CONTINUE":
        decision = "NEEDS_REVIEW"
        raw["assessment"] = (str(raw.get("assessment") or "") + "（已超过最长观察截止，不能继续在家观察）").strip()

    urgency = patient_urgency_for_decision(decision, str(raw.get("urgency") or ""))
    pathway = pathway_by_id(obs.approved_policy.get("rule_id", ""), release_stage=stage)
    next_review = None
    if decision == "CONTINUE" and pathway is not None:
        candidate = datetime.now() + timedelta(minutes=pathway.next_review_minutes)
        end = datetime.fromisoformat(obs.maximum_end_at) if obs.maximum_end_at else None
        if end and candidate >= end:
            decision = "NEEDS_REVIEW"
            urgency = "E3"
            raw["assessment"] = (str(raw.get("assessment") or "") + "（已接近最长观察截止，需要线下复评）").strip()
        else:
            next_review = candidate

    review_id = f"{idem_key}:review" if idem_key else None
    already = bool(review_id and any(r.get("review_id") == review_id for r in obs.reviews))
    if not already:
        obs = apply_review(obs, decision=decision, review_id=review_id, facts={"reply": user_text[:200]},
                           next_review_at=next_review, reason="fd-followup")
    plan = ActionablePlan(
        case_id=case_id, state=PlanState.NEEDS_VERIFICATION.value, urgency=urgency,
        care_mode=CareMode.SELF_CARE.value if decision == "CONTINUE" else (
            CareMode.STABLE_FOLLOWUP.value if decision == "END" else CareMode.IN_PERSON.value),
        primary_problem="居家观察复评",
        primary_action=str(raw.get("action_now") or "") or (obs.current_action if decision == "CONTINUE" else ""),
        rule_id=obs.approved_policy.get("rule_id"), rule_version=obs.approved_policy.get("version"),
        time=TimeWindow(display_window="继续按观察计划执行" if decision == "CONTINUE" else "尽快完成线下评估",
                        immediately=urgency == "E0"),
        destination=Destination(care_setting="self_care" if decision == "CONTINUE" else "in_person",
                                required_capabilities=[] if decision == "CONTINUE" else
                                ["有急诊/相关专科能力的医院"],
                                entry_method="在本群按观察计划执行" if decision == "CONTINUE" else "联系病历经理或直接前往医院"),
        escalation_triggers=[{"change": str(r.get("patient_condition")), "action": str(r.get("action")),
                              "urgency": str(r.get("urgency"))} for r in obs.escalation_rules][:4],
        missing_critical_data=list(raw.get("unresolved") or []),
        human_review_required=decision in ("NEEDS_REVIEW", "ESCALATE"),
        release_stage=stage,
        condition_summary=str(raw.get("assessment") or ("居家观察复评" if decision == "CONTINUE" else "观察期间出现变化")),
        possible_conditions=[{"name": obs.approved_policy.get("rule_id") or "已批准的低风险观察路径",
                              "likelihood": "观察中" if decision == "CONTINUE" else "需线下确认",
                              "evidence": str(raw.get("assessment") or "")[:120]}],
        risks_to_exclude=[str(r.get("patient_condition")) for r in obs.escalation_rules][:4]
        or ["症状加重或出现呼吸困难、意识改变、大出血等"],
        rationale="根据已批准的观察计划做复评：有升级指征就转线下评估，不继续在家等。",
        facility_level="居家观察（按计划）" if decision == "CONTINUE" else "可线下评估的医疗机构（含急诊）",
        confirm_method="由接诊医生结合查体和检查结果确认",
        doctor_review_note=None if decision == "ESCALATE" else _review_note(),
        followup={"required": decision == "CONTINUE", "task_id": obs.observation_id if decision == "CONTINUE" else None,
                  "due_at": obs.next_review_at if decision == "CONTINUE" else None,
                  "owner_queue_id": obs.owner_queue_id, "assignee_id": None,
                  "unavailable_reason": None if decision == "CONTINUE" else "观察已结束或转线下"},
    )
    if decision == "CONTINUE":
        plan.home_observation = obs
        plan.observation_review_at = obs.next_review_at
    saved = await fd_store.save_plan(group_id, plan, idem_key=f"{idem_key}:review" if idem_key else None)
    plan.plan_version = saved["plan_version"]
    if not already:
        await fd_store.update_observation(obs.observation_id, obs)
    violations = validate_plan(plan)
    if violations:
        logger.warning("复评计划不变量校验失败：%s", violations)
    view = fd_render.render(plan, emergency_number=_num(), profile=profile)
    issues = fd_render.check_patient_text(view, plan)
    _log_issues(issues)
    text = view.text()
    if raw.get("assessment"):
        text = f"{text}\n（复评：{str(raw['assessment'])[:60]}）"
    if raw.get("question"):
        text = f"{text}\n{str(raw['question'])}"
    return FdOutcome(plan=plan, view=view, text=text, screen=screen_res,
                     emergency=decision == "ESCALATE",
                     notify_manager=decision in ("ESCALATE", "NEEDS_REVIEW"),
                     push_doctor_card=decision in ("NEEDS_REVIEW", "ESCALATE"),
                     observation_id=obs.observation_id, review={"decision": decision, **(raw or {})},
                     triage={"source": "fd-followup"})


def _log_issues(issues: list[str]) -> None:
    if issues:
        logger.warning("患者文本一致性门控命中：%s", issues)


def _review_note() -> str:
    """标准服务：结论由真人医生审核（患者端不引导去线上问诊/找医生卡片）。"""
    return ("本结论已提交平台医生审核；医生审核意见如有调整会在本群同步。"
            "审核期间请按上面的时间安排执行，症状变化随时在群里说。")


def _fallback_questions(t: TriageOutput) -> list[str]:
    """模型没给出追问时，从缺失关键信息生成追问（最多 2 个）。"""
    qs: list[str] = []
    for item in (t.missing_critical_data or [])[:2]:
        item = str(item).strip().rstrip("？?")
        if item:
            qs.append(f"{item}？" if not item.endswith(("吗", "呢")) else item)
    if not qs:
        qs = ["症状从什么时候开始，现在比刚开始是好转、差不多还是加重？",
              "有没有发烧、呼吸困难、意识改变或出血等表现？"]
    return qs[:2]


def _questions_text(questions: list[str], t: TriageOutput) -> str:
    """问诊阶段的回复：先简短回应，再问最能改变去向的 1–2 个问题。"""
    lines = ["我想再确认几个关键点，这会直接影响去哪个级别的医院、挂什么科和做什么检查："]
    for i, q in enumerate(questions[:2], 1):
        lines.append(f"{i}. {q}")
    if t.explanation:
        lines.append(t.explanation[:80])
    return "\n".join(lines)


async def observation_due_message(obs_row: dict[str, Any]) -> str:
    """调度器发送的主动追问文本（真实任务触发，见 jobs.fd_followup_review）。"""
    obs = HomeObservationPlan.model_validate(obs_row["plan"])
    return fd_render.observation_followup_text(obs, emergency_number=_num())


def due_state_after_contact(obs: HomeObservationPlan) -> HomeObservationPlan:
    from app.wecom.fd.observation import mark_review_pending
    return mark_review_pending(obs)
