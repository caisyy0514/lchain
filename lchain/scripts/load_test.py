"""并发压测：验证服务端能扛多少并发（默认 50），失败/超时/延迟分布一目了然。

    PYTHONPATH=. .venv/bin/python -m scripts.load_test              # 50 并发 × 1 轮
    PYTHONPATH=. .venv/bin/python -m scripts.load_test -n 100 -r 2  # 100 并发 × 2 轮
    PYTHONPATH=. .venv/bin/python -m scripts.load_test --same-session   # 同一会话（会被会话锁串行）
    PYTHONPATH=. .venv/bin/python -m scripts.load_test --mode health    # 只测轻量接口（不花 token）

指标：成功率、p50/p90/p99、吞吐（req/s）、按状态码统计。
注意：这会真实调用大模型，每轮都消耗 token。
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time
import uuid

import httpx

BASE = "http://127.0.0.1:8000"


async def one_turn(client: httpx.AsyncClient, sid: str, msg: str) -> dict:
    t0 = time.perf_counter()
    status, err = 200, None
    try:
        async with client.stream("POST", f"{BASE}/api/chat/stream",
                                 json={"message": msg, "session_id": sid, "scenario": "consult"}) as r:
            status = r.status_code
            async for _ in r.aiter_lines():
                pass
    except Exception as e:  # noqa: BLE001
        status, err = 0, f"{type(e).__name__}: {e}"
    return {"ms": (time.perf_counter() - t0) * 1000, "status": status, "error": err}


async def one_health(client: httpx.AsyncClient, sid: str = "", msg: str = "") -> dict:
    t0 = time.perf_counter()
    status, err = 200, None
    try:
        r = await client.get(f"{BASE}/api/health")
        status = r.status_code
    except Exception as e:  # noqa: BLE001
        status, err = 0, f"{type(e).__name__}: {e}"
    return {"ms": (time.perf_counter() - t0) * 1000, "status": status, "error": err}


async def main() -> int:
    ap = argparse.ArgumentParser(description="LChain 并发压测")
    ap.add_argument("-n", "--concurrency", type=int, default=50, help="并发数")
    ap.add_argument("-r", "--rounds", type=int, default=1, help="轮数（每轮并发 n 个请求）")
    ap.add_argument("--mode", choices=["consult", "health"], default="consult")
    ap.add_argument("--same-session", action="store_true", help="所有请求用同一个 session_id（演示会话锁串行）")
    ap.add_argument("--timeout", type=float, default=300)
    args = ap.parse_args()

    run_id = uuid.uuid4().hex[:6]
    same_sid = f"load-{run_id}"
    fn = one_health if args.mode == "health" else one_turn
    print(f"目标 {BASE}｜模式 {args.mode}｜并发 {args.concurrency}｜轮数 {args.rounds}"
          f"{'｜同一会话（会被串行）' if args.same_session else ''}")

    all_rows: list[dict] = []
    total_ms = 0.0
    async with httpx.AsyncClient(timeout=args.timeout, trust_env=False) as client:
        for rd in range(args.rounds):
            sid_of = (lambda i: same_sid) if args.same_session else (lambda i: f"{same_sid}-{rd}-{i}")
            t0 = time.perf_counter()
            rows = await asyncio.gather(*[fn(client, sid_of(i), f"我上腹痛{i + 1}天了")
                                          for i in range(args.concurrency)])
            wall = (time.perf_counter() - t0) * 1000
            total_ms += wall
            all_rows += rows
            ok = sum(1 for r in rows if r["status"] == 200)
            print(f"  第 {rd + 1} 轮：{ok}/{args.concurrency} 成功，总耗时 {wall:.0f} ms，"
                  f"{args.concurrency / (wall / 1000):.1f} req/s")

    lat = sorted(r["ms"] for r in all_rows)
    ok = [r for r in all_rows if r["status"] == 200]
    bad = [r for r in all_rows if r["status"] != 200]
    q = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))]
    print(f"\n合计 {len(all_rows)} 个请求｜成功 {len(ok)}｜失败 {len(bad)}"
          f"｜吞吐 {len(all_rows) / (total_ms / 1000):.1f} req/s")
    print(f"延迟 min={lat[0]:.0f} p50={q(.5):.0f} p90={q(.9):.0f} p99={q(.99):.0f} max={lat[-1]:.0f} ms"
          + (f"（中位数标准差 {statistics.pstdev(lat):.0f}）" if len(lat) > 2 else ""))
    if bad:
        from collections import Counter
        print("失败分布：", Counter(r["status"] for r in bad))
        for r in bad[:5]:
            print("   ", r["error"])
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
