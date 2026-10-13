"""数据访问 + 上下文文本格式化。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, delete, func, or_, select, text, update

from app.core.db import session_scope
from app.wecom.models import AgentRun, Document, Group, Message, PendingReply, now

ROLE_LABEL = {
    "patient": "患者", "family": "家属", "case_manager": "病历经理", "doctor": "医生",
    "assistant": "助手", "system": "系统",
}
USER_ROLES = {"patient", "family"}
STAFF_ROLES = {"case_manager", "doctor"}

# 评测临时群的前缀：这类群的群 id 一定以它开头，群内消息**只在本地落库、不外发**（见 outbound._deliver），
# 跑完一轮评测会整组删除。放在这里是因为 outbound 与 eval 都要用到，而这个模块两边都会导入。
EVAL_GROUP_PREFIX = "eval-wecom-"


# ------------------------------------------------------------------ serialize
def msg_dict(m: Message) -> dict[str, Any]:
    return {
        "id": m.id, "group_id": m.group_id, "ext_msg_id": m.ext_msg_id, "sender_id": m.sender_id,
        "sender_name": m.sender_name, "sender_role": m.sender_role, "msg_type": m.msg_type,
        "content": m.content, "attachments": m.attachments or [], "mentions": m.mentions or [],
        "extra": m.extra or {}, "run_id": m.run_id, "created_at": m.created_at.isoformat(timespec="seconds"),
        "delivery": m.delivery, "idem_key": m.idem_key,
    }


def group_dict(g: Group) -> dict[str, Any]:
    return {
        "group_id": g.group_id, "name": g.name, "profile": g.profile,
        "case_manager_id": g.case_manager_id, "case_manager_name": g.case_manager_name,
        "summary": g.summary, "summary_until_id": g.summary_until_id,
        "summary_updated_at": g.summary_updated_at.isoformat(timespec="seconds") if g.summary_updated_at else None,
        "doctor_card_pushed_at": g.doctor_card_pushed_at.isoformat(timespec="seconds") if g.doctor_card_pushed_at else None,
    }


# ------------------------------------------------------------------ groups
async def get_group(group_id: str) -> Group | None:
    async with session_scope() as s:
        return await s.get(Group, group_id)


async def upsert_group(group_id: str, **fields: Any) -> Group:
    fields = {k: v for k, v in fields.items() if v is not None}
    async with session_scope() as s:
        g = await s.get(Group, group_id)
        if g is None:
            g = Group(group_id=group_id, name=fields.pop("name", "") or group_id, **fields)
            s.add(g)
        else:
            for k, v in fields.items():
                setattr(g, k, v)
        await s.flush()
        return g


async def update_group(group_id: str, **fields: Any) -> None:
    async with session_scope() as s:
        await s.execute(update(Group).where(Group.group_id == group_id).values(**fields))


# checkpoint 表由 langgraph-checkpoint-mysql 管理，不在 ORM 里，删群时按 thread_id（= run_id）清
_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


async def list_groups_page(limit: int = 20, offset: int = 0, q: str = "") -> tuple[list[dict[str, Any]], int]:
    """群列表（测试页的「会话历史」）：分页 + 关键词搜索，按最近有消息排序。

    每行带最后一条消息的预览 / 时间 / 消息数 / 执行次数，不点进去也能看出这个群在聊什么；
    排序用「最后一条消息时间」（没有消息的群用建群时间），空群不会把活跃群顶下去。
    """
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    conds = []
    if q := (q or "").strip():
        like = f"%{q}%"
        # 除了群名 / ID / 病历经理 / 患者档案，还能用**消息内容或发言人**搜（比如按患者名找群）
        in_messages = (select(Message.id)
                       .where(Message.group_id == Group.group_id,
                              or_(Message.sender_name.like(like), Message.content.like(like)))
                       .correlate(Group).exists())
        conds.append(or_(Group.name.like(like), Group.group_id.like(like),
                         Group.case_manager_name.like(like), Group.profile.like(like), in_messages))
    last_at = func.max(Message.created_at).label("last_at")
    msg_count = func.count(Message.id).label("msg_count")
    async with session_scope() as s:
        total = (await s.execute(select(func.count()).select_from(Group).where(*conds))).scalar() or 0
        rows = (await s.execute(
            select(Group, last_at, msg_count)
            .outerjoin(Message, Message.group_id == Group.group_id)
            .where(*conds).group_by(Group.group_id)
            .order_by(func.coalesce(last_at, Group.created_at).desc(), Group.group_id)
            .limit(limit).offset(offset))).all()
        ids = [g.group_id for g, _, _ in rows]
        previews = await _last_messages(s, ids)
        runs = dict((await s.execute(
            select(AgentRun.group_id, func.count(AgentRun.id)).where(AgentRun.group_id.in_(ids))
            .group_by(AgentRun.group_id))).all()) if ids else {}
    items = []
    for g, last, n in rows:
        m = previews.get(g.group_id)
        items.append({
            **group_dict(g),
            "msg_count": int(n or 0),
            "run_count": int(runs.get(g.group_id, 0)),
            "last_at": (last or g.created_at).isoformat(timespec="seconds"),
            "last_role": m.sender_role if m else "",
            "last_preview": ((m.content or "").strip().replace("\n", " ")[:60] if m else ""),
            "last_msg_type": m.msg_type if m else "",
        })
    return items, int(total)


async def _last_messages(s: Any, group_ids: list[str]) -> dict[str, Message]:
    """每个群的最后一条消息（窗口函数一次查完，只取 rn=1）。"""
    if not group_ids:
        return {}
    rn = func.row_number().over(partition_by=Message.group_id, order_by=Message.id.desc()).label("rn")
    sub = (select(Message.group_id, Message.sender_role, Message.msg_type, Message.content, rn)
           .where(Message.group_id.in_(group_ids)).subquery())
    rows = (await s.execute(select(sub).where(sub.c.rn == 1))).all()
    return {r.group_id: Message(group_id=r.group_id, sender_role=r.sender_role, msg_type=r.msg_type,
                                content=r.content) for r in rows}


async def delete_groups(group_ids: list[str]) -> int:
    """删除群及其全部数据（消息 / 文件 / 待回复 / 执行记录 / checkpoint）。返回删掉的群数。"""
    ids = [g for g in dict.fromkeys(group_ids) if g]
    if not ids:
        return 0
    async with session_scope() as s:
        run_ids = list((await s.execute(select(AgentRun.id).where(AgentRun.group_id.in_(ids)))).scalars().all())
        for model in (Message, Document, PendingReply, AgentRun):
            await s.execute(delete(model).where(model.group_id.in_(ids)))
        if run_ids:
            # 表名是常量，值走绑定参数（expanding）；checkpoint 表不在 ORM 里
            for table in _CHECKPOINT_TABLES:
                await s.execute(text(f"DELETE FROM {table} WHERE thread_id IN :ids")
                                .bindparams(bindparam("ids", expanding=True)), {"ids": run_ids})
        return (await s.execute(delete(Group).where(Group.group_id.in_(ids)))).rowcount or 0


async def list_groups() -> list[dict[str, Any]]:
    """全部群（不分页），给脚本 / 内部调用用；测试页请用 list_groups_page。"""
    async with session_scope() as s:
        rows = (await s.execute(select(Group).order_by(Group.updated_at.desc()).limit(500))).scalars().all()
        return [group_dict(g) for g in rows]


async def clear_group(group_id: str) -> None:
    async with session_scope() as s:
        for model in (Message, Document, PendingReply, AgentRun):
            await s.execute(delete(model).where(model.group_id == group_id))
        await s.execute(update(Group).where(Group.group_id == group_id).values(
            summary="", summary_until_id=0, summary_updated_at=None, doctor_card_pushed_at=None))


# ------------------------------------------------------------------ messages
async def find_by_ext_id(ext_msg_id: str) -> Message | None:
    async with session_scope() as s:
        return (await s.execute(select(Message).where(Message.ext_msg_id == ext_msg_id))).scalar_one_or_none()


async def add_message(**fields: Any) -> Message:
    async with session_scope() as s:
        m = Message(**fields)
        s.add(m)
        await s.flush()
        return m


async def find_by_idem(idem_key: str) -> Message | None:
    async with session_scope() as s:
        return (await s.execute(select(Message).where(Message.idem_key == idem_key))).scalar_one_or_none()


async def get_message(message_id: int) -> Message | None:
    async with session_scope() as s:
        return await s.get(Message, message_id)


async def get_messages(ids: list[int]) -> list[Message]:
    if not ids:
        return []
    async with session_scope() as s:
        rows = (await s.execute(select(Message).where(Message.id.in_(ids)).order_by(Message.id))).scalars().all()
        return list(rows)


async def list_messages(group_id: str, after_id: int = 0, limit: int = 300) -> list[Message]:
    async with session_scope() as s:
        q = select(Message).where(Message.group_id == group_id, Message.id > after_id).order_by(Message.id.desc()).limit(limit)
        return list(reversed((await s.execute(q)).scalars().all()))


async def recent_messages(group_id: str, before_id: int, limit: int) -> list[Message]:
    """某条消息之前的最近 limit 条（不含）。"""
    async with session_scope() as s:
        q = (select(Message).where(Message.group_id == group_id, Message.id < before_id)
             .order_by(Message.id.desc()).limit(limit))
        return list(reversed((await s.execute(q)).scalars().all()))


async def messages_between(group_id: str, after_id: int, upto_id: int) -> list[Message]:
    async with session_scope() as s:
        q = (select(Message).where(Message.group_id == group_id, Message.id > after_id, Message.id <= upto_id)
             .order_by(Message.id))
        return list((await s.execute(q)).scalars().all())


async def count_messages_between(group_id: str, after_id: int, upto_id: int) -> int:
    async with session_scope() as s:
        q = select(func.count()).select_from(Message).where(
            Message.group_id == group_id, Message.id > after_id, Message.id <= upto_id)
        return int((await s.execute(q)).scalar_one())


async def search_history(group_id: str, keywords: list[str], upto_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """在已压缩（不在上下文窗口内）的历史消息和文件解读中按关键词检索。medex 接入前的临时方案。"""
    kws = [k.strip() for k in keywords if k and k.strip()][:4]
    if not kws:
        return []
    out: list[dict[str, Any]] = []
    async with session_scope() as s:
        q = (select(Message).where(Message.group_id == group_id, Message.id <= upto_id,
                                   or_(*[Message.content.contains(k) for k in kws]))
             .order_by(Message.id.desc()).limit(limit))
        for m in (await s.execute(q)).scalars():
            out.append({"type": "message", "time": m.created_at.strftime("%Y-%m-%d %H:%M"),
                        "who": f"{m.sender_name}({ROLE_LABEL.get(m.sender_role, m.sender_role)})",
                        "content": m.content[:300]})
        q = (select(Document).where(Document.group_id == group_id, Document.status == "ok",
                                    or_(*[Document.parsed_text.contains(k) for k in kws]))
             .order_by(Document.id.desc()).limit(5))
        for d in (await s.execute(q)).scalars():
            out.append({"type": "document", "time": d.created_at.strftime("%Y-%m-%d %H:%M"),
                        "who": d.file_name, "content": d.parsed_text[:800]})
    return out


# ------------------------------------------------------------------ documents
async def add_document(**fields: Any) -> Document:
    async with session_scope() as s:
        d = Document(**fields)
        s.add(d)
        await s.flush()
        return d


async def documents_for_messages(message_ids: list[int]) -> dict[int, list[Document]]:
    if not message_ids:
        return {}
    async with session_scope() as s:
        rows = (await s.execute(select(Document).where(Document.message_id.in_(message_ids)))).scalars().all()
    out: dict[int, list[Document]] = {}
    for d in rows:
        out.setdefault(d.message_id or 0, []).append(d)
    return out


# ------------------------------------------------------------------ pending
async def claim_pending(group_id: str, messages: list[Message], run_id: str, reason: str) -> list[int]:
    """为一批消息登记待回复记录：已有未解决记录的（被中断合并的上一轮）改挂到本轮 run，其余新建。
    created_at 使用消息发送时间，合并重跑不会重置超时计时。"""
    ids = [m.id for m in messages]
    async with session_scope() as s:
        rows = (await s.execute(select(PendingReply).where(
            PendingReply.message_id.in_(ids), PendingReply.status.in_(["pending", "running"])))).scalars().all()
        have = {r.message_id for r in rows}
        for r in rows:
            r.run_id, r.status = run_id, "running"
        for m in messages:
            if m.id not in have:
                s.add(PendingReply(group_id=group_id, message_id=m.id, run_id=run_id, status="running",
                                   reason=reason, created_at=m.created_at))
        await s.flush()
    return ids


async def resolve_pending_messages(message_ids: list[int], status: str, resolved_by: str = "assistant") -> int:
    if not message_ids:
        return 0
    async with session_scope() as s:
        r = await s.execute(update(PendingReply).where(
            PendingReply.message_id.in_(message_ids), PendingReply.status.in_(["pending", "running"]))
            .values(status=status, resolved_by=resolved_by, resolved_at=now()))
        return r.rowcount or 0


async def resolve_pending(*, group_id: str, run_id: str | None = None, status: str = "replied",
                          resolved_by: str = "assistant") -> int:
    """回复后移除待回复记录（保留行，只改状态，便于统计）。run_id 为空时解决该群全部待回复。"""
    async with session_scope() as s:
        q = update(PendingReply).where(PendingReply.group_id == group_id,
                                       PendingReply.status.in_(["pending", "running"]))
        if run_id:
            q = q.where(PendingReply.run_id == run_id)
        r = await s.execute(q.values(status=status, resolved_by=resolved_by, resolved_at=now()))
        return r.rowcount or 0


async def mark_pending(run_id: str, status: str) -> None:
    async with session_scope() as s:
        await s.execute(update(PendingReply).where(PendingReply.run_id == run_id,
                                                   PendingReply.status.in_(["pending", "running"]))
                        .values(status=status))


async def overdue_pending(timeout_seconds: int) -> list[PendingReply]:
    deadline = datetime.now() - timedelta(seconds=timeout_seconds)
    async with session_scope() as s:
        q = select(PendingReply).where(PendingReply.status.in_(["pending", "running"]),
                                       PendingReply.created_at <= deadline).order_by(PendingReply.id).limit(50)
        return list((await s.execute(q)).scalars().all())


async def list_pending(group_id: str) -> list[dict[str, Any]]:
    async with session_scope() as s:
        q = select(PendingReply).where(PendingReply.group_id == group_id).order_by(PendingReply.id.desc()).limit(50)
        return [{"id": p.id, "message_id": p.message_id, "run_id": p.run_id, "status": p.status,
                 "reason": p.reason, "resolved_by": p.resolved_by,
                 "created_at": p.created_at.isoformat(timespec="seconds"),
                 "resolved_at": p.resolved_at.isoformat(timespec="seconds") if p.resolved_at else None,
                 "notified_at": p.notified_at.isoformat(timespec="seconds") if p.notified_at else None}
                for p in (await s.execute(q)).scalars()]


async def set_pending_notified(pending_id: int) -> None:
    async with session_scope() as s:
        await s.execute(update(PendingReply).where(PendingReply.id == pending_id)
                        .values(status="notified", notified_at=now()))


async def claim_pending_notify(pending_id: int) -> bool:
    """原子地把待回复记录标记为 notified，返回是否由本次调用标记成功。
    先标记再发通知：即使多个进程同时检查，同一条记录也只会通知一次。"""
    async with session_scope() as s:
        r = await s.execute(update(PendingReply).where(
            PendingReply.id == pending_id, PendingReply.status.in_(["pending", "running"]))
            .values(status="notified", notified_at=now()))
        return (r.rowcount or 0) == 1


# ------------------------------------------------------------------ runs
async def add_run(run_id: str, group_id: str, message_id: int, trace_id: str | None,
                  message_ids: list[int] | None = None, opts: dict[str, Any] | None = None) -> None:
    from app.core.cluster import current_node_id

    async with session_scope() as s:
        s.add(AgentRun(id=run_id, group_id=group_id, message_id=message_id, trace_id=trace_id,
                       message_ids=message_ids or [message_id], opts=opts or {}, node_id=current_node_id()))


async def get_run_row(run_id: str) -> AgentRun | None:
    async with session_scope() as s:
        return await s.get(AgentRun, run_id)


async def orphan_user_messages(max_age_seconds: int, node_ids: list[str | None] | None = None) -> list[Message]:
    """已入库、但没有被任何有效执行（非 merged / failed）覆盖的用户消息。
    典型场景：消息入库后、执行开始前进程被杀；或打断合并时，旧轮次已标记 merged、新轮次还没创建就被杀。"""
    since = datetime.now() - timedelta(seconds=max_age_seconds) if max_age_seconds else datetime(2000, 1, 1)
    async with session_scope() as s:
        q = select(Message).where(Message.sender_role.in_(list(USER_ROLES)), Message.created_at >= since)
        if node_ids is not None:
            q = q.where(_node_filter(Message.node_id, node_ids))
        msgs = (await s.execute(q.order_by(Message.id))).scalars().all()
        if not msgs:
            return []
        runs = (await s.execute(select(AgentRun.message_ids, AgentRun.message_id).where(
            AgentRun.group_id.in_({m.group_id for m in msgs}),
            AgentRun.status.notin_(["merged", "failed"])))).all()
        # 人工已接手（该消息之后有病历经理/医生发言）的不再处理
        staff = (await s.execute(select(Message.group_id, func.max(Message.id)).where(
            Message.group_id.in_({m.group_id for m in msgs}), Message.sender_role.in_(list(STAFF_ROLES)))
            .group_by(Message.group_id))).all()
    covered = {i for ids, mid in runs for i in (ids or [mid])}
    last_staff = dict(staff)
    return [m for m in msgs if m.id not in covered and m.id > (last_staff.get(m.group_id) or 0)]


def _node_filter(col: Any, node_ids: list[str | None]) -> Any:
    ids = [n for n in node_ids if n]
    conds = [col.in_(ids)] if ids else []
    if None in node_ids:
        conds.append(col.is_(None))  # 升级前的旧数据没有 node_id
    return or_(*conds) if conds else col.is_(None)


async def unfinished_runs(node_ids: list[str | None] | None = None) -> list[AgentRun]:
    """仍处于 running / interrupted 的执行（进程被杀 / 崩溃导致中断）。node_ids 为空表示所有节点。"""
    async with session_scope() as s:
        q = select(AgentRun).where(AgentRun.status.in_(["running", "interrupted"]))
        if node_ids is not None:
            q = q.where(_node_filter(AgentRun.node_id, node_ids))
        return list((await s.execute(q.order_by(AgentRun.created_at))).scalars().all())


async def runs_by_status(statuses: list[str], group_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    async with session_scope() as s:
        q = select(AgentRun).where(AgentRun.status.in_(statuses))
        if group_id:
            q = q.where(AgentRun.group_id == group_id)
        rows = (await s.execute(q.order_by(AgentRun.created_at.desc()).limit(limit))).scalars().all()
    return [{"id": r.id, "group_id": r.group_id, "message_ids": r.message_ids, "status": r.status, "error": r.error,
             "resume_count": r.resume_count, "created_at": r.created_at.isoformat(timespec="seconds"),
             "done_nodes": [st.get("node") for st in (r.steps or [])]} for r in rows]


async def recent_runs(group_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """群内最近 N 次执行（新 -> 旧），带每个节点的输入 / 输出（steps）。

    执行记录本来就落在 wecom_agent_runs，「Agent 执行过程」面板刷新后靠这个接口恢复；
    不带 context / actions（体积大且面板用不上），避免历史一大就把接口响应撑大。
    """
    async with session_scope() as s:
        rows = (await s.execute(
            select(AgentRun).where(AgentRun.group_id == group_id)
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(max(1, min(limit, 100))))).scalars().all()
    return [{"id": r.id, "group_id": r.group_id, "message_id": r.message_id, "message_ids": r.message_ids,
             "status": r.status, "need_reply": r.need_reply, "intent": r.intent, "steps": r.steps or [],
             "error": r.error, "latency_ms": r.latency_ms, "resume_count": r.resume_count,
             "created_at": r.created_at.isoformat(timespec="seconds")} for r in rows]


async def finished_run_ids_before(deadline: datetime, limit: int = 500) -> list[str]:
    async with session_scope() as s:
        q = (select(AgentRun.id).where(AgentRun.status.in_(["done", "skipped", "merged", "cancelled"]),
                                       AgentRun.updated_at < deadline).limit(limit))
        return list((await s.execute(q)).scalars().all())


async def finish_run(run_id: str, **fields: Any) -> None:
    async with session_scope() as s:
        await s.execute(update(AgentRun).where(AgentRun.id == run_id).values(**fields))


async def get_run(run_id: str) -> dict[str, Any] | None:
    async with session_scope() as s:
        r = await s.get(AgentRun, run_id)
    if not r:
        return None
    return {"id": r.id, "group_id": r.group_id, "message_id": r.message_id, "message_ids": r.message_ids,
            "status": r.status, "resume_count": r.resume_count, "opts": r.opts,
            "need_reply": r.need_reply, "intent": r.intent, "steps": r.steps, "actions": r.actions,
            "context": r.context, "trace_id": r.trace_id, "error": r.error, "latency_ms": r.latency_ms,
            "created_at": r.created_at.isoformat(timespec="seconds")}


# ------------------------------------------------------------------ formatting
def sender_label(m: Message) -> str:
    if m.sender_role == "system":
        return "系统消息"
    return f"{m.sender_name or m.sender_id}({ROLE_LABEL.get(m.sender_role, m.sender_role)})"


def format_message(m: Message, docs: list[Document] | None = None, doc_chars: int = 1500) -> str:
    t = m.created_at.strftime("%m-%d %H:%M")
    body = m.content or ""
    if m.msg_type == "doctor_card":
        body = f"[推送了找医生卡片] {body}"
    elif m.msg_type == "notify":
        body = f"[通知病历经理] {body}"
    for a in m.attachments or []:
        body += f"\n  [附件 {a.get('kind', 'file')}: {a.get('name', '')}]"
    for d in docs or []:
        if d.status == "ok" and d.parsed_text:
            txt = d.parsed_text if len(d.parsed_text) <= doc_chars else d.parsed_text[:doc_chars] + "…（已截断）"
            body += f"\n  [文件《{d.file_name}》解读]\n  " + txt.replace("\n", "\n  ")
        elif d.status != "ok":
            body += f"\n  [文件《{d.file_name}》解析失败：{d.error or d.status}]"
    return f"[{t}] {sender_label(m)}: {body.strip()}"
