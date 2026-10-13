"""渲染单个分镜的指定时刻，调画面时用（不用等整段渲染）。

    python probe.py 5 13.2 out.png      # 第 5 个分镜第 13.2 秒
    python probe.py 5 88% out.png       # 第 5 个分镜的 88% 处
"""
from __future__ import annotations

import sys

from scenes import SCENES
from sketch import new_canvas


def main() -> None:
    idx = int(sys.argv[1]) - 1
    arg = sys.argv[2]
    out = sys.argv[3] if len(sys.argv) > 3 else f"output/preview/probe{idx + 1}.png"
    fn, d, text = SCENES[idx]
    t = d * float(arg[:-1]) / 100 if arg.endswith("%") else float(arg)
    c = new_canvas()
    fn(c, t, d)
    c.to_image().save(out)
    print(f"{out}  分镜 {idx + 1} t={t:.1f}s/{d:.1f}s  {text[:20]}…")


if __name__ == "__main__":
    main()
