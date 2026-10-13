"""《和 AI 一起开发业务 Agent》分镜（9 个，约 100 秒）。

主题：**开发 Agent 的方式，就是一轮轮对话**——提需求 → 它写代码 → 你跑起来看 →
给具体反馈 → 它改 → 再看 → 最后沉淀成文档与用例。画面取自真实对话里发生过的内容。

每个场景函数签名：scene(c, t: 场景内秒数, d: 场景时长)。
"""
from __future__ import annotations

import math

from props import chat_bubble, check, icon_draw, person, robot, sparkle
from sketch import (BLUE, BLUE_FILL, GREEN, GREEN_FILL, INK, INK_SOFT, ORANGE, ORANGE_FILL, PURPLE, PURPLE_FILL, RED,
                    RED_FILL, W, WHITE, YELLOW_FILL, Canvas, ease, lerp, seg)

ACCENT = {"blue": (BLUE, BLUE_FILL), "green": (GREEN, GREEN_FILL), "orange": (ORANGE, ORANGE_FILL),
          "purple": (PURPLE, PURPLE_FILL), "red": (RED, RED_FILL)}


# ---------------------------------------------------------------------- 通用小工具
def out_k(t: float, d: float, dur: float = 0.6) -> float:
    return 1 - ease(seg(t, d - dur, d))


def decor(c: Canvas, seed: int, k: float = 1.0, colors=(BLUE_FILL, YELLOW_FILL)) -> None:
    c.blob(250, 210, 220, 150, colors[0], alpha=0.42 * k, seed=seed)
    c.blob(1690, 890, 260, 170, colors[1], alpha=0.42 * k, seed=seed + 1)


def head(c: Canvas, s: str, t: float, at: float = 0.1) -> None:
    c.text(s, (140, 92), size=44, anchor="lm", weight="semibold", progress=seg(t, at, at + 0.9))
    c.stroke([(140, 126), (lerp(140, 140 + 25 * len(s), ease(seg(t, at + 0.5, at + 1.3))), 126)], color=BLUE,
             width=6, smooth=False)


def round_no(c: Canvas, n: int, cx: float, cy: float, p: float = 1.0, color=BLUE, fill=BLUE_FILL) -> None:
    """「第几轮」圆标。"""
    if p <= 0:
        return
    c.ellipse(cx, cy, 32, 32, progress=p, color=color, fill=fill, width=4.4, seed=1000 + n)
    c.text(str(n), (cx, cy), size=32, color=color, weight="semibold", progress=seg(p, 0.4, 1))


def me(c: Canvas, x: float, y: float, p: float, label: str = "我") -> None:
    person(c, x, y, s=0.92, p=p, shirt=ORANGE_FILL, mood="smile", label=label, seed=11)


def ai(c: Canvas, x: float, y: float, p: float, t: float = 0.0) -> None:
    robot(c, x, y, s=0.86, p=p, wave=t if t > 1.0 else 0, blink=(t % 3.1) < 0.14,
          bob=math.sin(t * 2.2) * 6, glow=(t * 0.8) % 1)


def card(c: Canvas, x: float, y: float, w: float, h: float, title: str, lines: list[str], p: float,
         color=INK, fill=WHITE, size=34, line_size=27) -> None:
    if p <= 0:
        return
    c.rect(x, y, w, h, radius=18, progress=seg(p, 0, 0.65), color=color, fill=fill, width=4.2, seed=int(x + y))
    c.text(title, (x + 26, y + 34), size=size, anchor="lm", color=color, weight="semibold",
           progress=seg(p, 0.30, 0.90))
    n = max(1, len(lines))
    for i, s in enumerate(lines):
        a = 0.40 + 0.45 * i / n
        c.text(s, (x + 26, y + 80 + i * (line_size + 12)), size=line_size, anchor="lm", color=INK_SOFT,
               weight="regular", progress=seg(p, a, min(1.0, a + 0.32)))


def arrow_right(c: Canvas, x1: float, y1: float, x2: float, p: float, color=INK_SOFT) -> None:
    c.arrow([(x1, y1), (x1 + (x2 - x1) * ease(p), y1)], progress=1.0, color=color, width=4.5, head=14, seed=int(x1))


