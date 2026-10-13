"""简笔画素材 v2：更精致的角色和物件（统一线宽、圆润造型、水彩填色、表情、投影）。

所有函数签名：xxx(c, ..., p=进度 0~1, ...)。进度 0→1 时按“先轮廓、后细节、再上色”的顺序逐笔画出。
"""
from __future__ import annotations

import math

from sketch import (BLUE, BLUE_FILL, GRAY_FILL, GREEN, GREEN_FILL, INK, INK_SOFT, ORANGE, ORANGE_FILL, PURPLE,
                    PURPLE_FILL, RED, RED_FILL, TEAL, TEAL_FILL, WHITE, YELLOW_FILL, Canvas, back_out, bubble_pts,
                    ease, ellipse_pts, rounded_rect, seg)

LW = 4.2  # 统一主线宽


# ---------------------------------------------------------------------- 人物
def person(c: Canvas, x, y, s=1.0, p=1.0, shirt=ORANGE_FILL, mood="smile", wave=0.0, sweat=0.0, seed=100,
           label: str = "", hair=INK, bob: float = 0.0, coat=False) -> None:
    """简笔画人物（x, y 为脚底中心）：圆头 + 头发 + 梯形上衣 + 手臂 + 腿，带表情。

    coat=True 时外面再画一件白大褂 + 听诊器（问诊视频的 AI 医生用，属于本项目的加法改动）。
    """
    y = y - bob
    hr = 38 * s
    hx, hy = x, y - 232 * s
    # 腿
    c.stroke([(x - 18 * s, y - 92 * s), (x - 24 * s, y)], progress=seg(p, 0.55, 0.75), width=LW * s, seed=seed + 1,
             smooth=False)
    c.stroke([(x + 18 * s, y - 92 * s), (x + 24 * s, y)], progress=seg(p, 0.6, 0.8), width=LW * s, seed=seed + 2,
             smooth=False)
    c.stroke([(x - 24 * s, y), (x - 44 * s, y)], progress=seg(p, 0.72, 0.85), width=LW * s, seed=seed + 3, smooth=False)
    c.stroke([(x + 24 * s, y), (x + 44 * s, y)], progress=seg(p, 0.76, 0.9), width=LW * s, seed=seed + 4, smooth=False)
    # 上衣
    body = [(x - 34 * s, hy + hr + 14 * s), (x + 34 * s, hy + hr + 14 * s), (x + 48 * s, y - 88 * s),
            (x - 48 * s, y - 88 * s)]
    c.shape(rounded_rect(x - 44 * s, hy + hr + 12 * s, 88 * s, (y - 88 * s) - (hy + hr + 12 * s), 22 * s),
            progress=seg(p, 0.25, 0.6), fill=shirt, width=LW * s, seed=seed + 5)
    _ = body
    if coat:
        # 白大褂：比上衣略宽，两条下摆；再挂一副听诊器
        c.shape(rounded_rect(x - 54 * s, hy + hr + 8 * s, 108 * s, (y - 80 * s) - (hy + hr + 8 * s), 26 * s),
                progress=seg(p, 0.32, 0.66), fill=WHITE, color=INK_SOFT, width=LW * 0.9 * s, seed=seed + 40)
        c.line(x, hy + hr + 16 * s, x, y - 84 * s, progress=seg(p, 0.48, 0.7), color=INK_SOFT, width=2.6 * s,
               seed=seed + 41, wobble=0.6)
        for sx in (-1, 1):
            c.stroke([(x + sx * 30 * s, hy + hr + 18 * s), (x + sx * 16 * s, hy + hr + 44 * s)],
                     progress=seg(p, 0.5, 0.68), color=INK_SOFT, width=2.8 * s, seed=seed + 42 + sx, smooth=False)
            c.shape(rounded_rect(x + sx * 44 * s - 18 * s, y - 190 * s, 36 * s, 30 * s, 7 * s),
                    progress=seg(p, 0.6, 0.78), color=INK_SOFT, fill=WHITE, width=2.4 * s, seed=seed + 44 + sx)
        # 听诊器：绕脖的弧 + 左胸的听头
        c.stroke([(x - 16 * s, hy + hr + 16 * s), (x - 30 * s, hy + hr + 70 * s), (x - 10 * s, hy + hr + 108 * s)],
                 progress=seg(p, 0.62, 0.82), color=TEAL, width=3.4 * s, seed=seed + 46, smooth=True)
        c.shape(ellipse_pts(x + 4 * s, hy + hr + 116 * s, 13 * s, 13 * s, n=20)[:-1], progress=seg(p, 0.76, 0.92),
                color=TEAL, fill=TEAL_FILL, width=3 * s, seed=seed + 47)
    # 手臂
    sy = hy + hr + 30 * s
    c.curve([(x - 42 * s, sy), (x - 66 * s, sy + 40 * s), (x - 62 * s, sy + 86 * s)], progress=seg(p, 0.5, 0.7),
            width=LW * s, seed=seed + 6)
    if wave:
        a = -60 + math.sin(wave * 6.283 * 1.6) * 22
        ex = x + 42 * s + 78 * s * math.cos(math.radians(a))
        ey = sy + 78 * s * math.sin(math.radians(a))
        c.curve([(x + 42 * s, sy), ((x + 42 * s + ex) / 2 + 10 * s, (sy + ey) / 2 + 6 * s), (ex, ey)],
                progress=seg(p, 0.55, 0.75), width=LW * s, seed=seed + 7)
    else:
        c.curve([(x + 42 * s, sy), (x + 66 * s, sy + 40 * s), (x + 62 * s, sy + 86 * s)], progress=seg(p, 0.55, 0.75),
                width=LW * s, seed=seed + 7)
    # 头
    c.shape(ellipse_pts(hx, hy, hr, hr * 1.02, n=40)[:-1], progress=seg(p, 0, 0.35), fill=(255, 236, 222),
            width=LW * s, seed=seed + 8)
    # 头发（一刀刘海）
    c.curve([(hx - hr * 0.95, hy - hr * 0.1), (hx - hr * 0.6, hy - hr * 0.95), (hx + hr * 0.2, hy - hr * 1.02),
             (hx + hr * 0.9, hy - hr * 0.5), (hx + hr * 0.98, hy - hr * 0.05)], progress=seg(p, 0.2, 0.45),
            color=hair, width=LW * 1.3 * s, seed=seed + 9)
    c.curve([(hx - hr * 0.5, hy - hr * 0.8), (hx - hr * 0.1, hy - hr * 0.45), (hx + hr * 0.45, hy - hr * 0.55)],
            progress=seg(p, 0.35, 0.5), color=hair, width=LW * s, seed=seed + 10)
    if p > 0.5:
        _face(c, hx, hy + 4 * s, hr, s, mood, seed + 20, alpha=seg(p, 0.5, 0.7))
        if sweat > 0:
            for i, (dx, dy) in enumerate([(1.25, -0.55), (1.45, -0.05)]):
                sp = seg(sweat, i * 0.35, i * 0.35 + 0.6)
                if sp > 0:
                    ddx, ddy = hx + hr * dx, hy + hr * dy + (1 - sp) * -10
                    c.shape([(ddx, ddy - 14 * s), (ddx + 8 * s, ddy + 2 * s), (ddx, ddy + 9 * s), (ddx - 8 * s, ddy + 2 * s)],
                            progress=sp, color=BLUE, fill=BLUE_FILL, width=3 * s, seed=seed + 30 + i)
    if label:
        c.text(label, (x, y + 40 * s), size=int(28 * s), color=INK_SOFT, progress=seg(p, 0.8, 1), weight="regular")


