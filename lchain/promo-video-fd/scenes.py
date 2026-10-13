"""10 个分镜：LChain 家庭医生 Agent（VBC 分级诊疗）产品介绍视频。

每帧调用 scene(c, t: 场景内秒数, d: 场景时长)：
  1. 标题      —— 企微群里的家庭医生：先把安全说清楚，再把下一步说清楚
  2. 痛点      —— 直接让模型自由回答会踩的四个坑
  3. 安全门控  —— 28 条确定性红旗规则，毫秒级，命中立即行动
  4. 多轮问诊  —— 信息不足先追问，至少 2 轮、最多 4 轮
  5. 分级判定  —— E0–E5/EU 七级 + 服务端只升不降
  6. 计划卡    —— 病情/方向/需排除/为什么/去哪/查什么/怎么确诊
  7. 居家观察  —— 截止时间 + 升级指征 + 定时追问，未回复≠好转
  8. 医生审核  —— 非急症结论提交平台真人医生审核
  9. 可追溯    —— 自动化测试 + 样例群 + Langfuse 链路
  10. 结尾     —— 安全先于建议 · 交流先于结论 · 结论由医生审核

旁白写在 SCENES 末尾（口语化，voice.py 生成语音和字幕）。
"""
from __future__ import annotations

import math

from cprops import check_row, code_card, cross_mark, file_card, folder_tree, gate, note_card, stamp
from props import chat_bubble, clock, node_box, person, phone, progress_bar, robot, shield, sparkle
from sketch import (BLUE, BLUE_FILL, GREEN, GREEN_FILL, INK, INK_SOFT, ORANGE, ORANGE_FILL, PURPLE, PURPLE_FILL,
                    RED, RED_FILL, TEAL, TEAL_FILL, W, WHITE, YELLOW_FILL, Canvas, back_out, ease, ease_in_out,
                    lerp, seg)

# ---------------------------------------------------------------------- 公共小工具


def out_k(t: float, d: float, dur: float = 0.7) -> float:
    return 1 - ease(seg(t, d - dur, d))


def title(c: Canvas, s: str, t: float, k: float, y: float = 118, color=INK, size: int = 56,
          at: float = 0.2) -> None:
    c.text(s, (W / 2, y), size=size, color=color, progress=seg(t, at, at + 1.2), weight="semibold", alpha=k)


def decor(c: Canvas, seed: int, k: float, colors=(TEAL_FILL, YELLOW_FILL)) -> None:
    c.blob(268, 208, 230, 150, colors[0], alpha=0.45 * k, seed=seed)
    c.blob(1676, 892, 258, 168, colors[1], alpha=0.45 * k, seed=seed + 1)


def caption(c: Canvas, s: str, cx, cy, p: float, color=INK_SOFT, size: int = 27) -> None:
    c.text(s, (cx, cy), size=size, color=color, progress=p, weight="regular")


def bad_card(c: Canvas, cx, cy, w, text: str, p: float, seed: int) -> None:
    note_card(c, cx, cy, w, 138, [], p=p, color=RED, fill=RED_FILL, title=text, title_size=30, seed=seed)
    cross_mark(c, cx - w / 2 + 40, cy, s=0.62, p=seg(p, 0.5, 0.95))
    _ = back_out