def say(c: Canvas, x: float, y: float, w: float, text: str, p: float, tail="left", fill=WHITE, color=INK,
        size=32, h: float = 0) -> None:
    """对话气泡（带文字）。"""
    h = h or (98 if len(text) <= 14 else 132)
    chat_bubble(c, x, y, w, h, p=p, color=color, fill=fill, tail=tail, text=text, size=size, seed=int(x + y))


# ---------------------------------------------------------------------- 1. 标题
def s1_title(c: Canvas, t: float, d: float) -> None:
    c.alpha = out_k(t, d)
    decor(c, 1)
    me(c, 300, 880, seg(t, 0.4, 1.5))
    ai(c, 1620, 860, seg(t, 0.7, 1.9), t)
    say(c, 420, 690, 300, "帮我做个问诊场景", seg(t, 1.6, 2.4), tail="left", size=26)
    say(c, 1200, 690, 280, "好，我先搭起来", seg(t, 2.1, 2.9), tail="right", fill=BLUE_FILL, color=BLUE, size=28)
    c.text("和 AI 一起开发业务 Agent", (960, 360), size=86, weight="semibold", progress=seg(t, 2.6, 4.0))
    c.stroke([(620, 428), (1300, 428)], progress=ease(seg(t, 3.6, 4.4)), color=BLUE, width=7, smooth=False)
    c.text("一轮轮对话，就是开发方式", (960, 505), size=38, color=INK_SOFT, weight="regular",
           progress=seg(t, 4.1, 5.4))
    for i, (txt, col) in enumerate([("提需求", "blue"), ("跑起来", "green"), ("给反馈", "orange"),
                                    ("再迭代", "purple")]):
        c.pill(txt, 700 + i * 175, 940, size=30, color=ACCENT[col][0], fill=ACCENT[col][1],
               progress=seg(t, 4.6 + i * 0.2, 5.3 + i * 0.2))
    c.alpha = 1


# ---------------------------------------------------------------------- 2. 第一轮：一句话需求
def s2_ask(c: Canvas, t: float, d: float) -> None:
    c.alpha = out_k(t, d)
    decor(c, 3, colors=(ORANGE_FILL, BLUE_FILL))
    head(c, "第一轮：我提需求", t)
    round_no(c, 1, 88, 92, 1.0)
    me(c, 240, 900, seg(t, 0.3, 1.3))
    say(c, 350, 540, 940, "做一个问诊场景：多轮问病史，然后分诊、鉴别、诊断；", seg(t, 1.0, 1.9), size=27, h=104)
    say(c, 350, 668, 940, "病情变化了还要更新诊断，再给我 10 个常见病例自动测试。", seg(t, 1.5, 2.4),
        size=27, h=104)
    ai(c, 1660, 880, seg(t, 1.8, 2.8), t - 1.8)
    # 需求被拆成流程链
    chain = ["现病史采集", "分诊", "鉴别问询", "诊断", "变化更新"]
    for i, s in enumerate(chain):
        p = seg(t, 2.6 + i * 0.35, 3.4 + i * 0.35)
        x = 180 + i * 350
        c.rect(x, 240, 290, 110, radius=16, progress=p, color=INK, fill=WHITE, width=4)
        c.text(s, (x + 145, 295), size=30, anchor="mm", weight="medium", progress=seg(p, 0.35, 1.05)
               if p < 1 else 1.0)
        if i < len(chain) - 1:
            arrow_right(c, x + 300, 295, x + 340, seg(t, 3.0 + i * 0.35, 3.6 + i * 0.35))
    c.pill("10 个常见病例自动测试", 1460, 460, size=30, color=GREEN, fill=GREEN_FILL, progress=seg(t, 4.6, 5.4))
    c.text("需求说清楚：做什么、分几步、怎么验收", (960, 990), size=36, color=BLUE, weight="semibold",
           progress=seg(t, 5.0, 6.2))
    c.alpha = 1


