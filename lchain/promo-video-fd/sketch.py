"""线性简笔画引擎 v2：更精致的手绘质感。

- 3 倍超采样 + LANCZOS 缩放，线条边缘平滑
- Catmull-Rom 样条：折线控制点自动变成平滑曲线
- 笔触粗细变化：起笔、收笔处变细（像真实的笔），中段略有粗细起伏
- 低频抖动：线条有轻微的手绘弯曲，但不“毛躁”
- 水彩填色：线稿画完后，柔和的颜色填充从 0 淡入，且相对线稿略微错位（手工上色的感觉）
- 柔和投影、纸张纹理背景、PingFang 字体

坐标系统一为 1920×1080；所有绘制函数都支持 progress（0~1）逐笔画出。
"""
from __future__ import annotations

import math
import random
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1920, 1080
SS = 3

# ---------------------------------------------------------------------- 配色（低饱和、和谐）
PAPER = (250, 247, 240)
INK = (43, 45, 66)            # 深蓝灰墨线（比纯黑柔和）
INK_SOFT = (120, 124, 145)
TEAL = (30, 148, 152)         # 问诊主色（医疗感的青绿）
TEAL_FILL = (206, 240, 238)
BLUE = (72, 119, 245)         # 框架 / 技术主色
BLUE_FILL = (214, 228, 255)
GREEN = (46, 170, 120)
GREEN_FILL = (210, 242, 226)
ORANGE = (242, 150, 60)
ORANGE_FILL = (255, 232, 205)
RED = (232, 92, 88)
RED_FILL = (255, 221, 218)
YELLOW_FILL = (255, 241, 196)
PURPLE = (140, 110, 230)
PURPLE_FILL = (232, 224, 255)
GRAY_FILL = (236, 234, 228)
WHITE = (255, 255, 255)
SHADOW = (0, 0, 0)

_FONT_CANDIDATES = [
    "/System/Library/AssetsV2/com_apple_MobileAsset_Font8/86ba2c91f017a3749571a82f2c6d890ac7ffb2fb.asset/AssetData/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
]


def _find_font() -> str:
    for p in _FONT_CANDIDATES:
        if Path(p).exists():
            return p
    for p in Path("/System/Library/AssetsV2").glob("**/PingFang.ttc"):
        return str(p)
    return _FONT_CANDIDATES[-1]


FONT_PATH = _find_font()


def _sc_indices(path: str) -> dict[str, int]:
    """在 .ttc 里按名字找简体中文（SC）各字重的序号。
    注意：PingFang.ttc 里前几个是 HK/TC 字体，缺少部分简体字（如“杂”“稳”），必须选 SC。"""
    out: dict[str, int] = {}
    for i in range(40):
        try:
            fam, style = ImageFont.truetype(path, 20, index=i).getname()
        except OSError:
            break
        if fam.endswith(" SC") or fam in ("Hiragino Sans GB", "Heiti SC"):
            out.setdefault(style.lower(), i)
    return out


_WEIGHTS = _sc_indices(FONT_PATH)


@lru_cache(maxsize=128)
def font(size: int, weight: str = "medium") -> ImageFont.FreeTypeFont:
    idx = _WEIGHTS.get(weight, _WEIGHTS.get("regular", 0))
    return ImageFont.truetype(FONT_PATH, int(size * SS), index=idx)


# ---------------------------------------------------------------------- 缓动
def clamp(t: float) -> float:
    return max(0.0, min(1.0, t))


def ease(t: float) -> float:
    t = clamp(t)
    return t * t * (3 - 2 * t)


def ease_out(t: float) -> float:
    return 1 - (1 - clamp(t)) ** 3


def ease_in_out(t: float) -> float:
    t = clamp(t)
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def back_out(t: float, s: float = 1.7) -> float:
    """带回弹的弹出（先冲过头一点再回来）。"""
    t = clamp(t) - 1
    return t * t * ((s + 1) * t + s) + 1


def seg(t: float, a: float, b: float) -> float:
    if b <= a:
        return 1.0 if t >= b else 0.0
    return clamp((t - a) / (b - a))


