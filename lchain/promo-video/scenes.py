"""8 个分镜（v2）。每个场景函数签名：scene(c, t: 场景内秒数, d: 场景时长)。

旁白文案写成口语（像真人讲解），字幕按语音实际时间逐句显示（voice.py 生成 output/voice/subs.json）。
"""
from __future__ import annotations

import math

from props import (chat_bubble, check, clock, database, doctor_card, lightning, lock, node_box, person, phone,
                   progress_bar, robot, server, shield, sparkle)
from sketch import (BLUE, BLUE_FILL, GREEN, GREEN_FILL, INK, INK_SOFT, ORANGE, ORANGE_FILL, PURPLE, RED, RED_FILL,
                    W, WHITE, YELLOW_FILL, Canvas, back_out, ease, ease_in_out, lerp, seg)


def out_k(t: float, d: float, dur: float = 0.7) -> float:
    """场景结束前的退场系数 1→0（元素整体淡出）。"""
    return 1 - ease(seg(t, d - dur, d))


def title(c: Canvas, s: str, t: float, k: float, y: float = 118, color=INK, at: float = 0.2) -> None:
    c.text(s, (W / 2, y), size=58, color=color, progress=seg(t, at, at + 1.2), weight="semibold", alpha=k)


def decor(c: Canvas, seed: int, k: float, colors=(BLUE_FILL, YELLOW_FILL)) -> None:
    """背景柔和色块，让画面不单调。"""
    c.blob(260, 220, 220, 150, colors[0], alpha=0.45 * k, seed=seed)
    c.blob(1680, 880, 260, 170, colors[1], alpha=0.45 * k, seed=seed + 1)


# ---------------------------------------------------------------------- 1. 标题
def s1_title(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 1, 1)
    phone(c, 200, 230, p=seg(t, 0.2, 1.6))
    chat_bubble(c, 240, 330, 230, 84, p=seg(t, 1.2, 2.0), seed=11)
    chat_bubble(c, 280, 440, 210, 84, p=seg(t, 1.7, 2.5), tail="right", color=BLUE, fill=BLUE_FILL, seed=12)
    chat_bubble(c, 240, 550, 190, 76, p=seg(t, 2.2, 3.0), lines=1, seed=13)
    bob = math.sin(t * 2.2) * 6
    robot(c, 1560, 820, s=1.15, p=seg(t, 1.0, 2.8), wave=t if t > 2.8 else 0, blink=(t % 3.1) < 0.14, bob=bob,
          glow=(t * 0.8) % 1)
    c.text("LChain 企微群智能助手", (980, 440), size=86, weight="semibold", progress=seg(t, 2.4, 3.8))
    c.stroke([(700, 510), (1260, 510)], progress=ease(seg(t, 3.4, 4.3)), color=BLUE, width=7, smooth=False)
    c.text("让患者服务群里的每条消息，都有人接得住", (980, 590), size=38, color=INK_SOFT, weight="regular",
           progress=seg(t, 4.0, 5.4))
    for i, (x, y) in enumerate([(1360, 360), (1740, 470), (640, 380)]):
        sparkle(c, x, y, r=14, p=seg(t, 4.3 + i * 0.25, 4.9 + i * 0.25), color=(ORANGE, BLUE, GREEN)[i], seed=20 + i)
    c.alpha = 1