# ---------------------------------------------------------------------- 3. 它写出来，我跑一遍
def s3_run(c: Canvas, t: float, d: float) -> None:
    c.alpha = out_k(t, d)
    decor(c, 5, colors=(GREEN_FILL, RED_FILL))
    head(c, "它写代码，我跑起来看", t)
    round_no(c, 2, 88, 92, 1.0, color=GREEN, fill=GREEN_FILL)
    ai(c, 320, 900, seg(t, 0.3, 1.3), t)
    for i, s in enumerate(["搭好流程", "连上大模型", "写好页面"]):
        p = seg(t, 0.9 + i * 0.4, 1.7 + i * 0.4)
        check(c, 620, 300 + i * 96, s=0.75, p=p, color=GREEN, seed=31 + i)
        c.text(s, (690, 300 + i * 96), size=34, anchor="lm", weight="medium", progress=seg(p, 0.3, 1.0)
               if p < 1 else 1.0)
    say(c, 1120, 240, 640, "打开页面，发一句「我上腹痛 3 天了」", seg(t, 2.4, 3.4), tail="left", fill=BLUE_FILL,
        color=BLUE, size=30, h=104)
    # 看到的两个问题
    for i, s in enumerate(["回复太长，一次问了 4 个问题", "还带了「好的，再问两个」这种铺垫"]):
        p = seg(t, 3.6 + i * 0.6, 4.5 + i * 0.6)
        y = 500 + i * 120
        c.rect(1120, y, 660, 96, radius=14, progress=p, color=RED, fill=RED_FILL, width=4.2, seed=41 + i)
        c.text("✗ " + s, (1156, y + 48), size=29, anchor="lm", color=RED, weight="medium",
               progress=seg(p, 0.35, 1.0) if p < 1 else 1.0)
    c.text("跑一遍就知道问题在哪——这一步不能省", (960, 900), size=36, color=INK_SOFT, weight="regular",
           progress=seg(t, 5.2, 6.4))
    c.alpha = 1


# ---------------------------------------------------------------------- 4. 第二轮：把反馈说具体
def s4_feedback1(c: Canvas, t: float, d: float) -> None:
    c.alpha = out_k(t, d)
    decor(c, 7, colors=(BLUE_FILL, ORANGE_FILL))
    head(c, "第二轮：把反馈说具体", t)
    round_no(c, 2, 88, 92, 1.0)
    me(c, 220, 900, seg(t, 0.3, 1.2))
    say(c, 360, 700, 760, "每轮最多问 2 个问题；问题直接给，不要编号、不要铺垫。", seg(t, 0.9, 1.9),
        size=27, h=104)
    ai(c, 1680, 880, seg(t, 1.6, 2.6), t - 1.6)
    # 改前 / 改后
    for i, (tag, col, lines) in enumerate([
        ("改前", "red", ["好的，先问几个问题：", "1. 这三天疼得厉害吗？晚上能睡着吗？",
                         "2. 有没有发烧、恶心想吐、拉黑便？", "3. 以前有过胃病吗？吃过什么药？"]),
        ("改后", "green", ["这三天疼得厉害吗，晚上能睡着吗？", "有没有发烧、恶心想吐，或者拉黑便？"])]):
        p = seg(t, 2.6 + i * 0.9, 3.7 + i * 0.9)
        x = 200 + i * 800
        c.rect(x, 190, 720, 290, radius=18, progress=seg(p, 0, 0.6), color=ACCENT[col][0], fill=ACCENT[col][1],
               width=4.4, seed=51 + i)
        c.text(tag, (x + 32, 236), size=36, anchor="lm", color=ACCENT[col][0], weight="semibold",
               progress=seg(p, 0.3, 0.9))
        for j, s in enumerate(lines):
            a = seg(p, 0.5 + j * 0.1, min(1.0, 0.75 + j * 0.1))
            c.text(s, (x + 32, 300 + j * 46), size=25, anchor="lm", color=INK, weight="regular", progress=a)
    arrow_right(c, 940, 335, 1020, seg(t, 3.8, 4.4))
    c.text("反馈越具体，改得越到位", (960, 540), size=40, color=BLUE, weight="semibold", progress=seg(t, 4.6, 5.8))
    c.text("（不要说「感觉不太对」，要说「每轮最多 2 个问题」）", (960, 604), size=29, color=INK_SOFT,
           weight="regular", progress=seg(t, 5.0, 6.2))
    c.alpha = 1


