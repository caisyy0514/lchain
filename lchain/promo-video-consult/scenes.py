"""12 个分镜。每个场景函数签名：scene(c, t: 场景内秒数, d: 场景时长)。

讲的是 LChain 的「问诊智能体」（app/consult）：既讲产品（患者看到什么），
也讲框架（同事要复用的那套：图决定流程 / 判断提示词 / 代码闸门 / 用例库 + 跑批）。
旁白写成口语，字幕按语音实际时间逐句显示（voice.py 生成 output/voice/subs.json）。
"""
from __future__ import annotations

import math

from cprops import (bar_compare, check_row, code_card, cross_mark, file_card, folder_tree, gate, note_card,
                    record_card, scissors, stamp, tabs, ui_shot)
from props import (chat_bubble, clock, database, lightning, lock, node_box, person, phone, progress_bar, robot,
                   server, shield, sparkle)
from sketch import (BLUE, BLUE_FILL, GREEN, GREEN_FILL, INK, INK_SOFT, ORANGE, ORANGE_FILL, PURPLE, PURPLE_FILL,
                    RED, RED_FILL, TEAL, TEAL_FILL, W, WHITE, YELLOW_FILL, Canvas, back_out, ease, ease_in_out,
                    lerp, seg)

# ---------------------------------------------------------------------- 公共小工具


def out_k(t: float, d: float, dur: float = 0.7) -> float:
    """场景结束前的退场系数 1→0（元素整体淡出）。"""
    return 1 - ease(seg(t, d - dur, d))


def title(c: Canvas, s: str, t: float, k: float, y: float = 118, color=INK, size: int = 56, at: float = 0.2) -> None:
    c.text(s, (W / 2, y), size=size, color=color, progress=seg(t, at, at + 1.2), weight="semibold", alpha=k)


def decor(c: Canvas, seed: int, k: float, colors=(TEAL_FILL, YELLOW_FILL)) -> None:
    """背景柔和色块，让画面不单调。"""
    c.blob(268, 208, 230, 150, colors[0], alpha=0.45 * k, seed=seed)
    c.blob(1676, 892, 258, 168, colors[1], alpha=0.45 * k, seed=seed + 1)


def caption(c: Canvas, s: str, cx, cy, p: float, color=INK_SOFT, size: int = 28) -> None:
    c.text(s, (cx, cy), size=size, color=color, progress=p, weight="regular")


def bad_card(c: Canvas, cx, cy, w, text: str, p: float, seed: int) -> None:
    """一张写着「不行」的红卡片（痛点场景用）。"""
    note_card(c, cx, cy, w, 138, [], p=p, color=RED, fill=RED_FILL, title=text, title_size=31, seed=seed)
    cross_mark(c, cx - w / 2 + 40, cy, s=0.62, p=seg(p, 0.5, 0.95))
    _ = back_out


# ---------------------------------------------------------------------- 1. 标题
def s1_title(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 1, 1)
    phone(c, 176, 236, w=310, h=580, p=seg(t, 0.2, 1.5))
    chat_bubble(c, 232, 330, 226, 76, p=seg(t, 1.1, 1.9), text="我上腹疼 3 天", size=27, seed=11)
    chat_bubble(c, 232, 434, 226, 76, p=seg(t, 1.6, 2.4), color=TEAL, fill=TEAL_FILL, tail="left",
                text="有黑便吗？", size=27, seed=12)
    chat_bubble(c, 232, 538, 226, 76, p=seg(t, 2.1, 2.9), text="没有", size=27, seed=13)
    # 医生小人
    bob = math.sin(t * 2.2) * 6
    person(c, 1660, 1000, s=1.15, p=seg(t, 1.0, 2.6), shirt=(226, 232, 236), coat=True, bob=bob,
           wave=t if t > 2.6 else 0, label="AI 医生", seed=20)
    c.text("LChain 问诊智能体", (1145, 430), size=80, weight="semibold", progress=seg(t, 2.4, 3.9))
    c.stroke([(lerp(1145, 775, ease(seg(t, 3.4, 4.3))), 505), (lerp(1145, 1515, ease(seg(t, 3.4, 4.3))), 505)],
             progress=1, color=TEAL, width=7, smooth=False)
    c.text("患者发一条消息，它就回一条", (1145, 580), size=38, color=INK_SOFT, weight="regular",
           progress=seg(t, 3.8, 5.2))
    c.pill("场景入口：顶部导航「问诊」", 1145, 690, size=28, color=TEAL, progress=seg(t, 4.6, 5.6))
    for i, (x, y) in enumerate([(700, 320), (1520, 330), (860, 700)]):
        sparkle(c, x, y, r=14, p=seg(t, 4.4 + i * 0.25, 5.0 + i * 0.25), color=(ORANGE, TEAL, BLUE)[i], seed=30 + i)
    c.alpha = 1