# ---------------------------------------------------------------------- 1. 标题
def s1_title(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 1, 1)
    phone(c, 176, 236, w=330, h=620, p=seg(t, 0.2, 1.5))
    chat_bubble(c, 236, 322, 246, 78, p=seg(t, 1.1, 1.9), text="昨晚开始胸痛，出冷汗", size=25, seed=11)
    chat_bubble(c, 236, 424, 254, 78, p=seg(t, 1.6, 2.4), color=RED, fill=RED_FILL, tail="left",
                text="现在拨打 120，不要等", size=25, seed=12)
    chat_bubble(c, 236, 526, 246, 78, p=seg(t, 2.1, 2.9), text="好的，我马上打", size=25, seed=13)
    person(c, 1660, 1010, s=1.15, p=seg(t, 1.0, 2.6), shirt=(226, 232, 236), coat=True,
           bob=math.sin(t * 2.2) * 6, wave=t if t > 2.6 else 0, label="家庭医生", seed=20)
    c.text("LChain 家庭医生 Agent", (1145, 410), size=78, weight="semibold", progress=seg(t, 2.4, 3.9))
    c.stroke([(lerp(1145, 760, ease(seg(t, 3.4, 4.3))), 490), (lerp(1145, 1530, ease(seg(t, 3.4, 4.3))), 490)],
             progress=1, color=TEAL, width=7, smooth=False)
    c.text("在企微群里，把「下一步怎么办」说清楚", (1145, 565), size=38, color=INK_SOFT, weight="regular",
           progress=seg(t, 3.8, 5.2))
    c.pill("安全门控 · 分级诊疗 · 就医路径 · 居家观察", 1145, 675, size=28, color=TEAL, progress=seg(t, 4.6, 5.6))
    for i, (x, y) in enumerate([(700, 330), (1520, 330), (860, 700)]):
        sparkle(c, x, y, r=14, p=seg(t, 4.4 + i * 0.25, 5.0 + i * 0.25),
                color=(ORANGE, TEAL, BLUE)[i], seed=30 + i)
    c.alpha = 1


# ---------------------------------------------------------------------- 2. 痛点
def s2_pain(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 3, 1, (RED_FILL, YELLOW_FILL))
    title(c, "直接让模型自由回答，会踩四个坑", t, 1)
    robot(c, 960, 950, s=1.0, p=seg(t, 0, 1.1), bob=math.sin(t * 2.4) * 5, blink=(t % 3.0) < 0.14, mood="flat")
    spots = [(430, 300, "漏掉危险信号"), (1330, 288, "一两轮就下结论"),
             (470, 620, "只说去医院，不说去哪查什么"), (1370, 604, "让患者自己去找医生")]
    for i, (cx, cy, txt) in enumerate(spots):
        bad_card(c, cx, cy + math.sin(t * 2 + i) * 5, 600, txt, seg(t, 1.0 + i * 0.55, 1.9 + i * 0.55),
                 seed=40 + i * 10)
    if t > 4.4:
        for i, (x, y) in enumerate([(700, 200), (1180, 200), (330, 420), (1620, 420)]):
            sparkle(c, x, y, r=11, p=seg(t, 4.4 + i * 0.2, 4.9 + i * 0.2), color=RED, seed=90 + i)
        c.pill("患者拿不到可执行的下一步", 960, 470, size=30, color=RED, fill=WHITE, progress=seg(t, 4.6, 5.6))
    c.alpha = 1


# ---------------------------------------------------------------------- 3. 安全门控
GATE_RULES = [("疑似心梗 / 卒中", "E0 立即拨打 120"), ("严重呼吸困难 / 过敏", "E0 立即急救"),
              ("抗凝药 + 头部外伤", "E1 尽快到急诊")]


def s3_gate(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 5, 1, (RED_FILL, TEAL_FILL))
    title(c, "先过一道确定性安全门控", t, 1)
    chat_bubble(c, 250, 430, 340, 86, p=seg(t, 0.5, 1.4), text="胸痛，出冷汗，喘不上气", size=26, seed=50)
    c.arrow([(604, 473), (688, 473)], progress=seg(t, 1.5, 2.0), color=INK, width=4, seed=51)
    gate(c, 860, 520, 320, 430, p=seg(t, 2.0, 3.0), blocked=seg(t, 3.2, 4.0), seed=320)
    note_card(c, 1180, 520, 330, 200, ["28 条红旗规则"], p=seg(t, 3.0, 4.0), color=INK, fill=WHITE,
              title="确定性规则", title_size=28, align="center", seed=330)
    c.pill("毫秒级判定", 1180, 680, size=27, color=TEAL, progress=seg(t, 4.0, 4.8))
    c.pill("不调用模型 · 不等人工", 1180, 750, size=27, color=TEAL, fill=TEAL_FILL, progress=seg(t, 4.4, 5.2))
    for i, (a, b) in enumerate(GATE_RULES):
        note_card(c, 1580, 280 + i * 200, 600, 158, [b], p=seg(t, 5.0 + i * 0.5, 5.9 + i * 0.5), color=RED,
                  fill=RED_FILL, title=a, title_size=28, size=27, seed=340 + i * 10)
    shield(c, 220, 250, s=1.0, p=seg(t, 5.8, 6.8), seed=360)
    c.pill("已知危险信号：先给行动，再继续分析", 960, 900, size=30, color=RED, fill=WHITE,
           progress=seg(t, 7.0, 8.0))
    c.alpha = 1