# ---------------------------------------------------------------------- 5. 第三轮：细节也别放过
def s5_feedback2(c: Canvas, t: float, d: float) -> None:
    c.alpha = out_k(t, d)
    decor(c, 9, colors=(PURPLE_FILL, YELLOW_FILL))
    head(c, "第三轮：细节也别放过", t)
    round_no(c, 3, 88, 92, 1.0, color=PURPLE, fill=PURPLE_FILL)
    me(c, 220, 900, seg(t, 0.3, 1.2))
    say(c, 360, 690, 700, "两个问题写在一行，不要分成两段。", seg(t, 0.9, 1.9), size=27, h=100)
    for i, (tag, col, rows) in enumerate([("改前", "red", ["这三天疼得厉害吗？", "", "有没有发烧、想吐？"]),
                                          ("改后", "green", ["这三天疼得厉害吗？有没有发烧、想吐？"])]):
        p = seg(t, 2.2 + i * 0.8, 3.3 + i * 0.8)
        x = 200 + i * 800
        c.rect(x, 190, 720, 250, radius=18, progress=seg(p, 0, 0.6), color=ACCENT[col][0], fill=ACCENT[col][1],
               width=4.4, seed=61 + i)
        c.text(tag, (x + 32, 242), size=36, anchor="lm", color=ACCENT[col][0], weight="semibold",
               progress=seg(p, 0.3, 0.9))
        for j, s in enumerate(rows):
            if not s:
                continue
            c.text(s, (x + 32, 324 + j * 52), size=27, anchor="lm", color=INK, weight="regular",
                   progress=seg(p, 0.5 + j * 0.15, min(1.0, 0.75 + j * 0.15)))
    ai(c, 1680, 900, seg(t, 3.4, 4.4), t - 3.4)
    c.text("小细节也值得提——它每次都改到位", (900, 530), size=38, color=BLUE, weight="semibold",
           progress=seg(t, 4.4, 5.6))
    c.text("兜底：代码按问号计数截断，再合并成一行", (900, 596), size=29, color=INK_SOFT,
           weight="regular", progress=seg(t, 4.9, 6.1))
    c.alpha = 1


# ---------------------------------------------------------------------- 6. 关键一条：改用工作流
def s6_workflow(c: Canvas, t: float, d: float) -> None:
    c.alpha = out_k(t, d)
    decor(c, 11, colors=(BLUE_FILL, GREEN_FILL))
    head(c, "关键一条：应该用工作流", t)
    round_no(c, 4, 88, 92, 1.0, color=ORANGE, fill=ORANGE_FILL)
    # 三段式：图决定流程 / 提示词判断 / 代码守边界
    cols = [("流程由图决定", "judge", ["状态机写死阶段", "模型只回答「做什么」"], "blue"),
            ("判断交给提示词", "target", ["只输出 JSON 字段", "温度设 0 · 枚举列全"], "green"),
            ("安全留给代码", "search", ["模型判断错了也走不歪", "免责声明 · 急诊提示"], "orange")]
    for i, (title_, icon, lines, col) in enumerate(cols):
        p = seg(t, 2.5 + i * 0.7, 3.6 + i * 0.7)
        x = 150 + i * 570
        c.rect(x, 180, 510, 300, radius=18, progress=seg(p, 0, 0.6), color=ACCENT[col][0], fill=ACCENT[col][1],
               width=4.4, seed=71 + i)
        icon_draw(c, icon, x + 255, 256, ACCENT[col][0], p=seg(p, 0.2, 0.8), seed=75 + i)
        c.text(title_, (x + 255, 332), size=36, anchor="mm", weight="semibold", progress=seg(p, 0.3, 0.9))
        for j, s in enumerate(lines):
            c.text("· " + s, (x + 40, 390 + j * 52), size=27, anchor="lm", color=INK, weight="regular",
                   progress=seg(p, 0.5 + j * 0.15, min(1.0, 0.78 + j * 0.15)))
    # 用户这句反馈单独占一行，不要压住上下两块
    say(c, 610, 510, 700, "流程要用工作流来做，判断用专门的提示词。", seg(t, 0.9, 1.9), size=27, h=96)
    # 钳制闸门（人物放到框下方，别钻到框里）
    c.rect(150, 640, 1620, 200, radius=18, progress=seg(t, 4.8, 5.8), color=INK, fill=WHITE, width=4.4)
    c.text("模型给的动作，能不能执行由代码说了算", (960, 700), size=36, anchor="mm", weight="semibold",
           progress=seg(t, 5.1, 6.1))
    for i, (s, col) in enumerate([("不许跳步", "blue"), ("不许半途而废", "orange"), ("防死循环", "purple")]):
        c.pill(s, 560 + i * 400, 786, size=30, color=ACCENT[col][0], fill=ACCENT[col][1],
               progress=seg(t, 5.5 + i * 0.25, 6.3 + i * 0.25))
    c.text("这一条，是从「总觉得哪里不对」聊出来的", (960, 940), size=34, anchor="mm", color=INK_SOFT,
           weight="regular", progress=seg(t, 6.6, 7.8))
    c.alpha = 1


