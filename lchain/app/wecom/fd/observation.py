"""居家观察闭环（第 24 节）：准入、时限、主动追问、复评与退出。

本模块只负责**规则计算与状态机**；消息发送、调度与持久化分别在 graph / jobs / fd.store 中。
关键约束：
- 观察时限来自已批准路径，不由模型猜数字；缺路径/缺时限 → 不允许 self_care
- 主动追问必须由真实任务触发（调度器 job），仅“在聊天里说会问”不算
- 未回复 ≠ 好转；到期 ≠ 完成；延长必须有医师批准回执
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from app.wecom.fd.contracts import HomeObservationPlan, ObservationState, Urgency
from app.wecom.fd.pathways import ObservationPathway
from app.wecom.fd.red_flags import ScreenResult, REVIEW_STATUS


def admission_checks(pathway: ObservationPathway | None, screen: ScreenResult,
                     *, patient_can_observe: bool = True, task_ready: bool = True,
                     modifiers: list[str] | None = None) -> dict[str, Any]:
    """第 24.1 节六项准入检查。任一未知/失败 → 不允许居家观察。"""
    mods = modifiers if modifiers is not None else screen.modifiers
    checks: dict[str, bool | None] = {
        "pathway_valid": bool(pathway is not None and pathway.review_status),
        "key_risks_assessed": not screen.matched,
        "condition_fits": bool(pathway is not None),
        "high_risk_modifiers_handled": None,
        "patient_can_observe": patient_can_observe,
        "followup_executable": task_ready,
    }
    unresolved: list[str] = []
    if pathway is None:
        unresolved.append("没有匹配的已批准低风险路径")
    if screen.matched:
        unresolved.append("已命中红旗规则：" + "、".join(m["rule_id"] for m in screen.matched))
    if mods:
        # 高危修饰项：本演示路径均不覆盖孕产/婴幼儿/免疫抑制等，交由线下评估
        risky = [m for m in mods if m in ("pregnancy", "infant", "immunosuppressed", "transplant", "dialysis",
                                          "anticoagulant", "cancer", "elderly", "recent_surgery")]
        checks["high_risk_modifiers_handled"] = not risky
        if risky:
            unresolved.append("存在高风险修饰项，需要医师按更严格路径评估：" + "、".join(risky))
    else:
        checks["high_risk_modifiers_handled"] = True
    if not patient_can_observe:
        unresolved.append("患者/照护者无法识别变化或无法及时求助")
    if not task_ready:
        unresolved.append("主动追问任务/调度不可用")

    required = ("pathway_valid", "key_risks_assessed", "condition_fits",
                "high_risk_modifiers_handled", "patient_can_observe", "followup_executable")
    eligible = all(checks.get(k) is True for k in required) and not unresolved
    return {"eligible": eligible, "checks": checks, "unresolved_items": unresolved,
            "review_status": REVIEW_STATUS}


def build_observation_plan(case_id: str, plan_version: int, pathway: ObservationPathway, *,
                           now: datetime | None = None, task_id: str | None = None,
                           channel_receipt_id: str | None = None,
                           emergency_number: str = "120") -> HomeObservationPlan:
    """按已批准路径生成观察计划（时间全部来自路径，不由模型决定）。"""
    now = now or datetime.now()
    escalation = [{**r, "action": (r.get("action") or "").format(emergency_number=emergency_number)}
                  for r in pathway.escalation_rules]
    obs = HomeObservationPlan(
        observation_id=uuid.uuid4().hex[:16],
        case_id=case_id,
        plan_version=plan_version,
        state=ObservationState.ACTIVE.value,
        approved_policy={"rule_id": pathway.pathway_id, "version": pathway.version,
                         "approval_receipt_id": pathway.approval_receipt_id or f"DEMO-APPROVAL-{pathway.pathway_id}",
                         "clinician_review_receipt_id": None,
                         "review_status": pathway.review_status},
        admission={"eligible": True, "checks": {}, "unresolved_items": []},
        started_at=now.isoformat(timespec="seconds"),
        anchor_source="first_observed",
        maximum_end_at=(now + timedelta(hours=pathway.max_hours)).isoformat(timespec="seconds"),
        next_review_at=(now + timedelta(minutes=pathway.next_review_minutes)).isoformat(timespec="seconds"),
        response_due_at=(now + timedelta(minutes=pathway.response_due_minutes)).isoformat(timespec="seconds"),
        reassessment_due_at=(now + timedelta(hours=pathway.reassessment_due_hours)).isoformat(timespec="seconds"),
        contact_policy_id=f"contact-{pathway.pathway_id}",
        owner_queue_id=pathway.owner_queue_id,
        scheduler_receipt_id=task_id,
        channel_authorization_receipt_id=channel_receipt_id or "group-channel",
        followup_task_ids=[task_id] if task_id else [],
        observation_items=[dict(x) for x in pathway.observation_items],
        escalation_rules=escalation,
        current_action=pathway.current_action,
    )
    obs.admission = admission_checks(pathway, ScreenResult(), task_ready=bool(task_id))
    return obs


DECISION_TO_STATE = {
    "CONTINUE": ObservationState.ACTIVE.value,
    "ESCALATE": ObservationState.ESCALATED.value,
    "NEEDS_REVIEW": ObservationState.NEEDS_REVIEW.value,
    "END": ObservationState.COMPLETED.value,
}


def apply_review(obs: HomeObservationPlan, *, decision: str, review_id: str | None = None,
                 task_id: str | None = None, facts: dict[str, Any] | None = None,
                 next_review_at: datetime | None = None, reason: str = "") -> HomeObservationPlan:
    """记录一次真实复评并推进状态（第 24.6 节）。延长时间必须另行提供医师批准。"""
    now = datetime.now().isoformat(timespec="seconds")
    rec = {"review_id": review_id or uuid.uuid4().hex[:12], "task_id": task_id,
           "observation_time": now, "received_at": now, "assessed_at": now,
           "facts_source_ids": list((facts or {}).keys()),
           "decision": decision, "decision_rule_id": reason or "fd-followup",
           "clinician_receipt_id": None, "next_task_id": None}
    obs.reviews = [*obs.reviews, rec]
    obs.state = DECISION_TO_STATE.get(decision, ObservationState.NEEDS_REVIEW.value)
    if decision == "CONTINUE" and next_review_at is not None:
        obs.next_review_at = next_review_at.isoformat(timespec="seconds")
        obs.response_due_at = (next_review_at + timedelta(hours=12)).isoformat(timespec="seconds")
        rec["next_task_id"] = None
    if decision == "END":
        obs.closure = {"at": now, "rule_id": reason or "pathway-end", "verified_source_ids": list((facts or {}).keys())}
    return obs


def extend_observation(obs: HomeObservationPlan, *, new_end_at: datetime, approval_receipt_id: str,
                       reason: str) -> HomeObservationPlan:
    """延长观察：必须带医师批准回执，且只能延长、不能重置起点。"""
    if not approval_receipt_id:
        raise ValueError("延长观察必须有医师批准回执")
    old = obs.maximum_end_at
    if old and datetime.fromisoformat(old) >= new_end_at:
        raise ValueError("新的观察截止必须晚于原截止")
    obs.extensions = [*obs.extensions, {"old_end_at": old, "new_end_at": new_end_at.isoformat(timespec="seconds"),
                                        "clinician_approval_receipt_id": approval_receipt_id, "reason": reason}]
    obs.maximum_end_at = new_end_at.isoformat(timespec="seconds")
    return obs


def mark_contact_overdue(obs: HomeObservationPlan) -> HomeObservationPlan:
    """未回复：标记为待确认（不推断好转、不自动关闭）。"""
    if obs.state in (ObservationState.ACTIVE.value, ObservationState.REVIEW_PENDING.value):
        obs.state = ObservationState.CONTACT_OVERDUE.value
    return obs


def mark_review_pending(obs: HomeObservationPlan, *, task_id: str | None = None) -> HomeObservationPlan:
    if obs.state == ObservationState.ACTIVE.value:
        obs.state = ObservationState.REVIEW_PENDING.value
    if task_id and task_id not in obs.followup_task_ids:
        obs.followup_task_ids = [*obs.followup_task_ids, task_id]
    return obs


def is_expired(obs: HomeObservationPlan, *, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    end = obs.maximum_end_at
    return bool(end and datetime.fromisoformat(end) <= now)


def patient_urgency_for_decision(decision: str, urgency: str | None) -> str:
    if decision == "ESCALATE":
        return urgency if urgency in (Urgency.E0.value, Urgency.E1.value) else Urgency.E1.value
    if decision == "NEEDS_REVIEW":
        return Urgency.E2.value
    if decision == "END":
        return Urgency.E5.value
    return urgency or Urgency.E4.value