# ---------------------------------------------------------------------- 4. 多轮问诊
def s4_dialog(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 7, 1)
    title(c, "没有危险信号，就进入多轮问诊", t, 1)
    phone(c, 150, 190, w=560, h=790, p=seg(t, 0.3, 1.3), seed=400)
    bubbles = [(214, 272, "肚子有点不舒服", INK, WHITE, "left"),
               (286, 370, "具体位置？多久了？", TEAL, TEAL_FILL, "right"),
               (214, 468, "右上腹，两周了", INK, WHITE, "left"),
               (286, 566, "有没有发烧、黑便、皮肤发黄？", TEAL, TEAL_FILL, "right"),
               (214, 664, "都没有", INK, WHITE, "left")]
    for i, (x, y, txt, col, fl, tail) in enumerate(bubbles):
        chat_bubble(c, x, y, 400, 78, p=seg(t, 0.8 + i * 0.7, 1.6 + i * 0.7), color=col, fill=fl, tail=tail,
                    text=txt, size=26, seed=410 + i)
    note_card(c, 1300, 350, 620, 280, ["已完成 2 轮", "已确认：部位 / 时长 / 伴随症状",
                                       "已问过的问题不再重复"], p=seg(t, 2.0, 3.4), color=TEAL, fill=WHITE,
              title="问诊进度", title_size=30, size=27, seed=430)
    note_card(c, 1300, 700, 620, 250, ["普通病例至少 2 轮才给结论", "最多 4 轮后按现有信息保守结论"],
              p=seg(t, 3.6, 5.0), color=ORANGE, fill=ORANGE_FILL, title="轮次门槛", title_size=30, size=27,
              seed=440)
    c.pill("每轮 1–2 个能改变去向的问题", 1300, 900, size=28, color=TEAL, progress=seg(t, 5.4, 6.4))
    progress_bar(c, 214, 860, 420, 44, ease(seg(t, 5.8, 8.4)), p=seg(t, 5.6, 6.4), color=TEAL, fill=TEAL_FILL,
                 seed=450)
    caption(c, "问诊进度会持久化，断点重问也不丢", 424, 940, seg(t, 6.6, 7.6))
    c.alpha = 1


# ---------------------------------------------------------------------- 5. 分级判定
LEVELS = [("E0", "立即急救", RED, RED_FILL), ("E1", "尽快急诊", RED, RED_FILL), ("E2", "当日就医", ORANGE, ORANGE_FILL),
          ("E3", "限时面诊", ORANGE, ORANGE_FILL), ("E4", "计划门诊 / 观察", TEAL, TEAL_FILL),
          ("E5", "稳定复诊", GREEN, GREEN_FILL), ("EU", "暂不能分级", PURPLE, PURPLE_FILL)]


def s5_levels(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 9, 1, (BLUE_FILL, TEAL_FILL))
    title(c, "信息够了，再给统一的分级", t, 1)
    xs = [210 + i * 250 for i in range(7)]
    for i, (code, label, col, fl) in enumerate(LEVELS):
        p = seg(t, 1.0 + i * 0.42, 1.9 + i * 0.42)
        note_card(c, xs[i], 470, 232, 240, [label], p=p, color=col, fill=fl, title=code, title_size=40,
                  size=27, align="center", seed=500 + i * 10)
    c.pill("医院等级 ≠ 医疗能力", 700, 760, size=30, color=INK, fill=WHITE, progress=seg(t, 4.6, 5.6))
    c.pill("紧急程度只能升级，不能降级", 1250, 760, size=30, color=TEAL, fill=TEAL_FILL, progress=seg(t, 5.2, 6.2))
    c.arrow([(240, 850), (760, 850)], progress=seg(t, 5.8, 6.6), color=TEAL, width=5, seed=560)
    c.arrow([(1680, 850), (1160, 850)], progress=seg(t, 6.2, 7.0), color=TEAL, width=5, seed=561)
    caption(c, "服务端钳制：模型说要在家观察，规则判定要急救 → 按急救执行", 960, 930, seg(t, 7.2, 8.6))
    c.alpha = 1