# ---------------------------------------------------------------------- 2. 痛点
def s2_pain(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 3, 1, (RED_FILL, YELLOW_FILL))
    title(c, "消息太多，根本回不过来", t, 1)
    cx = W / 2
    shake = math.sin(t * 30) * 2.5 * seg(t, 3.5, 4.5)
    person(c, cx + shake, 880, s=1.25, p=seg(t, 0, 1.2), shirt=ORANGE_FILL, mood="worry" if t > 2.5 else "smile",
           sweat=seg(t, 3.0, 5.0), label="病历经理", seed=20)
    spots = [(470, 300, "医生在吗？"), (1450, 290, "报告出来了"), (330, 520, "药能停吗"), (1600, 500, "怎么还没回？"),
             (500, 740, "发货了吗"), (1440, 730, "头有点晕"), (820, 250, "在吗在吗"), (1110, 240, "急！")]
    for i, (bx, by, txt) in enumerate(spots):
        urgent = txt in ("怎么还没回？", "急！", "药能停吗")
        bob = math.sin(t * 2 + i) * 4
        chat_bubble(c, bx - 115, by - 40 + bob, 230, 80, p=seg(t, 0.8 + i * 0.32, 1.4 + i * 0.32),
                    tail="left" if bx > cx else "right", color=RED if urgent else INK,
                    fill=RED_FILL if urgent else WHITE, text=txt, size=28, seed=30 + i)
    clock(c, 1700, 170, r=70, p=seg(t, 1.5, 2.4), t=t / 3)
    # 未读角标
    if t > 3.2:
        n = min(99, int(lerp(12, 99, ease(seg(t, 3.2, 6.5)))))
        c.shape([(1080, 800 - 30), (1150, 800 - 30), (1150, 800 + 30), (1080, 800 + 30)], progress=1, color=RED,
                fill=RED, width=2) if False else None
        c.pill(f"{n}+ 条未读", 1180, 860, size=30, color=RED, fill=RED_FILL, progress=seg(t, 3.2, 3.9))
    c.alpha = 1


# ---------------------------------------------------------------------- 3. 流程
STEPS = [("判断要不要回", "eye"), ("看懂报告", "doc"), ("查资料", "search"), ("识别意图", "target"), ("生成回复", "chat")]


def s3_flow(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 5, 1)
    title(c, "每条消息，都有一套完整的思考", t, 1)
    xs = [270 + i * 345 for i in range(5)]
    y = 540
    travel = seg(t, 3.0, 9.4)
    for i in range(4):
        c.arrow([(xs[i] + 128, y), (xs[i + 1] - 132, y)], progress=seg(t, 1.0 + i * 0.3, 1.5 + i * 0.3),
                color=BLUE if travel * 4.4 > i + 0.6 else INK_SOFT, width=3.6, seed=60 + i)
    for i, (x, (label, icon)) in enumerate(zip(xs, STEPS)):
        lit = seg(travel * 4.4, i, i + 0.4)
        node_box(c, x, y, label, icon, p=seg(t, 0.4 + i * 0.25, 1.2 + i * 0.25), lit=lit, seed=70 + i * 10)
    if 3.0 < t:
        pos = ease_in_out(travel)
        mx = lerp(xs[0], xs[-1], pos)
        hop = -abs(math.sin(pos * math.pi * 4)) * 26
        chat_bubble(c, mx - 60, y - 230 + hop, 120, 66, p=seg(t, 3.0, 3.5), color=BLUE, fill=BLUE_FILL, lines=1,
                    seed=90)
    # 底部：机器人在读
    robot(c, 1700, 950, s=0.55, p=seg(t, 1.8, 3.0), bob=math.sin(t * 2.4) * 4, blink=(t % 2.7) < 0.14,
          glow=travel)
    if travel >= 1:
        c.pill("回复已送达", xs[-1], y + 150, size=28, color=GREEN, fill=GREEN_FILL, progress=seg(t, 9.5, 10.2))
    c.alpha = 1