# ---------------------------------------------------------------------- 2. 痛点
def s2_pain(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 3, 1, (RED_FILL, YELLOW_FILL))
    title(c, "只让模型自由发挥，问诊会走歪", t, 1)
    bob = math.sin(t * 2.4) * 5
    robot(c, 960, 950, s=1.0, p=seg(t, 0, 1.1), bob=bob, blink=(t % 3.0) < 0.14, mood="flat")
    spots = [(430, 300, "关键病史没问到"), (1330, 288, "还没问清就下结论"),
             (470, 620, "回复一大段没重点"), (1370, 604, "免责声明忘了写")]
    for i, (cx, cy, txt) in enumerate(spots):
        bob2 = math.sin(t * 2 + i) * 5
        bad_card(c, cx, cy + bob2, 560, txt, seg(t, 1.0 + i * 0.55, 1.9 + i * 0.55), seed=40 + i * 10)
    if t > 4.4:
        for i, (x, y) in enumerate([(700, 200), (1180, 200), (330, 420), (1620, 420)]):
            sparkle(c, x, y, r=11, p=seg(t, 4.4 + i * 0.2, 4.9 + i * 0.2), color=RED, seed=90 + i)
        c.pill("患者的等待，变成了不确定", 960, 470, size=30, color=RED, fill=WHITE, progress=seg(t, 4.6, 5.6))
    c.alpha = 1


# ---------------------------------------------------------------------- 3. 固定路径
PATH = [("现病史采集", "doc"), ("分诊", "target"), ("鉴别问询", "search"), ("诊断意见", "chat"),
        ("变化随访", "eye"), ("更新诊断", "chat")]


def s3_path(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 5, 1)
    title(c, "问诊是一条固定的路径", t, 1)
    xs = [240 + i * 288 for i in range(6)]
    y = 400
    travel = seg(t, 3.2, 9.0)
    for i in range(5):
        c.arrow([(xs[i] + 122, y), (xs[i + 1] - 126, y)], progress=seg(t, 1.0 + i * 0.3, 1.5 + i * 0.3),
                color=TEAL if travel * 5.2 > i + 0.6 else INK_SOFT, width=3.6, seed=60 + i)
    for i, (x, (label, icon)) in enumerate(zip(xs, PATH)):
        lit = seg(travel * 5.2, i, i + 0.4)
        node_box(c, x, y, label, icon, p=seg(t, 0.4 + i * 0.26, 1.2 + i * 0.26), lit=lit, seed=70 + i * 10, w=232)
    if t > 3.2:
        pos = ease_in_out(travel)
        mx = lerp(xs[0], xs[-1], pos)
        hop = -abs(math.sin(pos * math.pi * 5)) * 24
        chat_bubble(c, mx - 58, y - 190 + hop, 116, 62, p=seg(t, 3.2, 3.6), color=TEAL, fill=TEAL_FILL, lines=1,
                    seed=90)
    if travel >= 1:
        c.pill("每一轮只走一步，不会跳步", 960, 596, size=28, color=TEAL, fill=WHITE, progress=seg(t, 9.2, 10.0))
    ui_shot(c, 960, 806, 1180, "ui-progress", p=seg(t, 10.4, 11.6), seed=95)
    c.alpha = 1