# ---------------------------------------------------------------------- 6. 计划卡
def s6_plan(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 11, 1, (TEAL_FILL, YELLOW_FILL))
    title(c, "给患者一张看得懂、做得到的计划卡", t, 1)
    note_card(c, 660, 590, 1040, 690, [
        "病情了解：右上腹隐痛两周，饭后明显，无发热呕吐黄疸",
        "可能的方向：胆囊结石 / 慢性胆囊炎（较可能）",
        "需要优先排除：胆管炎、胰腺炎、溃疡并发症",
        "为什么这样安排：先做超声和肝功能，再决定下一步",
        "去哪：二级医院 · 消化内科 · 腹部超声 / 肝功能",
        "怎么确诊：医生结合超声与化验结果综合判断",
    ], p=seg(t, 0.4, 2.0), color=TEAL, fill=WHITE, title="患者计划卡", title_size=34, size=27, seed=600)
    cards = [(1520, 300, "去哪", "机构级别 · 科室", TEAL, TEAL_FILL),
             (1520, 490, "查什么", "检查清单（按优先级）", BLUE, BLUE_FILL),
             (1520, 680, "怎么确诊", "结合检查由医生判断", GREEN, GREEN_FILL),
             (1520, 870, "危险变化", "立即行动，不等回访", RED, RED_FILL)]
    for i, (cx, cy, a, b, col, fl) in enumerate(cards):
        note_card(c, cx, cy, 660, 156, [b], p=seg(t, 2.6 + i * 0.5, 3.5 + i * 0.5), color=col, fill=fl,
                  title=a, title_size=28, size=27, seed=610 + i * 10)
    c.pill("一句建议 + 少量步骤 + 必要警示", 960, 990, size=30, color=TEAL, fill=WHITE, progress=seg(t, 5.4, 6.4))
    c.alpha = 1


# ---------------------------------------------------------------------- 7. 居家观察
def s7_watch(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 13, 1, (GREEN_FILL, TEAL_FILL))
    title(c, "低风险轻症：居家观察，但不是放养", t, 1)
    # 时间轴
    c.arrow([(260, 520), (1660, 520)], progress=seg(t, 0.6, 1.4), color=INK_SOFT, width=4, seed=700)
    for i, (x, label, sub) in enumerate([(360, "现在", "已批准路径"), (960, "24 小时", "主动追问"),
                                         (1560, "最长截止", "必须复评")]):
        pr = seg(t, 1.2 + i * 0.6, 2.0 + i * 0.6)
        clock(c, x, 520, r=46, p=pr, t=t, seed=710 + i)
        c.text(label, (x, 610), size=30, weight="semibold", progress=pr)
        caption(c, sub, x, 660, seg(t, 1.8 + i * 0.6, 2.6 + i * 0.6), size=26)
    chat_bubble(c, 620, 210, 680, 96, p=seg(t, 3.2, 4.2), color=TEAL, fill=TEAL_FILL, tail="left", lines=1,
                text="和上次相比，是好一些、差不多，还是更重了？", size=27, seed=720)
    note_card(c, 370, 830, 560, 170, ["好转 → 继续观察"], p=seg(t, 4.6, 5.6), color=GREEN, fill=GREEN_FILL,
              size=28, seed=730)
    note_card(c, 1000, 830, 620, 170, ["出现升级指征 → 立即急诊"], p=seg(t, 5.2, 6.2), color=RED, fill=RED_FILL,
              size=28, seed=740)
    note_card(c, 1560, 830, 560, 170, ["未回复 ≠ 好转", "会按流程催办 + 通知人工"], p=seg(t, 5.8, 6.8),
              color=ORANGE, fill=ORANGE_FILL, size=27, seed=750)
    # 升级指征卡
    note_card(c, 1480, 410, 700, 260, ["具体变化 → 多久内 → 去哪 / 联系谁", "例如：呼吸困难 → 立即 → 拨打 120"],
              p=seg(t, 6.6, 7.8), color=ORANGE, fill=ORANGE_FILL, title="升级指征不是「加重就来」", title_size=28,
              size=26, seed=760)
    c.alpha = 1


