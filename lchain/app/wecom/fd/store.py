"""家庭医生 Agent 的持久化：病例/计划/居家观察任务（MySQL）。

与 app/wecom/store.py 同样的 session_scope 模式；表在应用启动时由 Base.metadata.create_all 自动创建。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from app.core.db import session_scope
from app.wecom.fd.contracts import ActionablePlan, HomeObservationPlan, ObservationState
from app.wecom.models import FdCase, FdObservation


def _case_dict(row: FdCase) -> dict[str, Any]:
    return {"case_id": row.case_id, "group_id": row.group_id, "plan_version": row.plan_version,
            "urgency": row.urgency, "care_mode": row.care_mode, "state": row.state,
            "plan": row.plan, "consultation": row.consultation, "release_stage": row.release_stage,
            "updated_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else None}


def _obs_dict(row: FdObservation) -> dict[str, Any]:
    return {"observation_id": row.observation_id, "group_id": row.group_id, "case_id": row.case_id,
            "plan_version": row.plan_version, "state": row.state, "plan": row.plan,
            "idem_key": row.idem_key,
            "maximum_end_at": row.maximum_end_at.isoformat(timespec="seconds") if row.maximum_end_at else None,
            "next_review_at": row.next_review_at.isoformat(timespec="seconds") if row.next_review_at else None,
            "response_due_at": row.response_due_at.isoformat(timespec="seconds") if row.response_due_at else None,
            "reassessment_due_at": row.reassessment_due_at.isoformat(timespec="seconds") if row.reassessment_due_at else None,
            "last_contact_at": row.last_contact_at.isoformat(timespec="seconds") if row.last_contact_at else None,
            "contact_count": row.contact_count, "failure_reason": row.failure_reason,
            "updated_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else None}


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def get_case(group_id: str) -> dict[str, Any] | None:
    async with session_scope() as s:
        row = (await s.execute(select(FdCase).where(FdCase.group_id == group_id))).scalar_one_or_none()
        return _case_dict(row) if row else None


async def get_case_by_id(case_id: str) -> dict[str, Any] | None:
    async with session_scope() as s:
        row = (await s.execute(select(FdCase).where(FdCase.case_id == case_id))).scalar_one_or_none()
        return _case_dict(row) if row else None


async def save_plan(group_id: str, plan: ActionablePlan, *, idem_key: str | None = None) -> dict[str, Any]:
    """写入/更新当前有效计划：新计划版本 +1，旧版本由 plan_version 标识（不再展示）。

    idem_key：同一 run 重放时只写一次（断点续跑/节点重试不重复升版本）。
    """
    async with session_scope() as s:
        row = (await s.execute(select(FdCase).where(FdCase.group_id == group_id))).scalar_one_or_none()
        if row is not None and idem_key and row.last_idem_key == idem_key:
            plan.plan_version = row.plan_version or 1
            return _case_dict(row)
        if row is None:
            row = FdCase(case_id=plan.case_id or uuid.uuid4().hex[:16], group_id=group_id,
                         plan_version=1, plan=plan.model_dump(mode="json"), last_idem_key=idem_key)
            s.add(row)
        else:
            row.plan_version = (row.plan_version or 0) + 1
            plan.plan_version = row.plan_version
            row.urgency, row.care_mode, row.state = plan.urgency, plan.care_mode, plan.state
            row.plan, row.release_stage = plan.model_dump(mode="json"), plan.release_stage
            row.last_idem_key = idem_key
        await s.flush()
        return _case_dict(row)


async def latest_plan(group_id: str) -> ActionablePlan | None:
    c = await get_case(group_id)
    if not c or not c.get("plan"):
        return None
    try:
        return ActionablePlan.model_validate(c["plan"])
    except Exception:  # noqa: BLE001
        return None


async def save_consultation(group_id: str, consultation: dict[str, Any] | None,
                            *, case_id: str | None = None) -> str | None:
    """保存多轮问诊进度；没有病例行时创建 DRAFT 病例（plan_version=0，尚无结论）。"""
    if consultation is None:
        return None
    async with session_scope() as s:
        row = (await s.execute(select(FdCase).where(FdCase.group_id == group_id))).scalar_one_or_none()
        if row is None:
            row = FdCase(case_id=case_id or uuid.uuid4().hex[:16], group_id=group_id,
                         plan_version=0, state="DRAFT", consultation=consultation)
            s.add(row)
        else:
            row.consultation = consultation
        await s.flush()
        return row.case_id


async def active_observation(group_id: str) -> dict[str, Any] | None:
    """当前进行中的观察（ACTIVE / REVIEW_PENDING / CONTACT_OVERDUE 都算未结束）。"""
    async with session_scope() as s:
        stmt = (select(FdObservation).where(
            FdObservation.group_id == group_id,
            FdObservation.state.in_([ObservationState.ACTIVE.value, ObservationState.REVIEW_PENDING.value,
                                     ObservationState.CONTACT_OVERDUE.value]))
            .order_by(FdObservation.created_at.desc()).limit(1))
        row = (await s.execute(stmt)).scalar_one_or_none()
        return _obs_dict(row) if row else None


async def create_observation(group_id: str, obs: HomeObservationPlan, *, idem_key: str | None = None) -> dict[str, Any]:
    async with session_scope() as s:
        if idem_key:
            old = (await s.execute(select(FdObservation).where(
                FdObservation.idem_key == idem_key))).scalar_one_or_none()
            if old is not None:
                return _obs_dict(old)
        row = FdObservation(
            observation_id=obs.observation_id, group_id=group_id, case_id=obs.case_id,
            plan_version=obs.plan_version, state=obs.state, plan=obs.model_dump(mode="json"),
            idem_key=idem_key,
            maximum_end_at=_dt(obs.maximum_end_at), next_review_at=_dt(obs.next_review_at),
            response_due_at=_dt(obs.response_due_at), reassessment_due_at=_dt(obs.reassessment_due_at),
        )
        s.add(row)
        await s.flush()
        return _obs_dict(row)


async def update_observation(observation_id: str, obs: HomeObservationPlan, *, state: str | None = None,
                             last_contact_at: datetime | None = None, contact_count: int | None = None,
                             failure_reason: str | None = None) -> dict[str, Any] | None:
    async with session_scope() as s:
        row = (await s.execute(select(FdObservation).where(
            FdObservation.observation_id == observation_id))).scalar_one_or_none()
        if row is None:
            return None
        row.plan = obs.model_dump(mode="json")
        row.state = state or obs.state
        row.plan_version = obs.plan_version
        row.next_review_at = _dt(obs.next_review_at)
        row.response_due_at = _dt(obs.response_due_at)
        row.reassessment_due_at = _dt(obs.reassessment_due_at)
        row.maximum_end_at = _dt(obs.maximum_end_at)
        if last_contact_at is not None:
            row.last_contact_at = last_contact_at
        if contact_count is not None:
            row.contact_count = contact_count
        if failure_reason is not None:
            row.failure_reason = failure_reason
        await s.flush()
        return _obs_dict(row)


async def get_observation(observation_id: str) -> dict[str, Any] | None:
    async with session_scope() as s:
        row = (await s.execute(select(FdObservation).where(
            FdObservation.observation_id == observation_id))).scalar_one_or_none()
        return _obs_dict(row) if row else None


async def due_observations(now: datetime | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """到主动追问时间的观察（ACTIVE 且 next_review_at <= now）。"""
    now = now or datetime.now()
    async with session_scope() as s:
        stmt = (select(FdObservation).where(
            FdObservation.state == ObservationState.ACTIVE.value,
            FdObservation.next_review_at.is_not(None),
            FdObservation.next_review_at <= now).order_by(FdObservation.next_review_at).limit(limit))
        return [_obs_dict(r) for r in (await s.execute(stmt)).scalars().all()]


async def overdue_observations(now: datetime | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """回应窗口已过仍未复评的观察（REVIEW_PENDING 且 response_due_at <= now）；不推断好转。"""
    now = now or datetime.now()
    async with session_scope() as s:
        stmt = (select(FdObservation).where(
            FdObservation.state == ObservationState.REVIEW_PENDING.value,
            FdObservation.response_due_at.is_not(None),
            FdObservation.response_due_at <= now).order_by(FdObservation.response_due_at).limit(limit))
        return [_obs_dict(r) for r in (await s.execute(stmt)).scalars().all()]


async def expired_observations(now: datetime | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """超过最长观察截止且未结束（ACTIVE / REVIEW_PENDING / CONTACT_OVERDUE）。"""
    now = now or datetime.now()
    states = [ObservationState.ACTIVE.value, ObservationState.REVIEW_PENDING.value,
              ObservationState.CONTACT_OVERDUE.value]
    async with session_scope() as s:
        stmt = (select(FdObservation).where(
            FdObservation.state.in_(states),
            FdObservation.maximum_end_at.is_not(None),
            FdObservation.maximum_end_at <= now).order_by(FdObservation.maximum_end_at).limit(limit))
        return [_obs_dict(r) for r in (await s.execute(stmt)).scalars().all()]


async def list_observations(group_id: str, limit: int = 10) -> list[dict[str, Any]]:
    async with session_scope() as s:
        stmt = (select(FdObservation).where(FdObservation.group_id == group_id)
                .order_by(FdObservation.created_at.desc()).limit(limit))
        return [_obs_dict(r) for r in (await s.execute(stmt)).scalars().all()]


async def claim_state(observation_id: str, from_states: list[str], to_state: str) -> bool:
    """原子状态跃迁（定时任务多进程/重入时只允许一个成功）。"""
    async with session_scope() as s:
        r = await s.execute(update(FdObservation).where(
            FdObservation.observation_id == observation_id,
            FdObservation.state.in_(from_states)).values(state=to_state))
        return (r.rowcount or 0) == 1


async def mark_contacted(observation_id: str) -> None:
    """记录一次真实的主动追问（更新最后联系时间与次数）。"""
    async with session_scope() as s:
        row = (await s.execute(select(FdObservation).where(
            FdObservation.observation_id == observation_id))).scalar_one_or_none()
        if row is not None:
            row.last_contact_at = datetime.now()
            row.contact_count = (row.contact_count or 0) + 1