# ---------------------------------------------------------------------- 4. 分支
def s4_branch(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 7, 1, (GREEN_FILL, RED_FILL))
    title(c, "看情况，给最合适的回应", t, 1)
    ox, oy = 330, 580
    robot(c, ox, oy + 230, s=0.9, p=seg(t, 0, 1.0), bob=math.sin(t * 2.2) * 5, blink=(t % 3) < 0.14)
    rows = [(GREEN, GREEN_FILL, "普通问题", "直接回答", 300), (BLUE, BLUE_FILL, "复杂问题", "推送医生卡片", 560),
            (RED, RED_FILL, "紧急情况", "提醒就医 · 通知病历经理", 820)]
    for i, (col, fl, a, b, by) in enumerate(rows):
        st = 1.0 + i * 2.8
        c.curve([(ox + 110, oy + 60), (ox + 240, oy + 60), (ox + 330, (oy + 60 + by) / 2), (ox + 420, by)],
                progress=seg(t, st, st + 0.8), color=col, width=4.5, seed=220 + i)
        c.pill(a, ox + 530, by, size=32, color=col, fill=fl, progress=seg(t, st + 0.5, st + 1.2), weight="semibold")
        c.text(b, (ox + 640, by), size=32, color=col, anchor="lm", progress=seg(t, st + 0.9, st + 1.8), weight="regular")
    chat_bubble(c, 1440, 230, 360, 110, p=seg(t, 2.0, 2.8), color=GREEN, fill=GREEN_FILL, tail="left",
                text="空腹血糖 7 以下为宜", size=30, text_color=INK, seed=240)
    doctor_card(c, 1455, 470, p=seg(t, 4.8, 6.0), seed=250)
    if t > 7.6:
        pulse = 1 + 0.07 * math.sin((t - 7.6) * 7)
        c.shape([(1545 + 70 * pulse * math.cos(a / 30 * 6.283), 830 + 70 * pulse * math.sin(a / 30 * 6.283))
                 for a in range(30)], progress=seg(t, 7.6, 8.3), color=RED, fill=RED_FILL, width=4.5, seed=260)
        c.text("120", (1545, 830), size=56, color=RED, progress=seg(t, 7.9, 8.4), weight="semibold")
        c.pill("@病历经理", 1745, 830, size=30, color=RED, fill=WHITE, progress=seg(t, 8.4, 9.2))
    c.alpha = 1


# ---------------------------------------------------------------------- 5. 像真人
def s5_human(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 9, 1)
    title(c, "聊起天来，像真人一样", t, 1)
    # 分隔
    c.stroke([(W / 2, 250), (W / 2, 900)], progress=seg(t, 0.3, 1.2), color=INK_SOFT, width=2.4, smooth=False,
             alpha=0.6)
    c.pill("连发消息 · 合并回复", 480, 250, size=28, color=BLUE, progress=seg(t, 0.6, 1.4))
    c.pill("人工接手 · 自动让开", 1440, 250, size=28, color=ORANGE, progress=seg(t, 5.2, 6.0))
    # 左
    person(c, 190, 900, s=0.95, p=seg(t, 0.2, 1.2), shirt=GREEN_FILL, seed=300, bob=math.sin(t * 2) * 2)
    merge = ease_in_out(seg(t, 3.2, 4.2))
    texts = ["医生你好", "空腹 8.9", "要紧吗？"]
    for i, s in enumerate(texts):
        y0 = 350 + i * 118
        y = lerp(y0, 468, merge)
        if merge < 0.97:
            chat_bubble(c, 300, y, 230, 80, p=seg(t, 0.8 + i * 0.7, 1.5 + i * 0.7), text=s, size=30, seed=310 + i,
                        tail="left")
    if merge > 0.9:
        chat_bubble(c, 290, 440, 250, 96, p=seg(t, 4.0, 4.4), text="3 条 → 1 次", size=32, color=BLUE,
                    fill=BLUE_FILL, seed=320, pop=True)
        c.arrow([(560, 488), (640, 488)], progress=seg(t, 4.3, 4.8), color=BLUE, seed=321)
        chat_bubble(c, 660, 430, 250, 116, p=seg(t, 4.6, 5.4), text="一起回复你", size=32, color=GREEN,
                    fill=GREEN_FILL, tail="right", text_color=INK, seed=322)
        robot(c, 800, 900, s=0.62, p=seg(t, 4.4, 5.4), bob=math.sin(t * 2.4) * 4, blink=(t % 2.6) < 0.14, seed=325)
    # 右
    rx = 1440
    aside = ease_in_out(seg(t, 7.2, 8.4))
    person(c, rx - 140, 900, s=0.95, p=seg(t, 5.4, 6.4), shirt=ORANGE_FILL, label="病历经理", seed=330, wave=(t - 6.6)
           if 6.6 < t < 8 else 0)
    chat_bubble(c, rx - 290, 380, 280, 96, p=seg(t, 6.3, 7.1), text="我来处理～", size=32, color=ORANGE,
                fill=ORANGE_FILL, text_color=INK, seed=340)
    robot(c, rx + 130 + 200 * aside, 900, s=0.72, p=seg(t, 5.6, 6.6), blink=aside > 0.4, seed=350,
          bob=math.sin(t * 2.2) * 4)
    if aside > 0.6:
        c.text("好的，交给你～", (rx + 330, 520), size=28, color=BLUE, progress=seg(t, 8.2, 9.0), weight="regular")
    c.alpha = 1


