"""群消息调度：让助手像真人一样处理“连续发消息”。

每个群一个 GroupWorker（状态在本节点进程内存中），不做额外等待：
1. 立即处理：消息到达后马上交给 Agent。
2. 打断合并：Agent 正在处理、但**正式回复还没发出**时又来了新消息 -> 取消本轮，把本轮消息和新消息合并后立即重新处理。
   已经推送过的引导语不会重复推送，已经解析过的文件直接复用。
   （发送前的判断在 graph.node_respond 的 _commit：本轮已被取代就不发送）
3. 回复已经开始发送（commit 之后）才来的新消息 -> 不打断，本轮发送完成后立即作为下一批处理。
4. 病历经理/医生在群里发言 -> 视为人工接手：取消尚未发出的回复，清空待处理消息，移除待回复记录。
5. 断点续跑：resume() 从 LangGraph checkpoint 继续某次中断的执行，同样走串行 / 打断合并逻辑。
6. 恢复：recover_tasks(node_ids) 恢复指定节点遗留的任务——web 节点启动时恢复自己的，
   定时任务进程接管失联节点的（app/wecom/jobs.py: takeover_dead_nodes）。

多节点部署：每次执行持有 MySQL 群锁（lchain:group:<id>），同一个群跨节点也是串行的；
打断合并只在同一个节点内生效，建议网关按 group_id 粘性路由。
所有过程通过事件推给本节点的订阅者（Web 测试页 SSE），同步接口 /api/wecom/reply 通过 Future 等待结果。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core.cluster import NamedLock, current_node_id
from app.wecom import store
from app.wecom.graph import resume_stream, run_stream

logger = logging.getLogger(__name__)


class GroupWorker:
    def __init__(self, group_id: str) -> None:
        self.group_id = group_id
        self.buffer: list[int] = []            # 等待处理的用户消息 id
        self.starter: asyncio.Task | None = None
        self.run: asyncio.Task | None = None
        self.run_ids: list[int] = []
        self.current_run_id: str | None = None   # 当前执行的 run_id（用于续跑时判断是否正在运行）
        self.run_token = 0
        self.committed = False                 # 本轮正式回复是否已开始发送
        self.interrupted = False               # 本轮是否已被打断
        self.guides: set[str] = set()          # 本批已推送的引导语（跨打断重跑保留）
        self.opts: dict[str, Any] = {}
        self.futures: dict[int, asyncio.Future] = {}
        self.subscribers: set[asyncio.Queue] = set()
        self.lock = NamedLock(f"group:{group_id}")  # 跨节点群锁：同一个群同一时间只有一个节点在处理

    # ------------------------------------------------------------------ events
    def publish(self, ev: dict[str, Any]) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    @property
    def running(self) -> bool:
        return self.run is not None and not self.run.done()

    def status(self) -> dict[str, Any]:
        return {"buffer": list(self.buffer), "running": self.running, "run_ids": self.run_ids if self.running else [],
                "committed": self.committed}

    # ------------------------------------------------------------------ inbound
    def submit_user(self, message_id: int, opts: dict[str, Any]) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        self.futures[message_id] = fut
        self.opts = {**opts, "force_reply": opts.get("force_reply") or self.opts.get("force_reply", False)}
        self.buffer.append(message_id)
        if self.running and not self.committed and not self.interrupted:
            # 回复还没发出：打断本轮，合并重跑
            self.interrupted = True
            self.buffer = sorted(set(self.run_ids + self.buffer))
            self.run.cancel()  # type: ignore[union-attr]
            self.publish({"type": "interrupt", "message_ids": self.run_ids,
                          "reason": "用户又发来新消息，合并后重新回复"})
        self._schedule()
        self.publish({"type": "queued", **self.status()})
        return fut

    async def staff_spoke(self, message: Any) -> int:
        """病历经理/医生发言：人工接手。"""
        cancelled: list[int] = []
        if self.running and not self.committed and not self.interrupted:
            self.interrupted = True
            cancelled = list(self.run_ids)
            self.run.cancel()  # type: ignore[union-attr]
        cancelled += self.buffer
        self.buffer, self.opts = [], {}
        self.guides.clear()
        for mid in cancelled:
            self._resolve(mid, {"status": "cancelled", "need_reply": False,
                                "reason": f"{message.sender_name} 已在群里回复，助手不再回复", "actions": []})
        n = await store.resolve_pending(group_id=self.group_id, status="replied",
                                        resolved_by=message.sender_id or message.sender_role)
        self.publish({"type": "handover", "message_ids": cancelled, "pending_resolved": n,
                      "by": message.sender_name})
        return n

    # ------------------------------------------------------------------ scheduling
    def _schedule(self) -> None:
        if self.starter and not self.starter.done():
            return  # 已有启动任务在等上一轮结束，它会带上最新的 buffer
        self.starter = asyncio.create_task(self._start_next())

    async def _start_next(self) -> None:
        if self.run and not self.run.done():
            # 等上一轮结束：被打断的轮次在做清理（很快）；已 commit 的轮次在发送回复
            await asyncio.wait([self.run])
        if not self.buffer:
            return
        ids, opts = sorted(set(self.buffer)), dict(self.opts)
        self.buffer, self.opts = [], {}
        self._launch(ids, opts)

    def _launch(self, ids: list[int], opts: dict[str, Any], resume_run_id: str | None = None) -> None:
        self.run_token += 1
        self.run_ids, self.committed, self.interrupted = ids, False, False
        self.run = asyncio.create_task(self._execute(ids, opts, self.run_token, resume_run_id))

    async def resume(self, run_id: str, message_ids: list[int]) -> asyncio.Future:
        """从 checkpoint 续跑一次中断的执行。本群正在处理时，等当前轮结束后再续跑。"""
        fut = asyncio.get_running_loop().create_future()
        last = sorted(message_ids)[-1]
        self.futures[last] = fut
        if self.run and not self.run.done():
            await asyncio.wait([self.run])
        self._launch(sorted(message_ids), {}, resume_run_id=run_id)
        return fut

    def _commit(self, token: int):
        def fn() -> bool:
            if token != self.run_token or self.interrupted:
                return False
            self.committed = True
            return True
        return fn

    def _guide_once(self, key: str) -> bool:
        if key in self.guides:
            return False
        self.guides.add(key)
        return True

    def _resolve(self, mid: int, result: dict[str, Any]) -> None:
        fut = self.futures.pop(mid, None)
        if fut and not fut.done():
            fut.set_result(result)

    async def _execute(self, ids: list[int], opts: dict[str, Any], token: int, resume_run_id: str | None = None) -> None:
        self.publish({"type": "typing", "on": True, "message_ids": ids})
        result: dict[str, Any] = {}
        steps: list[dict[str, Any]] = []
        hooks = {"commit": self._commit(token), "guide_once": self._guide_once}
        locked = False
        try:
            # 跨节点群锁：其他节点正在处理这个群时等待（最长 5 分钟），保证群内串行
            locked = await self.lock.acquire(timeout=0)
            if not locked:
                self.publish({"type": "waiting", "reason": "其他节点正在处理该群，等待中", "message_ids": ids})
                locked = await self.lock.acquire(timeout=300)
            if not locked:
                raise RuntimeError("等待群锁超时（其他节点处理该群超过 5 分钟）")
            if resume_run_id:
                # 续跑时，已推送过的引导语由幂等键保证不重复，这里不需要再额外去重
                stream = resume_stream(resume_run_id, hooks=hooks)
            else:
                stream = run_stream(self.group_id, ids, overrides=opts.get("prompt_overrides"),
                                    force_reply=bool(opts.get("force_reply")), hooks=hooks)
            async for ev in stream:
                ev = {**ev, "message_ids": ids}
                if ev["type"] == "meta":
                    self.current_run_id = ev.get("run_id")
                self.publish(ev)
                if ev["type"] == "step":
                    steps.append(ev)
                elif ev["type"] in ("meta", "done", "error"):
                    result.update(ev)
        except asyncio.CancelledError:
            # 被新消息打断（或人工接手）：消息已放回 buffer，由下一轮统一处理
            self.publish({"type": "merged", "run_id": result.get("run_id"), "message_ids": ids})
            return
        except Exception as e:  # noqa: BLE001  续跑前置检查失败等
            logger.exception("执行失败")
            result.update({"type": "error", "message": f"{type(e).__name__}: {e}"})
            self.publish({"type": "error", "message": result["message"], "message_ids": ids})
        finally:
            if locked:
                await asyncio.shield(self.lock.release())
            if token == self.run_token:
                self.publish({"type": "typing", "on": False})
        self.guides.clear()
        result["steps"] = steps
        last = ids[-1]
        for mid in ids:
            if mid == last:
                self._resolve(mid, {**result, "message_ids": ids})
            else:  # 合并处理的其他消息：回复动作只在最后一条消息的结果里返回，避免调用方重复发送
                self._resolve(mid, {"status": "merged", "merged_into": last, "run_id": result.get("run_id"),
                                    "message_ids": ids, "actions": []})


_workers: dict[str, GroupWorker] = {}


def worker(group_id: str) -> GroupWorker:
    w = _workers.get(group_id)
    if w is None:
        w = _workers[group_id] = GroupWorker(group_id)
    return w


def forget(group_id: str) -> None:
    """丢掉某个群的 worker（跑批用完临时群后调用）：队列 / 订阅者 / 上报状态不再保留在内存里。

    只在没有在跑的轮次时丢，避免把正在执行的群的状态清掉。
    """
    w = _workers.get(group_id)
    if w is not None and not w.running and not w.buffer:
        _workers.pop(group_id, None)


async def dispatch(message: Any, opts: dict[str, Any] | None = None) -> asyncio.Future | None:
    """新消息入口。用户消息返回 Future（该消息最终的处理结果）；员工消息返回 None。"""
    w = worker(message.group_id)
    w.publish({"type": "message", "message": store.msg_dict(message)})
    if message.sender_role in store.STAFF_ROLES:
        await w.staff_spoke(message)
        return None
    if message.sender_role in store.USER_ROLES:
        return w.submit_user(message.id, opts or {})
    return None


async def _recover_group(group_id: str, runs: list[Any], orphans: list[int], opts: dict[str, Any]) -> None:
    """同一个群按顺序恢复：先依次续跑未完成的执行，再处理没有被执行覆盖的消息。"""
    w = worker(group_id)
    for run in runs:
        try:
            fut = await w.resume(run.id, run.message_ids or [run.message_id])
            await asyncio.wait_for(fut, timeout=300)
        except Exception:  # noqa: BLE001
            logger.exception("续跑执行 %s 失败", run.id)
    if orphans:
        if w.running:
            await asyncio.wait([w.run])  # type: ignore[list-item]
        logger.info("群 %s 重新处理未完成的消息 %s", group_id, orphans)
        w.buffer = sorted(set(w.buffer + orphans))
        w.opts = opts
        w._schedule()


async def recover_tasks(node_ids: list[str | None], *, max_age_seconds: int, redeliver: bool = True) -> list[str]:
    """恢复指定节点遗留的未完成任务，保证「不丢、不重」。

    - web 节点重启：node_ids=[本节点]，恢复自己上次没做完的任务
    - 定时任务接管：node_ids=[失联的节点...]，由定时任务进程代为执行（见 app/scheduler.py）

    步骤：
    1. 补投递：已落库但没投递成功的回复（delivery=pending/failed）先补发
    2. 已跑完、只是没来得及标记完成的执行（checkpoint 没有下一步）-> 直接标记完成，不重跑
    3. 打断合并时被杀、多个执行覆盖同一批消息 -> 只续跑最新的那个，旧的标记 merged
    4. 有 checkpoint 的执行 -> 从断点续跑（同一个 run_id，已发送的动作靠幂等键跳过）
       没有 checkpoint 的执行 -> 什么都还没发，把消息放回队列重新处理
    5. 已入库但没被任何执行覆盖的用户消息（入库后、执行前被杀）-> 重新处理
    同一个群内按顺序执行（跨节点由群锁保证）。
    """
    from datetime import datetime

    from app.wecom.graph import checkpoint_state, get_graph
    from app.wecom.outbound import redeliver_pending

    if redeliver:
        await redeliver_pending(node_ids=node_ids)

    recovered: list[str] = []
    per_group: dict[str, list[Any]] = {}
    orphan_msgs: dict[str, list[int]] = {}
    group_opts: dict[str, dict[str, Any]] = {}
    runs = [r for r in await store.unfinished_runs(node_ids) if r.status == "running"]  # interrupted 需人工确认后续跑
    for run in runs:
        age = (datetime.now() - run.created_at).total_seconds()
        if max_age_seconds and age > max_age_seconds:
            await store.finish_run(run.id, status="interrupted", error="服务中断时间过长，未自动续跑")
            continue
        st = await checkpoint_state(run.id)
        if st.get("exists") and not st.get("next"):
            # 全部节点已完成，只差 finish_run：按 checkpoint 里的最终状态补记，不重跑（否则会换 run_id 重复回复）
            snap = await get_graph().aget_state({"configurable": {"thread_id": run.id}})
            v = snap.values or {}
            await store.finish_run(run.id, status="done" if v.get("need_reply") else "skipped",
                                   need_reply=v.get("need_reply"), intent=v.get("route"), steps=v.get("steps"),
                                   context=v.get("context"), error=None)
            logger.info("执行 %s 实际已完成（崩溃在收尾前），补记完成状态", run.id)
            recovered.append(run.id)
            continue
        if not st.get("exists"):
            # 还没有任何 checkpoint：什么都没做、也没发送，把消息放回队列重新处理
            await store.finish_run(run.id, status="merged", error="服务中断，无 checkpoint，重新处理")
            orphan_msgs.setdefault(run.group_id, []).extend(run.message_ids or [run.message_id])
            group_opts[run.group_id] = run.opts or {}
            recovered.append(run.id)
            continue
        per_group.setdefault(run.group_id, []).append(run)

    # 同一群内：消息被更新的执行完全覆盖的旧执行（打断合并时崩溃）不再续跑
    for gid, items in per_group.items():
        items.sort(key=lambda r: r.created_at)
        keep: list[Any] = []
        for i, run in enumerate(items):
            ids = set(run.message_ids or [run.message_id])
            if any(ids <= set(later.message_ids or [later.message_id]) for later in items[i + 1:]):
                await store.finish_run(run.id, status="merged", error="已被后续合并的执行覆盖")
                logger.info("执行 %s 已被后续合并的执行覆盖，跳过", run.id)
                continue
            keep.append(run)
        per_group[gid] = keep

    # 没被任何有效执行覆盖的用户消息
    for m in await store.orphan_user_messages(max_age_seconds, node_ids):
        if m.id not in orphan_msgs.get(m.group_id, []):
            orphan_msgs.setdefault(m.group_id, []).append(m.id)

    tasks = []
    for gid in set(per_group) | set(orphan_msgs):
        runs_g = per_group.get(gid, [])
        for r in runs_g:
            st = await checkpoint_state(r.id)
            logger.info("续跑中断的执行 %s（节点 %s，从 %s 继续，已完成 %s）", r.id, r.node_id, st.get("next"),
                        st.get("completed_nodes"))
            await store.finish_run(r.id, node_id=current_node_id())  # 由当前进程接管
        recovered += [r.id for r in runs_g]
        tasks.append(asyncio.create_task(
            _recover_group(gid, runs_g, sorted(set(orphan_msgs.get(gid, []))), group_opts.get(gid, {}))))
    if any(orphan_msgs.values()):
        recovered.append(f"orphan_messages:{sum(len(v) for v in orphan_msgs.values())}")
    _recover_tasks.extend(tasks)
    return recovered


_recover_tasks: list[asyncio.Task] = []


async def wait_recovered(timeout: float = 300) -> None:
    """等待 recover_tasks 发起的恢复全部完成（定时任务接管时使用）。"""
    pending = [t for t in _recover_tasks if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)
    _recover_tasks[:] = [t for t in _recover_tasks if not t.done()]


async def resume_interrupted_on_startup() -> list[str]:
    """web 节点启动时恢复**本节点**上次未完成的任务（其他节点的任务由它自己重启恢复，或由定时任务接管）。"""
    from app.core.config import get_settings

    s = get_settings()
    if not s.wecom_resume_on_startup:
        return []
    return await recover_tasks([current_node_id()], max_age_seconds=s.wecom_resume_max_age_seconds)


def active_workers() -> list[GroupWorker]:
    return [w for w in _workers.values() if w.running or w.buffer]


async def drain(timeout: float) -> int:
    """优雅停止：等待进行中的任务完成（最长 timeout 秒）。返回仍未完成的群数。"""
    import time

    deadline = time.monotonic() + timeout
    while active_workers() and time.monotonic() < deadline:
        await asyncio.sleep(0.5)
    left = active_workers()
    if left:
        logger.warning("停止时仍有 %d 个群的任务未完成，将由本节点重启后或定时任务接管恢复", len(left))
    return len(left)
