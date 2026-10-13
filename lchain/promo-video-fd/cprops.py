"""结构化问诊视频的专用素材（在 props.py 的通用素材之上加问诊场景要用的东西）。

风格与 props.py 保持一致：统一线宽、圆润造型、水彩填色、手绘抖动。

包含：
- 界面截图嵌图 `ui_shot()`：把从测试页截下来的真实 UI 贴进手绘外框里
- 病历卡 / 十项要素勾选 `record_card()`、`check_row()`
- 白板卡片 `note_card()`、代码卡 `code_card()`、进度对比条 `bar_compare()`
- 闸门 `gate()`、盖章 `stamp()`、剪刀 `scissors()`、红叉 `cross_mark()`
- 文件卡 `file_card()`、目录树 `folder_tree()`、标签页 `tabs()`
"""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from PIL import Image

from sketch import (BLUE, BLUE_FILL, GRAY_FILL, GREEN, GREEN_FILL, INK, INK_SOFT, ORANGE, ORANGE_FILL, PURPLE,
                    PURPLE_FILL, RED, RED_FILL, SS, TEAL, TEAL_FILL, WHITE, YELLOW_FILL, Canvas, back_out, clamp,
                    ease, ellipse_pts, mix, rounded_rect, seg)

LW = 4.2
ASSETS = Path(__file__).parent / "assets"


def seq(p: float, i: int, n: int, overlap: float = 1.2) -> float:
    """一串元素的第 i 个（共 n 个）的进度：p 从 0→1 时依次展开，**p=1 时最后一个也刚好完成**。

    不要用 seg(p, 0.4 + i*0.1, 0.8 + i*0.1) 这种写法：上界会超出 1，后面几个元素永远展不完
    （曾经因此出现「最后几行字只画了一半、后几个勾永远不打」的问题）。
    """
    if p <= 0:
        return 0.0
    return clamp((p * (n + overlap) - i) / overlap)


# ---------------------------------------------------------------------- UI 截图嵌图
@lru_cache(maxsize=16)
def _asset(name: str) -> Image.Image:
    return Image.open(ASSETS / f"{name}.png").convert("RGBA")


def _paste(c: Canvas, im: Image.Image, x: float, y: float, w: float, alpha: float) -> None:
    """把图片按宽度 w（逻辑坐标）贴在 (x, y) 左上角，alpha 淡入。"""
    w2 = max(1, int(w * SS))
    h2 = max(1, int(im.height * w2 / im.width))
    im2 = im.resize((w2, h2), Image.LANCZOS)
    if alpha < 0.999:
        a = im2.getchannel("A").point(lambda v: int(v * alpha))
        im2.putalpha(a)
    c.img.paste(im2, (int(x * SS), int(y * SS)), im2)


def ui_shot(c: Canvas, cx, cy, w, name: str, p: float = 1.0, rise: float = 20, seed: int = 3000) -> None:
    """真实界面截图 + 手绘外框（视频里「这就是跑起来的样子」的那种嵌图）。"""
    if p <= 0:
        return
    im = _asset(name)
    h = w * im.height / im.width
    k = ease(p)
    y = cy - h / 2 + (1 - k) * rise
    x = cx - w / 2
    c.shape(rounded_rect(x - 12, y - 12, w + 24, h + 24, 24), progress=seg(p, 0, 0.5), color=INK, fill=WHITE,
            width=LW, seed=seed, shadow=True, fill_delay=0.15, wobble=0.7)
    _paste(c, im, x, y, w, k * c.alpha)


# ---------------------------------------------------------------------- 卡片
def note_card(c: Canvas, cx, cy, w, h, lines: list[str], p: float = 1.0, color=INK, fill=WHITE, size: int = 30,
              title: str = "", title_size: int = 32, seed: int = 1000, strike: float = 0.0,
              align: str = "left") -> None:
    """一块白板卡片：标题 + 若干行文字；strike>0 时画一条红色删除线（表示被否掉）。"""
    if p <= 0:
        return
    sc = back_out(seg(p, 0, 0.45))
    w2, h2 = w * sc, h * sc
    x, y = cx - w2 / 2, cy - h2 / 2
    c.shape(rounded_rect(x, y, w2, h2, 22), progress=seg(p, 0, 0.6), color=color, fill=fill, width=LW, seed=seed,
            shadow=True, smooth=False, fill_delay=0.12, wobble=0.8)
    tc = seg(p, 0.35, 0.9)
    tx, anchor = (cx, "mm") if align == "center" else (x + 30, "lm")
    ty = y + 46
    if title:
        c.text(title, (cx if align == "center" else x + w / 2, y + 44), size=title_size, color=color,
               progress=tc, weight="semibold", anchor="mm")
        ty = y + 108
    for i, ln in enumerate(lines):
        yy = ty + i * (size + 20)
        if yy > y + h2 - 20:
            break
        lp = seq(p, i, len(lines), 0.9)
        if align == "center":
            c.text(ln, (cx, yy), size=size, color=INK, progress=lp, anchor="mm")
        else:
            c.dot(x + 34, yy - 1, 5, color, alpha=lp)
            c.text(ln, (tx + 22, yy), size=size, color=INK, progress=lp, anchor="lm")
    if strike > 0:
        sy = cy if not title else cy + 12
        c.line(x + 24, sy, x + w2 - 24, sy, progress=strike, color=RED, width=6, seed=seed + 7, wobble=0.5)