# ---------------------------------------------------------------------- 6. 可靠
def s6_reliable(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 11, 1, (YELLOW_FILL, GREEN_FILL))
    title(c, "服务重启？也不怕", t, 1)
    sx, sy = 330, 380
    down = 2.0 < t < 4.6
    tilt = ease(seg(t, 1.8, 2.2)) - ease(seg(t, 4.2, 4.8))
    server(c, sx + 14 * tilt, sy + 20 * tilt, p=seg(t, 0, 1.0), lights=(RED, RED, RED) if down else (GREEN, GREEN, GREEN),
           fill=RED_FILL if down else WHITE, seed=400, blink_t=t if down else 0, label="服务节点")
    if 1.2 < t < 2.6:
        lightning(c, sx + 120, sy - 250, s=1.0, p=seg(t, 1.2, 1.6), seed=410)
    if down:
        c.pill("重启中…", sx + 95, sy + 330, size=28, color=RED, fill=RED_FILL, progress=seg(t, 2.2, 2.8))
    else:
        if t > 4.8:
            c.pill("已恢复", sx + 95, sy + 330, size=28, color=GREEN, fill=GREEN_FILL, progress=seg(t, 4.8, 5.4))
    # 进度条
    bx, by, bw = 760, 500, 620
    val = lerp(0, 0.55, ease(seg(t, 0.6, 1.8))) + lerp(0, 0.45, ease(seg(t, 4.8, 6.6)))
    progress_bar(c, bx, by, bw, 64, val, p=seg(t, 0.4, 1.2), seed=420)
    bpx = bx + 8 + (bw - 16) * 0.55
    if t > 2.0:
        c.stroke([(bpx, by - 26), (bpx, by + 90)], progress=seg(t, 2.0, 2.4), color=ORANGE, width=3.6, smooth=False)
        c.pill("断点", bpx, by - 58, size=26, color=ORANGE, fill=ORANGE_FILL, progress=seg(t, 2.1, 2.8))
    if t > 5.0:
        c.text("从断点接着跑", (bx + bw / 2, by + 150), size=34, color=GREEN, progress=seg(t, 5.0, 6.0))
    shield(c, 1620, 500, s=1.25, p=seg(t, 6.6, 8.0), seed=440)
    c.pill("不丢", 1560, 720, size=32, color=GREEN, fill=GREEN_FILL, progress=seg(t, 7.8, 8.4), weight="semibold")
    c.pill("不重", 1690, 720, size=32, color=GREEN, fill=GREEN_FILL, progress=seg(t, 8.1, 8.7), weight="semibold")
    for i, (x, y) in enumerate([(1470, 340), (1780, 380), (1760, 640)]):
        sparkle(c, x, y, r=13, p=seg(t, 8.2 + i * 0.2, 8.8 + i * 0.2), color=GREEN, seed=450 + i)
    c.alpha = 1


# ---------------------------------------------------------------------- 7. 部署
def s7_deploy(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 13, 1, (BLUE_FILL, ORANGE_FILL))
    title(c, "多节点部署，定时任务不重复", t, 1)
    xs = [330, 620, 910]
    for i, x in enumerate(xs):
        server(c, x - 95, 250, p=seg(t, 0.3 + i * 0.35, 1.3 + i * 0.35), label=f"Web 节点 {i + 1}", seed=500 + i * 10,
               blink_t=t * 0.6 + i)
    c.pill("可以部署很多个", 620, 600, size=26, color=BLUE, progress=seg(t, 1.6, 2.3))
    # 右侧：定时任务（时钟 + 锁）
    clock(c, 1500, 350, r=100, p=seg(t, 1.8, 2.8), t=t / 2, color=ORANGE, fill=ORANGE_FILL, seed=540)
    lock(c, 1610, 262, s=1.1, p=seg(t, 5.4, 6.3), seed=545)
    c.pill("定时任务 × 1", 1500, 500, size=28, color=ORANGE, fill=WHITE, progress=seg(t, 2.6, 3.3))
    c.pill("只跑一份，不会重复执行", 1500, 570, size=26, color=ORANGE, fill=ORANGE_FILL,
           progress=seg(t, 6.0, 6.8))
    database(c, 1000, 700, w=250, h=200, p=seg(t, 3.0, 4.2), seed=550)
    # 连线 + 流动的数据点
    links = [((x, 650), (930 + i * 70, 722)) for i, x in enumerate(xs)] + [((1400, 620), (1110, 740))]
    for i, (a, b) in enumerate(links):
        c.curve([a, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2 + 30), b], progress=seg(t, 3.8 + i * 0.2, 4.6 + i * 0.2),
                color=INK_SOFT, width=3, seed=560 + i, alpha=0.7)
        if t > 4.8:
            u = ((t - 4.8) * 0.5 + i * 0.27) % 1
            px = lerp(a[0], b[0], u)
            py = lerp(a[1], b[1], u) + 30 * math.sin(u * math.pi)
            c.dot(px, py, 7, ORANGE if i == 3 else BLUE, alpha=0.9)
    c.alpha = 1