# ---------------------------------------------------------------------- 4. 十项要素
ELEMENTS = ["起病时间与方式", "症状部位范围", "症状性质", "严重程度", "病程演变",
            "伴随症状", "诱因与缓解", "既往史", "目前用药", "过敏史"]


def s4_intake(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 7, 1)
    title(c, "现病史：十项要素，缺哪项问哪项", t, 1)
    record_card(c, 620, 610, 900, 530, ELEMENTS, p=seg(t, 0.3, 4.5), done=8, seed=1400)
    # 右栏：轮次 / 每轮问题数 / 不纠缠
    c.pill("现病史 第 2 轮", 1480, 330, size=30, color=TEAL, progress=seg(t, 1.4, 2.4))
    c.pill("每轮最多 2 个问题", 1480, 430, size=30, color=ORANGE, fill=ORANGE_FILL, progress=seg(t, 2.0, 3.0))
    note_card(c, 1480, 610, 620, 180, ["要素齐 + 轮次够 → 才能分诊"],
              p=seg(t, 2.8, 3.8), color=TEAL, fill=WHITE, title="轮次门槛", title_size=30, seed=1500)
    note_card(c, 1480, 830, 620, 240, ["同一项连问 3 次还没答", "就记「未明确」，继续往前走"],
              p=seg(t, 4.4, 5.6), color=ORANGE, fill=ORANGE_FILL, title="不纠缠", title_size=30, seed=1510)
    c.alpha = 1


# ---------------------------------------------------------------------- 5. 两个判断并发
def s5_parallel(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 9, 1, (BLUE_FILL, TEAL_FILL))
    title(c, "两个判断，同时做", t, 1)
    bx, by = 190, 480
    chat_bubble(c, bx - 90, by - 38, 180, 76, p=seg(t, 0.3, 1.1), color=TEAL, fill=TEAL_FILL, lines=1, seed=200)
    caption(c, "患者的新消息", bx, by + 74, seg(t, 0.9, 1.5), size=26)
    # 分叉
    c.curve([(bx + 92, by), (300, by), (390, 330), (432, 330)], progress=seg(t, 1.3, 2.1),
            color=BLUE, width=4.4, seed=210)
    c.curve([(bx + 92, by), (300, by), (390, 680), (432, 680)], progress=seg(t, 1.5, 2.3),
            color=BLUE, width=4.4, seed=211)
    node_box(c, 600, 330, "患者说了什么", "doc", p=seg(t, 2.0, 2.9), lit=seg(t, 2.6, 3.2), seed=220, w=330, h=150)
    node_box(c, 600, 680, "这一轮做什么", "target", p=seg(t, 2.2, 3.1), lit=seg(t, 2.8, 3.4), seed=230, w=330, h=150)
    c.pill("extract", 600, 235, size=26, color=BLUE, fill=WHITE, progress=seg(t, 2.2, 3.0))
    c.pill("assess", 600, 775, size=26, color=BLUE, fill=WHITE, progress=seg(t, 2.4, 3.2))
    c.pill("并发", 600, 505, size=28, color=ORANGE, fill=ORANGE_FILL, progress=seg(t, 3.2, 4.0))
    # 汇合
    c.curve([(765, 330), (815, 330), (830, 500), (847, 500)], progress=seg(t, 4.0, 4.7), color=BLUE, width=4.4,
            seed=240)
    c.curve([(765, 680), (815, 680), (830, 500), (847, 500)], progress=seg(t, 4.2, 4.9), color=BLUE, width=4.4,
            seed=241)
    node_box(c, 1000, 500, "合并 + 钳制", "target", p=seg(t, 4.8, 5.7), lit=seg(t, 5.4, 6.0), seed=250, w=300, h=150)
    c.pill("decide", 1000, 405, size=26, color=TEAL, fill=WHITE, progress=seg(t, 5.0, 5.8))
    c.pill("两处偏差由钳制补回", 1000, 660, size=27, color=TEAL, fill=TEAL_FILL, progress=seg(t, 6.2, 7.2))
    ui_shot(c, 1480, 560, 620, "ui-steps", p=seg(t, 8.0, 9.2), seed=260)
    bar_compare(c, 170, 856, 620, "3.99s 单轮", "2.62s 单轮", p=seg(t, 9.0, 10.6), seed=270)
    c.alpha = 1


