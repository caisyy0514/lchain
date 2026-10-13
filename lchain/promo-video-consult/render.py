"""逐帧渲染：python render.py [--preview] [--fps 30]

- 字幕按旁白实际时间逐句显示（output/voice/subs.json，由 voice.py 生成；没有时退回整段文案）
- 分镜之间 0.5 秒交叉淡化转场
--preview  每个分镜渲染 2 张关键帧到 output/preview/，用于快速检查画面
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from pathlib import Path

from PIL import Image

from scenes import NO_SUBTITLE, SCENES
from sketch import new_canvas, seg

ROOT = Path(__file__).parent
OUT = ROOT / "output"
FRAMES = OUT / "frames"
XFADE = 0.5


def timeline() -> list[tuple[float, float]]:
    out, t = [], 0.0
    for _, d, _ in SCENES:
        out.append((t, d))
        t += d
    return out


def total_duration() -> float:
    return sum(d for _, d, _ in SCENES)


@lru_cache(maxsize=1)
def subtitles() -> list[dict]:
    p = OUT / "voice" / "subs.json"
    if p.exists():
        return json.loads(p.read_text())
    subs = []
    for (_, d, text), (start, _) in zip(SCENES, timeline()):
        subs.append({"start": start + 0.8, "end": start + d - 0.6, "text": text})
    return subs


def _scene_image(idx: int, lt: float) -> Image.Image:
    fn, d, _ = SCENES[idx]
    c = new_canvas()
    fn(c, lt, d)
    return c


def draw_frame(t: float):
    tl = timeline()
    idx = next((i for i, (s, d) in enumerate(tl) if s <= t < s + d), len(tl) - 1)
    start, d = tl[idx]
    c = _scene_image(idx, t - start)
    # 交叉淡化：本分镜开头与上一分镜末尾叠化
    if idx > 0 and t - start < XFADE:
        pfn, pd, _ = SCENES[idx - 1]
        pc = new_canvas()
        pfn(pc, pd - 0.001 + (t - start) * 0, pd)
        a = seg(t - start, 0, XFADE)
        c.img = Image.blend(pc.img, c.img, a)
        from PIL import ImageDraw

        c.d = ImageDraw.Draw(c.img, "RGBA")
    fn = SCENES[idx][0]
    if fn not in NO_SUBTITLE:
        for s in subtitles():
            if s["start"] - 0.15 <= t <= s["end"] + 0.15:
                alpha = seg(t, s["start"] - 0.15, s["start"] + 0.1) * (1 - seg(t, s["end"], s["end"] + 0.15))
                c.subtitle(s["text"], alpha)
                break
    return c


def render_one(args: tuple[int, float]) -> int:
    i, t = args
    draw_frame(t).to_image().save(FRAMES / f"{i:05d}.png", compress_level=1)
    return i


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--workers", type=int, default=0)
    a = ap.parse_args()

    if a.preview:
        pv = OUT / "preview"
        pv.mkdir(parents=True, exist_ok=True)
        for idx, ((_, d, _), (start, _)) in enumerate(zip(SCENES, timeline())):
            for frac in (0.5, 0.88):
                draw_frame(start + d * frac).to_image().save(pv / f"scene{idx + 1}_{int(frac * 100)}.png")
        print("预览帧已生成：", pv)
        return

    FRAMES.mkdir(parents=True, exist_ok=True)
    for f in FRAMES.glob("*.png"):
        f.unlink()
    n = int(total_duration() * a.fps)
    jobs = [(i, i / a.fps) for i in range(n)]
    t0 = time.time()
    workers = a.workers or max(1, (os.cpu_count() or 4) - 1)
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for k, _ in enumerate(ex.map(render_one, jobs, chunksize=6)):
            if k % 120 == 0:
                print(f"\r渲染 {k}/{n} 帧  {time.time() - t0:.0f}s", end="", file=sys.stderr, flush=True)
    print(f"\n完成：{n} 帧，用时 {time.time() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
