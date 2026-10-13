"""回复动作的发送：发件箱（outbox）模式，保证重启后「不丢、不重」。

发送一条消息分两步，状态记录在 wecom_messages.delivery：
  1. 落库：写入消息，delivery=pending，并带幂等键 idem_key（run_id:动作）——同一动作永远只有一条记录
  2. 投递：推给企微发送端（WECOM_OUTBOUND_WEBHOOK），成功后 delivery=sent

重启 / 续跑时：
  - 节点被 checkpoint 续跑重新执行 -> 幂等键命中已存在的记录，不会生成第二条
      · 已 sent：直接复用，不再投递
      · 仍 pending / failed：说明上次落库后、投递前进程中断了 -> 补投递一次
  - 服务启动时 redeliver_pending() 扫描所有 pending / failed 的消息并补投递

未配置 webhook 时（测试环境，消息只落库、由页面展示），落库即视为已送达，直接标记 sent。
投递端请用 message_id 做去重：极端情况下（投递成功但标记 sent 前进程被杀）同一条消息可能被补投递一次，
这是 at-least-once 的固有边界，webhook 接收方按 message_id 去重即可做到“恰好一次”。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.db import session_scope
from app.wecom import store
from app.wecom.models import Message

logger = logging.getLogger(__name__)

OUTBOUND_KINDS = {"text", "guide", "doctor_card", "notify", "plan_card"}  # 需要外发到群里的动作；system 只落库


def crash_point(name: str) -> None:
    """崩溃演练（仅测试用）：WECOM_CRASH_AT=崩溃点 时直接杀掉进程（os._exit，模拟断电 / kill -9），
    用来验证重启后的续跑和不重复发送。崩溃点：
      ingested                用户消息已入库、尚未开始处理
      node:<节点名>           节点开始执行时（如 node:respond）
      run:graph_done          全部节点已完成、尚未记录执行完成状态
      emit_saved:<动作>       消息已落库、尚未投递时（如 emit_saved:text）
      emit_sent:<动作>        消息已投递、所在节点尚未完成（checkpoint 未保存）时
    """
    target = get_settings().wecom_crash_at
    if target and target == name:
        import os

        logger.critical("[崩溃演练] 在 %s 处杀掉进程", name)
        os._exit(137)


async def emit(group_id: str, *, kind: str, content: str, run_id: str | None = None,
               mentions: list[str] | None = None, extra: dict[str, Any] | None = None,
               role: str = "assistant", idem_key: str | None = None,
               sender_name: str | None = None) -> dict[str, Any]:
    """kind：text（文本回复）/ guide（引导语）/ doctor_card（找医生卡片）/ notify（通知病历经理）/ system（系统判断，不外发）

    idem_key：幂等键。checkpoint 续跑时节点可能被重放，同一个 key 只会生成一条消息、只投递一次。
    返回的消息带 replayed=True 表示这条消息之前已经生成过（本次没有新发送）。
    """
    s = get_settings()
    if idem_key:
        old = await store.find_by_idem(idem_key)
        if old:
            await _ensure_delivered(old)  # 上次落库后没来得及投递 -> 补投递
            return {**store.msg_dict(await store.get_message(old.id)), "replayed": True}
    msg_type = {"guide": "text"}.get(kind, kind)
    outbound = kind in OUTBOUND_KINDS and role == "assistant"
    try:
        m = await store.add_message(
            group_id=group_id, sender_id="bot" if role == "assistant" else "system",
            sender_name=sender_name or (s.wecom_bot_name if role == "assistant" else "系统"),
            sender_role=role, msg_type=msg_type,
            content=content, mentions=mentions or None, extra={"kind": kind, **(extra or {})}, run_id=run_id,
            idem_key=idem_key, delivery="pending" if outbound else "none",
        )
    except IntegrityError:  # 并发下被别的执行抢先写入
        old = await store.find_by_idem(idem_key or "")
        if old:
            return {**store.msg_dict(old), "replayed": True}
        raise
    if outbound:
        crash_point(f"emit_saved:{kind}")
        if await _claim(m):
            await _deliver(m)
        crash_point(f"emit_sent:{kind}")
        m = await store.get_message(m.id) or m
    return store.msg_dict(m)


async def _claim(m: Message) -> bool:
    """把 pending/failed 原子地改成 sending，返回是否认领成功（行级更新，多进程只有一个能成功）。
    sending 超过 WECOM_REDELIVER_GRACE_SECONDS 仍未完成（认领后进程崩溃）的可以被重新认领。"""
    from datetime import timedelta

    stale = datetime.now() - timedelta(seconds=get_settings().wecom_redeliver_grace_seconds)
    async with session_scope() as sess:
        r = await sess.execute(update(Message).where(
            Message.id == m.id,
            (Message.delivery.in_(["pending", "failed"])) |
            ((Message.delivery == "sending") & (Message.delivered_at < stale)),
        ).values(delivery="sending", delivered_at=datetime.now()))
        return (r.rowcount or 0) == 1


async def _ensure_delivered(m: Message) -> None:
    if m.delivery in ("pending", "failed", "sending") and await _claim(m):
        logger.info("消息 #%s（%s）上次未投递完成，补投递", m.id, m.idem_key)
        await _deliver(m)


def outbound_card(m: Message) -> dict[str, Any] | None:
    """投递给企微发送端的结构化卡片数据。

    - doctor_card：原样透传配置好的找医生卡片（title/description/url/avatar/doctor_name）；
    - plan_card：带上新版就医规划卡渲染所需的完整数据（view + 紧急程度等），
      发送端用 app/web/plan-card.css + planCardHTML 的同款组件渲染；
      配置 APP_PUBLIC_URL 后额外给一个 url，患者手机可直接打开同款 H5 卡片。
    """
    extra = m.extra or {}
    card = extra.get("card")
    if card is not None or m.msg_type != "plan_card":
        return card
    view = extra.get("view")
    if not view:
        return None  # 没有患者视图就不下发空卡片（发送端无法渲染）
    card = {
        "kind": "plan_card",
        "urgency": extra.get("urgency"),
        "care_mode": extra.get("care_mode"),
        "release_stage": extra.get("release_stage"),
        "clinician_review": extra.get("clinician_review"),
        "view": view,
    }
    base = get_settings().app_public_url.rstrip("/")
    if base:
        card["url"] = f"{base}/wecom-card/{m.id}"
    return card


async def _deliver(m: Message) -> bool:
    """投递一条消息并更新 delivery 状态。未配置 webhook 时直接视为已送达。

    群 id 以 `EVAL_GROUP_PREFIX` 开头的是本地评测临时群：只落库、不上发（评测不能把消息真的推到患者群里）。
    """
    s = get_settings()
    ok, err = True, None
    if m.group_id.startswith(store.EVAL_GROUP_PREFIX):
        logger.debug("评测临时群 %s：消息 #%s 只落库不外发", m.group_id, m.id)
    elif s.wecom_outbound_webhook:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(s.wecom_outbound_webhook, json={
                    "message_id": m.id,          # 接收方请用 message_id 去重
                    "idem_key": m.idem_key,
                    "group_id": m.group_id, "type": m.msg_type, "content": m.content,
                    "mentions": m.mentions or [], "card": outbound_card(m), "run_id": m.run_id,
                })
                r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            ok, err = False, e
    async with session_scope() as sess:
        values: dict[str, Any] = {"delivery_attempts": Message.delivery_attempts + 1}
        values.update({"delivery": "sent", "delivered_at": datetime.now()} if ok else {"delivery": "failed"})
        await sess.execute(update(Message).where(Message.id == m.id).values(**values))
    if not ok:
        logger.warning("消息 #%s 投递失败（已落库，稍后重试）: %s", m.id, err)
    return ok


async def redeliver_pending(max_attempts: int = 10, *, node_ids: list[str | None] | None = None,
                            older_than_seconds: int = 0) -> int:
    """补投递 pending / failed 的消息。按 id 顺序投递，保证群内顺序。

    - web 节点启动：只补投递本节点任务产生的消息（node_ids=[本节点]）
    - 定时任务：补投递所有节点的，但只处理 older_than_seconds 之前的，避免和 web 节点正在进行的投递撞车
    """
    from datetime import timedelta

    from app.wecom.models import AgentRun

    async with session_scope() as sess:
        q = select(Message).where(Message.delivery.in_(["pending", "failed", "sending"]),
                                  Message.delivery_attempts < max_attempts)
        if older_than_seconds:
            q = q.where(Message.created_at <= datetime.now() - timedelta(seconds=older_than_seconds))
        if node_ids is not None:
            from app.wecom.store import _node_filter

            q = q.where(Message.run_id.in_(select(AgentRun.id).where(_node_filter(AgentRun.node_id, node_ids))))
        rows = (await sess.execute(q.order_by(Message.id).limit(200))).scalars().all()
    n = 0
    for m in rows:
        # 先原子地“认领”这条消息（pending/failed -> sending），认领成功的进程才投递，避免多进程同时补投同一条
        if not await _claim(m):
            continue
        if await _deliver(m):
            n += 1
    if rows:
        logger.info("补投递消息 %d/%d 条", n, len(rows))
    return n