# ---------------------------------------------------------------------- 8. 医生审核
def s8_review(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 15, 1, (GREEN_FILL, BLUE_FILL))
    title(c, "结论由平台真人医生审核", t, 1)
    for i, (cx, label, icon) in enumerate([(400, "AI 结论", "chat"), (960, "医生审核", "target"),
                                           (1520, "患者可见", "eye")]):
        node_box(c, cx, 420, label, icon, p=seg(t, 0.5 + i * 0.5, 1.4 + i * 0.5),
                 lit=seg(t, 1.2 + i * 0.5, 1.8 + i * 0.5), seed=800 + i * 10, w=300, h=170)
    c.arrow([(566, 420), (788, 420)], progress=seg(t, 1.8, 2.3), color=TEAL, width=4.5, seed=830)
    c.arrow([(1126, 420), (1348, 420)], progress=seg(t, 2.6, 3.1), color=TEAL, width=4.5, seed=831)
    note_card(c, 960, 700, 900, 220, ["不引导患者再去找医生问诊", "审核意见会回写到本群"],
              p=seg(t, 3.4, 4.6), color=GREEN, fill=GREEN_FILL, title="标准服务", title_size=30, size=28,
              seed=840)
    stamp(c, 1600, 790, "真人医生审核", p=seg(t, 4.8, 5.8), color=GREEN, size=28, seed=850)
    person(c, 250, 1010, s=1.0, p=seg(t, 2.2, 3.4), shirt=(226, 232, 236), coat=True,
           bob=math.sin(t * 2.1) * 5, label="审核医生", seed=860)
    c.pill("handoff：REQUESTED → ACCEPTED", 960, 930, size=28, color=TEAL, progress=seg(t, 5.6, 6.6))
    c.alpha = 1


# ---------------------------------------------------------------------- 9. 可追溯与回归
TREE = [(0, "app/wecom/fd", "dir"), (1, "red_flags.py 28 条红旗规则", ""),
        (1, "contracts.py E0–E5/EU · 不变量", ""), (1, "engine.py 多轮问诊 + 服务端门控", ""),
        (0, "tests/agent", "dir"), (1, "test_family_doctor.py 50 项", ""),
        (0, "scripts", "dir"), (1, "seed_fd_samples.py 10 个样例群", "")]


def s9_quality(c: Canvas, t: float, d: float) -> None:
    k = out_k(t, d)
    c.alpha = k
    decor(c, 17, 1, (PURPLE_FILL, TEAL_FILL))
    title(c, "每一次分诊都可追溯、可回归", t, 1)
    folder_tree(c, 620, 570, 980, 600, TREE, p=seg(t, 0.4, 1.8), seed=900, title="lchain/")
    for i, (label, delay) in enumerate([("确定性红旗 28 条", 2.2), ("自动化测试 50 项", 2.8),
                                        ("全量回归 204 项", 3.4)]):
        check_row(c, 1250, 300 + i * 100, 520, label, p=seg(t, delay, delay + 0.9),
                  done=seg(t, delay + 0.5, delay + 1.1), color=GREEN, seed=910 + i)
    file_card(c, 1490, 780, 620, 300, "Langfuse Trace",
              ["每次分诊 = 一条链路", "规则版本 / 提示词版本", "模型耗时与 token"], p=seg(t, 4.2, 5.4),
              color=PURPLE, seed=920)
    c.pill("样例群覆盖 E0–E5：打开页面就能复现", 960, 990, size=30, color=TEAL, fill=WHITE,
           progress=seg(t, 5.8, 6.8))
    c.alpha = 1