def _face(c: Canvas, x, y, r, s, mood, seed, alpha=1.0, color=INK) -> None:
    ey = y - r * 0.12
    for sx in (-1, 1):
        c.dot(x + sx * r * 0.36, ey, 4.2 * s, color, alpha=alpha)
    # 腮红
    for sx in (-1, 1):
        c.dot(x + sx * r * 0.6, y + r * 0.2, 7 * s, (255, 150, 150), alpha=0.35 * alpha)
    if mood == "smile":
        c.arc(x, y + r * 0.05, r * 0.32, 25, 130, width=3.4 * s, color=color, seed=seed, alpha=alpha, taper=True)
    elif mood == "worry":
        c.arc(x, y + r * 0.55, r * 0.26, 205, 130, width=3.4 * s, color=color, seed=seed, alpha=alpha)
        for sx in (-1, 1):  # 八字眉
            c.line(x + sx * r * 0.22, ey - r * 0.3, x + sx * r * 0.5, ey - r * 0.22, width=3 * s, color=color,
                   seed=seed + 1 + sx, alpha=alpha)
    elif mood == "happy":
        c.shape(ellipse_pts(x, y + r * 0.18, r * 0.26, r * 0.2, 0, 180, n=16), progress=1, color=color,
                fill=(240, 110, 110), width=3 * s, seed=seed)