# ---------------------------------------------------------------------- 6. 闸门
GATE_RULES = [("要素不全", "不能分诊"), ("鉴别不足 2 轮", "不能下诊断"), ("还没给诊断", "不能结束")]


def s6_gate(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 11, 1, (YELLOW_FILL, GREEN_FILL))
    title(c, "模型只提建议，闸门在代码里", t, 1)
    robot(c, 190, 880, s=0.72, p=seg(t, 0, 1.0), bob=math.sin(t * 2.2) * 4, blink=(t % 2.8) < 0.14, seed=300)
    caption(c, "模型给出 action", 190, 936, seg(t, 0.9, 1.5), size=26)
    note_card(c, 380, 560, 250, 130, [], p=seg(t, 1.2, 2.0), color=INK, fill=WHITE, title="diagnose",
              title_size=32, seed=310)
    gate(c, 700, 540, 330, 440, p=seg(t, 2.2, 3.2), blocked=seg(t, 3.6, 4.6), seed=320)
    note_card(c, 1010, 560, 260, 130, [], p=seg(t, 5.0, 5.8), color=TEAL, fill=TEAL_FILL, title="ask_differential",
              title_size=28, seed=330)
    c.arrow([(510, 560), (536, 560)], progress=seg(t, 4.6, 5.0), color=RED, width=4, seed=340)
    c.arrow([(866, 560), (884, 560)], progress=seg(t, 5.4, 5.8), color=TEAL, width=4, seed=341)
    for i, (a, b) in enumerate(GATE_RULES):
        cy = 320 + i * 170
        note_card(c, 1520, cy, 690, 138, [f"{a} → {b}"], p=seg(t, 6.0 + i * 0.5, 6.9 + i * 0.5), color=RED,
                  fill=RED_FILL, size=30, seed=350 + i * 10)
    c.pill("白名单 + 阶段钳制 clamp_action()", 1520, 830, size=30, color=TEAL, fill=WHITE, progress=seg(t, 8.2, 9.2))
    c.pill("模型说错，流程也走不歪", 1520, 910, size=30, color=TEAL, progress=seg(t, 8.8, 9.8))
    c.alpha = 1


# ---------------------------------------------------------------------- 7. 安全边界
def s7_safety(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 15, 1, (GREEN_FILL, TEAL_FILL))
    title(c, "分诊和诊断的文字，由代码拼装", t, 1)
    code_card(c, 400, 270, 560, 210, ["render_triage(triage)", "render_diagnosis(entry)"], p=seg(t, 0.3, 1.5),
              seed=500)
    for i, txt in enumerate(["免责声明每次都带", "紧急情况强制提示就医"]):
        c.pill(txt, 400, 470 + i * 96, size=29, color=GREEN, fill=GREEN_FILL, progress=seg(t, 1.6 + i * 0.6,
                                                                                          2.6 + i * 0.6))
    stamp(c, 400, 700, "不能替代面诊", p=seg(t, 3.4, 4.4), color=RED, size=27, seed=510)
    note_card(c, 400, 860, 620, 150, ["红旗征 → urgency 抬到 urgent"], p=seg(t, 4.6, 5.6), color=ORANGE,
              fill=ORANGE_FILL, size=28, seed=520)
    ui_shot(c, 1010, 640, 470, "ui-triage", p=seg(t, 5.4, 6.6), seed=530)
    ui_shot(c, 1540, 640, 470, "ui-diagnosis", p=seg(t, 6.2, 7.4), seed=540)
    c.alpha = 1