def lerp(a, b, t):
    return a + (b - a) * t


def mix(c1, c2, t):
    return tuple(int(round(lerp(c1[i], c2[i], t))) for i in range(3))


# ---------------------------------------------------------------------- 几何
Pt = tuple[float, float]


def catmull_rom(pts: list[Pt], closed: bool = False, samples: int = 10) -> list[Pt]:
    """把控制点变成平滑曲线。"""
    if len(pts) < 3:
        return list(pts)
    P = list(pts)
    if closed:
        P = [P[-1]] + P + [P[0], P[1]]
    else:
        P = [P[0]] + P + [P[-1]]
    out: list[Pt] = []
    for i in range(1, len(P) - 2):
        p0, p1, p2, p3 = P[i - 1], P[i], P[i + 1], P[i + 2]
        d = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        n = max(2, int(d / 5))
        for k in range(n):
            t = k / n
            t2, t3 = t * t, t * t * t
            x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                       + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
            y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                       + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            out.append((x, y))
    out.append(P[-2])
    return out


def densify(pts: list[Pt], step: float = 4) -> list[Pt]:
    out = [pts[0]]
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        d = math.hypot(x2 - x1, y2 - y1)
        n = max(1, int(d / step))
        for i in range(1, n + 1):
            out.append((x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n))
    return out


def jitter(pts: list[Pt], seed: int, amp: float) -> list[Pt]:
    if amp <= 0:
        return pts
    rnd = random.Random(seed)
    ph = [rnd.random() * 6.283 for _ in range(4)]
    n = len(pts)
    total = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:])) or 1
    freq = max(1.0, total / 260)  # 长线多几个起伏，短线基本不弯
    out = []
    for i, (x, y) in enumerate(pts):
        u = i / max(1, n - 1)
        dx = math.sin(u * freq * 2.1 + ph[0]) * amp + math.sin(u * freq * 5.3 + ph[1]) * amp * 0.25
        dy = math.sin(u * freq * 1.7 + ph[2]) * amp + math.sin(u * freq * 4.7 + ph[3]) * amp * 0.25
        out.append((x + dx, y + dy))
    return out


def partial(pts: list[Pt], p: float) -> list[Pt]:
    if p >= 1:
        return pts
    if p <= 0 or len(pts) < 2:
        return []
    lens = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:])]
    target = sum(lens) * p
    out = [pts[0]]
    acc = 0.0
    for (a, b), l in zip(zip(pts, pts[1:]), lens):
        if acc + l >= target:
            r = (target - acc) / l if l else 0
            out.append((a[0] + (b[0] - a[0]) * r, a[1] + (b[1] - a[1]) * r))
            return out
        out.append(b)
        acc += l
    return out


def rounded_rect(x, y, w, h, r) -> list[Pt]:
    r = min(r, w / 2, h / 2)
    pts = []
    for cx, cy, a0 in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0), (x + r, y + h - r, 90), (x + r, y + r, 180)):
        for i in range(9):
            a = math.radians(a0 + 90 * i / 8)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def ellipse_pts(cx, cy, rx, ry, start=-90, sweep=360, n=None) -> list[Pt]:
    n = n or max(24, int(abs(sweep) / 5))
    return [(cx + rx * math.cos(math.radians(start + sweep * i / n)),
             cy + ry * math.sin(math.radians(start + sweep * i / n))) for i in range(n + 1)]


def bubble_pts(x, y, w, h, tail: str = "left") -> list[Pt]:
    r = min(26, h / 2)
    pts = rounded_rect(x, y, w, h, r)
    # rounded_rect 第 18~26 个点是左下角；在底边（右下角结束到左下角开始之间）插入尾巴
    if tail == "left":
        bx = x + 44
        ins = [(bx + 18, y + h), (bx - 12, y + h + 26), (bx - 2, y + h)]
    else:
        bx = x + w - 44
        ins = [(bx + 2, y + h), (bx + 12, y + h + 26), (bx - 18, y + h)]
    return pts[:18] + ins + pts[18:]