# ---------------------------------------------------------------------- 助手机器人
def robot(c: Canvas, x, y, s=1.0, p=1.0, wave=0.0, blink=False, mood="smile", seed=200, bob=0.0, glow=0.0) -> None:
    """圆润的助手机器人（x, y 为脚底中心）。bob：上下浮动；glow：胸口指示灯亮度。"""
    y = y - bob
    # 影子
    c.fill(ellipse_pts(x, y + 8 * s + bob, 70 * s, 12 * s, n=30)[:-1], (0, 0, 0), progress=seg(p, 0.6, 1) * 0.08,
           offset=(0, 0))
    hw, hh = 132 * s, 104 * s
    hx, hy = x - hw / 2, y - 282 * s
    bw, bh = 118 * s, 104 * s
    by = hy + hh + 14 * s
    # 腿
    for i, sx in enumerate((-1, 1)):
        c.stroke([(x + sx * 28 * s, by + bh - 4 * s), (x + sx * 28 * s, y - 6 * s)], progress=seg(p, 0.7, 0.85),
                 color=BLUE, width=LW * s, seed=seed + 1 + i, smooth=False)
        c.shape(rounded_rect(x + sx * 28 * s - 17 * s, y - 12 * s, 34 * s, 14 * s, 7 * s), progress=seg(p, 0.8, 0.95),
                color=BLUE, fill=BLUE_FILL, width=3.2 * s, seed=seed + 3 + i)
    # 身体
    c.shape(rounded_rect(x - bw / 2, by, bw, bh, 34 * s), progress=seg(p, 0.35, 0.65), color=BLUE, fill=BLUE_FILL,
            width=LW * s, seed=seed + 5, shadow=True)
    # 胸口屏幕
    c.shape(rounded_rect(x - 32 * s, by + 26 * s, 64 * s, 40 * s, 12 * s), progress=seg(p, 0.55, 0.75), color=BLUE,
            fill=WHITE, width=3 * s, seed=seed + 6)
    if p > 0.75:
        pulse = 0.55 + 0.45 * glow
        for i in range(3):
            c.dot(x - 14 * s + i * 14 * s, by + 46 * s, 4.5 * s, BLUE, alpha=pulse if i == int(glow * 3) % 3 else 0.35)
    # 手臂
    ay = by + 30 * s
    c.curve([(x - bw / 2, ay), (x - bw / 2 - 30 * s, ay + 30 * s), (x - bw / 2 - 26 * s, ay + 66 * s)],
            progress=seg(p, 0.6, 0.78), color=BLUE, width=LW * s, seed=seed + 7)
    c.dot(x - bw / 2 - 26 * s, ay + 70 * s, 9 * s, BLUE, alpha=seg(p, 0.75, 0.85))
    if wave:
        a = -75 + math.sin(wave * 6.283 * 1.6) * 24
        ex, ey = x + bw / 2 + 70 * s * math.cos(math.radians(a)), ay + 70 * s * math.sin(math.radians(a))
    else:
        ex, ey = x + bw / 2 + 26 * s, ay + 66 * s
    c.curve([(x + bw / 2, ay), ((x + bw / 2 + ex) / 2 + 14 * s, (ay + ey) / 2 + 4 * s), (ex, ey)],
            progress=seg(p, 0.62, 0.8), color=BLUE, width=LW * s, seed=seed + 8)
    c.dot(ex, ey + 3 * s, 9 * s, BLUE, alpha=seg(p, 0.78, 0.88))
    # 头
    c.shape(rounded_rect(hx, hy, hw, hh, 40 * s), progress=seg(p, 0.05, 0.4), color=BLUE, fill=BLUE_FILL,
            width=LW * s, seed=seed + 9, shadow=True)
    # 耳朵
    for i, sx in enumerate((-1, 1)):
        c.shape(rounded_rect(x + sx * (hw / 2 + 4 * s) - 9 * s, hy + 34 * s, 18 * s, 36 * s, 9 * s),
                progress=seg(p, 0.3, 0.5), color=BLUE, fill=WHITE, width=3.2 * s, seed=seed + 10 + i)
    # 天线
    c.curve([(x, hy), (x + 4 * s, hy - 18 * s), (x, hy - 32 * s)], progress=seg(p, 0, 0.15), color=BLUE,
            width=LW * s, seed=seed + 12)
    c.shape(ellipse_pts(x, hy - 42 * s, 11 * s, 11 * s, n=20)[:-1], progress=seg(p, 0.1, 0.25), color=BLUE,
            fill=YELLOW_FILL if glow else WHITE, width=3.2 * s, seed=seed + 13)
    # 脸屏
    c.shape(rounded_rect(hx + 18 * s, hy + 18 * s, hw - 36 * s, hh - 36 * s, 24 * s), progress=seg(p, 0.3, 0.55),
            color=BLUE, fill=WHITE, width=3.2 * s, seed=seed + 14)
    if p > 0.55:
        al = seg(p, 0.55, 0.7)
        fy = hy + hh / 2 - 4 * s
        for sx in (-1, 1):
            if blink:
                c.arc(x + sx * 22 * s, fy + 4 * s, 9 * s, 200, 140, color=INK, width=3.4 * s, seed=seed + 20 + sx,
                      alpha=al)
            else:
                c.dot(x + sx * 22 * s, fy, 7.5 * s, INK, alpha=al)
                c.dot(x + sx * 22 * s + 2.5 * s, fy - 2.5 * s, 2.3 * s, WHITE, alpha=al)
        for sx in (-1, 1):
            c.dot(x + sx * 38 * s, fy + 14 * s, 7 * s, (255, 150, 150), alpha=0.4 * al)
        if mood == "smile":
            c.arc(x, fy + 6 * s, 12 * s, 30, 120, color=INK, width=3.2 * s, seed=seed + 23, alpha=al)
        else:
            c.line(x - 10 * s, fy + 18 * s, x + 10 * s, fy + 18 * s, color=INK, width=3.2 * s, seed=seed + 23, alpha=al)


