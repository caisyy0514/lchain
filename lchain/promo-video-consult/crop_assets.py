"""把从浏览器抓下来的整屏截图裁成视频里要用的 UI 素材。

用法：python crop_assets.py
输入：`shots.json`（浏览器里抓图时记录的 {name, src, rect} 列表）
输出：`assets/<name>.png`（按 rect 裁剪 + 圆角 + 透明边），给 scenes.py 用。

为什么不在浏览器里直接截元素：浏览器工具只能整屏截图，这里用元素的位置自己裁，精度一样，
而且可以顺手做圆角和留白，贴在简笔画的手绘框里更自然。
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).parent
ASSETS = ROOT / "assets"
PAD = 18          # 裁剪时四周多留一点
RADIUS = 22       # 圆角半径
MAX_W = 1500      # 超过这个宽度就等比缩小（视频是 1920 宽，UI 嵌图不需要更大）


def main() -> None:
    shots = json.loads((ROOT / "shots.json").read_text())
    ASSETS.mkdir(exist_ok=True)
    for s in shots:
        src = Path(s["src"])
        if not src.is_file():
            print(f"⚠ 缺少 {src}，跳过 {s['name']}")
            continue
        r = s["rect"]
        im = Image.open(src).convert("RGB")
        box = (max(0, r["x"] - PAD), max(0, r["y"] - PAD),
               min(im.width, r["x"] + r["w"] + PAD), min(im.height, r["y"] + r["h"] + PAD))
        for pa in s.get("patch", []):   # 局部遮盖（例如按产品新命名抹掉界面上的旧文案）
            c = pa["color"]
            im.paste(tuple(c), (pa["x"], pa["y"], pa["x"] + pa["w"], pa["y"] + pa["h"]))
        crop = im.crop(box)
        if crop.width > MAX_W:
            crop = crop.resize((MAX_W, round(crop.height * MAX_W / crop.width)), Image.LANCZOS)
        # 圆角 + 透明边
        w, h = crop.size
        mask = Image.new("L", (w * 2, h * 2), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, w * 2 - 1, h * 2 - 1], radius=RADIUS * 2, fill=255)
        mask = mask.resize((w, h), Image.LANCZOS)
        out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        out.paste(crop, (0, 0), mask)
        out.save(ASSETS / f"{s['name']}.png")
        print(f"✓ {s['name']}.png  {w}×{h}")


if __name__ == "__main__":
    main()