# ---------------------------------------------------------------------- 7. 功能也是一点点补的
def s7_features(c: Canvas, t: float, d: float) -> None:
    c.alpha = out_k(t, d)
    decor(c, 13, colors=(GREEN_FILL, PURPLE_FILL))
    head(c, "功能也是一点点补的", t)
    feats = [("问诊独立目录", "app/consult/ 与企微群平级", "blue"),
             ("去掉调试入口", "侧栏配置默认收起", "purple"),
             ("会话历史", "分页 · 多选删除 · 点开回看", "green"),
             ("测试报告独立页", "选集合 · 批量跑 · AI 分析", "orange"),
             ("用例即文件", "一个用例一个 md，按科室分类", "red")]
    for i, (title_, sub, col) in enumerate(feats):
        p = seg(t, 0.5 + i * 0.55, 1.6 + i * 0.55)
        y = 200 + i * 150
        c.rect(150, y, 1100, 118, radius=16, progress=seg(p, 0, 0.65), color=ACCENT[col][0], fill=ACCENT[col][1],
               width=4.2, seed=81 + i)
        c.text(title_, (190, y + 42), size=36, anchor="lm", weight="semibold", progress=seg(p, 0.3, 0.9))
        c.text(sub, (190, y + 86), size=27, anchor="lm", color=INK_SOFT, weight="regular",
               progress=seg(p, 0.45, min(1.0, 0.85)))
        check(c, 1310, y + 59, s=0.8, p=seg(p, 0.6, 1.0), color=GREEN, seed=85 + i)
    ai(c, 1690, 900, seg(t, 3.2, 4.2), t - 3.2)
    c.text("每一句「再加个…」", (1620, 260), size=30, anchor="mm", color=INK_SOFT, weight="regular",
           progress=seg(t, 3.8, 4.7))
    c.text("都是一次小迭代", (1620, 316), size=30, anchor="mm", color=INK_SOFT, weight="regular",
           progress=seg(t, 4.1, 5.0))
    c.alpha = 1


# ---------------------------------------------------------------------- 8. 工程问题照样提
def s8_engineer(c: Canvas, t: float, d: float) -> None:
    c.alpha = out_k(t, d)
    decor(c, 15, colors=(RED_FILL, BLUE_FILL))
    head(c, "工程问题照样提", t)
    items = [("刷新不能中断任务", ["跑批移到服务端后台", "前端只轮询状态"], "blue"),
             ("并发要扛住 50 个", ["50 个会话并发全部成功", "同步阻塞调用丢线程池"], "green"),
             ("单轮要更快", ["两个判断节点并发执行", "3.99s → 2.62s"], "orange")]
    for i, (title_, lines, col) in enumerate(items):
        p = seg(t, 0.5 + i * 0.8, 1.7 + i * 0.8)
        x = 150 + i * 570
        c.rect(x, 240, 510, 340, radius=18, progress=seg(p, 0, 0.6), color=ACCENT[col][0], fill=ACCENT[col][1],
               width=4.4, seed=91 + i)
        c.text(title_, (x + 255, 320), size=36, anchor="mm", weight="semibold", progress=seg(p, 0.3, 0.9))
        for j, s in enumerate(lines):
            c.text("· " + s, (x + 40, 400 + j * 60), size=27, anchor="lm", color=INK, weight="regular",
                   progress=seg(p, 0.5 + j * 0.2, min(1.0, 0.8 + j * 0.2)))
    c.rect(150, 650, 1620, 230, radius=18, progress=seg(t, 3.2, 4.2), color=INK, fill=WHITE, width=4.4)
    c.text("性能和稳定性，也要当成需求提出来", (960, 715), size=38, anchor="mm", weight="semibold",
           progress=seg(t, 3.5, 4.7))
    c.text("「感觉有点卡」→ 先测一遍，再动手改", (960, 800), size=32, anchor="mm", color=INK_SOFT,
           weight="regular", progress=seg(t, 4.0, 5.2))
    c.alpha = 1