# ---------------------------------------------------------------------- 8. 状态与并发
def s8_state(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 17, 1, (PURPLE_FILL, YELLOW_FILL))
    title(c, "状态落盘，重启不丢；同会话串行", t, 1)
    file_card(c, 430, 560, 560, 330, "consult.json",
              ["stage: differential", "record_rounds: 3", "triage: 消化内科",
               "turns: 12 条对话记录（含 trace）"], p=seg(t, 0.3, 1.6), seed=600)
    if 2.2 < t < 3.4:
        lightning(c, 300, 180, s=0.75, p=seg(t, 2.2, 2.7), seed=610)
    c.pill("服务重启也不怕", 430, 800, size=30, color=PURPLE, fill=PURPLE_FILL, progress=seg(t, 3.2, 4.2))
    caption(c, "状态是文件的读-改-写，所以同一会话必须串行", 430, 880, seg(t, 4.2, 5.2), size=26)
    # 中间：锁 + 三个标签页
    lock(c, 940, 300, s=1.05, p=seg(t, 1.0, 2.0), color=ORANGE, seed=620)
    tabs(c, 700, 480, ["标签页 1", "标签页 2", "标签页 3"], p=seg(t, 1.6, 3.2), active=0, w=150, h=96, seed=630)
    c.pill("多标签页共用会话 → 排队", 940, 660, size=28, color=ORANGE, fill=ORANGE_FILL, progress=seg(t, 3.6, 4.6))
    c.pill("先发 queued 事件，不会像卡死", 940, 740, size=26, color=ORANGE, fill=WHITE, progress=seg(t, 4.2, 5.2))
    # 右侧：不同会话并发
    for i in range(50):
        col, row = i % 10, i // 10
        p = seg(t, 5.0 + i * 0.035, 5.5 + i * 0.035)
        if p > 0:
            c.dot(1310 + col * 56, 330 + row * 62, 12, TEAL, alpha=0.35 + 0.6 * ease(p))
    c.pill("50 个不同会话并发", 1580, 230, size=30, color=TEAL, progress=seg(t, 4.8, 5.8))
    progress_bar(c, 1300, 700, 560, 56, ease(seg(t, 6.4, 9.0)), p=seg(t, 6.2, 7.0), color=TEAL, fill=TEAL_FILL,
                 seed=640)
    c.text("50/50 成功 · p99 6.1s", (1580, 800), size=30, color=TEAL, progress=seg(t, 8.6, 9.6), weight="semibold")
    c.alpha = 1


# ---------------------------------------------------------------------- 9. Langfuse 追溯
def s9_trace(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 19, 1, (BLUE_FILL, TEAL_FILL))
    title(c, "每一步都能追溯：Langfuse Trace", t, 1)
    ui_shot(c, 530, 560, 780, "ui-trace", p=seg(t, 0.4, 1.8), seed=1000)
    ui_shot(c, 1070, 560, 225, "ui-graph", p=seg(t, 1.6, 3.0), seed=1010)
    c.text("LangGraph 的流程，在 Langfuse 里直接看", (960, 198), size=32, color=INK_SOFT,
           progress=seg(t, 2.8, 3.8), weight="regular")
    note_card(c, 1520, 380, 660, 320, ["每个节点一层，耗时看得见", "LLM 调用带 token 和输入输出"],
              p=seg(t, 3.4, 4.8), color=BLUE, fill=WHITE, title="一次问诊 = 一条 Trace", title_size=30, seed=1020)
    c.pill("提示词改完 → 发新版本", 1520, 640, size=28, color=ORANGE, fill=ORANGE_FILL, progress=seg(t, 5.4, 6.4))
    c.pill("跑批报告 → 闭环验证", 1520, 730, size=28, color=TEAL, fill=TEAL_FILL, progress=seg(t, 6.0, 7.0))
    caption(c, "改哪里、改完是不是变好，都能查", 1520, 850, seg(t, 7.0, 8.2), size=27)
    c.alpha = 1


