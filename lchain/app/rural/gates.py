"""山区远程问诊的门禁与安全边界：可及性要素、处置层级、动作钳制、急救要点。

面向的是**偏远山区、医疗条件差的村民本人**（老人、低文化水平、可能用方言口述），
和 `app/wecom/` 的群回复、`app/consult/` 的城市线上问诊都不同，差异集中在三处：

1. **可及性**：不只要有「建议看哪个科」，还要判断「今晚能不能到、怎么去、有没有人能陪」——
   `RURAL_FIELDS` 是比现病史更"土"的要素，答案决定 `disposition`（居家/村卫生室/乡镇/县医院/120）。
2. **本地能做与不能做**：建议的检查与处理必须限制在村卫生室/家里有的条件内（配置在
   `rural_local_resources`），给不了的检查要写清"到哪儿能做"。
3. **沟通方式**：老人一次只问一个问题（`MAX_QUESTIONS_PER_TURN = 1`），短句，不问专业术语；
   患者口述里可能有错别字、方言词、语音转文字的错误，判断节点要理解意图而不是挑字面。

**安全边界一律由代码兜底，不靠模型自觉**：
- 10 项现病史要素、免责声明、急诊提示复用 `app.consult.gates`（同一套临床思路，不重复维护）
- 「转诊/处置层级」只允许收进 `DISPOSITIONS` 白名单，模型说错不会真的让患者"在家等着"
- 药物剂量在 `strip_dosage()` 里直接剔除（线上问诊不给具体用药）
- 蛇咬伤、农药中毒、煤气中毒、烫伤、外伤出血等常见急救场景的**正确做法**写在 `FIRST_AID` 里，
  由 `render.py` 按识别到的危险信号关键词追加——避免模型教出「绑扎 / 切开吸毒 / 涂牙膏」这类有害处理
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.core.config import get_settings
# 复用城市问诊的要素定义与通用工具：山区问诊的临床采集部分与它一致，不要另起一套
from app.consult.gates import (  # noqa: F401  （部分名字是给外部统一引用的）
    _NO_DOSE_RE,
    _bullets,
    DISCLAIMER,
    REQUIRED_HISTORY,
    URGENCY,
    is_question,
    limit_questions,
    missing_history,
    note_asked,
    one_line,
)

logger = logging.getLogger(__name__)


def emergency_hint() -> str:
    """山区版的立即就医提示（写全，并带上山区本地才有的急症）。

    不用 `app.consult.gates.EMERGENCY_HINT`：那份是城市场景的通用名单（且以顿号结尾），
    山区还要提醒蛇咬、农药、煤气、摔伤、孕产、婴幼儿这些本地高发情况。
    """
    return (f"⚠️ 出现以下情况请立即拨打 {get_settings().rural_emergency_number} 或尽快送到能急救的医院："
            "叫不醒或意识不清、喘不上气、胸痛伴大汗、抽搐、大出血或呕血黑便、"
            "蛇咬伤、农药或煤气中毒、摔伤后不能动、孕产妇出血或破水、婴幼儿精神差。")


# ------------------------------------------------------------------ 阶段
STAGES = {
    "intake": "现病史采集",
    "access": "就医条件确认",
    "triage": "已给就医建议",
    "differential": "鉴别问询",
    "diagnosis": "已给诊断",
    "followup": "病情变化追问",
    "closed": "已结束",
}

# 老人一次只问一个问题（比城市问诊的 2 个更严：一次问多了基本答不全）
MAX_QUESTIONS_PER_TURN = 1
MIN_INTAKE_ROUNDS = 2          # 至少几轮患者回复后才允许给就医建议
MIN_DIFFERENTIAL_ROUNDS = 1    # 山区问诊诊断前的鉴别问询轮次（少一轮，减少老人负担）
MAX_ASK_PER_ELEMENT = 3
MAX_ASK_ACCESS = 2             # 就医条件最多问这么多次，问不到就按最坏情况建议（不让流程卡死）

# ------------------------------------------------------------------ 山区附加要素：就医可及性
# 这些不是医学要素，而是「能不能及时看上病」的条件：直接决定处置层级（见 DISPOSITIONS）
RURAL_FIELDS: list[tuple[str, str]] = [
    ("access_distance", "到最近能看病的地方有多远（走路/骑车/坐车大概多久）"),
    ("access_transport", "怎么去（有没有车、路通不通、雨雪天能不能走）"),
    ("access_companion", "身边有没有人能陪着去（是否独居、有没有家属）"),
    ("local_conditions", "家里和村卫生室有什么可用的（有没有药、血压计、血糖仪、氧气袋等）"),
]
ACCESS_KEYS = [key for key, _ in RURAL_FIELDS]
ACCESS_LABEL = {key: label for key, label in RURAL_FIELDS}
ACCESS_BRIEF = "、".join(label.split("（")[0] for _key, label in RURAL_FIELDS)

# ------------------------------------------------------------------ 处置层级（转诊决策）
# 顺序即严重度：紧急情况只能往上抬、不能往下降（见 escalate()）
DISPOSITIONS = {
    "home": "居家照护，由村医隔天跟进",
    "village": "到村卫生室处理（今天去）",
    "township": "尽快到乡镇卫生院（1~3 天内）",
    "county": "到县医院就诊（尽快，最好今天出发）",
    "emergency": "立即呼叫 120 / 紧急转运（不要等、不要自己扛）",
}
DISPOSITION_ORDER = ["home", "village", "township", "county", "emergency"]
URGENT_DISPOSITIONS = ("county", "emergency")

# 常见急救场景的正确做法：render.py 按危险信号里的关键词追加，避免模型教出有害处理
FIRST_AID: dict[str, dict[str, Any]] = {
    "蛇咬": {"keywords": ("蛇咬", "蛇咬伤", "被蛇"),
             "check_any": ("别切开", "不要切开", "别用嘴吸", "不要用嘴吸"),
             "steps": ["伤肢放低、别走动（走动毒扩散更快），摘掉戒指手镯",
                       "记住蛇的样子（能拍照更好），别切开、别用嘴吸、别捆扎",
                       "尽快送到有抗蛇毒血清的县医院，路上安静、别喝酒"]},
    "蜂蜇": {"keywords": ("蜂蜇", "蜂蛰", "马蜂", "蜜蜂"),
             "check_any": ("刮掉毒刺", "不要用手捏", "冷敷"),
             "steps": ["用硬卡片刮掉毒刺，不要用手捏（会挤出更多毒液）",
                       "局部冷敷；出现全身皮疹、嗓子发紧、喘不上气要立即送医（过敏会很快加重）"]},
    "农药": {"keywords": ("农药", "敌敌畏", "百草枯", "杀虫剂", "除草剂"),
             "check_any": ("不要自行催吐", "不要催吐", "脱掉", "离开"),
             "steps": ["立刻离开洒药现场，脱掉污染衣裤，用清水冲洗皮肤头发",
                       "带上农药瓶（医生要知道是什么药），不要自行催吐，尽快送医院",
                       "叫不醒或口吐白沫：侧卧、清理口鼻，同时打 120"]},
    "煤气": {"keywords": ("煤气", "一氧化碳", "炭火", "烧炭", "取暖中毒"),
             "check_any": ("通风", "室外"),
             "steps": ["马上开窗开门，把人抬到室外通风处，解开衣领、注意保暖",
                       "没有呼吸或叫不醒要立刻打 120，并开始胸外按压（家里有人会做人工呼吸更好）"]},
    "烫伤": {"keywords": ("烫伤", "烧伤", "开水烫"),
             "check_any": ("凉水", "冷水", "流水"),
             "steps": ["用流动的凉水冲 15~20 分钟，边冲边脱掉（剪开）烫湿的衣服，不要硬撕",
                       "不要涂牙膏、酱油、紫药水；水泡不要挑破，用干净布盖住后送医"]},
    "出血": {"keywords": ("出血", "割伤", "砍伤", "伤口流血", "呕血", "黑便", "便血"),
             "check_any": ("直接压住", "压迫", "不要往伤口里填"),
             "steps": ["伤口出血：用干净纱布或毛巾**直接压住**止血，抬高患处，不要往伤口里填泥土草药",
                       "呕血、黑便、便血说明是身体里面出血，不要吃东西喝水，尽快送医院"]},
    "骨折": {"keywords": ("骨折", "摔伤", "坠落", "扭伤", "摔下", "摔了"),
             "check_any": ("不要搬动", "不要拉拽", "不要复位", "固定"),
             "steps": ["怀疑骨折或摔到腰背脖子：**不要搬动、不要拉拽复位**，就地用木板布条固定",
                       "打 120 等担架，搬运时保持身体平直；山路难走，多叫几个人一起抬"]},
    "惊厥": {"keywords": ("抽搐", "惊厥", "抽风", "抽筋不止"),
             "check_any": ("不要往嘴里", "侧躺", "侧卧"),
             "steps": ["让孩子侧躺着，清开周围的硬东西，**不要往嘴里塞任何东西**、不要压住手脚",
                       "记下抽搐开始的时间，抽完马上送医院；如果超过 5 分钟不停，直接打 120"]},
    "临产": {"keywords": ("临产", "要生", "腹痛临产", "见红", "破水", "孕晚期出血"),
             "check_any": ("平躺", "垫高", "不要走路", "不要下地"),
             "steps": ["破水后平躺、垫高臀部，不要走路下地，尽快联系 120 或乡镇卫生院接人",
                       "准备好身份证、母子健康手册、干净被褥，出血多或肚子剧痛要立即送医"]},
    "胸痛": {"keywords": ("胸痛", "胸闷", "胸骨后", "心口疼", "大汗"),
             "check_any": ("不要自己开车", "打 120", "打120", "安静"),
             "steps": ["安静躺下或半坐、不要走动、不要自己开车去，立即打 120",
                       "家里有医生开过的硝酸甘油可按医嘱舌下含服；不要自行吃止痛药硬扛"]},
    "呼吸困难": {"keywords": ("呼吸困难", "喘不上气", "憋气", "窒息"),
                 "check_any": ("坐起", "抬高", "打 120", "打120"),
             "steps": ["坐起来、上半身抬高，解开衣领，保持空气流通",
                           "立即打 120；慢性肺病的老人家里有氧气袋可以先吸氧"]},
    "意识": {"keywords": ("意识不清", "叫不醒", "昏迷", "头晕倒地", "摔倒老人"),
             "check_any": ("侧卧", "不要喂水", "不要喂药"),
             "steps": ["侧卧位、清理口鼻异物，防止呕吐物呛进肺里，不要喂水喂药",
                       "注意保暖并立即打 120；同时联系能陪同的家属一起下山"]},
}

# 山区常见危险信号（注入提示词，提醒判断节点主动筛查）
RURAL_RED_FLAGS = [
    "蛇咬伤、蜂蜇后全身起疹 / 喘不上气",
    "喷洒农药后头晕、出汗、呕吐（农药中毒）",
    "烧炭取暖后头晕、恶心、叫不醒（一氧化碳中毒）",
    "高处坠落、摔伤腰背脖子、骨头变形",
    "大出血、呕血、黑便",
    "抽搐、意识不清、说话不清、半边身子动不了",
    "胸痛伴大汗、喘不上气",
    "孕妇破水、出血、肚子剧痛",
    "婴幼儿精神差、不吃奶、呼吸急促、抽搐",
    "老人独居无人照看且症状在加重",
]

# 「一次两片下去」这类口语说法没有单位，靠药量动词 + 中文数词兑一层
_DOSE_WORDS = ("剂量", "每次吃", "一天吃", "一天几片", "加量", "加倍", "打针用药", "输液用药")
_CN_NUM = "0-9一二两三四五六七八九十半几多"


def has_dosage(text: str) -> bool:
    """文本里是否出现具体药物剂量 / 用法（线上问诊一律不给，由代码剔除）。"""
    text = text or ""
    if _NO_DOSE_RE.search(text):
        return True
    # 「一次两片下去」这类口语说法没有单位，靠药量动词兜一层
    return bool(re.search(rf"(吃|服|喝|打|输)\s*[{_CN_NUM}]\s*(片|粒|支|袋|包|瓶|丸)", text))


def strip_dosage(items: Any, field: str = "") -> list[str]:
    """筛掉带剂量的条目（而不是报错）：宁可少给一条建议，也不能把用法用量发给患者。"""
    out: list[str] = []
    for raw in items or []:
        text = str(raw).strip()
        if not text:
            continue
        if has_dosage(text):
            logger.warning("山区问诊剔除带剂量的内容 field=%s：%s", field, text[:80])
            continue
        out.append(text)
    return out


# ------------------------------------------------------------------ 可及性校验
def missing_access(state: dict[str, Any]) -> list[str]:
    """还缺哪些就医条件要素（给患者看的缺失清单）。"""
    access = state.get("access") or {}
    return [ACCESS_LABEL[key] for key in ACCESS_KEYS if not str(access.get(key) or "").strip()]


def missing_access_keys(state: dict[str, Any]) -> list[str]:
    access = state.get("access") or {}
    return [key for key in ACCESS_KEYS if not str(access.get(key) or "").strip()]


def note_access_asked(state: dict[str, Any]) -> None:
    """记一笔「就医条件又问了一轮」；问够 MAX_ASK_ACCESS 次仍没答案就不再纠缠。

    为什么需要：山区患者（尤其老人）常常答不上"有多远""有没有车"，如果死等就永远给不出建议。
    真医生也会按最坏情况（假设没有条件）先给建议，让家属再核实。
    """
    asked = state.setdefault("asked_access", {})
    access = state.setdefault("access", {})
    for key in missing_access_keys(state):
        asked[key] = int(asked.get(key, 0)) + 1
        if asked[key] >= MAX_ASK_ACCESS:
            access[key] = "未明确（按最坏情况考虑：距离远、无车、无人陪同）"
    if missing_access_keys(state):
        state["access_asked"] = True


# ------------------------------------------------------------------ 本轮动作（工作流）
ACTIONS = ("ask_history", "ask_access", "triage", "ask_differential", "diagnose",
           "ask_followup", "re_diagnose", "close")


def clamp_action(state: dict[str, Any], action: Any, *, wants_to_stop: bool = False,
                 fresh_change: bool = False) -> tuple[str, str]:
    """把判断节点的行动收进白名单并按阶段钳制，返回 (最终动作, 钳制说明)。

    与 `app.consult.gates.clamp_action` 同一套规则，多一道**就医条件门禁**：
    - 现病史齐 + 轮次够之后，就医条件（距离/交通/陪同/本地条件）还没问过 → 先 `ask_access`
      （问过一次仍不明确就按最坏情况给建议，不让流程卡住）
    - 要素不全不能 `triage`；鉴别轮次不够不能 `diagnose`；没有诊断不能 `close`；
      变化刚发生必须先 `ask_followup`
    - 报急诊信号时一律走 `emergency`（在 triage/diagnose 的字段里抬升，见 `escalate`）
    """
    want = str(action or "").strip()
    note = ""
    if want not in ACTIONS:
        note = f"判断节点给出的动作 {want!r} 不在白名单，改为按阶段推进"
        want = ""

    history_ready = not missing_history(state) and int(state.get("record_rounds") or 0) >= MIN_INTAKE_ROUNDS
    access_missing = bool(missing_access_keys(state))
    access_asked = bool(state.get("access_asked"))
    has_triage, has_diag = bool(state.get("triage")), bool(state.get("diagnoses"))

    if want == "close" and fresh_change and not wants_to_stop:
        # extract 与 assess 并发时，assess 看不到本轮刚提取到的新变化；新变化一律先追问
        note += "；患者刚报告了病情变化，不能直接结束（先追问）"
        want = "ask_followup"
    if want == "close" and not has_diag and not wants_to_stop:
        note += "；还没有给出诊断，不能结束（患者没有明确要求结束）"
        want = ""

    if not has_triage:
        if want == "triage":
            if not history_ready:
                note += "；现病史要素未齐或轮次不够，先继续采集"
                return "ask_history", note
            if access_missing and not access_asked:
                note += "；还给不出就医方式（不知道远不远、有没有车），先问清楚"
                return "ask_access", note
            return "triage", note
        if want == "ask_access" and history_ready:
            if access_missing and not access_asked:
                return "ask_access", note
            note += "；就医条件已经问过（或已答），直接给就医建议"
            return "triage", note
        if want == "ask_history" and history_ready:
            if access_missing and not access_asked:
                note += "；现病史已齐，改问就医条件（决定去哪看、怎么去）"
                return "ask_access", note
            note += "；现病史已采集完整，直接给就医建议"
            return "triage", note
        return ("close", note) if want == "close" else ("ask_history", note)

    if not has_diag:
        if want == "diagnose":
            if len(state.get("differential") or []) >= MIN_DIFFERENTIAL_ROUNDS:
                return "diagnose", note
            note += f"；鉴别问询不足 {MIN_DIFFERENTIAL_ROUNDS} 轮，先继续问询"
            return "ask_differential", note
        if want == "close":
            return "close", note
        return "ask_differential", note

    # 已有诊断：只处理「变化 → 追问 → 更新诊断」循环
    if want == "close":
        return "close", note
    if want == "re_diagnose":
        if state.get("stage") == "followup" and not state.get("followup_answered"):
            note += "；变化刚记录、还没追问，先问清楚再更新诊断"
            return "ask_followup", note
        if state.get("changes"):
            return "re_diagnose", note
        note += "；没有记录到新变化"
        return "ask_followup", note
    if want == "ask_followup" and state.get("followup_answered") and state.get("changes") and not fresh_change:
        note += "；变化已经追问过，直接给更新版诊断（山区来回问会耽误时间）"
        return "re_diagnose", note
    return "ask_followup", note


# ------------------------------------------------------------------ 危险信号 → 抬升处置层级
def first_aid_for(flags: list[str]) -> list[dict[str, Any]]:
    """按危险信号关键词找出该场景的正确急救要点（可能命中多条）。"""
    text = "；".join(str(f) for f in flags or [])
    hits: list[dict[str, Any]] = []
    for name, item in FIRST_AID.items():
        if any(kw in text for kw in item["keywords"]):
            hits.append({"name": name, "steps": list(item["steps"]),
                         "check_any": list(item.get("check_any") or [])})
    return hits


def missing_first_aid(flags: list[str], text: str) -> list[str]:
    """命中的急救场景里，哪些在 `text` 里找不到正确做法（评测用：教错做法要能扣分）。

    `check_any` 里任一个关键词出现即算讲到（例如蛇咬伤只要提到「别切开」就算）。
    """
    out: list[str] = []
    for item in first_aid_for(flags):
        kws = item.get("check_any") or []
        if kws and not any(k in text for k in kws):
            out.append(item["name"])
    return out


def escalate(structured: dict[str, Any], found_flags: list[str], *, all_flags: list[str] | None = None) -> None:
    """患者前面说过危险信号、但这一步给了轻处置：抬到 emergency，并补上急救要点。

    同一件事在别处也做过（`gates.escalate` 抬 urgency）——这里抬的是**处置层级**：
    山区最怕的是"患者自己在家扛"，所以宁可建议他多跑一趟。
    """
    flags = [str(f) for f in (all_flags or []) if str(f).strip()]
    aid = first_aid_for(found_flags)
    if not aid and not flags:
        return
    level = str(structured.get("disposition") or "home")
    if level not in DISPOSITIONS:
        level = "home"
    if aid or flags:
        structured["disposition"] = "emergency" if level in ("home", "village") else level
    if flags:
        structured["red_flags"] = list(dict.fromkeys([*flags, *(structured.get("red_flags") or [])]))[:8]
    if aid and not structured.get("first_aid"):
        structured["first_aid"] = aid


def disposition_label(level: str) -> str:
    return DISPOSITIONS.get(str(level), DISPOSITIONS["home"])
