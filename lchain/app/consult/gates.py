"""问诊阶段常量与门禁校验：阶段名、10 项现病史要素、安全边界、参数校验。

这里是「流程不许跑偏」的唯一事实来源：工具在推进阶段前先调用本模块的函数，
不满足就返回 "❌ …" 把「还差什么」告诉模型（不抛异常，否则模型收不到纠正提示）。

改名/新增要素只改 `REQUIRED_HISTORY`；放宽或收紧轮次要改 `MIN_INTAKE_ROUNDS` /
`MIN_DIFFERENTIAL_ROUNDS`。安全边界（免责声明、急诊提示、拒绝药物剂量）也在这里，
不要为了让流程更容易通过而放开。
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 阶段与要素
STAGES = {
    "intake": "现病史采集",
    "triage": "已分诊",
    "differential": "鉴别问询",
    "diagnosis": "已给诊断",
    "followup": "病情变化追问",
    "closed": "已结束",
}
# 现病史必须问到的要素（key 用于记录，label 用于提示；顺序即建议的提问顺序）
REQUIRED_HISTORY: list[tuple[str, str]] = [
    ("onset", "起病时间与方式（何时开始、突然还是逐渐）"),
    ("location", "症状部位/范围"),
    ("character", "症状性质（什么样的痛/不适）"),
    ("severity", "严重程度及是否影响活动睡眠"),
    ("course", "病程演变（持续、间断、加重或缓解）"),
    ("associated", "伴随症状（发热、恶心、咳嗽、体重变化等）"),
    ("triggers", "诱因与缓解因素（什么情况加重/减轻）"),
    ("past_history", "既往史（慢性病、手术、类似发作）"),
    ("medications", "目前用药情况（含自行服药）"),
    ("allergies", "药物/食物过敏史"),
]
URGENCY = {"routine": "常规就诊", "soon": "建议尽早就诊（1~3 天内）", "urgent": "尽快就诊（今天内）",
           "emergency": "需要急诊（立即就医）"}
# 标签（“（”前的部分）→ 要素 key 的反查表：模型偶尔会用中文名当 key，这里兼容一下，避免整个流程卡在报错上
LABEL_TO_KEY = {label.split("（")[0]: key for key, label in REQUIRED_HISTORY}
HISTORY_KEYS = "、".join(f"{key}（{label.split('（')[0]}）" for key, label in REQUIRED_HISTORY)
MIN_INTAKE_ROUNDS = 2       # 至少几轮患者回复后才允许分诊
MIN_DIFFERENTIAL_ROUNDS = 2  # 至少几轮鉴别问询后才允许给诊断
MAX_QUESTIONS_PER_TURN = 2   # 每轮最多问几个问题（产品要求：问太多患者会答不全，回复必须短）
MAX_ASK_PER_ELEMENT = 3      # 同一个要素问这么多次还没答案就不再纠缠，记「未明确」继续流程
DISCLAIMER = "以上为线上问诊参考意见，不能替代医生面诊与检查；症状加重或出现新的不适请及时就医。"
EMERGENCY_HINT = "⚠️ 出现以下情况请立即拨打 120 或到急诊：意识不清、呼吸困难、持续胸痛、大汗、剧烈头痛、抽搐、"
_NO_DOSE_RE = re.compile(r"(\d+\s*(mg|毫克|g\b|克|ml|毫升|片|粒|支|IU|单位))|每次\s*\d+|每日\s*\d+\s*次|bid|tid", re.I)


# ------------------------------------------------------------------ 参数校验
def _clean_list(value: Any, field: str, *, required: bool = False, limit: int = 20) -> list[str]:
    if value is None:
        items: list[str] = []
    elif isinstance(value, str):
        items = [x.strip() for x in re.split(r"[；;\n]", value)]
    elif isinstance(value, (list, tuple)):
        items = [str(x).strip() for x in value]
    else:
        raise ValueError(f"{field} 需要字符串或字符串数组")
    items = [x for x in items if x]
    if required and not items:
        raise ValueError(f"{field} 不能为空")
    if len(items) > limit:
        raise ValueError(f"{field} 最多 {limit} 条")
    if any(len(x) > 200 for x in items):
        raise ValueError(f"{field} 单条不超过 200 字")
    return items


def _no_dosage(text: str, field: str) -> None:
    if _NO_DOSE_RE.search(text or ""):
        raise ValueError(f"{field} 不要给出具体药物剂量或用法（如「10mg 每日 2 次」）；"
                         "线上问诊只给方向性建议，具体用药由接诊医生决定")


def _bullets(items: list[str], prefix: str = "- ") -> str:
    """把条目拼成 Markdown 列表（用 `- ` 而不是 `•`：前端 md() 才会渲染成列表，读起来不费力）。"""
    return "\n".join(f"{prefix}{x}" for x in items)


def _normalize_history_key(raw: Any) -> str:
    """把模型传来的要素名归一成标准 key：支持英文 key，也兼容中文标签。
    中文匹配要求唯一命中，否则宁可报错让模型写准确 key，避免把「症状」误归到某个字段里。"""
    key = str(raw or "").strip()
    if key in {k for k, _ in REQUIRED_HISTORY}:
        return key
    short = key.split("（")[0].strip()
    if short in LABEL_TO_KEY:
        return LABEL_TO_KEY[short]
    if len(short) >= 2:  # 模型常把标签写短（「严重程度」vs「严重程度及是否影响活动睡眠」）
        hits = [k for label, k in LABEL_TO_KEY.items() if label.startswith(short) or short.startswith(label)]
        if len(hits) == 1:
            return hits[0]
    return key



# ------------------------------------------------------------------ 阶段校验
def missing_history(state: dict[str, Any]) -> list[str]:
    known = state.get("history") or {}
    return [f"{label}（{key}）" for key, label in REQUIRED_HISTORY if not str(known.get(key) or "").strip()]


def note_asked(state: dict[str, Any]) -> None:
    """记一笔「这一轮又问了哪些还没拿到的要素」；问太多次的就直接记「未明确」，不再卡流程。

    为什么需要：患者可能一直在答别的、或者表述入不了要素，如果死等某个要素就永远推不到分诊。
    真医生也会在反复问不出来时写「未明确」先往下走。
    """
    asked = state.setdefault("asked_times", {})
    history = state.setdefault("history", {})
    for key, _label in REQUIRED_HISTORY:
        if str(history.get(key) or "").strip():
            continue
        asked[key] = int(asked.get(key, 0)) + 1
        if asked[key] >= MAX_ASK_PER_ELEMENT:
            history[key] = "未明确（已询问多次，患者未说明）"


def _require_stage(state: dict[str, Any], allowed: set[str], action: str) -> str | None:
    if state.get("stage") not in allowed:
        stage = STAGES.get(state.get("stage", ""), "未开始")
        return (f"❌ 当前阶段「{stage}」不能执行「{action}」。请按问诊流程："
                "现病史采集 → 分诊 → 鉴别问询 → 诊断；诊断后出现新变化时先记录变化并追问，再更新诊断。")
    return None


def _check_questions(questions: Any) -> list[str] | str:
    """校验一组问题：非空且**不超过 MAX_QUESTIONS_PER_TURN 个**。

    一次问太多患者会答不全，而且回复会变得很长——这是产品要求，所以在代码里硬拦。
    返回问题列表（通过）或拒绝说明（不通过）。
    """
    try:
        qs = _clean_list(questions, "questions", required=True, limit=10)
    except ValueError as e:
        return f"❌ {e}"
    if len(qs) > MAX_QUESTIONS_PER_TURN:
        return (f"❌ 每轮最多问 {MAX_QUESTIONS_PER_TURN} 个问题（本轮收到 {len(qs)} 个）。"
                "请只保留最关键的 1~2 个，其余下一轮再问。")
    return qs


# ------------------------------------------------------------------ 本轮动作（工作流）
# 判断节点（consult-assess）给出 action，代码在这里收进白名单并做硬性钳制：
#   **内容由判断提示词决定，流程安全由代码决定**——模型说错也不会跳过分诊直接下诊断。
ACTIONS = ("ask_history", "triage", "ask_differential", "diagnose", "ask_followup", "re_diagnose", "close")


def clamp_action(state: dict[str, Any], action: Any, *, wants_to_stop: bool = False,
                 fresh_change: bool = False) -> tuple[str, str]:
    """把判断节点的 action 收进白名单并做阶段钳制，返回 (最终动作, 钳制说明)。

    规则：
    - 还没分诊：只允许 ask_history / triage；要素不全时 triage 降为 ask_history
    - 已分诊没诊断：允许 ask_differential / diagnose；鉴别轮次不够时 diagnose 降为 ask_differential
    - 已给诊断：只走「变化 → 追问 → 更新诊断」循环；变化还没追问时 re_diagnose 降为 ask_followup
    - **没有诊断时不允许 close**（除非患者明确表示要结束/去线下）：分诊后就收尾等于把流程半途而废，
      这是产品要求的完整流程：采集 → 分诊 → 鉴别 → 诊断

    `wants_to_stop` / `fresh_change` 是「本轮」的瞬时信号（参数传入而不是读 state）：
    extract 与 assess 是**并发**跑的，assess 看不到本轮刚提取出来的内容，
    所以“患者刚报告了变化”这件事必须在合并之后用这两个参数补回给钳制逻辑。
    """
    want = str(action or "").strip()
    note = ""
    if want not in ACTIONS:
        note = f"判断节点给出的动作 {want!r} 不在白名单，改为按阶段推进"
        want = ""

    has_triage, has_diag = bool(state.get("triage")), bool(state.get("diagnoses"))
    if want == "close" and fresh_change and not wants_to_stop:
        # assess 并发时不知道本轮刚报告了变化，可能给出 close；新变化一律先追问
        # （decide 合并时已把 followup_answered 置 False，所以这里直接走 ask_followup）
        note += "；患者刚报告了病情变化，不能直接结束（先追问）"
        want = "ask_followup"
    if want == "close" and not has_diag and not wants_to_stop:
        note += "；还没有给出诊断，不能结束（患者没有明确要求结束）"
        want = ""

    if not has_triage:
        if want == "triage":
            if not missing_history(state):
                return "triage", note
            note += "；现病史要素未齐，先继续采集"
            return "ask_history", note
        # 要素齐 + 轮次够之后不允许再问病史（否则会一直采集不下去）；红旗征筛查放在鉴别阶段
        if want == "ask_history" and not missing_history(state) \
                and int(state.get("record_rounds") or 0) >= MIN_INTAKE_ROUNDS:
            note += "；现病史已采集完整，直接进入分诊（红旗征在鉴别问询阶段筛查）"
            return "triage", note
        return ("close", note) if want == "close" else ("ask_history", note)

    if not has_diag:
        # 分诊后必须继续鉴别问询，不因为患者说了「好的/谢谢」就收尾
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
        # 防止无限追问：变化已经追问过一轮、患者也答了，就该出更新版诊断。
        # `fresh_change` 是例外：本轮刚冒出来的新变化即使 followup_answered 还没刷新（并发场景），也要先追问。
        note += "；变化已经追问过，直接给更新版诊断"
        return "re_diagnose", note
    return "ask_followup", note


_QUESTION_RE = re.compile(r"[？?]")


def is_question(text: str) -> bool:
    """一行是不是一个问题（问题直接给，所以只靠问号判断）。"""
    return bool(_QUESTION_RE.search(text or ""))


def one_line(text: str) -> str:
    """把多行合并成一行（问句专用）：两个问题写在同一行，不要分成两段。"""
    return re.sub(r"\s*\n+\s*", " ", (text or "").strip())


def limit_questions(text: str, n: int = MAX_QUESTIONS_PER_TURN) -> str:
    """把回复截到最多 n 个问题。

    按**问号**计数（不是按行）：回复要求「两个问题写在同一行」，所以第 n 个问号之后的内容
    （多出来的问题、以及尾巴上的收尾语）一律删掉。分诊/诊断块由代码渲染、不含问号，不受影响。
    """
    text = (text or "").strip()
    if not text or n <= 0:
        return text
    positions = [m.end() for m in _QUESTION_RE.finditer(text)]
    if len(positions) <= n:
        return text
    logger.info("截断超出上限的问题：%d → %d 个", len(positions), n)
    return text[: positions[n - 1]].strip()


# ------------------------------------------------------------------ 本轮动作（工作流）
# 判断节点（consult-assess）给出 action，代码在这里收进白名单并做硬性钳制：
#   **内容由判断提示词决定，流程安全由代码决定**——模型说错也不会跳过分诊直接下诊断。
ACTIONS = ("ask_history", "triage", "ask_differential", "diagnose", "ask_followup", "re_diagnose", "close")


def clamp_action(state: dict[str, Any], action: Any, *, wants_to_stop: bool = False,
                 fresh_change: bool = False) -> tuple[str, str]:
    """把判断节点的 action 收进白名单并做阶段钳制，返回 (最终动作, 钳制说明)。

    规则：
    - 还没分诊：只允许 ask_history / triage；要素不全时 triage 降为 ask_history
    - 已分诊没诊断：允许 ask_differential / diagnose；鉴别轮次不够时 diagnose 降为 ask_differential
    - 已给诊断：只走「变化 → 追问 → 更新诊断」循环；变化还没追问时 re_diagnose 降为 ask_followup
    - **没有诊断时不允许 close**（除非患者明确表示要结束/去线下）：分诊后就收尾等于把流程半途而废，
      这是产品要求的完整流程：采集 → 分诊 → 鉴别 → 诊断

    `wants_to_stop` / `fresh_change` 是「本轮」的瞬时信号（参数传入而不是读 state）：
    extract 与 assess 是**并发**跑的，assess 看不到本轮刚提取出来的内容，
    所以“患者刚报告了变化”这件事必须在合并之后用这两个参数补回给钳制逻辑。
    """
    want = str(action or "").strip()
    note = ""
    if want not in ACTIONS:
        note = f"判断节点给出的动作 {want!r} 不在白名单，改为按阶段推进"
        want = ""

    has_triage, has_diag = bool(state.get("triage")), bool(state.get("diagnoses"))
    if want == "close" and fresh_change and not wants_to_stop:
        # assess 并发时不知道本轮刚报告了变化，可能给出 close；新变化一律先追问
        # （decide 合并时已把 followup_answered 置 False，所以这里直接走 ask_followup）
        note += "；患者刚报告了病情变化，不能直接结束（先追问）"
        want = "ask_followup"
    if want == "close" and not has_diag and not wants_to_stop:
        note += "；还没有给出诊断，不能结束（患者没有明确要求结束）"
        want = ""

    if not has_triage:
        if want == "triage":
            if not missing_history(state):
                return "triage", note
            note += "；现病史要素未齐，先继续采集"
            return "ask_history", note
        # 要素齐 + 轮次够之后不允许再问病史（否则会一直采集不下去）；红旗征筛查放在鉴别阶段
        if want == "ask_history" and not missing_history(state) \
                and int(state.get("record_rounds") or 0) >= MIN_INTAKE_ROUNDS:
            note += "；现病史已采集完整，直接进入分诊（红旗征在鉴别问询阶段筛查）"
            return "triage", note
        return ("close", note) if want == "close" else ("ask_history", note)

    if not has_diag:
        # 分诊后必须继续鉴别问询，不因为患者说了「好的/谢谢」就收尾
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
        # 防止无限追问：变化已经追问过一轮、患者也答了，就该出更新版诊断。
        # `fresh_change` 是例外：本轮刚冒出来的新变化即使 followup_answered 还没刷新（并发场景），也要先追问。
        note += "；变化已经追问过，直接给更新版诊断"
        return "re_diagnose", note
    return "ask_followup", note


_QUESTION_RE = re.compile(r"[？?]")


def is_question(text: str) -> bool:
    """一行是不是一个问题（问题直接给，所以只靠问号判断）。"""
    return bool(_QUESTION_RE.search(text or ""))


def limit_questions(text: str, n: int = MAX_QUESTIONS_PER_TURN) -> str:
    """把回复里的问题截到最多 n 个。

    回复格式是「一行一个问题、不带编号、不带开场白」，所以按问号识别：
    - 多个问题行时，保留前 n 行，其余问题行（及其紧跟的补充行）删掉
    - 全部写在一行时，按第 n+1 个问号切开
    提示词已经要求「最多 2 个问题」，这里是最后一道硬保证。只用于问句节点；
    分诊/诊断块由代码渲染（render.py），不要对它们调用本函数。
    """
    text = (text or "").strip()
    if not text or n <= 0:
        return text
    lines = text.split("\n")
    marks = [i for i, ln in enumerate(lines) if is_question(ln)]
    if len(marks) > n:
        drop: set[int] = set()
        for i in marks[n:]:
            drop.add(i)
            j = i + 1   # 连它下面的补充行一起删（多行描述同一条问题）
            while j < len(lines) and lines[j].strip() and not is_question(lines[j]):
                drop.add(j)
                j += 1
        # 只保留到第 n 个问题为止：后面的收尾语（如「先回答这些就够了」）也不要
        tail = marks[n]
        drop |= set(range(tail, len(lines)))
        logger.info("截断超出上限的问题：保留 %d 个，删掉 %d 行", n, len(drop))
        return re.sub(r"\n{3,}", "\n\n", "\n".join(l for k, l in enumerate(lines) if k not in drop)).strip()
    if not marks:
        return text
    # 都在同一行（或同一段）里：按第 n+1 个问号截开
    positions = [m.end() for m in _QUESTION_RE.finditer(text)]
    if len(positions) > n:
        logger.info("截断同一段内超出上限的问题：保留 %d 个", n)
        return text[: positions[n - 1]].strip()
    return text