# ---------------------------------------------------------------------- 10. 用例库 + 回归跑批
TREE = [(0, "消化内科", "dir"), (1, "01-上腹痛3天-急性胃炎.md", ""), (1, "08-腹泻呕吐1天-急性胃肠炎.md", ""),
        (0, "急诊科", "dir"), (1, "02-突发胸痛伴大汗-急性冠脉综合征.md", ""), (0, "儿科", "dir"),
        (1, "04-幼儿发热后出疹-幼儿急疹.md", "")]


def s10_eval(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 19, 1, (TEAL_FILL, ORANGE_FILL))
    title(c, "用例库：给 AI 服务做回归", t, 1)
    folder_tree(c, 450, 590, 660, 560, TREE, p=seg(t, 0.3, 3.4), seed=700)
    c.pill("科室目录 = 场景分类 · 10 个常见病例", 450, 920, size=27, color=TEAL, fill=TEAL_FILL,
           progress=seg(t, 3.4, 4.4))
    ui_shot(c, 1380, 360, 920, "ui-eval", p=seg(t, 4.4, 5.8), seed=710)
    cards = [("① 改提示词 / 流程", "改完立刻回归"), ("② 全套用例跑一遍", "桩测试 + 真实模型"),
             ("③ 报告 · 5 项判定", "通过率 + AI 分析")]
    for i, (head, line) in enumerate(cards):
        note_card(c, 1040 + i * 350, 740, 280, 190, [line], p=seg(t, 6.2 + i * 0.7, 7.3 + i * 0.7),
                  color=TEAL, fill=WHITE if i % 2 == 0 else TEAL_FILL, size=22, title=head, title_size=27,
                  align="center", seed=720 + i * 10)
        if i < 2:
            c.arrow([(1040 + i * 350 + 150, 740), (1040 + i * 350 + 190, 740)],
                    progress=seg(t, 7.0 + i * 0.7, 7.6 + i * 0.7), color=ORANGE, width=4, seed=740 + i)
    # 回归闭环：不达标就回到第一步
    c.arrow([(1740, 838), (1740, 892), (1040, 892), (1040, 848)], progress=seg(t, 8.6, 9.6), color=ORANGE,
            width=4.5, head=16, seed=750)
    caption(c, "不达标 → 定位原因 → 再改一轮", 1390, 936, seg(t, 9.6, 10.6), size=27)
    c.alpha = 1


# ---------------------------------------------------------------------- 11. 复用清单
REUSE = [("① 画流程图", ["NODES / BRANCHES", "条件边决定下一步"]),
         ("② 写判断提示词", ["JSON · temperature 0", "只回答「该做什么」"]),
         ("③ 加代码闸门", ["clamp_action", "limit_questions"]),
         ("④ 建用例库 + 跑批", ["eval/<业务>/<用例>.md", "报告 + AI 分析"])]


def s11_reuse(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 21, 1, (TEAL_FILL, YELLOW_FILL))
    title(c, "想用这套框架做别的业务？照抄四步", t, 1)
    xs = [280, 720, 1160, 1600]
    for i, (x, (tt, lines)) in enumerate(zip(xs, REUSE)):
        note_card(c, x, 580, 380, 320, lines, p=seg(t, 0.6 + i * 0.9, 1.7 + i * 0.9), color=TEAL,
                  fill=WHITE if i % 2 == 0 else TEAL_FILL, size=26, title=tt, title_size=32, align="center",
                  seed=800 + i * 10)
        if i < 3:
            c.arrow([(x + 200, 580), (x + 420, 580)], progress=seg(t, 1.4 + i * 0.9, 2.0 + i * 0.9), color=ORANGE,
                    width=4.5, seed=820 + i)
    c.pill("流程安全由代码兜底，模型只负责说得像医生", 960, 880, size=30, color=TEAL, progress=seg(t, 5.6, 6.8))
    caption(c, "换一个业务 = 换流程图、提示词、闸门、用例库", 960, 950, seg(t, 7.0, 8.2), size=27)
    c.alpha = 1