def code_card(c: Canvas, cx, cy, w, h, lines: list[str], p: float = 1.0, seed: int = 1100) -> None:
    """代码卡：深一点的浅灰底 + 等宽感的一行行字（示意「这块是代码在管」）。"""
    x, y = cx - w / 2, cy - h / 2
    c.shape(rounded_rect(x, y, w, h, 18), progress=seg(p, 0, 0.55), color=INK, fill=(238, 236, 232), width=LW,
            seed=seed, shadow=True, smooth=False, fill_delay=0.15)
    # 顶部三个小圆点（窗口按钮）
    for i in range(3):
        c.dot(x + 30 + i * 26, y + 26, 7, INK_SOFT, alpha=seg(p, 0.3, 0.6))
    for i, ln in enumerate(lines):
        c.text(ln, (x + 34, y + 68 + i * 40), size=28, color=INK if i else TEAL,
               progress=seq(p, i, len(lines), 1.0), weight="regular" if i else "semibold", anchor="lm")


def bar_compare(c: Canvas, x, y, w, before: str, after: str, p: float = 1.0, seed: int = 1200) -> None:
    """耗时对比条：上面长条 + 下面短条，右边写秒数（讲并发的时候用）。"""
    if p <= 0:
        return
    rows = [(0.0, ORANGE, ORANGE_FILL, before, 1.0), (0.5, GREEN, GREEN_FILL, after, 0.66)]
    for i, (delay, col, fl, label, k) in enumerate(rows):
        y0 = y + i * 86
        pr = seg(p, delay, delay + 0.55)
        h = 52
        c.shape(rounded_rect(x, y0, w, h, h / 2), progress=seg(pr, 0, 0.5), color=INK, fill=WHITE, width=LW * 0.85,
                seed=seed + i, shadow=True, smooth=False)
        iw = (w - 14) * k
        if pr > 0.45:
            c.shape(rounded_rect(x + 7, y0 + 7, max(iw, h - 14), h - 14, (h - 14) / 2), progress=1, color=col, fill=col,
                    width=2, seed=seed + 10 + i, smooth=False, fill_delay=0, wobble=0.3)
        c.text(label, (x + w + 26, y0 + h / 2), size=32, color=col, progress=seg(pr, 0.5, 1), weight="semibold",
               anchor="lm")


# ---------------------------------------------------------------------- 病历与闸门
def check_row(c: Canvas, x, y, w, label: str, p: float = 1.0, done: float = 0.0, color=TEAL, seed: int = 1300) -> None:
    """一个要素行：方框 + 文字，done 时打勾。"""
    if p <= 0:
        return
    bs = 26
    c.shape(rounded_rect(x, y - bs / 2, bs, bs, 7), progress=seg(p, 0, 0.5), color=INK_SOFT, fill=WHITE, width=2.8,
            seed=seed, smooth=False, fill_delay=0)
    c.text(label, (x + bs + 18, y), size=30, color=INK if done else INK_SOFT, progress=seg(p, 0.1, 0.7), anchor="lm")
    if done > 0:
        c.stroke([(x + 5, y), (x + 11, y + 8), (x + 22, y - 9)], progress=done, color=color, width=5, seed=seed + 1,
                 smooth=False, taper=False, wobble=0)
        _ = w


def record_card(c: Canvas, cx, cy, w, h, items: list[str], p: float = 1.0, done: int = 0, seed: int = 1400) -> None:
    """病历卡：标题条 + 两列要素勾选（十项现病史）。"""
    x, y = cx - w / 2, cy - h / 2
    c.shape(rounded_rect(x, y, w, h, 26), progress=seg(p, 0, 0.5), color=TEAL, fill=WHITE, width=LW, seed=seed,
            shadow=True, smooth=False, fill_delay=0.15)
    c.fill(rounded_rect(x + 8, y + 8, w - 16, 66, 18), TEAL_FILL, progress=seg(p, 0.3, 0.7), offset=(0, 0))
    c.text("现病史采集", (x + w / 2, y + 42), size=32, color=TEAL, progress=seg(p, 0.35, 0.8), weight="semibold")
    half = (len(items) + 1) // 2
    for i, label in enumerate(items):
        col, row = divmod(i, half)
        bx = x + 46 + col * (w / 2 - 20)
        by = y + 128 + row * 74
        ap = seq(p, i, len(items), 1.6)
        dp = ease(seq(p, i, len(items), 2.4)) if i < done else 0.0
        check_row(c, bx, by, w / 2 - 60, label, p=ap, done=dp, seed=seed + 10 + i)


