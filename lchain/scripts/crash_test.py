"""企微群回复「重启续跑」端到端测试：在各个崩溃点真实杀掉服务进程（os._exit，等同 kill -9），
重启后检查未完成的任务是否继续执行、已发送的消息是否没有重复发送。

前置：MySQL 已启动；会占用 8000 端口（先停掉 make dev）；会临时修改 .env 的 WECOM_CRASH_AT / WECOM_OUTBOUND_WEBHOOK。
用法：
  make crash-test                              # 全部场景
  .venv/bin/python scripts/crash_test.py reply_sent card_sent   # 指定场景
  BURST=1  .venv/bin/python scripts/crash_test.py               # 连续发消息 + 多群同时崩溃
  WHDOWN=1 .venv/bin/python scripts/crash_test.py               # 发送端宕机后重启补投递
"""
import json, os, re, signal, subprocess, sys, time, uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
B = "http://127.0.0.1:8000/api/wecom"
WH = Path("/tmp/wh.jsonl")
LOG = ROOT / "logs" / "crash-test-console.log"  # 应用日志本身写在 logs/app.log，这里只存测试时的控制台输出

SCENARIOS = {
    # 名称: (崩溃点, 消息, 预期路由/说明)
    "ingested":        ("ingested",           "最近空腹血糖8.9要紧吗",       "消息已入库、尚未开始处理"),
    "node_plan":       ("node:plan",          "晚上睡前能吃水果吗",          "执行到一半（plan 节点）"),
    "search_guide":    ("emit_sent:guide",    "胰岛素笔针头医保能报吗",      "引导语已投递、节点未完成"),
    "reply_saved":     ("emit_saved:text",    "我想把二甲双胍停了，胃不舒服", "回复已落库、尚未投递"),
    "reply_sent":      ("emit_sent:text",     "我想把二甲双胍停了，胃不舒服", "回复已投递、卡片还没发、checkpoint 未保存"),
    "card_sent":       ("emit_sent:doctor_card", "我想换个降糖药",          "回复 + 卡片已投递、节点未完成"),
    "graph_done":      ("run:graph_done",     "睡前能喝点牛奶吗",            "全部节点完成、未标记完成"),
    "node_finalize":   ("node:finalize",      "运动后血糖低怎么办",          "回复已发出、收尾节点开始时"),
}