# ---------------------------------------------------------------------- 12. 结尾
def s12_end(c: Canvas, t: float, d: float) -> None:
    decor(c, 23, 1)
    bob = math.sin(t * 2.2) * 6
    person(c, 960, 1010, s=1.15, p=seg(t, 0, 1.4), shirt=(226, 232, 236), coat=True, bob=bob,
           wave=t if t > 1.2 else 0, label="AI 医生", seed=900)
    for i, (bx, by, col, fl) in enumerate([(430, 300, TEAL, TEAL_FILL), (1290, 290, TEAL, TEAL_FILL),
                                           (300, 540, INK, WHITE), (1440, 520, GREEN, GREEN_FILL)]):
        chat_bubble(c, bx, by + math.sin(t * 1.8 + i) * 8, 200, 70, p=seg(t, 0.6 + i * 0.22, 1.3 + i * 0.22),
                    color=col, fill=fl, lines=1, tail="left" if bx > 960 else "right", seed=910 + i)
    c.text("流程由图决定", (960, 330), size=54, weight="semibold", progress=seg(t, 1.6, 2.6))
    c.text("内容由提示词决定 · 安全由代码决定", (960, 430), size=54, weight="semibold", progress=seg(t, 2.4, 3.6))
    c.stroke([(lerp(960, 640, ease(seg(t, 3.4, 4.4))), 500), (lerp(960, 1280, ease(seg(t, 3.4, 4.4))), 500)],
             progress=1, color=TEAL, width=7, smooth=False)
    c.text("LChain Agent · 问诊智能体（app/consult）", (960, 590), size=32, color=INK_SOFT, weight="regular",
           progress=seg(t, 4.0, 5.2))
    for i, (x, y) in enumerate([(660, 240), (1260, 240), (860, 180), (1080, 190)]):
        sparkle(c, x, y, r=13, p=seg(t, 2.2 + i * 0.3, 2.8 + i * 0.3), color=(ORANGE, TEAL, GREEN, ORANGE)[i],
                seed=930 + i)


# ---------------------------------------------------------------------- 时间线
# (场景函数, 时长秒, 旁白)。旁白写成口语，读起来像真人讲解。
SCENES = [
    (s1_title, 10.0, "这是 LChain 的问诊智能体，患者发一条消息，它就回一条。"),
    (s2_pain, 13.0, "如果让模型自由发挥，它会漏问关键病史，还没问清就下结论，回复又长又没重点。"),
    (s3_path, 15.0, "所以我们把问诊做成一条固定的路径：采集现病史、分诊、鉴别问询、给出诊断，之后病情有变化，再更新诊断。"),
    (s4_intake, 14.0, "先采十项现病史，缺哪项问哪项，每轮最多两个问题，问不出来的就记未明确，继续往前走。"),
    (s5_parallel, 15.0, "每条消息进来，先同时判断两件事：患者说了什么，这一轮该做什么，再汇合到一处做决定。"),
    (s6_gate, 15.0, "模型只提建议，真正决定流程的是代码里的闸门：要素不全不能分诊，鉴别不够不能下诊断，没有诊断不能结束。"),
    (s7_safety, 13.0, "分诊和诊断的文本由代码拼装，免责声明、紧急提示一条都不会少。"),
    (s8_state, 14.0, "状态和对话记录都存在文件里，服务重启也不丢；同一个会话排队串行，不同会话并发跑，互不干扰。"),
    (s9_trace, 14.0, "每一步都能追溯：每次问诊都会在 Langfuse 留下完整 Trace，调用了什么、花了多久，一眼就能看完。"),
    (s10_eval, 17.0, "用例库是给 AI 服务做回归的：常见的场景，一个病例一个文件，改完提示词或流程，就整套跑一遍，通过率不降，才敢上线。"),
    (s11_reuse, 15.0, "想用这套框架做别的业务，照抄四步就行：先画流程图，再写判断提示词，然后加代码闸门，最后建用例库跑批。"),
    (s12_end, 10.0, "流程由图决定，内容由提示词决定，安全由代码决定。LChain，问诊智能体。"),
]
NO_SUBTITLE = {s12_end}
_ = (clock, database, shield, server, back_out, TEAL, WHITE, check_row)