def gate(c: Canvas, cx, cy, w, h, p: float = 1.0, blocked: float = 0.0, seed: int = 1500) -> None:
    """闸门：两根立柱 + 一根横杆（横杆落下来表示拦住）。blocked>0 时横杆下压并闪红。"""
    x = cx - w / 2
    for sx in (0, 1):
        c.shape(rounded_rect(x + sx * (w - 30), cy - h / 2, 30, h, 10), progress=seg(p, 0, 0.45), color=INK,
                fill=GRAY_FILL, width=LW, seed=seed + sx, smooth=False, shadow=True)
    drop = ease(blocked) * (h / 2 - 140)
    bar_y = cy - h / 2 + 30 + drop
    c.shape(rounded_rect(x - 16, bar_y, w + 32, 48, 14), progress=seg(p, 0.3, 0.75), color=RED if blocked > 0.5 else INK,
            fill=RED_FILL if blocked > 0.5 else (240, 238, 232), width=LW, seed=seed + 5, smooth=False, shadow=True)
    c.text("流程闸门", (cx, bar_y + 24), size=30, color=RED if blocked > 0.5 else INK,
           progress=seg(p, 0.5, 0.95), weight="semibold")
    if blocked > 0.5:
        for i in range(2):
            c.text("✕", (x - 44 - i * 44, cy + 40 + i * 40), size=44, color=RED,
                   progress=seg(blocked, 0.5 + i * 0.2, 1), weight="semibold")


def cross_mark(c: Canvas, cx, cy, s: float = 1.0, p: float = 1.0, color=RED) -> None:
    """红叉（表示被拦下 / 不行）。"""
    if p <= 0:
        return
    k = back_out(seg(p, 0, 0.6))
    for a in (45, 135):
        r = math.radians(a)
        c.line(cx - 22 * s * k * math.cos(r), cy - 22 * s * k * math.sin(r),
               cx + 22 * s * k * math.cos(r), cy + 22 * s * k * math.sin(r), color=color, width=7 * s, seed=int(a),
               taper=True)


def scissors(c: Canvas, cx, cy, s: float = 1.0, p: float = 1.0, angle: float = -25.0, seed: int = 1600) -> None:
    """剪刀（讲「把多出来的问题剪掉」时用）。"""
    if p <= 0:
        return
    a = math.radians(angle)
    for sx in (-1, 1):
        # 刀刃
        bx, by = cx + sx * 14 * s, cy - 10 * s
        ex = bx + 96 * s * math.cos(a + sx * 0.22)
        ey = by + 96 * s * math.sin(a + sx * 0.22)
        c.stroke([(bx, by), (ex, ey)], progress=seg(p, 0, 0.6), color=INK_SOFT, width=5 * s, seed=seed + sx,
                 smooth=False, taper=True)
        # 把手
        hx = cx + sx * 30 * s
        hy = cy + 30 * s
        c.shape(ellipse_pts(hx, hy + sx * 12 * s, 22 * s, 15 * s, 0, 360, n=24)[:-1], progress=seg(p, 0.4, 1),
                color=INK, fill=WHITE, width=4 * s, seed=seed + 10 + sx)
    c.dot(cx, cy - 4 * s, 6 * s, INK, alpha=seg(p, 0.6, 1))


def stamp(c: Canvas, cx, cy, text: str, p: float = 1.0, color=RED, size: int = 30, seed: int = 1700) -> None:
    """盖章：双线圆角框 + 文字（斜一点点，像随手盖上去的）。"""
    if p <= 0:
        return
    k = back_out(seg(p, 0, 0.55))
    w = len(text) * size * 0.95 + 54
    h = size + 40
    x, y = cx - w / 2, cy - h / 2
    c.shape(rounded_rect(x, y, w * k, h, 14), progress=seg(p, 0, 0.6), color=color, fill=None, width=5, seed=seed,
            smooth=False, wobble=1.4)
    c.text(text, (cx, cy), size=size, color=color, progress=seg(p, 0.35, 1), weight="semibold")