# ---------------------------------------------------------------------- 物件
def phone(c: Canvas, x, y, w=320, h=600, p=1.0, seed=300) -> None:
    c.shape(rounded_rect(x, y, w, h, 46), progress=seg(p, 0, 0.5), fill=WHITE, width=LW, seed=seed, shadow=True)
    c.shape(rounded_rect(x + 18, y + 60, w - 36, h - 130, 18), progress=seg(p, 0.3, 0.6), fill=(242, 246, 255),
            color=INK_SOFT, width=2.6, seed=seed + 1)
    c.line(x + w / 2 - 36, y + 32, x + w / 2 + 36, y + 32, progress=seg(p, 0.5, 0.7), width=5, seed=seed + 2)
    c.shape(ellipse_pts(x + w / 2, y + h - 36, 15, 15, n=20)[:-1], progress=seg(p, 0.6, 0.8), width=3, seed=seed + 3)


def chat_bubble(c: Canvas, x, y, w, h, p=1.0, color=INK, fill=WHITE, tail="left", lines: int = 2, text: str = "",
                size: int = 30, seed=400, pop: bool = True, text_color=None) -> None:
    """对话气泡：弹出式出现（带回弹），然后线稿 → 填色 → 文字。"""
    if p <= 0:
        return
    sc = back_out(seg(p, 0, 0.45)) if pop else 1.0
    cx, cy = x + w / 2, y + h / 2
    w2, h2 = w * sc, h * sc
    x2, y2 = cx - w2 / 2, cy - h2 / 2
    c.shape(bubble_pts(x2, y2, w2, h2, tail), progress=seg(p, 0, 0.6), color=color, fill=fill, width=3.6, seed=seed,
            smooth=False, shadow=True, fill_delay=0.2)
    if text:
        c.text(text, (cx, cy), size=size, color=text_color or color, progress=seg(p, 0.45, 1), rise=6)
    else:
        for i in range(lines):
            ly = y + h * (i + 1) / (lines + 1)
            lw = (w - 64) * (0.92 if i < lines - 1 else 0.55)
            c.line(x + 32, ly, x + 32 + lw, ly, progress=seg(p, 0.5 + i * 0.15, 0.8 + i * 0.15), color=color,
                   width=3.4, seed=seed + 1 + i, alpha=0.75)