# ---------------------------------------------------------------------- 9. 沉淀 → 循环
def s9_loop(c: Canvas, t: float, d: float) -> None:
    decor(c, 17)
    me(c, 250, 900, seg(t, 0.2, 1.2))
    ai(c, 1670, 880, seg(t, 0.5, 1.5), t)
    c.text("最后，把过程沉淀下来", (960, 180), size=52, weight="semibold", progress=seg(t, 0.6, 1.8))
    for i, (title_, sub, col) in enumerate([("方法论文档", "docs/business-agent-guide.md", "blue"),
                                            ("用例文件", "eval/ 一个用例一个 md", "green"),
                                            ("自动化测试", "桩测试秒级 + 真实跑批", "orange")]):
        p = seg(t, 1.6 + i * 0.5, 2.6 + i * 0.5)
        x = 220 + i * 500
        c.rect(x, 280, 440, 150, radius=16, progress=seg(p, 0, 0.65), color=ACCENT[col][0], fill=ACCENT[col][1],
               width=4.2, seed=101 + i)
        c.text(title_, (x + 220, 330), size=34, anchor="mm", weight="semibold", progress=seg(p, 0.3, 0.9))
        c.text(sub, (x + 220, 386), size=25, anchor="mm", color=INK_SOFT, weight="regular",
               progress=seg(p, 0.45, 1.0))
    # 循环回路
    cx, cy, r = 960, 690, 190
    c.arc(cx, cy, r, -160, 320, progress=seg(t, 3.6, 5.0), color=BLUE, width=6, seed=111)
    c.arrow([(cx + r * math.cos(math.radians(-160)), cy + r * math.sin(math.radians(-160))),
             (cx + r * 0.97 * math.cos(math.radians(-130)), cy + r * 0.97 * math.sin(math.radians(-130)))],
            progress=seg(t, 4.8, 5.3), color=BLUE, width=7, head=18, seed=112)
    for i, s in enumerate(["提需求", "跑起来看", "给反馈", "它改进", "再沉淀"]):
        ang = math.radians(-140 + i * 68)
        c.text(s, (cx + r * 0.72 * math.cos(ang), cy + r * 0.72 * math.sin(ang)), size=29, anchor="mm",
               color=INK, weight="medium", progress=seg(t, 4.0 + i * 0.28, 4.8 + i * 0.28))
    c.text("数据：39 个测试 · 10 个用例 · 单轮 2.62s · 50 并发验证", (960, 940), size=30, color=INK_SOFT,
           weight="regular", progress=seg(t, 5.6, 6.8))
    c.text("开发 Agent 的方式，就是这样聊出来的", (960, 1010), size=42, color=BLUE, weight="semibold",
           progress=seg(t, 6.2, 7.4))


# ---------------------------------------------------------------------- 时间线
# (场景函数, 时长秒, 旁白)。旁白口语化，就像真人在讲这次过程。
SCENES = [
    (s1_title, 9.5, "这是用 AI 开发业务 Agent 的真实过程，一轮轮对话，就是开发方式。"),
    (s2_ask, 12.5, "第一轮我说需求：做个问诊场景，多轮问病史，再分诊、鉴别、诊断，病情变化了还要更新诊断。"),
    (s3_run, 12.0, "它很快把流程搭起来，我打开页面跑一遍，问题就来了：回复太长，一次问了四个问题，还带着铺垫的话。"),
    (s4_feedback1, 11.5, "第二轮我把反馈说具体，每轮最多问两个问题，问题直接给，不要编号，也不要那些过渡的话。"),
    (s5_feedback2, 11.0, "第三轮连细节也不放过：两个问题要写在同一行，不要分成两段。它每次都改到位。"),
    (s6_workflow, 14.5, "接下来是一条关键的反馈，应该用工作流来做：流程由图决定，判断交给专门的提示词，安全边界由代码守住。"),
    (s7_features, 12.5, "功能一点点补：独立目录、隐藏调试入口、侧栏收起、历史多选删除、测试报告独立页，还能多选集合跑。"),
    (s8_engineer, 12.5, "工程问题照样提：刷新页面不能中断任务，并发要扛住五十个，单轮耗时从四秒压到两点六秒。"),
    (s9_loop, 12.0, "最后把过程沉淀成文档、用例和测试。开发 Agent 的方式，就是这样一轮轮聊出来的。"),
]
NO_SUBTITLE = {s9_loop}
_ = (W, INK, lerp, sparkle, chat_bubble)