# ---------------------------------------------------------------------- 画布
class Canvas:
    def __init__(self) -> None:
        self.img = Image.new("RGB", (W * SS, H * SS), PAPER)
        self.d = ImageDraw.Draw(self.img, "RGBA")
        self._seed = 0
        self.dx = 0.0  # 整体平移（转场用）
        self.dy = 0.0
        self.alpha = 1.0  # 整体透明度（转场用，作用于新画的元素）

    def _s(self, p: Pt) -> tuple[float, float]:
        return ((p[0] + self.dx) * SS, (p[1] + self.dy) * SS)

    def _next_seed(self, seed):
        if seed is None:
            self._seed += 1
            return self._seed
        return seed

    def _col(self, color, a: float = 1.0):
        a = clamp(a * self.alpha)
        return (*color[:3], int(255 * a))

    # ------------------------------------------------------------------ 笔触
    def stroke(self, pts: list[Pt], progress: float = 1.0, color=INK, width: float = 4.5, smooth: bool = True,
               closed: bool = False, seed=None, wobble: float = 1.2, taper: bool = True, alpha: float = 1.0) -> None:
        """画一条手绘笔触。"""
        if progress <= 0 or len(pts) < 2 or self.alpha <= 0:
            return
        seed = self._next_seed(seed)
        P = catmull_rom(pts, closed=closed) if smooth and len(pts) >= 3 else list(pts) + ([pts[0]] if closed else [])
        P = jitter(densify(P, 3), seed, wobble)
        P = partial(P, ease_out(progress) if progress < 1 else 1)
        if len(P) < 2:
            return
        n = len(P)
        col = self._col(color, alpha)
        rnd = random.Random(seed * 7 + 1)
        ph = rnd.random() * 6.28
        full_n = n / max(ease_out(progress), 1e-3) if progress < 1 else n
        # 逐小段画圆，宽度随位置变化：两端细，中间有轻微起伏
        pts_s = [self._s(p) for p in P]
        for i in range(n - 1):
            u = i / max(1, full_n - 1)
            w = width
            if taper:
                w *= min(1.0, 0.35 + u * 6) * min(1.0, 0.35 + (1 - u) * 6) if not closed else 1.0
            w *= 1 + 0.12 * math.sin(u * 9 + ph)
            r = w * SS / 2
            x1, y1 = pts_s[i]
            x2, y2 = pts_s[i + 1]
            self.d.line([x1, y1, x2, y2], fill=col, width=max(1, int(r * 2)))
            self.d.ellipse([x2 - r, y2 - r, x2 + r, y2 + r], fill=col)
        x0, y0 = pts_s[0]
        r0 = width * SS / 2 * (0.35 if taper and not closed else 1)
        self.d.ellipse([x0 - r0, y0 - r0, x0 + r0, y0 + r0], fill=col)

    def fill(self, pts: list[Pt], color, progress: float = 1.0, smooth: bool = True, closed: bool = True,
             offset: Pt = (5, 6), seed=None) -> None:
        """水彩式填色：淡入 + 轻微错位。"""
        if progress <= 0 or self.alpha <= 0:
            return
        P = catmull_rom(pts, closed=closed) if smooth and len(pts) >= 3 else list(pts)
        a = ease(progress)
        ox, oy = offset
        poly = [self._s((x + ox, y + oy)) for x, y in P]
        self.d.polygon(poly, fill=self._col(color, a * 0.95))

    def shadow(self, pts: list[Pt], progress: float = 1.0, smooth: bool = True, offset: Pt = (0, 14),
               blur: float = 14, strength: float = 0.10) -> None:
        """柔和投影（单独一层模糊后合成）。"""
        if progress <= 0 or self.alpha <= 0:
            return
        P = catmull_rom(pts, closed=True) if smooth and len(pts) >= 3 else list(pts)
        xs = [p[0] for p in P]
        ys = [p[1] for p in P]
        pad = blur * 3
        x0, y0 = min(xs) - pad + offset[0], min(ys) - pad + offset[1]
        x1, y1 = max(xs) + pad + offset[0], max(ys) + pad + offset[1]
        bw, bh = int((x1 - x0) * SS), int((y1 - y0) * SS)
        if bw <= 0 or bh <= 0:
            return
        layer = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(layer).polygon([((x + offset[0] - x0) * SS, (y + offset[1] - y0) * SS) for x, y in P],
                                      fill=int(255 * strength * ease(progress) * self.alpha))
        layer = layer.filter(ImageFilter.GaussianBlur(blur * SS))
        self.img.paste(SHADOW, (int((x0 + self.dx) * SS), int((y0 + self.dy) * SS)), layer)

    # ------------------------------------------------------------------ 组合形状
    def shape(self, pts: list[Pt], progress: float = 1.0, color=INK, fill=None, width: float = 4.5, seed=None,
              shadow: bool = False, smooth: bool = True, wobble: float = 1.2, fill_delay: float = 0.55) -> None:
        """闭合形状：先投影 → 填色（线稿画到一半后开始淡入）→ 线稿。"""
        seed = self._next_seed(seed)
        if shadow:
            self.shadow(pts, progress=seg(progress, fill_delay, 1), smooth=smooth)
        if fill is not None:
            self.fill(pts, fill, progress=seg(progress, fill_delay, 1), smooth=smooth)
        self.stroke(pts, progress=seg(progress, 0, 0.8), color=color, width=width, closed=True, seed=seed,
                    smooth=smooth, wobble=wobble)

    def rect(self, x, y, w, h, radius=20, **kw) -> None:
        self.shape(rounded_rect(x, y, w, h, radius), smooth=False, **kw)

    def ellipse(self, cx, cy, rx, ry=None, **kw) -> None:
        self.shape(ellipse_pts(cx, cy, rx, ry or rx, n=48)[:-1], smooth=True, **kw)

    def arc(self, cx, cy, r, start, sweep, progress=1.0, **kw) -> None:
        self.stroke(ellipse_pts(cx, cy, r, r, start, sweep), progress=progress, smooth=False, **kw)

    def line(self, x1, y1, x2, y2, **kw) -> None:
        kw.setdefault("smooth", False)
        self.stroke([(x1, y1), (x2, y2)], **kw)

    def curve(self, pts, **kw) -> None:
        self.stroke(pts, smooth=True, **kw)

    def arrow(self, pts: list[Pt], progress=1.0, color=INK, width=4, head=16, seed=None, **kw) -> None:
        seed = self._next_seed(seed)
        self.stroke(pts, progress=progress, color=color, width=width, seed=seed, smooth=len(pts) > 2, **kw)
        if progress >= 0.92:
            (x1, y1), (x2, y2) = pts[-2], pts[-1]
            a = math.atan2(y2 - y1, x2 - x1)
            p = seg(progress, 0.92, 1)
            for s in (-1, 1):
                self.stroke([(x2, y2), (x2 - head * math.cos(a + s * 0.45), y2 - head * math.sin(a + s * 0.45))],
                            progress=p, color=color, width=width, seed=seed + 50 + s, smooth=False, taper=False)

    def dot(self, x, y, r, color=INK, alpha=1.0) -> None:
        if self.alpha <= 0:
            return
        cx, cy = self._s((x, y))
        rr = r * SS
        self.d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=self._col(color, alpha))

    def blob(self, cx, cy, rx, ry, color, alpha=1.0, seed=0) -> None:
        """背景装饰色块（不规则柔和圆斑）。"""
        rnd = random.Random(seed)
        pts = []
        for i in range(10):
            a = i / 10 * 6.283
            k = 1 + (rnd.random() - 0.5) * 0.18
            pts.append((cx + rx * k * math.cos(a), cy + ry * k * math.sin(a)))
        P = catmull_rom(pts, closed=True)
        self.d.polygon([self._s(p) for p in P], fill=self._col(color, alpha))

    # ------------------------------------------------------------------ 文字
    def text(self, s: str, xy: Pt, size: int = 44, color=INK, progress: float = 1.0, anchor: str = "mm",
             weight: str = "medium", alpha: float = 1.0, rise: float = 10) -> None:
        """逐字浮现：每个字从下方 rise 像素处淡入上移。"""
        if progress <= 0 or not s or self.alpha <= 0:
            return
        f = font(size, weight)
        full_w = self.d.textlength(s, font=f)
        x, y = self._s(xy)
        if anchor[0] == "m":
            x -= full_w / 2
        elif anchor[0] == "r":
            x -= full_w
        n = len(s)
        span = 0.45  # 每个字淡入所占的进度比例
        for i, ch in enumerate(s):
            start = (1 - span) * i / max(1, n - 1) if n > 1 else 0
            cp = ease_out(seg(progress, start, start + span))
            if cp <= 0:
                break
            cw = self.d.textlength(ch, font=f)
            prefix = self.d.textlength(s[:i], font=f)
            self.d.text((x + prefix, y + (1 - cp) * rise * SS), ch, font=f, fill=self._col(color, alpha * cp),
                        anchor="l" + anchor[1])
            _ = cw

    def pill(self, s: str, cx, cy, size=30, color=BLUE, fill=None, progress=1.0, pad=(26, 14), weight="medium") -> None:
        """胶囊标签。"""
        if progress <= 0:
            return
        f = font(size, weight)
        tw = self.d.textlength(s, font=f) / SS
        w, h = tw + pad[0] * 2, size + pad[1] * 2
        sc = back_out(seg(progress, 0, 0.6))
        w2, h2 = w * sc, h * sc
        self.shape(rounded_rect(cx - w2 / 2, cy - h2 / 2, w2, h2, h2 / 2), progress=seg(progress, 0, 0.7), color=color,
                   fill=fill or mix(color, WHITE, 0.85), width=3, smooth=False, wobble=0.5)
        self.text(s, (cx, cy), size=size, color=color, progress=seg(progress, 0.3, 1), weight=weight, rise=4)

    def subtitle(self, s: str, alpha: float = 1.0) -> None:
        if not s or alpha <= 0:
            return
        f = font(34, "regular")
        tw = self.d.textlength(s, font=f) / SS
        cx, cy = W / 2, H - 78
        w, h = tw + 64, 64
        box = [(cx - w / 2) * SS, (cy - h / 2) * SS, (cx + w / 2) * SS, (cy + h / 2) * SS]
        self.d.rounded_rectangle(box, radius=h / 2 * SS, fill=(43, 45, 66, int(210 * alpha)))
        self.d.text((cx * SS, cy * SS), s, font=f, fill=(255, 255, 255, int(255 * alpha)), anchor="mm")

    # ------------------------------------------------------------------ 输出
    def to_image(self) -> Image.Image:
        return self.img.resize((W, H), Image.LANCZOS)


# ---------------------------------------------------------------------- 纸张背景（缓存）
@lru_cache(maxsize=1)
def paper_texture() -> Image.Image:
    rng = np.random.default_rng(3)
    base = np.array(PAPER, dtype=np.float32)[None, None, :] * np.ones((H, W, 1), dtype=np.float32)
    noise = rng.normal(0, 1, (H // 4, W // 4)).astype(np.float32)
    noise = np.array(Image.fromarray(((noise + 3) / 6 * 255).clip(0, 255).astype(np.uint8)).resize((W, H), Image.BICUBIC),
                     dtype=np.float32) / 255 - 0.5
    fine = rng.normal(0, 1, (H, W)).astype(np.float32) * 0.5
    img = base + (noise * 5 + fine * 2.2)[..., None]
    # 暗角
    yy, xx = np.mgrid[0:H, 0:W]
    v = ((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2
    img *= (1 - 0.06 * np.clip(v, 0, 1.4))[..., None]
    return Image.fromarray(img.clip(0, 255).astype(np.uint8)).resize((W * SS, H * SS), Image.BILINEAR)


def new_canvas() -> Canvas:
    c = Canvas()
    c.img.paste(paper_texture())
    c.d = ImageDraw.Draw(c.img, "RGBA")
    return c
