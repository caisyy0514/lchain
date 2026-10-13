"""定时任务（只在独立的定时任务进程 `python -m app.scheduler` 中运行，web 节点不跑）。

| 任务 | 间隔 | 说明 |
| --- | --- | --- |
| check_overdue | WECOM_JOB_INTERVAL_SECONDS | 待回复超过 WECOM_PENDING_TIMEOUT_SECONDS 群里无人回复 -> @病历经理 |
| redeliver | WECOM_JOB_INTERVAL_SECONDS | 投递失败 / 长时间未投递的回复补投递 |
| takeover_dead_nodes | WECOM_JOB_INTERVAL_SECONDS | 失联 web 节点遗留的未完成任务由定时任务进程接管执行 |
| cleanup_checkpoints | 每小时 | 删除已完成执行的过期 checkpoint |

每个任务本身也是幂等的（通知先原子标记再发送、补投递先认领再投递），即使误部署多个定时任务进程
或手动触发与定时执行重叠，也不会重复通知 / 重复发送；正常情况下由 scheduler 的单例锁保证只有一个进程在跑。
"""
from __future__ import annotations

import logging

from app.core.config import get_settings
from app.wecom import store, templates
from app.wecom.outbound import emit

logger = logging.getLogger(__name__)


async def check_overdue() -> list[dict]:
    s = get_settings()
    out = []
    for p in await store.overdue_pending(s.wecom_pending_timeout_seconds):
        # 先原子标记为 notified，标记成功的才发通知（防并发重复通知）
        if not await store.claim_pending_notify(p.id):
            continue
        g = await store.get_group(p.group_id)
        m = await store.get_message(p.message_id)
        if not g or not m:
            continue
        content = templates.TIMEOUT_NOTIFY.format(group=g.name, sender=m.sender_name or m.sender_id,
                                                  minutes=max(1, s.wecom_pending_timeout_seconds // 60),
                                                  content=(m.content or "[文件]")[:200])
        msg = await emit(g.group_id, kind="notify", content=f"@{g.case_manager_name or '病历经理'} {content}",
                         mentions=[g.case_manager_id] if g.case_manager_id else None,
                         extra={"pending_id": p.id, "reply_to": m.id},
                         idem_key=f"timeout:{p.id}")  # 幂等：同一条待回复记录只通知一次
        logger.info("待回复超时，已通知病历经理：group=%s message=%s", g.group_id, m.id)
        out.append(msg)
    return out


async def redeliver() -> int:
    """补投递：只处理创建超过宽限期的消息，刚生成的消息由 web 节点自己投递。"""
    from app.wecom.outbound import redeliver_pending

    return await redeliver_pending(older_than_seconds=get_settings().wecom_redeliver_grace_seconds)


async def takeover_dead_nodes() -> list[str]:
    """接管失联 web 节点（心跳超时）遗留的未完成任务。

    节点失联后它的群锁会随连接断开自动释放；这里在定时任务进程内按同样的规则续跑 / 重新处理，
    幂等键保证已经发出去的消息不会重复发送。接管完成后把节点标记为 dead，之后该节点如果恢复，
    只会处理新的消息（它的旧任务 node_id 已改成定时任务进程）。
    """
    from app.core.cluster import dead_web_nodes, mark_node
    from app.wecom.dispatcher import recover_tasks, wait_recovered

    dead = await dead_web_nodes()
    if not dead:
        return []
    s = get_settings()
    recovered = await recover_tasks(dead, max_age_seconds=s.wecom_resume_max_age_seconds, redeliver=False)
    for n in dead:
        await mark_node(n, "dead")
    if recovered:
        logger.warning("接管失联节点 %s 的未完成任务：%s", dead, recovered)
        await wait_recovered()
    return recovered


async def cleanup_checkpoints() -> int:
    """删除已完成执行的 checkpoint（保留 WECOM_CHECKPOINT_TTL_DAYS 天，便于排查）。"""
    from app.wecom.checkpoint import cleanup, ttl_deadline

    ids = await store.finished_run_ids_before(ttl_deadline())
    return await cleanup(ids) if ids else 0


# --------------------------------------------------------------------------- 家庭医生·居家观察闭环
async def _fd_enabled() -> bool:
    s = get_settings()
    return bool(s.wecom_fd_enabled and s.wecom_fd_observation_enabled)


async def fd_followup_review() -> list[dict]:
    """到点主动追问：真实任务触发（观察行持久化在 MySQL），不是靠模型“记得回来”。"""
    if not await _fd_enabled():
        return []
    from app.wecom.fd import render as fd_render
    from app.wecom.fd import store as fd_store
    from app.wecom.fd.contracts import ObservationState

    s = get_settings()
    out: list[dict] = []
    for o in await fd_store.due_observations():
        # 原子标记：同一个任务被多个进程看到时只发一次
        if not await fd_store.claim_state(o["observation_id"], [ObservationState.ACTIVE.value],
                                         ObservationState.REVIEW_PENDING.value):
            continue
        obs = o["plan"].get("observation_id") if isinstance(o.get("plan"), dict) else None
        text = await _fd_followup_text(o)
        msg = await emit(o["group_id"], kind="text", content=text, sender_name=s.wecom_fd_name,
                         extra={"fd_followup": True, "observation_id": o["observation_id"]},
                         idem_key=f"fd-obs:{o['observation_id']}:{o.get('next_review_at')}")
        await fd_store.mark_contacted(o["observation_id"])
        logger.info("居家观察主动追问已发送：%s（group=%s）", o["observation_id"], o["group_id"])
        out.append(msg)
    return out


async def _fd_followup_text(o: dict) -> str:
    from app.wecom.fd import render as fd_render
    from app.wecom.fd.contracts import HomeObservationPlan

    obs = HomeObservationPlan.model_validate(o["plan"])
    return fd_render.observation_followup_text(
        obs, emergency_number=get_settings().wecom_fd_emergency_number or "120")


async def fd_contact_overdue() -> list[dict]:
    """回应窗口已过仍未回复：标记 CONTACT_OVERDUE，再催一次并通知人工（不推断好转、不自动关闭）。"""
    if not await _fd_enabled():
        return []
    from app.wecom.fd import store as fd_store
    from app.wecom.fd.contracts import ObservationState

    s = get_settings()
    out: list[dict] = []
    for o in await fd_store.overdue_observations():
        if not await fd_store.claim_state(o["observation_id"], [ObservationState.REVIEW_PENDING.value],
                                         ObservationState.CONTACT_OVERDUE.value):
            continue
        g = await store.get_group(o["group_id"])
        retry = (o.get("contact_count") or 0) < 2
        if retry:
            text = (f"还没收到你的回复。上次的观察问题仍然需要确认：和之前相比症状是好一些、差不多，还是更重了？\n"
                    f"如果没有好转或出现升级指征，请不要继续等，按观察计划里的方式及时就医。")
            await emit(o["group_id"], kind="text", content=text, sender_name=s.wecom_fd_name,
                       extra={"fd_followup": True, "observation_id": o["observation_id"], "retry": True},
                       idem_key=f"fd-obs-retry:{o['observation_id']}:{o.get('response_due_at')}")
        content = (f"居家观察未按时复评：观察 {o['observation_id']}（群 {g.name if g else o['group_id']}）"
                   f"已到回应窗口仍无有效回复，最长观察截止 {o.get('maximum_end_at')}。"
                   f"请人工联系确认并按批准流程决定是否升级。")
        msg = await emit(o["group_id"], kind="notify",
                         content=f"@{g.case_manager_name if g else '病历经理'} {content}",
                         mentions=[g.case_manager_id] if g and g.case_manager_id else None,
                         extra={"fd_overdue": True, "observation_id": o["observation_id"]},
                         idem_key=f"fd-obs-overdue:{o['observation_id']}")
        out.append(msg)
    return out


async def fd_expired_observations() -> list[dict]:
    """超过最长观察截止：不允许继续观察，转入 NEEDS_REVIEW 并通知人工。"""
    if not await _fd_enabled():
        return []
    from app.wecom.fd import store as fd_store
    from app.wecom.fd.contracts import ObservationState

    s = get_settings()
    out: list[dict] = []
    for o in await fd_store.expired_observations():
        if not await fd_store.claim_state(
                o["observation_id"],
                [ObservationState.ACTIVE.value, ObservationState.REVIEW_PENDING.value,
                 ObservationState.CONTACT_OVERDUE.value], ObservationState.NEEDS_REVIEW.value):
            continue
        g = await store.get_group(o["group_id"])
        content = (f"居家观察已超过最长截止（{o.get('maximum_end_at')}）仍未完成复评："
                   f"观察 {o['observation_id']}（群 {g.name if g else o['group_id']}）。请安排线下/医生评估，不能继续在家观察。")
        out.append(await emit(o["group_id"], kind="notify",
                              content=f"@{g.case_manager_name if g else '病历经理'} {content}",
                              mentions=[g.case_manager_id] if g and g.case_manager_id else None,
                              extra={"fd_expired": True, "observation_id": o["observation_id"]},
                              idem_key=f"fd-obs-expired:{o['observation_id']}"))
    return out