# ---------------------------------------------------------------------- 10. 结尾
def s10_end(c: Canvas, t: float, d: float) -> None:
    decor(c, 19, 1)
    bob = math.sin(t * 2.2) * 6
    person(c, 960, 1010, s=1.15, p=seg(t, 0, 1.4), shirt=(226, 232, 236), coat=True, bob=bob,
           wave=t if t > 1.2 else 0, label="家庭医生", seed=930)
    for i, (bx, by, col, fl) in enumerate([(430, 300, TEAL, TEAL_FILL), (1290, 290, TEAL, TEAL_FILL),
                                           (300, 540, INK, WHITE), (1440, 520, GREEN, GREEN_FILL)]):
        chat_bubble(c, bx, by + math.sin(t * 1.8 + i) * 8, 200, 70, p=seg(t, 0.6 + i * 0.22, 1.3 + i * 0.22),
                    color=col, fill=fl, lines=1, tail="left" if bx > 960 else "right", seed=940 + i)
    c.text("安全先于建议", (960, 330), size=54, weight="semibold", progress=seg(t, 1.6, 2.6))
    c.text("交流先于结论 · 结论由医生审核", (960, 430), size=54, weight="semibold", progress=seg(t, 2.4, 3.6))
    c.stroke([(lerp(960, 620, ease(seg(t, 3.4, 4.4))), 500), (lerp(960, 1300, ease(seg(t, 3.4, 4.4))), 500)],
             progress=1, color=TEAL, width=7, smooth=False)
    c.text("LChain Agent · 家庭医生（app/wecom/fd）", (960, 590), size=32, color=INK_SOFT, weight="regular",
           progress=seg(t, 4.0, 5.2))
    for i, (x, y) in enumerate([(660, 240), (1260, 240), (860, 180), (1080, 190)]):
        sparkle(c, x, y, r=13, p=seg(t, 2.2 + i * 0.3, 2.8 + i * 0.3), color=(ORANGE, TEAL, GREEN, ORANGE)[i],
                seed=950 + i)


# ---------------------------------------------------------------------- 时间线
# (场景函数, 时长秒, 旁白)。旁白口语化，voice.py 生成语音和同步字幕。
SCENES = [
    (s1_title, 11.0, "这是 LChain 的家庭医生 Agent。患者在企微群里发一条消息，它先判断安全，再把下一步说清楚。"),
    (s2_pain, 13.0, "直接让模型自由回答，容易踩四个坑：漏掉危险信号、一两轮就下结论、只说去医院、让患者自己去找医生。"),
    (s3_gate, 15.0, "所以先过一道确定性安全门控：二十八条红旗规则，毫秒级判定。心梗、卒中这类危险信号，立刻提示拨打一二零，不调用模型、不等人工。"),
    (s4_dialog, 16.0, "没有危险信号，就进入多轮问诊。每轮只问一两个能改变去向的问题，普通病例至少两轮、最多四轮，问过的信息不会重复问。"),
    (s5_levels, 15.0, "信息够了，再给统一的分级：从 E 零立即急救，到 E 五稳定复诊，还有一个 E U 暂不能分级。紧急程度只能升级，服务端会兜住模型的降级。"),
    (s6_plan, 18.0, "结论不是一句去医院，而是一张计划卡：病情了解、可能的方向、需要优先排除的风险、为什么这样安排，以及去什么级别的医院、什么科室、做哪些检查、怎么确诊。"),
    (s7_watch, 16.0, "低风险轻症可以居家观察，但不是放养：有最长观察截止、具体升级指征，还有定时任务主动追问；患者没回复，也不会被当成好转。"),
    (s8_review, 12.0, "所有非急症结论，都会提交平台真人医生审核；患者不用再去找医生问诊，审核意见会回写到群里。"),
    (s9_quality, 13.0, "每一次分诊都有完整记录，也有自动化测试兜底：规则、契约、多轮问诊、居家观察，还有覆盖 E 零到 E 五的样例群。"),
    (s10_end, 10.0, "安全先于建议，交流先于结论，结论由医生审核。LChain，家庭医生 Agent。"),
]
NO_SUBTITLE = {s10_end}
_ = (code_card, progress_bar, BLUE_FILL, INK_SOFT, caption)