def server(c: Canvas, x, y, w=190, h=240, p=1.0, lights=(GREEN, GREEN, GREEN), seed=500, label="", fill=WHITE,
           blink_t: float = 0.0) -> None:
    c.shape(rounded_rect(x, y, w, h, 18), progress=seg(p, 0, 0.45), fill=fill, width=LW, seed=seed, shadow=True)
    for i in range(3):
        yy = y + 16 + i * (h - 32) / 3
        hh = (h - 32) / 3 - 12
        c.shape(rounded_rect(x + 14, yy, w - 28, hh, 10), progress=seg(p, 0.35 + i * 0.1, 0.6 + i * 0.1),
                color=INK_SOFT, fill=GRAY_FILL, width=2.6, seed=seed + 1 + i)
        c.line(x + 32, yy + hh / 2, x + w - 90, yy + hh / 2, progress=seg(p, 0.5 + i * 0.1, 0.7 + i * 0.1),
               color=INK_SOFT, width=3, seed=seed + 5 + i)
        if p > 0.75 and lights[i]:
            on = 1.0 if not blink_t else (0.45 + 0.55 * (math.sin(blink_t * 9 + i * 2) > 0))
            c.dot(x + w - 42, yy + hh / 2, 9, lights[i], alpha=on)
    if label:
        c.text(label, (x + w / 2, y + h + 38), size=28, color=INK_SOFT, progress=seg(p, 0.7, 1), weight="regular")


def clock(c: Canvas, cx, cy, r=78, p=1.0, t=0.0, color=INK, fill=WHITE, seed=600) -> None:
    c.shape(ellipse_pts(cx, cy, r, r, n=48)[:-1], progress=seg(p, 0, 0.5), color=color, fill=fill, width=LW, seed=seed,
            shadow=True)
    if p > 0.5:
        al = seg(p, 0.5, 0.8)
        for i in range(12):
            a = math.radians(i * 30)
            c.dot(cx + (r - 15) * math.cos(a), cy + (r - 15) * math.sin(a), 3.2 if i % 3 else 5, color, alpha=al)
        am = math.radians(-90 + t * 360)
        ah = math.radians(-90 + t * 30 + 60)
        c.line(cx, cy, cx + (r - 26) * math.cos(am), cy + (r - 26) * math.sin(am), color=color, width=4.2,
               wobble=0, seed=seed + 1, alpha=al)
        c.line(cx, cy, cx + (r - 44) * math.cos(ah), cy + (r - 44) * math.sin(ah), color=color, width=5.5,
               wobble=0, seed=seed + 2, alpha=al)
        c.dot(cx, cy, 7, color, alpha=al)


def database(c: Canvas, cx, y, w=230, h=200, p=1.0, seed=700, label="MySQL") -> None:
    rx, ry = w / 2, 28
    body = ellipse_pts(cx, y + h - ry, rx, ry, 0, 180, n=24) + ellipse_pts(cx, y + ry, rx, ry, 180, 180, n=24)
    c.shape(body, progress=seg(p, 0, 0.45), fill=PURPLE_FILL, color=PURPLE, width=LW, seed=seed, shadow=True,
            smooth=False)
    c.stroke(ellipse_pts(cx, y + ry, rx, ry, 0, 180, n=24), progress=seg(p, 0.35, 0.55), color=PURPLE, width=LW,
             seed=seed + 1, smooth=False)
    c.stroke(ellipse_pts(cx, y + h / 2, rx, ry, 0, 180, n=24), progress=seg(p, 0.5, 0.7), color=PURPLE, width=3.4,
             seed=seed + 2, smooth=False)
    if label:
        c.text(label, (cx, y + h / 2 + 32), size=30, color=PURPLE, progress=seg(p, 0.7, 1), weight="semibold")