# ---------------------------------------------------------------------- 8. 结尾
def s8_end(c: Canvas, t: float, d: float) -> None:
    decor(c, 15, 1)
    bob = math.sin(t * 2.2) * 6
    robot(c, 960, 690, s=1.25, p=seg(t, 0, 1.4), wave=t if t > 1.4 else 0, blink=(t % 2.9) < 0.14, bob=bob,
          glow=(t * 0.8) % 1)
    for i, (bx, by, col, fl) in enumerate([(560, 250, INK, WHITE), (1220, 230, BLUE, BLUE_FILL),
                                           (420, 500, BLUE, BLUE_FILL), (1360, 480, GREEN, GREEN_FILL)]):
        chat_bubble(c, bx, by + math.sin(t * 1.8 + i) * 8, 150, 66, p=seg(t, 0.8 + i * 0.25, 1.5 + i * 0.25),
                    lines=1, color=col, fill=fl, tail="left" if bx > 960 else "right", seed=820 + i)
    c.text("让每一条消息，都被好好回应", (960, 800), size=66, weight="semibold", progress=seg(t, 1.4, 3.2))
    c.stroke([(lerp(960, 640, ease(seg(t, 3.0, 4.0))), 868), (lerp(960, 1280, ease(seg(t, 3.0, 4.0))), 868)],
             color=BLUE, width=7, smooth=False)
    c.text("LChain  ·  LangGraph + FastAPI + Langfuse", (960, 940), size=30, color=INK_SOFT, weight="regular",
           progress=seg(t, 3.8, 5.0))
    for i, (x, y) in enumerate([(700, 380), (1250, 400), (860, 170), (1080, 180)]):
        sparkle(c, x, y, r=13, p=seg(t, 2.0 + i * 0.3, 2.6 + i * 0.3), color=(ORANGE, BLUE, GREEN, ORANGE)[i],
                seed=840 + i)


# ---------------------------------------------------------------------- 时间线
# (场景函数, 时长秒, 旁白)。旁白写成口语，读起来像真人讲解。
SCENES = [
    (s1_title, 7.5, "这是 LChain，一个住在患者服务群里的 AI 助手。"),
    (s2_pain, 9.0, "群里的消息一多，病历经理根本回不过来，患者就只能干等着。"),
    (s3_flow, 13.0, "现在，每一条消息，它都会先判断要不要回，看懂报告，查好资料，想清楚对方的意图，再认真回复。"),
    (s4_branch, 13.5, "小问题，直接帮你解答；复杂一点的，推荐合适的医生；要是情况紧急，马上提醒就医，同时通知病历经理。"),
    (s5_human, 10.0, "你连着发好几条，它会等你说完，一起回复；病历经理一接手，它就自觉地让开。"),
    (s6_reliable, 12.0, "就算服务突然重启，任务也会从断点接着跑，已经发出去的消息，绝不会重复发送。"),
    (s7_deploy, 10.0, "支持多个节点一起扛流量，定时任务只跑一份，稳稳当当。"),
    (s8_end, 8.0, "LChain，让每一条消息，都被好好回应。"),
]
NO_SUBTITLE = {s8_end}
_ = (PURPLE, back_out, check, WHITE)