def set_env(key, value):
    lines = [l for l in ENV.read_text().splitlines() if not l.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    ENV.write_text("\n".join(lines) + "\n")


def kill_service():
    subprocess.run("pkill -9 -f 'run.py'; pkill -9 -f 'multiprocessing.spawn'; pkill -9 -f 'uvicorn app.main'", shell=True)
    time.sleep(1.5)


def start_service():
    # 不用 --reload，崩溃时整个进程退出
    p = subprocess.Popen([str(ROOT / ".venv/bin/uvicorn"), "app.main:app", "--port", "8000"], cwd=ROOT,
                         stdout=open(LOG, "a"), stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            if httpx.get("http://127.0.0.1:8000/api/health", timeout=1).status_code == 200:
                return p
        except Exception:
            pass
        if p.poll() is not None:
            raise RuntimeError("服务启动失败")
        time.sleep(0.5)
    raise RuntimeError("服务启动超时")


def wh_records(gid):
    if not WH.exists():
        return []
    return [r for r in (json.loads(l) for l in WH.read_text().splitlines() if l.strip()) if r.get("group_id") == gid]


def run(name):
    crash, text, desc = SCENARIOS[name]
    gid = f"crash-{name}-{uuid.uuid4().hex[:4]}"
    print(f"\n=== [{name}] 崩溃点 {crash}：{desc}")
    kill_service()
    set_env("WECOM_CRASH_AT", crash)
    p = start_service()
    c = httpx.Client(timeout=30)
    c.put(f"{B}/groups/{gid}", json={"name": "崩溃测试", "case_manager_id": "cm_li", "case_manager_name": "李经理",
                                     "profile": "王某68岁男，2型糖尿病，二甲双胍0.5g bid"})
    try:
        c.post(f"{B}/messages", json={"group_id": gid, "msg_id": uuid.uuid4().hex, "sender_id": "u1",
                                      "sender_name": "王叔叔", "content": text, "force_reply": True})
    except Exception:
        pass  # ingested 崩溃点：请求本身就会断开
    for _ in range(120):
        if p.poll() is not None:
            break
        time.sleep(0.5)
    crashed = p.poll() is not None
    print(f"  进程崩溃: {crashed}（exit={p.poll()}）")
    if not crashed:
        print("  ⚠ 未触发崩溃（该路径未经过崩溃点）")
        p.kill()
    before = wh_records(gid)
    print(f"  崩溃前已投递: {[(r['type'], r['content'][:20]) for r in before]}")

    set_env("WECOM_CRASH_AT", "")
    start_service()
    # 等待恢复完成：出现系统判断消息
    msgs = []
    for _ in range(120):
        msgs = c.get(f"{B}/groups/{gid}/messages").json()["items"]
        if any(m["sender_role"] == "system" for m in msgs):
            break
        time.sleep(0.5)
    time.sleep(1)
    msgs = c.get(f"{B}/groups/{gid}/messages").json()["items"]
    after = wh_records(gid)
    runs = c.get(f"{B}/runs?status=done,skipped,merged,interrupted,running,failed&group_id={gid}").json()["items"]
    pending = c.get(f"{B}/groups/{gid}/pending").json()["items"]

    bot = [m for m in msgs if m["sender_role"] == "assistant"]
    replies = [m for m in bot if m["extra"].get("kind") == "text"]
    cards = [m for m in bot if m["msg_type"] == "doctor_card"]
    guides = [m for m in bot if m["extra"].get("kind") == "guide"]
    systems = [m for m in msgs if m["sender_role"] == "system"]
    delivered_ids = [r["message_id"] for r in after]
    dup_deliveries = {i for i in delivered_ids if delivered_ids.count(i) > 1}
    undelivered = [m["id"] for m in bot if m["delivery"] != "sent"]

    for m in msgs:
        print(f"  #{m['id']} {m['sender_role']:<10}{m['extra'].get('kind') or m['msg_type']:<12}"
              f"{m.get('delivery',''):<6} {m['content'][:40]!r}")
    print(f"  runs: {[(r['id'][:8], r['status'], r['resume_count']) for r in runs]}")
    print(f"  pending: {[(p['message_id'], p['status']) for p in pending]}")
    print(f"  webhook 投递: {[(r['message_id'], r['type']) for r in after]}")

    checks = {
        "恰好 1 条回复": len(replies) == 1,
        "卡片不超过 1 张": len(cards) <= 1,
        "引导语不超过 1 条": len(guides) <= 1,
        "系统判断 1 条": len(systems) == 1,
        "助手消息全部已投递": not undelivered,
        "没有重复投递": not dup_deliveries,
        "每条助手消息都投递过": set(m["id"] for m in bot) <= set(delivered_ids),
        "待回复已关闭": all(p["status"] in ("replied", "handover", "skipped") for p in pending) and bool(pending),
    }
    ok = all(checks.values())
    for k, v in checks.items():
        if not v:
            print(f"  ❌ {k}")
    print(f"  结果: {'✅ 通过' if ok else '❌ 失败'}")
    return ok


def _prepare():
    """启动模拟发送端，并把 .env 的 WECOM_OUTBOUND_WEBHOOK 指过去（结束后恢复）。"""
    global _orig_webhook
    _orig_webhook = next((l.split("=", 1)[1] for l in ENV.read_text().splitlines()
                          if l.startswith("WECOM_OUTBOUND_WEBHOOK=")), "")
    set_env("WECOM_OUTBOUND_WEBHOOK", "http://127.0.0.1:8099/")
    subprocess.run(f"pkill -f scripts/mock_webhook.py; nohup {ROOT}/.venv/bin/python {ROOT}/scripts/mock_webhook.py "
                   ">/dev/null 2>&1 &", shell=True)
    time.sleep(1)


def _restore():
    set_env("WECOM_CRASH_AT", "")
    set_env("WECOM_OUTBOUND_WEBHOOK", _orig_webhook)
    subprocess.run("pkill -f scripts/mock_webhook.py", shell=True)


_orig_webhook = ""

if __name__ == "__main__" and not os.environ.get("BURST") and not os.environ.get("WHDOWN"):
    _prepare()
    names = sys.argv[1:] or list(SCENARIOS)
    res = {n: run(n) for n in names}
    kill_service()
    _restore()
    print("\n==== 汇总 ====")
    for n, ok in res.items():
        print(f"  {'✅' if ok else '❌'} {n:<16} {SCENARIOS[n][0]:<24} {SCENARIOS[n][2]}")


def run_burst():
    """连续发 2 条消息，第二条到达（打断合并）后在回复落库前崩溃；另一个群同时有执行中的任务。"""
    print("\n=== [burst] 连续发消息 + 多群同时处理时崩溃")
    kill_service()
    set_env("WECOM_CRASH_AT", "node:respond")
    p = start_service()
    c = httpx.Client(timeout=30)
    g1, g2 = f"crash-burst-{uuid.uuid4().hex[:4]}", f"crash-other-{uuid.uuid4().hex[:4]}"
    for g in (g1, g2):
        c.put(f"{B}/groups/{g}", json={"name": g, "case_manager_id": "cm_li", "case_manager_name": "李经理",
                                       "profile": "王某68岁男，2型糖尿病"})
    send = lambda g, t: c.post(f"{B}/messages", json={"group_id": g, "msg_id": uuid.uuid4().hex, "sender_id": "u1",
                                                       "sender_name": "王叔叔", "content": t, "force_reply": True})
    send(g1, "医生你好"); time.sleep(1.5)
    send(g2, "睡前能吃坚果吗")
    send(g1, "我最近空腹血糖8.9，晚上还老起夜，要紧吗")
    for _ in range(120):
        if p.poll() is not None:
            break
        time.sleep(0.5)
    print(f"  进程崩溃: {p.poll() is not None}")
    set_env("WECOM_CRASH_AT", "")
    start_service()
    ok_all = True
    for g in (g1, g2):
        for _ in range(120):
            msgs = c.get(f"{B}/groups/{g}/messages").json()["items"]
            if any(m["sender_role"] == "system" for m in msgs):
                break
            time.sleep(0.5)
        time.sleep(1)
        msgs = c.get(f"{B}/groups/{g}/messages").json()["items"]
        runs = c.get(f"{B}/runs?status=done,skipped,merged,interrupted,running,failed&group_id={g}").json()["items"]
        after = wh_records(g)
        replies = [m for m in msgs if m["extra"].get("kind") == "text"]
        ids = [r["message_id"] for r in after]
        print(f"  [{g}]")
        for m in msgs:
            print(f"    #{m['id']} {m['sender_role']:<10}{m['extra'].get('kind') or m['msg_type']:<12}{m.get('delivery',''):<6} {m['content'][:45]!r}")
        print(f"    runs: {[(r['id'][:8], r['status'], r['message_ids'], r['resume_count']) for r in runs]}")
        user_ids = {m["id"] for m in msgs if m["sender_role"] == "patient"}
        done_cover = set().union(*[set(r["message_ids"]) for r in runs if r["status"] == "done"] or [set()])
        ok = len(replies) == 1 and len(ids) == len(set(ids)) and user_ids <= done_cover
        print(f"    结果: {'✅' if ok else '❌'}（回复 {len(replies)} 条，合并覆盖 {sorted(done_cover)}）")
        ok_all &= ok
    return ok_all


if __name__ == "__main__" and os.environ.get("BURST"):
    _prepare()
    r = run_burst()
    kill_service(); _restore()
    print("burst:", "✅" if r else "❌")


def run_webhook_down():
    """发送端宕机：回复落库但投递失败 -> 服务重启后自动补投递（只投一次）。"""
    print("\n=== [webhook_down] 发送端不可用 + 服务重启")
    kill_service()
    subprocess.run("pkill -f scripts/mock_webhook.py", shell=True); time.sleep(0.5)
    start_service()
    c = httpx.Client(timeout=30)
    g = f"crash-wh-{uuid.uuid4().hex[:4]}"
    c.put(f"{B}/groups/{g}", json={"name": g, "profile": "王某68岁男，2型糖尿病"})
    c.post(f"{B}/messages", json={"group_id": g, "msg_id": uuid.uuid4().hex, "sender_id": "u1",
                                  "sender_name": "王叔叔", "content": "睡前能吃苹果吗", "force_reply": True})
    for _ in range(80):
        msgs = c.get(f"{B}/groups/{g}/messages").json()["items"]
        if any(m["sender_role"] == "system" for m in msgs): break
        time.sleep(0.5)
    print("  发送端宕机时:", [(m["id"], m["extra"].get("kind"), m["delivery"]) for m in msgs if m["sender_role"] == "assistant"])
    kill_service()
    subprocess.run(f"nohup {ROOT}/.venv/bin/python {ROOT}/scripts/mock_webhook.py >/dev/null 2>&1 &", shell=True); time.sleep(1)
    start_service(); time.sleep(2)
    msgs = c.get(f"{B}/groups/{g}/messages").json()["items"]
    bot = [m for m in msgs if m["sender_role"] == "assistant"]
    ids = [r["message_id"] for r in wh_records(g)]
    print("  重启后:", [(m["id"], m["extra"].get("kind"), m["delivery"]) for m in bot], "webhook:", ids)
    ok = all(m["delivery"] == "sent" for m in bot) and sorted(ids) == sorted(m["id"] for m in bot)
    print("  结果:", "✅" if ok else "❌")
    return ok


if __name__ == "__main__" and os.environ.get("WHDOWN"):
    _prepare()
    r = run_webhook_down(); kill_service(); _restore()