def doctor_card(c: Canvas, x, y, w=330, h=190, p=1.0, seed=800) -> None:
    if p <= 0:
        return
    sc = back_out(seg(p, 0, 0.45))
    cx, cy = x + w / 2, y + h / 2
    w2, h2 = w * sc, h * sc
    x2, y2 = cx - w2 / 2, cy - h2 / 2
    c.shape(rounded_rect(x2, y2, w2, h2, 22), progress=seg(p, 0, 0.5), color=BLUE, fill=WHITE, width=LW, seed=seed,
            shadow=True, fill_delay=0.1)
    # 顶部色条
    c.fill(rounded_rect(x2 + 6, y2 + 6, w2 - 12, 44, 16), BLUE_FILL, progress=seg(p, 0.3, 0.6), offset=(0, 0))
    ax, ay = x + 70, y + h / 2 + 22
    c.shape(ellipse_pts(ax, ay, 40, 40, n=30)[:-1], progress=seg(p, 0.35, 0.6), color=BLUE, fill=RED_FILL, width=3.4,
            seed=seed + 1)
    c.line(ax, ay - 18, ax, ay + 18, progress=seg(p, 0.5, 0.65), color=RED, width=7, seed=seed + 2, wobble=0.3)
    c.line(ax - 18, ay, ax + 18, ay, progress=seg(p, 0.55, 0.7), color=RED, width=7, seed=seed + 3, wobble=0.3)
    c.text("找医生", (x + 210, y + 104), size=40, color=INK, progress=seg(p, 0.55, 0.8), weight="semibold")
    c.text("在线问诊 →", (x + 210, y + 152), size=24, color=BLUE, progress=seg(p, 0.7, 0.95), weight="regular")
    c.text("医生卡片", (x + w / 2, y + 28), size=22, color=BLUE, progress=seg(p, 0.4, 0.7), weight="medium")


def lightning(c: Canvas, x, y, s=1.0, p=1.0, seed=900) -> None:
    pts = [(x, y), (x - 44 * s, y + 96 * s), (x + 4 * s, y + 96 * s), (x - 34 * s, y + 200 * s), (x + 56 * s, y + 72 * s),
           (x + 8 * s, y + 72 * s), (x + 40 * s, y)]
    c.shape(pts, progress=p, color=ORANGE, fill=YELLOW_FILL, width=4.5, seed=seed, smooth=False, fill_delay=0.3)


def shield(c: Canvas, cx, cy, s=1.0, p=1.0, seed=950) -> None:
    pts = [(cx, cy - 118 * s), (cx + 100 * s, cy - 80 * s), (cx + 94 * s, cy + 14 * s), (cx + 54 * s, cy + 82 * s),
           (cx, cy + 120 * s), (cx - 54 * s, cy + 82 * s), (cx - 94 * s, cy + 14 * s), (cx - 100 * s, cy - 80 * s)]
    c.shape(pts, progress=seg(p, 0, 0.6), color=GREEN, fill=GREEN_FILL, width=5, seed=seed, shadow=True)
    c.stroke([(cx - 40 * s, cy + 2 * s), (cx - 10 * s, cy + 34 * s), (cx + 46 * s, cy - 30 * s)],
             progress=seg(p, 0.6, 1), color=GREEN, width=9 * s, seed=seed + 1, smooth=False)


def check(c: Canvas, cx, cy, s=1.0, p=1.0, color=GREEN, seed=980) -> None:
    if p <= 0:
        return
    sc = back_out(seg(p, 0, 0.5))
    c.shape(ellipse_pts(cx, cy, 20 * s * sc, 20 * s * sc, n=24)[:-1], progress=1, color=color, fill=color, width=2,
            seed=seed, fill_delay=0)
    c.stroke([(cx - 9 * s, cy), (cx - 2 * s, cy + 8 * s), (cx + 10 * s, cy - 7 * s)], progress=seg(p, 0.4, 1),
             color=WHITE, width=4 * s, seed=seed + 1, smooth=False, taper=False, wobble=0)


def node_box(c: Canvas, cx, cy, label: str, icon: str, p=1.0, lit: float = 0.0, seed=1000, w=236, h=150) -> None:
    """流程节点卡片：图标 + 文字；lit 时变蓝并打勾。"""
    on = lit > 0.3
    col = BLUE if on else INK
    fill = BLUE_FILL if on else WHITE
    lift = 8 * ease(seg(lit, 0.3, 0.8))
    y0 = cy - h / 2 - lift
    c.shape(rounded_rect(cx - w / 2, y0, w, h, 28), progress=seg(p, 0, 0.6), color=col, fill=fill, width=LW,
            seed=seed, shadow=True)
    icon_draw(c, icon, cx, y0 + 52, col, p=seg(p, 0.4, 0.8), seed=seed + 10)
    c.text(label, (cx, y0 + 112), size=30, color=col, progress=seg(p, 0.5, 1), weight="semibold")
    if on:
        check(c, cx + w / 2 - 8, y0 + 8, s=1.0, p=seg(lit, 0.3, 0.9), seed=seed + 5)


