"""患者视图渲染：由有效计划**确定性**生成计划卡与纯文本（第 23 节）。

核心原则（第 23.4 节）：完整计划是事实源，患者视图是其投影，不重新自由生成临床方案。
- 核心行动、时间、去向、警示、安排状态由本模块按已审模板直接渲染
- 模型只能润色 `explanation` 等非关键表达，且必须通过一致性校验（check_patient_text）
- 首屏：一句建议 + 1–2 步 + 必要警示；关键安全信息直接可见，不折叠、不为字数删减
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.wecom.fd import care_network, risk_signals
from app.wecom.fd.contracts import (
    CareMode,
    PlanState,
    URGENCY_PATIENT_WORD,
    ActionablePlan,
    ObservationState,
    PatientPlanView,
    PlanCondition,
    PlanRisk,
    PlanTask,
    SafetyGroup,
    Urgency,
)

# 患者端不出现内部代码；这些词只用于工程判断
_INTERNAL_CODE_RE = re.compile(r"\bE[0-5]\b|\bEU\b|rule_id|JSON|schema", re.I)
# 危险场景下禁止出现的“等待/降级”表达（一致性门控）
_DOWNGRADE_WORDS = re.compile(r"明天|后天|过几天|先观察|再等等|等等看|应该没事|问题不大|不用去")


def fmt_dt(value: str | None, *, with_date: bool = True) -> str:
    if not value:
        return ""
    try:
        d = datetime.fromisoformat(value)
    except ValueError:
        return value
    week = "一二三四五六日"[d.weekday()]
    return d.strftime(f"%m月%d日（周{week}）%H:%M") if with_date else d.strftime("%H:%M")


def _entry(dest: Any) -> str:
    caps = "、".join(dest.required_capabilities[:3]) or "合适的医疗机构"
    if dest.facility_name and dest.verified:
        return f"{dest.facility_name}{('·' + dest.department) if dest.department else ''}（{dest.entry_method or '按官方入口进入'}）"
    if dest.department:
        return f"{dest.department}（需到具备能力：{caps}；当前尚未核实具体院区）"
    return f"需要具备：{caps}；当前尚未核实具体院区，请先联系病历经理协助"


def _where_text(plan: ActionablePlan, dest: Any) -> str:
    """机构级别 + 科室，拼成患者一眼能看懂的"去哪里"（去掉括号里的补充说明）。"""
    level = re.sub(r"（[^）]*）", "", plan.facility_level or "").strip()
    dept = re.sub(r"（[^）]*）", "", dest.department or "").strip()
    return "·".join([x for x in (level, dept) if x]) or "相应医疗机构"


def _clean_window(text: str | None) -> str:
    """时间窗取第一小句并去掉括号说明，避免模型把整句话塞进去导致标题过长。"""
    first = re.split(r"[，,]", (text or "").strip())[0]
    return re.sub(r"（[^）]*）", "", first).strip()


def _tests_detail(plan: ActionablePlan, limit: int = 6) -> str:
    """检查项按优先级列出（去掉重复的“（按/由医生判断）”限定词）；患者需要知道要查什么，不做截断。"""
    tests = [re.sub(r"（[^）]*医生[^）]*判断[^）]*）", "", t).strip() for t in plan.tests_needed[:limit]]
    return "、".join(t for t in tests if t)


def _brief_action(plan: ActionablePlan, window: str, facility: Any = None) -> str:
    """短建议：时限 + 去向 + 就诊/复诊，一句话就说完（不再用模型的长段落）。

    已推荐具体机构时标题直接落到机构名（患者一眼知道去哪家），否则退回机构级别+科室。
    """
    if facility is not None and getattr(facility, "name", ""):
        where = facility.name + (f"·{facility.department}" if facility.department else "")
    else:
        where = _where_text(plan, plan.destination)
    verb = "复诊" if plan.care_mode == CareMode.STABLE_FOLLOWUP.value else "就诊"
    if window:
        return f"{window}到{where}{verb}。"
    return f"尽快到{where}{verb}。"


def _same_topic(risk: str, change: str) -> bool:
    """粗粒度主题匹配（中文二元组互相包含）：用于把升级触发条件挂到对应风险上。"""
    grams = {risk[i:i + 2] for i in range(len(risk) - 1)} if len(risk) > 1 else {risk}
    return any(g and g in change for g in grams)


def _build_risk_signals(plan: ActionablePlan) -> list[PlanRisk]:
    """重点排除的风险 → 危险信号。

    优先用风险知识表；再把升级指征里同一主题的条件挂上去；都没有时用未使用的升级指征兜底，
    保证每条风险都能告诉患者「出现什么要立即就医」。同名风险去重。
    """
    triggers: list[tuple[str, str]] = []
    for t in plan.escalation_triggers:
        change = re.sub(r"^出现", "", str(t.get("change") or "").strip()).strip()
        if change:
            triggers.append((change, str(t.get("action") or "").strip()))
    used: set[int] = set()
    out: list[PlanRisk] = []
    seen: set[str] = set()
    for raw in plan.risks_to_exclude:
        name = _strip_internal(str(raw))
        if not name or name in seen:
            continue
        seen.add(name)
        signals = risk_signals.signals_for(name)
        action = ""
        for i, (change, act) in enumerate(triggers):
            if i in used or not _same_topic(name, change):
                continue
            if change not in signals:
                signals.append(change)
            action = action or act
            used.add(i)
        if not signals:  # 知识表没命中：用未使用的升级指征兜底
            for i, (change, act) in enumerate(triggers):
                if i in used:
                    continue
                signals.append(change)
                action = action or act
                used.add(i)
                if len(signals) >= 2:
                    break
        if not signals:
            signals = [f"{name}相关症状突然加重或出现新的严重不适"]
        out.append(PlanRisk(name=name, signals=signals[:4],
                            action=action or risk_signals.DEFAULT_ACTION))
    return out[:4]


_SAFETY_ORDER = ("emergency", "today", "soon")
_SAFETY_ACTION = {
    "emergency": "出现以下任一情况，立即拨打120或去最近急诊",
    "today": "出现以下任一情况，今天内就医",
    "soon": "出现以下任一情况，尽快就医",
}


def _safety_level(action: str) -> str:
    """按患者行动文案判断紧急程度：先看明确时限（今天内去急诊 ≠ 立即急诊）。"""
    text = action or ""
    if re.search(r"120|急救|立即|马上|呼叫", text):
        return "emergency"
    if re.search(r"当天|今天|当日|今日|\d+\s*小时内", text):
        return "today"
    if re.search(r"急诊", text):
        return "emergency"
    return "soon"


def _norm_signal(text: str) -> str:
    return re.sub(r"[\s，,。；;、（）()\u3000]", "", text or "")


def _build_safety_groups(plan: ActionablePlan, risks: list[PlanRisk]) -> list[SafetyGroup]:
    """把升级指征 + 风险危险信号合并去重，按行动紧急程度分组成首屏安全提醒。"""
    pairs: list[tuple[str, str]] = []
    for t in plan.escalation_triggers:
        change = re.sub(r"^出现", "", str(t.get("change") or "").strip()).strip()
        if change:
            pairs.append((_safety_level(str(t.get("action") or "")), change))
    for r in risks:
        for s in r.signals:
            pairs.append((_safety_level(r.action), s))
    pairs.sort(key=lambda p: _SAFETY_ORDER.index(p[0]))
    groups: dict[str, SafetyGroup] = {}
    seen: set[str] = set()
    for level, signal in pairs:
        key = _norm_signal(signal)
        if not key or key in seen:
            continue
        seen.add(key)
        group = groups.setdefault(level, SafetyGroup(level=level, action=_SAFETY_ACTION[level]))
        if len(group.signals) < 5:
            group.signals.append(signal)
    return [groups[l] for l in _SAFETY_ORDER if l in groups]


def _money(value: float) -> str:
    return f"¥{value:g}"


def _build_tasks(plan: ActionablePlan, urgency: str, dest: Any, *, emergency_number: str,
                 facility: Any = None) -> list[PlanTask]:
    """把结构化计划投影成「接下来要做」的任务清单。

    每条任务补齐四要素：任务（title）、时间（when）、地点（where）、花费（cost）；
    facility：已推荐的机构（提供具体入口与费用口径）。
    """
    tasks: list[PlanTask] = []
    window = _clean_window(plan.time.display_window) or "尽快"
    facility_name = getattr(facility, "name", "") if facility is not None else ""
    facility_dept = getattr(facility, "department", "") if facility is not None else ""
    facility_level = getattr(facility, "level", "") if facility is not None else ""
    entry = getattr(facility, "entry_method", "") if facility is not None else ""
    where_text = "·".join(x for x in (facility_name, facility_dept) if x) or "合适的医疗机构"
    reg_fee = care_network.registration_fee(facility_level) if facility_level else None
    tests_fee = (care_network.tests_cost(facility_level, plan.tests_needed)
                 if facility_level and plan.tests_needed else None)

    # 居家观察：观察动作 + 平台回访安排
    if plan.care_mode == CareMode.SELF_CARE.value:
        obs = plan.home_observation
        review_text = fmt_dt(obs.next_review_at) if obs is not None and obs.next_review_at else ""
        if obs is not None:
            action = (obs.current_action or "").strip()
            if action:
                # 观察动作常含多个分句：第一句做标题，其余放详情，避免任务标题变成一段话
                parts = [p.strip() for p in re.split(r"[；;]", action) if p.strip()]
                tasks.append(PlanTask(kind="observe", title=parts[0], detail="；".join(parts[1:]), status="active",
                                      when="观察期内", where="在家", cost="居家观察不产生检查费用"))
            if obs.next_review_at or obs.maximum_end_at:
                tasks.append(PlanTask(
                    kind="observe", title="等我们的主动回访", status="active",
                    when=review_text or "到回访时间", where="本群（不用跑医院）", cost="免费",
                    detail=f"{fmt_dt(obs.next_review_at)} 通过本群问你情况；最长观察到 {fmt_dt(obs.maximum_end_at)}；"
                           f"出现上面的变化请立即升级，不用等回访。"))
        for act in plan.preparation[:1]:
            tasks.append(PlanTask(kind="prepare", title=act, when="观察期内", where="在家", cost="按需自备"))
        return tasks[:4]

    # 急救：支持性动作（拨打/前往本身已在标题里，不重复）
    if urgency in (Urgency.E0.value, Urgency.E1.value):
        tasks.append(PlanTask(
            kind="go", title="身边有人时请他协助拨打，说清位置和症状",
            when="立即", where="原地（保持安静、等待急救）" if urgency == Urgency.E0.value else "最近医院急诊",
            cost="救治优先，费用按医保政策结算",
            detail="疑似卒中/心梗不要自行驾车" if urgency == Urgency.E0.value else "不要在家等待；无法自行前往请拨打120"))
        for act in plan.preparation[:2]:
            tasks.append(PlanTask(kind="prepare", title=act, when="出发前", where="随身携带", cost="无"))
        if plan.tests_needed:
            tasks.append(PlanTask(kind="check", title="到院后按医生安排检查", detail=_tests_detail(plan),
                                  when="到院后", where="急诊", cost="按急诊医保政策结算"))
        return tasks[:4]

    # 普通就医：先预约 → 带资料 → 到院检查
    state = plan.booking.get("state", "NOT_REQUESTED")
    if state == "CONFIRMED":
        booked_at = fmt_dt(plan.booking.get("appointment_at"))
        tasks.append(PlanTask(kind="book", title="预约已确认", status="confirmed",
                              when=booked_at or window, where=where_text,
                              cost=f"挂号费约 {_money(reg_fee)}（已含在总费用）" if reg_fee is not None else "挂号费以医院公示为准",
                              detail=(" · ".join(x for x in (facility_name, booked_at) if x))))
    elif urgency in (Urgency.E2.value, Urgency.E3.value, Urgency.E4.value):
        if entry:
            detail = f"通过{entry}"      # 机构入口本身含机构名
        elif facility_name:
            detail = f"通过{facility_name}官方渠道挂号"
        else:
            detail = "通过医院官方渠道挂号"
        tasks.append(PlanTask(kind="book", title="先完成预约挂号", status="todo",
                              when=window, where=where_text,
                              cost=f"挂号费约 {_money(reg_fee)}" if reg_fee is not None else "按医院公示价",
                              detail=f"{detail}；不方便时联系病历经理协助"))
    for act in plan.preparation[:2]:
        tasks.append(PlanTask(kind="prepare", title=act, when="出发前", where="在家准备、随身携带", cost="无"))
    if plan.tests_needed:
        tasks.append(PlanTask(kind="check", title="到院后按医生安排检查", detail=_tests_detail(plan),
                              when="就诊当天", where=where_text,
                              cost=f"检查费约 {_money(tests_fee)}（按总费用一次结算）" if tests_fee is not None else "以医院公示价为准"))
    return tasks[:5]


def render(plan: ActionablePlan, *, emergency_number: str = "120",
           revision_notice: str | None = None, profile: str = "") -> PatientPlanView:
    """把 ActionablePlan 渲染成患者计划卡（确定性）。

    profile：患者档案自由文本（从中识别生活区域，用于机构推荐与费用测算）。
    """
    u = plan.urgency
    dest = plan.destination
    window = _clean_window(plan.time.display_window)
    care_options = care_network.recommend(plan, profile)
    facility = care_options.options[0] if care_options and care_options.options else None
    view = PatientPlanView(
        case_id=plan.case_id, plan_version=plan.plan_version,
        rendered_at=datetime.now().isoformat(timespec="seconds"),
        urgency_word=URGENCY_PATIENT_WORD.get(u, ""), revision_notice=revision_notice,
    )
    bindings: dict[str, str] = {}

    # ---- 一句建议（headline）：短句，只讲「什么时候、去哪里、做什么」----
    # 不用模型生成的 primary_action（常把行动/自理/警示/预约全塞成一大段），
    # 由服务端按结构化字段确定性拼句，避免卡片首屏出现“文字墙”。
    if u == Urgency.E0.value:
        headline = f"现在请拨打{emergency_number}，不要等到明天。"
        bindings["primary_action"] = "headline"
    elif u == Urgency.E1.value:
        headline = "现在尽快去急诊评估，不要在家等待。"
        bindings["primary_action"] = "headline"
    elif plan.care_mode == CareMode.SELF_CARE.value:
        headline = "可以先居家观察，按下面的清单执行。"
        bindings["primary_action"] = "tasks"
        view.urgency_word = "居家观察"
    elif u == Urgency.EU.value:
        headline = "还不能安全判断，需要尽快线下或人工评估。"
        bindings["urgency"] = "headline"
    else:
        headline = _brief_action(plan, window, facility)
        bindings["primary_action"] = "headline"
    # 模型可能在正文里描述“截止时间由系统生成”之类的工程细节，统一清掉
    headline = re.sub(r"（[^）]*(系统|路径|服务端|批准)[^）]*）", "", headline).strip()
    if (window and not plan.time.immediately
            and plan.care_mode != CareMode.SELF_CARE.value
            and window not in headline):
        headline = (headline[:-1] + f"（{window}）。") if headline.endswith("。") else f"{headline}（{window}）"
    bindings["time.display_window"] = "headline"
    view.headline = _strip_internal(headline)

    # ---- 少量步骤 ----
    steps: list[str] = []
    if plan.care_mode == CareMode.SELF_CARE.value:
        obs = plan.home_observation
        if obs is not None and obs.current_action:
            steps.append(obs.current_action)
    elif u in (Urgency.E0.value, Urgency.E1.value):
        steps.append("身边有人时请他协助拨打并告诉急救人员位置和症状；疑似卒中/心梗不要自行驾车")
    elif facility is not None:
        steps.append(f"{facility.name}·{facility.department}（{facility.entry_method or '按官方入口进入'}）")
    else:
        level = f"{plan.facility_level}·" if plan.facility_level else ""
        steps.append(level + _entry(dest))
    if plan.tests_needed:
        steps.append("医生可能会安排检查：" + "、".join(plan.tests_needed[:4]))
    if plan.confirm_method:
        steps.append(f"如何确诊：{plan.confirm_method}")
    for act in (plan.preparation[:1] if plan.preparation else []):
        steps.append(act)
    if plan.booking.get("state") not in (None, "NOT_REQUESTED", "CONFIRMED"):
        steps.append(f"预约状态：{plan.booking.get('state')}（尚未拿到确认回执前不要默认已挂上号）")
    view.steps = [_strip_internal(s) for s in steps[:5]]
    # ---- 任务清单（卡片的主视图）----
    view.tasks = [PlanTask(title=_strip_internal(t.title), detail=_strip_internal(t.detail),
                           kind=t.kind, status=t.status,
                           when=_strip_internal(t.when), where=_strip_internal(t.where),
                           cost=_strip_internal(t.cost))
                  for t in _build_tasks(plan, u, dest, emergency_number=emergency_number, facility=facility)
                  if _strip_internal(t.title)]
    bindings["tasks"] = "tasks"

    # ---- 必要警示（全部直接可见）----
    alerts: list[str] = []
    for t in plan.escalation_triggers:
        # 模型给的 change 常自带“出现”，避免患者文本里出现“如果出现出现…”
        change = re.sub(r"^出现", "", (t.get("change") or "").strip()).strip()
        action = (t.get("action") or "").strip()
        if change and action:
            alerts.append(_strip_internal(f"如果出现{change}，请{action}"))
    if u == Urgency.E0.value:
        alerts.insert(0, "不要为了继续聊天耽误求助，也不要等家属回复后再打")
    if not alerts and u in (Urgency.E0.value, Urgency.E1.value, Urgency.E2.value, Urgency.E3.value):
        alerts.append(f"症状加重或出现呼吸困难、意识改变、大出血等，请立即拨打{emergency_number}或前往急诊")
    view.alerts = alerts

    # ---- 安排事实 / 观察回访 ----
    if plan.care_mode == CareMode.SELF_CARE.value and plan.home_observation:
        obs = plan.home_observation
        view.observation_review_note = _strip_internal(
            f"目前可在家观察到 {fmt_dt(obs.maximum_end_at)}；我们会在 {fmt_dt(obs.next_review_at)} "
            f"通过本群再问你情况，出现上面任一变化请立即升级，不用等回访。")
        bindings["home_observation.next_review_at"] = "observation_review_note"
        bindings["home_observation.maximum_end_at"] = "observation_review_note"
    elif u in (Urgency.E2.value, Urgency.E3.value):
        state = plan.booking.get("state", "NOT_REQUESTED")
        if state == "CONFIRMED":
            view.arrangement_note = f"预约已确认：{fmt_dt(plan.booking.get('appointment_at'))}"
        else:
            view.arrangement_note = "当前尚未预约（仍需你或病历经理按上面的方式落实）"
        bindings["booking.state"] = "arrangement_note"

    # ---- 未落实事项 ----
    if plan.unresolved_dependencies:
        view.arrangement_note = ((view.arrangement_note + "；") if view.arrangement_note else "") + \
            "尚未落实：" + "；".join(plan.unresolved_dependencies[:3])
        bindings["unresolved_dependencies"] = "arrangement_note"

    view.must_show_bindings = bindings

    # ---- 病情说明与决策依据（患者要有知情和理解，才愿意配合）----
    view.condition_summary = _strip_internal(plan.condition_summary)
    view.possible_conditions = []
    for c in plan.possible_conditions[:3]:
        name = _strip_internal(str(c.get("name", "")))
        likelihood = _strip_internal(str(c.get("likelihood", "")))
        if not name:
            continue
        if likelihood and likelihood not in name and not name.endswith(f"（{likelihood}）"):
            name = f"{name}（{likelihood}）"
        view.possible_conditions.append(name)
    view.risks_to_exclude = [_strip_internal(r) for r in plan.risks_to_exclude[:4]]
    # 重点排除风险 + 危险信号（去重、可用升级指征兜底）
    view.risk_signals = _build_risk_signals(plan)
    bindings["risks_to_exclude"] = "risk_signals"
    # 首屏安全提醒：升级指征 + 风险信号合并去重，按紧急程度分组
    view.safety_groups = _build_safety_groups(plan, view.risk_signals)
    if view.safety_groups:
        bindings["escalation_triggers"] = "safety_groups"
    view.time_window = window
    # 推荐机构 + 医保费用测算（急症/居家观察/线上不生成；已在标题/任务里引用）
    view.care_options = care_options
    if view.care_options is not None:
        bindings["care_options"] = "care_options"
    # 疑似诊断（结构）：名称里的“（较可能）/（需要优先排除）”提到 likelihood，避免重复
    view.conditions = []
    for c in plan.possible_conditions[:3]:
        name = _strip_internal(str(c.get("name", "")))
        likelihood = _strip_internal(str(c.get("likelihood", "")))
        evidence = _strip_internal(str(c.get("evidence", "")))
        if not likelihood:
            found = re.search(r"（(较可能|需优先排除|需要优先排除|待明确)）", name)
            if found:
                likelihood = found.group(1)
        name = re.sub(r"（(较可能|需优先排除|需要优先排除|待明确)）", "", name).strip()
        if name:
            view.conditions.append(PlanCondition(name=name, likelihood=likelihood, evidence=evidence))
    view.rationale = _strip_internal(plan.rationale)
    view.facility_level = plan.facility_level
    view.tests_needed = [_strip_internal(t) for t in plan.tests_needed[:6]]
    view.confirm_method = _strip_internal(plan.confirm_method)
    view.doctor_review_note = plan.doctor_review_note
    if view.condition_summary:
        bindings["condition_summary"] = "details"
    if view.rationale:
        bindings["rationale"] = "details"
    return view


def _strip_internal(text: str) -> str:
    if not text:
        return ""
    return _INTERNAL_CODE_RE.sub("", text).strip()


def check_patient_text(view: PatientPlanView, plan: ActionablePlan) -> list[str]:
    """服务端语义一致性门控（第 23.4 / X24 节）：返回违规列表，空 = 通过。"""
    v: list[str] = []
    text = view.text()
    if _INTERNAL_CODE_RE.search(text):
        v.append("患者可见文本包含内部代码/工程字段")
    if plan.urgency == Urgency.E0.value:
        if not re.search(r"(拨打|呼叫|120|急救|急诊)", text):
            v.append("E0 计划未明确给出急救行动")
        if _DOWNGRADE_WORDS.search(text) and not re.search(r"不要(等到?)?明天|不能等", text):
            v.append("E0 计划出现等待/降级表达")
    if plan.care_mode == CareMode.SELF_CARE.value:
        if "立即" not in text and "升级" not in text:
            v.append("居家观察计划缺少升级指征")
        if not view.observation_review_note:
            v.append("居家观察计划缺少主动回访安排")
    if plan.booking.get("state") == "CONFIRMED":
        if "预约已确认" not in text:
            v.append("预约已确认但患者文本未说明")
    elif re.search(r"预约已确认|已挂(上)?号|预约成功|已约(好|上)", text):
        v.append("未拿到回执却声称预约成功")
    if plan.state == PlanState.FEASIBLE.value and "尚未核实" in text:
        v.append("计划可执行但文本仍显示未核实")
    if not view.headline:
        v.append("缺少首要行动")
    # 结论必须讲清楚病情与依据；非急症结论必须说明真人医生审核（标准服务），不引导患者再去找医生
    if plan.care_mode in (CareMode.EMERGENCY.value, CareMode.IN_PERSON.value, CareMode.STABLE_FOLLOWUP.value,
                          CareMode.SELF_CARE.value):
        if not view.condition_summary:
            v.append("缺少病情说明（condition_summary）")
        if not view.rationale:
            v.append("缺少决策依据（rationale）")
        if plan.urgency not in (Urgency.E0.value, Urgency.E1.value) and not view.doctor_review_note:
            v.append("非急症结论缺少真人医生审核说明")
    if re.search(r"找医生卡片|点击卡片|线上问诊", text):
        v.append("患者文本出现找医生/线上问诊引导（标准服务由平台医生审核结论，不引导患者再问诊）")
    return v


def card_extra(view: PatientPlanView, plan: ActionablePlan) -> dict[str, Any]:
    """计划卡消息的 extra（前端渲染用），不含内部代码。"""
    return {
        "kind": "plan_card",
        "view": view.model_dump(),
        "urgency": plan.urgency,        # 前端按紧急程度换色（E0/E1 红、E2/E3 橙、E4/E5 蓝、EU 紫、居家观察绿）
        "urgency_word": view.urgency_word,
        "care_mode": plan.care_mode,
        "release_stage": plan.release_stage,
        "clinician_review": plan.human_review_required,
        "missing_critical_data": plan.missing_critical_data[:3],
    }


def observation_followup_text(obs: Any, *, emergency_number: str = "120") -> str:
    """主动追问消息（第 24.8 节）：提出可回答的问题，不是“记得观察”。"""
    items = [i.get("field") for i in (obs.observation_items or []) if i.get("field")][:2]
    changes = [re.sub(r"^出现", "", r.get("patient_condition") or "")
               for r in (obs.escalation_rules or []) if r.get("patient_condition")][:2]
    q1 = f"和上次相比，{'、'.join(items) or '症状'}是好一些、差不多，还是更重了？" if items else "和上次相比，症状是好一些、差不多，还是更重了？"
    q2 = f"现在有没有出现：{'；'.join(changes)}？" if changes else "现在有没有出现新的不适？"
    return (f"到复评时间了。{q1}\n{q2}\n"
            f"如果出现上述变化，请立即拨打{emergency_number}或前往急诊，不用等我们下一次回复。")
