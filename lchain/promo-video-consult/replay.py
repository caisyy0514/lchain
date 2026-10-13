"""重跑一次演示问诊，拿到诊断那一轮的 Langfuse trace 链接（用于重抓 Trace 截图）。

用法：PYTHONPATH=. .venv/bin/python replay.py promo-abd-01
需要 web 服务已在 localhost:8000 运行；session id 用 promo-* 开头，避免截图里出现跑批会话名。
回答按 eval/consult/消化内科/01-上腹痛3天-急性胃炎.md 的 history 写死，保证每次跑出来的对话一样。
"""
import asyncio, json, sys
import httpx

SID = sys.argv[1] if len(sys.argv) > 1 else "promo-demo-abd"
BASE = "http://127.0.0.1:8000/api/chat/stream"
HIST = {
    "起病": "3 天前聚餐后慢慢开始疼的",
    "部位": "上腹正中偏左，范围不大",
    "性质": "隐痛，还有点烧灼感",
    "严重": "大概 5 分，吃东西受影响",
    "病程": "一直疼，饭后更明显",
    "伴随": "反酸嗳气，没发烧也没吐",
    "诱因": "吃辣的、空肚子的时候更疼",
    "既往": "以前没有胃病，没做过胃镜",
    "用药": "自己吃了铝碳酸镁，能好一点",
    "过敏": "没有药物和食物过敏",
    "变化": "昨晚开始拉了两次稀水样便，没发烧，肚子比昨天轻一些",
    "黑便": "没有黑便，也没吐血",
    "体重": "体重没什么变化，吃饭还行",
    "烧心": "有一点反酸，不打嗝了",
    "大便": "昨天开始拉的，稀水样，两次",
}

async def turn(msg):
    parts, action, trace = [], "", ""
    ev_name = None
    async with httpx.AsyncClient(timeout=240) as cl:
        async with cl.stream("POST", BASE, json={"message": msg, "session_id": SID,
                                                "scenario": "consult", "user_id": "promo"}) as r:
            async for line in r.aiter_lines():
                if line.startswith("event:"):
                    ev_name = line[6:].strip()
                elif line.startswith("data:"):
                    raw = line[5:].strip()
                    try:
                        d = json.loads(raw)
                    except Exception:
                        continue
                    if ev_name == "meta":
                        trace = d.get("trace_url", "")
                    elif ev_name == "token":
                        parts.append(d.get("content", ""))
                    elif ev_name == "replace":
                        parts = [d.get("content", "")]
                    elif ev_name == "done":
                        action = d.get("action") or ""
    return "".join(parts), action, trace

async def main():
    q = "我上腹疼 3 天了"
    for i in range(10):
        reply, action, trace = await turn(q)
        print(f"=== 第 {i+1} 轮 action={action}\n{reply[:300]}\nTRACE {trace}\n", flush=True)
        if action == "close" or i >= 9:
            break
        ans = []
        for key, val in HIST.items():
            if key in reply and val not in ans:
                ans.append(val)
                if len(ans) >= 2:
                    break
        if action == "diagnose":
            q = HIST["变化"]
        else:
            q = "；".join(ans) if ans else HIST["变化"]
    print("done", SID)

asyncio.run(main())