def icon_draw(c: Canvas, name: str, cx, cy, col, p=1.0, seed=0) -> None:
    """小图标（约 50px）。"""
    if name == "eye":      # 是否回复：眼睛
        c.stroke(ellipse_pts(cx, cy, 28, 14, 180, 180, n=16) + ellipse_pts(cx, cy, 28, 14, 0, 180, n=16)[1:],
                 progress=p, color=col, width=3.6, seed=seed, smooth=False)
        c.dot(cx, cy, 7, col, alpha=seg(p, 0.6, 1))
    elif name == "doc":    # 看懂报告：文档
        c.shape(rounded_rect(cx - 20, cy - 26, 40, 52, 6), progress=p, color=col, fill=WHITE, width=3.4, seed=seed)
        for i in range(3):
            c.line(cx - 10, cy - 12 + i * 12, cx + 10 - (i == 2) * 8, cy - 12 + i * 12, progress=seg(p, 0.5, 1),
                   color=col, width=3, seed=seed + 1 + i)
    elif name == "search":  # 查资料：放大镜
        c.shape(ellipse_pts(cx - 6, cy - 6, 17, 17, n=24)[:-1], progress=p, color=col, fill=WHITE, width=3.6, seed=seed)
        c.line(cx + 7, cy + 7, cx + 22, cy + 22, progress=seg(p, 0.5, 1), color=col, width=5, seed=seed + 1)
    elif name == "target":  # 识别意图：靶心
        c.stroke(ellipse_pts(cx, cy, 24, 24, n=28), progress=p, color=col, width=3.4, seed=seed, smooth=False)
        c.stroke(ellipse_pts(cx, cy, 12, 12, n=20), progress=seg(p, 0.3, 1), color=col, width=3.4, seed=seed + 1,
                 smooth=False)
        c.dot(cx, cy, 4, col, alpha=seg(p, 0.7, 1))
    elif name == "chat":   # 生成回复：气泡
        c.shape(bubble_pts(cx - 26, cy - 20, 52, 36, "left"), progress=p, color=col, fill=WHITE, width=3.4, seed=seed,
                smooth=False)
        for i in range(3):
            c.dot(cx - 12 + i * 12, cy - 2, 3.2, col, alpha=seg(p, 0.6, 1))


def sparkle(c: Canvas, cx, cy, r=14, p=1.0, color=ORANGE, seed=0) -> None:
    """闪光小星星（点缀用）。"""
    if p <= 0:
        return
    k = back_out(p)
    for a in (0, 90):
        rad = math.radians(a)
        c.line(cx - r * k * math.cos(rad), cy - r * k * math.sin(rad), cx + r * k * math.cos(rad),
               cy + r * k * math.sin(rad), color=color, width=3.6, seed=seed + a, taper=True, wobble=0)


def lock(c: Canvas, cx, cy, s=1.0, p=1.0, color=ORANGE, seed=0) -> None:
    c.arc(cx, cy - 12 * s, 17 * s, 180, 180, progress=seg(p, 0, 0.4), color=color, width=4 * s, seed=seed)
    c.shape(rounded_rect(cx - 26 * s, cy - 12 * s, 52 * s, 40 * s, 9 * s), progress=seg(p, 0.3, 0.9), color=color,
            fill=ORANGE_FILL, width=4 * s, seed=seed + 1)
    c.dot(cx, cy + 6 * s, 4.5 * s, color, alpha=seg(p, 0.8, 1))


def progress_bar(c: Canvas, x, y, w, h, value: float, p=1.0, color=GREEN, fill=GREEN_FILL, seed=0) -> None:
    c.shape(rounded_rect(x, y, w, h, h / 2), progress=seg(p, 0, 0.6), color=INK, fill=WHITE, width=LW, seed=seed,
            shadow=True)
    if value > 0.01 and p > 0.5:
        iw = (w - 16) * min(1.0, value)
        c.shape(rounded_rect(x + 8, y + 8, max(iw, h - 16), h - 16, (h - 16) / 2), progress=1, color=color, fill=color,
                width=2, seed=seed + 1, wobble=0.3, fill_delay=0)
        # 高光
        c.fill(rounded_rect(x + 16, y + 12, max(iw - 16, 8), (h - 16) / 3, (h - 16) / 6), (255, 255, 255),
               progress=0.35, offset=(0, 0))
    _ = fill


__all__ = ["person", "robot", "phone", "chat_bubble", "server", "clock", "database", "doctor_card", "lightning",
           "shield", "check", "node_box", "icon_draw", "sparkle", "lock", "progress_bar"]
_unused = (ORANGE_FILL, RED, PURPLE, ease)