# ---------------------------------------------------------------------- 文件 / 目录 / 标签页
def file_card(c: Canvas, cx, cy, w, h, name: str, lines: list[str], p: float = 1.0, color=PURPLE,
              seed: int = 1800) -> None:
    """文件卡：consult.json（右上折角 + 文件名 + 几行内容）。"""
    x, y = cx - w / 2, cy - h / 2
    fold = 44
    pts = [(x, y), (x + w - fold, y), (x + w, y + fold), (x + w, y + h), (x, y + h)]
    c.shape(pts, progress=seg(p, 0, 0.55), color=color, fill=PURPLE_FILL if color == PURPLE else WHITE, width=LW,
            seed=seed, smooth=False, shadow=True, fill_delay=0.15)
    c.stroke([(x + w - fold, y), (x + w - fold, y + fold), (x + w, y + fold)], progress=seg(p, 0.35, 0.7),
             color=color, width=3.6, seed=seed + 1, smooth=False)
    c.text(name, (x + w / 2, y + 40), size=32, color=color, progress=seg(p, 0.4, 0.85), weight="semibold")
    for i, ln in enumerate(lines):
        c.text(ln, (x + 34, y + 92 + i * 44), size=26, color=INK, progress=seq(p, i, len(lines), 1.0), anchor="lm")


def folder_tree(c: Canvas, cx, cy, w, h, rows: list[tuple[int, str, str]], p: float = 1.0, seed: int = 1900,
                title: str = "eval/consult/") -> None:
    """目录树卡：rows = [(缩进层级, 名字, 备注)]，目录用文件夹色，文件用墨色。"""
    x, y = cx - w / 2, cy - h / 2
    c.shape(rounded_rect(x, y, w, h, 22), progress=seg(p, 0, 0.5), color=INK, fill=WHITE, width=LW, seed=seed,
            shadow=True, smooth=False, fill_delay=0.15)
    c.fill(rounded_rect(x + 8, y + 8, w - 16, 58, 16), (240, 238, 232), progress=seg(p, 0.3, 0.65), offset=(0, 0))
    c.text(title, (x + 30, y + 38), size=28, color=INK, progress=seg(p, 0.35, 0.8), anchor="lm",
           weight="semibold")
    for i, (depth, name, note) in enumerate(rows):
        yy = y + 96 + i * 62
        if yy > y + h - 40:
            break
        pr = seq(p, i, len(rows), 1.5)
        is_dir = note == "dir"
        c.shape([(x + 40 + depth * 40, yy - 12), (x + 72 + depth * 40, yy - 12), (x + 80 + depth * 40, yy - 6),
                 (x + 80 + depth * 40, yy + 12), (x + 40 + depth * 40, yy + 12)],
                progress=seg(pr, 0, 0.5), color=ORANGE if is_dir else INK_SOFT,
                fill=ORANGE_FILL if is_dir else WHITE, width=2.8, seed=seed + i, smooth=False, fill_delay=0)
        c.text(name, (x + 96 + depth * 40, yy), size=24, color=ORANGE if is_dir else INK, progress=seg(pr, 0.3, 1),
               anchor="lm", weight="semibold" if is_dir else "regular")
        if note and not is_dir:
            c.text(note, (x + w - 30, yy), size=23, color=INK_SOFT, progress=seg(pr, 0.5, 1), anchor="rm")


def tabs(c: Canvas, x, y, labels: list[str], p: float = 1.0, active: int = 0, w: float = 150, h: float = 96,
         seed: int = 2000) -> None:
    """几个浏览器标签页（讲「多标签页共用会话会排队」时用）。"""
    for i, lb in enumerate(labels):
        bx = x + i * (w + 22)
        pr = seq(p, i, len(labels), 1.0)
        act = i == active
        c.shape(rounded_rect(bx, y + (0 if act else 12), w, h - (0 if act else 12), 14),
                progress=seg(pr, 0, 0.65), color=TEAL if act else INK_SOFT, fill=TEAL_FILL if act else WHITE,
                width=LW * 0.85, seed=seed + i, smooth=False, shadow=True, fill_delay=0.1)
        c.text(lb, (bx + w / 2, y + (0 if act else 12) + (h - (0 if act else 12)) / 2), size=26,
               color=TEAL if act else INK_SOFT, progress=seg(pr, 0.45, 1), anchor="mm")


def progress_pill(c: Canvas, cx, cy, text: str, p: float = 1.0, color=TEAL, size: int = 30) -> None:
    """带勾的胶囊（步骤完成）。"""
    c.pill(text, cx, cy, size=size, color=color, fill=mix(color, WHITE, 0.86), progress=p, weight="medium")


_ = (BLUE, BLUE_FILL, GREEN, GREEN_FILL, YELLOW_FILL, (255, 236, 222), math)
