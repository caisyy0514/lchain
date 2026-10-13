"""确定性安全门控：红旗规则注册表（red_flag_registry）。

这是独立于大模型的硬门控（《VBC…v1.3.md》第 5 / 12 / 24 节）：
- 先用确定性规则快速筛查；命中的高危信号立即给已审行动，不等待完整推理
- 规则带 rule_id / version / 依据 / 审批状态；生产放行需医学组审核，当前为演示版本
- 只做升级：LLM 的紧急程度不得低于本门控命中的下界（contracts.clamp_urgency）
- 规则可配置、可版本化、可单测；空的“全病种自动安全”规则禁止存在

规则匹配是「分句 + 触发词 + 否定」的启发式，不替代临床判断：
在同一分句里出现否定/已缓解/既往描述时不触发；不同分句独立判断，避免一句“没有胸痛，但是胸闷”
被整体否定。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.wecom.fd.contracts import Urgency, worst_urgency

RED_FLAG_REGISTRY_VERSION = "fd-redflags-2026.10-v1.3.0-demo"
REVIEW_STATUS = "DEMO_PENDING_CLINICAL_REVIEW"   # 真实上线前必须由临床质量委员会审核


@dataclass(frozen=True)
class RiskModifier:
    modifier_id: str
    label: str
    patterns: tuple[str, ...]


@dataclass(frozen=True)
class RedFlagRule:
    rule_id: str
    category: str
    title: str
    urgency: str                      # 基础紧急程度
    action: str                       # 已审患者行动模板（可用 {emergency_number}）
    any_of: tuple[str, ...] = ()      # 任一组匹配
    all_of: tuple[tuple[str, ...], ...] = ()  # 每组至少一个分句匹配
    none_of: tuple[str, ...] = ()     # 分句级否定/排除
    requires_modifier: str | None = None
    escalate_with: tuple[str, ...] = ()
    escalated_urgency: str | None = None
    min_data: tuple[str, ...] = ()
    source: str = "机构红旗协议（演示版，待医学组审核）"


RISK_MODIFIERS: tuple[RiskModifier, ...] = (
    RiskModifier("anticoagulant", "使用抗凝/抗血小板药",
                 (r"抗凝", r"华法林", r"利伐沙班", r"阿哌沙班", r"达比加群", r"艾多沙班",
                  r"抗血小板", r"阿司匹林", r"氯吡格雷", r"替格瑞洛", r"双抗")),
    RiskModifier("immunosuppressed", "免疫抑制/化疗/放疗中",
                 (r"免疫抑制", r"免疫抑制剂", r"他克莫司", r"环孢素", r"化疗", r"放疗", r"靶向治疗",
                  r"免疫治疗", r"生物制剂", r"大剂量激素")),
    RiskModifier("transplant", "器官/造血干细胞移植后", (r"移植",)),
    RiskModifier("pregnancy", "妊娠/产后", (r"怀孕", r"妊娠", r"孕妇", r"孕\d+周", r"产后", r"哺乳期", r"宫外孕")),
    RiskModifier("infant", "婴幼儿", (r"新生儿", r"婴儿", r"宝宝", r"幼儿", r"月龄", r"满月", r"奶粉喂养")),
    RiskModifier("elderly", "高龄/衰弱", (r"高龄", r"老人", r"老年", r"八十", r"九十", r"卧床", r"失能")),
    RiskModifier("cancer", "肿瘤/血液系统疾病", (r"肿瘤", r"癌症", r"癌", r"白血病", r"淋巴瘤", r"骨髓瘤")),
    RiskModifier("diabetes", "糖尿病", (r"糖尿病", r"血糖")),
    RiskModifier("dialysis", "透析", (r"透析", r"血透", r"腹透")),
    RiskModifier("recent_surgery", "近期手术/术后", (r"术后", r"刚做完手术", r"刚手术", r"手术第\d+天")),
)

# 分句分隔符：分句独立判定；注意不把“、”当分隔符——“没有 A、B”的否定要覆盖 A 和 B
_CLAUSE_SPLIT = re.compile(r"[，。！？；;,\n\t]|(?<=[^0-9])\s+|(?<=[0-9])\s+(?=[\u4e00-\u9fff])")

# 否定/既往窗口：触发词附近出现这些词时不触发
# 注意 “无” 单字容易误伤“无力/无明显”等词，只匹配句首或分隔符后的“无”
_NEG_TOKENS = re.compile(
    r"没有|没再|并没有|否认|未见|不伴|以前|之前|曾经|既往|已(经)?缓解|缓解了|好了|不疼了|不痛了|"
    r"(?:^|[，。；;、\s])无(?=\S)"
)


def _clauses(text: str) -> list[str]:
    return [c.strip() for c in _CLAUSE_SPLIT.split(text or "") if c and c.strip()]


def _compile(patterns: tuple[str, ...]) -> list[re.Pattern[str]]:
    return [re.compile(p) for p in patterns]


_RULE_CACHE: dict[str, dict[str, list[re.Pattern[str]]]] = {}


def _compiled(rule: RedFlagRule) -> dict[str, list[re.Pattern[str]]]:
    if rule.rule_id not in _RULE_CACHE:
        _RULE_CACHE[rule.rule_id] = {
            "any_of": _compile(rule.any_of),
            "all_of": [p for group in rule.all_of for p in _compile(group)],
            "none_of": _compile(rule.none_of),
        }
    return _RULE_CACHE[rule.rule_id]


def detect_modifiers(text: str) -> list[str]:
    found = []
    for m in RISK_MODIFIERS:
        if any(re.search(p, text) for p in m.patterns):
            found.append(m.modifier_id)
    return found


def _clause_hits(clause: str, patterns: list[re.Pattern[str]], none_of: list[re.Pattern[str]]) -> bool:
    """分句命中：规则否定/既往描述直接排除；每个触发词还要通过窗口否定检查。"""
    if any(p.search(clause) for p in none_of):
        return False
    for p in patterns:
        for m in p.finditer(clause):
            if not _window_negated(clause, m):
                return True
    return False


def _window_negated(text: str, m: re.Match[str]) -> bool:
    s = max(0, m.start() - 8)
    e = min(len(text), m.end() + 6)
    return bool(_NEG_TOKENS.search(text[s:e]))


def _fulltext_hit(text: str, patterns: list[re.Pattern[str]], none_of: list[re.Pattern[str]]) -> bool:
    """全文回退：支持跨分句的时序描述；匹配词附近有否定/既往词时不算命中。"""
    if any(p.search(text) for p in none_of):
        return False
    for p in patterns:
        for m in p.finditer(text):
            if not _window_negated(text, m):
                return True
    return False


def _rule_matches(rule: RedFlagRule, clauses: list[str], modifiers: list[str],
                  full_text: str = "") -> tuple[bool, list[str]]:
    if rule.requires_modifier and rule.requires_modifier not in modifiers:
        return False, []
    c = _compiled(rule)
    evidence: list[str] = []
    # all_of：每组至少一个分句命中（可分句组合，如“胸痛”在上一句、“冷汗”在下一句）
    for group in rule.all_of:
        pats = _compile(group)
        hit = [x for x in clauses if _clause_hits(x, pats, c["none_of"])]
        if not hit:
            return False, []
        evidence.extend(hit)
    if rule.any_of:
        hit = [x for x in clauses if _clause_hits(x, c["any_of"], c["none_of"])]
        if not hit:
            return False, []
        evidence.extend(hit)
    if evidence:
        return True, list(dict.fromkeys(evidence))[:3]
    # 分句匹配失败时做全文回退（跨分句模式，如“说话不清，几分钟就好了”）
    if not full_text:
        return False, []
    for group in rule.all_of:
        if not _fulltext_hit(full_text, _compile(group), c["none_of"]):
            return False, []
    if rule.any_of and not _fulltext_hit(full_text, c["any_of"], c["none_of"]):
        return False, []
    return True, []


def _load_rules() -> tuple[RedFlagRule, ...]:
    return _RULES


@dataclass
class ScreenResult:
    matched: list[dict[str, Any]] = field(default_factory=list)
    urgency_floor: str | None = None
    emergency: bool = False
    safety_flags: list[str] = field(default_factory=list)
    modifiers: list[str] = field(default_factory=list)
    version: str = RED_FLAG_REGISTRY_VERSION

    def floor_code(self) -> str:
        return self.urgency_floor or Urgency.EU.value

    def to_dict(self) -> dict[str, Any]:
        return {"matched": self.matched, "urgency_floor": self.urgency_floor, "emergency": self.emergency,
                "safety_flags": self.safety_flags, "modifiers": self.modifiers, "version": self.version}

    def prompt_digest(self, emergency_number: str = "120") -> str:
        """注入提示词的确定性筛查结果（供 LLM 参考，不允许降级）。"""
        if not self.matched:
            mods = "、".join(m.label for m in RISK_MODIFIERS if m.modifier_id in self.modifiers) or "无"
            return (f"确定性安全初筛（{self.version}）：未命中已知红旗规则。\n"
                    f"高危修饰项：{mods}（存在修饰项时按更低阈值判断）。\n"
                    "未命中不等于安全；关键危险信号仍需按医学规则判断。")
        lines = [f"确定性安全初筛（{self.version}）命中 {len(self.matched)} 条红旗规则，"
                 f"紧急程度下界 = {self.urgency_floor}（LLM 只能升级、不得降级）："]
        for m in self.matched:
            lines.append(f"- [{m['rule_id']}] {m['title']}（{m['urgency']}）：{m['action'].format(emergency_number=emergency_number)}")
            if m.get("evidence"):
                lines.append(f"  依据：{' / '.join(m['evidence'])}")
        return "\n".join(lines)


def screen(text: str, *, profile: str = "", emergency_number: str = "120") -> ScreenResult:
    """确定性快速筛查。text = 当前消息（含合并批次），profile = 患者档案（只用于高危修饰项）。"""
    clauses = _clauses(text)
    modifiers = detect_modifiers(text + "\n" + profile)
    res = ScreenResult(modifiers=modifiers)
    floor: str | None = None
    for rule in _load_rules():
        ok, evidence = _rule_matches(rule, clauses, modifiers, text)
        if not ok:
            continue
        urgency = rule.urgency
        if rule.escalate_with and any(m in modifiers for m in rule.escalate_with) and rule.escalated_urgency:
            urgency = rule.escalated_urgency
        floor = worst_urgency(floor, urgency) if floor else urgency
        res.matched.append({
            "rule_id": rule.rule_id, "category": rule.category, "title": rule.title,
            "urgency": urgency, "base_urgency": rule.urgency, "action": rule.action,
            "evidence": evidence, "min_data": list(rule.min_data), "source": rule.source,
            "review_status": REVIEW_STATUS, "version": RED_FLAG_REGISTRY_VERSION,
        })
        if urgency == Urgency.E0.value:
            res.safety_flags.append(f"红旗:{rule.rule_id}")
    if res.matched:
        res.urgency_floor = floor
        res.emergency = floor in (Urgency.E0.value, Urgency.E1.value)
    return res


def rule_count() -> int:
    return len(_RULES)


def pattern_hit(text: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """文本中是否存在**未被否定/既往修饰**的触发词。

    供低风险路径的排除项等使用：“没有血便黑便”不能当成“有血便/黑便”。
    """
    compiled = _compile(tuple(patterns))
    for p in compiled:
        for m in p.finditer(text):
            if not _window_negated(text, m):
                return True
    return False


# --------------------------------------------------------------------------- 规则表
# 覆盖第 5 节要求的心脑血管 / 呼吸过敏 / 感染 / 神经脊髓 / 腹部代谢 / 妇产儿科 /
# 外伤出血血栓 / 肿瘤血液移植 / 精神安全 / 眼耳时敏器官场景；每条可单独版本化。
_RULES: tuple[RedFlagRule, ...] = (
    # ---- 心脑血管 ----
    RedFlagRule("RF-CARD-ACS", "cardio", "疑似急性冠脉综合征（胸痛/胸闷伴危险表现）", "E0",
                "立即拨打{emergency_number}，保持静坐或半卧、不要自行驾车；不要因为「休息一下就好」而等待。",
                all_of=((r"胸痛|胸闷|胸口(疼|痛|压|闷)|心前区(疼|痛)",),
                        (r"冷汗|出冷汗|大汗|濒死感|压榨|放射|左肩|后背|气短|呼吸困难|喘不上气|恶心",)),
                none_of=(r"(以前|之前|曾经|既往)(也?有过?|得过?|有)",)),
    RedFlagRule("RF-CARD-SYNCOPE", "cardio", "晕厥/一过性意识丧失", "E1",
                "尽快（今天内）到有急诊的医院评估，不要独自活动或驾车；再次晕厥立即拨打{emergency_number}。",
                any_of=(r"(突然|忽然|莫名)晕倒|晕厥|眼前发黑.*倒地|意识丧失|短暂失去意识",),
                none_of=(r"(没有|从未|否认)(晕倒|晕厥|意识丧失)",)),
    RedFlagRule("RF-CARD-ARRHYTHMIA", "cardio", "严重心律失常/循环不稳征象", "E1",
                "尽快到急诊评估（今天内），不要剧烈活动；伴晕厥或胸痛立即拨打{emergency_number}。",
                all_of=((r"心跳(很快|特别快|乱|忽快忽慢)|心慌|心悸|心率\s*\d{3}",),
                        (r"晕|眼前发黑|黑蒙|胸闷|气短|乏力到不能动|出冷汗",))),
    # ---- 神经/脊髓 ----
    RedFlagRule("RF-NEU-STROKE", "neuro", "疑似急性卒中（突发神经功能缺损）", "E0",
                "立即拨打{emergency_number}。记住「最后正常时间」，不要自行驾车、不要在家等好转。",
                any_of=(r"口角(歪|斜)|嘴歪|单侧(无力|麻木|不能动)|一侧(无力|手脚不听)|半边(身|身体)",
                        r"说话(不清|不利索)|言语不清|说不出来话|听不懂话|突发(视力丧失|看不见|视物重影)",
                        r"(走路|站立)(不稳|跑偏)|突然(跌倒|摔倒)伴(无力|说不清)")),
    RedFlagRule("RF-NEU-TIA", "neuro", "一过性卒中样症状（已恢复也不能等）", "E1",
                "尽快（今天内）到有卒中救治能力的医院就诊；症状再次出现立即拨打{emergency_number}。",
                any_of=(r"(手脚麻木|无力|说话不清|看不清).{0,10}(几分钟|一会|半小时|1小时|自行)?(就?(好|恢复|缓解))",
                        r"短暂(性)?(脑缺血|缺血|麻木|无力|说话不清)"),
                none_of=(r"(没有|从未|否认)(麻木|无力|说话不清)",)),
    RedFlagRule("RF-NEU-SEIZURE", "neuro", "新发抽搐/癫痫持续", "E0",
                "立即拨打{emergency_number}。抽搐时保护头部、侧卧防误吸，不要往嘴里塞东西。",
                all_of=((r"抽(搐|风|了)|四肢(僵硬|抖动)|口吐白沫|翻白眼",),),
                none_of=(r"(没有|从未|否认)(抽搐|抽风)",)),
    RedFlagRule("RF-NEU-HEADACHE", "neuro", "剧烈头痛伴神经/意识异常", "E1",
                "尽快（今天内）到急诊评估；出现意识不清、喷射性呕吐、肢体无力立即拨打{emergency_number}。",
                all_of=((r"(剧烈|炸裂|最严重|从没这么|突发)头痛|头痛(剧烈|难忍)",),
                        (r"呕吐|喷射|意识|看不清|脖子(硬|发僵)|说话不清|肢体无力",))),
    RedFlagRule("RF-NEU-CAUDA-EQUINA", "neuro", "疑似马尾/脊髓压迫", "E1",
                "尽快（今天内）到急诊/脊柱外科评估，不要按摩推拿；出现大小便失禁立即拨打{emergency_number}。",
                all_of=((r"(尿不出|尿潴留|大小便失禁|憋不住尿|大便失禁)",),
                        (r"会阴(麻木|没感觉)|鞍区麻木|双腿(无力|麻木|发软)|腿(越来越)?无力",))),
    # ---- 呼吸/过敏 ----
    RedFlagRule("RF-RESP-SEVERE", "resp", "严重呼吸困难/缺氧征象", "E0",
                "立即拨打{emergency_number}，保持坐位、解开衣领，不要独处；家里有医生开过的急救吸入药可按医嘱使用。",
                any_of=(r"(严重|极度)呼吸困难|喘不上气|说不出完整(一句)?话|不能平卧|窒息|憋气憋醒",
                        r"口唇(发紫|发青)|嘴唇发紫|指甲发紫|血氧(饱和度)?\s*(低于|<|=)\s*(9[0-4]|8[0-9]|[0-7][0-9])",
                        r"喉头(发紧|水肿|堵)|喉咙(发紧|堵得)|声音嘶哑伴呼吸困难"),
                none_of=(r"(没有|未)(出现)?(呼吸困难|憋气)",)),
    RedFlagRule("RF-RESP-ANAPHYLAXIS", "resp", "急性严重过敏反应（喉头水肿/多系统）", "E0",
                "立即拨打{emergency_number}，就地平卧、抬高双腿；如医生曾开肾上腺素笔并按指导受过培训，按医嘱使用。",
                all_of=((r"(全身|大片)?(风团|荨麻疹|皮疹|过敏)|眼睛肿|嘴唇肿|舌头肿|喉咙(发紧|肿)",),
                        (r"呼吸困难|喘|憋气|胸闷|头晕|意识|血压低|眼前发黑|喉头",))),
    RedFlagRule("RF-RESP-HEMOPTYSIS", "resp", "咯血伴危险表现", "E1",
                "尽快到有急诊和呼吸/胸科能力的医院评估（今天内）；大咯血或呼吸困难立即拨打{emergency_number}。",
                all_of=((r"(咯血|咳血|痰里带血|咳出血)",), (r"呼吸困难|胸闷|头晕|心慌|血量大|止不住",))),
    # ---- 感染 ----
    RedFlagRule("RF-INF-SEPSIS", "infection", "疑似脓毒症/严重感染", "E1",
                "尽快（今天内）到急诊评估；意识改变、呼吸急促、皮肤花斑或血压低立即拨打{emergency_number}。",
                all_of=((r"(发热|发烧|高热|体温\s*3[89]|寒战)",),
                        (r"意识(模糊|不清)|说胡话|嗜睡(叫不醒)?|反应(差|迟钝)|呼吸(很快|急促)|皮肤(花斑|瘀点)|"
                         r"血压(低|8\d|9\d/)|脱水|尿(很少|明显减少)|持续(加重|恶化)",)),
                escalate_with=("elderly", "infant", "immunosuppressed", "transplant", "pregnancy"),
                escalated_urgency="E0", min_data=("体温", "意识状态")),
    RedFlagRule("RF-INF-INFANT", "infection", "小婴儿发热/喂养异常", "E0",
                "立即带宝宝到有儿科急诊能力的医院就诊（新生儿/3个月以下发热按急诊处理），不要在家退烧观察。",
                all_of=((r"(新生儿|婴儿|宝宝|宝宝|幼儿|2个月|3个月|满月|一个月)",),
                        (r"发热|发烧|体温\s*3[89]|不吃奶|吃奶(减少|变差)|嗜睡|精神差|哭声(弱|尖)|呼吸(快|费力)|吐奶",)),
                requires_modifier="infant"),
    # ---- 腹部/消化/代谢 ----
    RedFlagRule("RF-ABD-ACUTE", "abdomen", "急性腹症/腹膜刺激征", "E1",
                "尽快（今天内）到急诊外科评估，先不要进食、不要自行吃止痛药；腹痛剧烈加重立即拨打{emergency_number}。",
                all_of=((r"(突然|突发|剧烈)腹痛|肚子(疼|痛)(得|到)?(厉害|受不了)|腹(部|部)按(下去)?疼|拒按",),
                        (r"肚子(硬|像板)|不敢动|冒冷汗|恶心|呕吐|发热",))),
    RedFlagRule("RF-ABD-BLEED", "abdomen", "消化道出血伴循环不稳", "E1",
                "尽快（今天内）到急诊评估；呕血、黑便量大或头晕心慌立即拨打{emergency_number}，先不要进食。",
                all_of=((r"(呕血|吐血|黑便|柏油样便|大便(发黑|带血)|便血)",),
                        (r"头晕|心慌|乏力|出冷汗|眼前发黑|站不稳|血压低|面色苍白",)),
                escalate_with=("anticoagulant",), escalated_urgency="E0"),
    RedFlagRule("RF-ENDO-DKA", "metabolism", "糖尿病急性代谢失代偿征象", "E0",
                "立即拨打{emergency_number}。不要自行加药或停药，记录血糖值和测量时间给急救人员。",
                all_of=((r"糖尿病|血糖(高|很高)|血糖\s*(1[6-9]|2\d|3\d)",),
                        (r"呕吐|腹痛|肚子疼|呼吸(深|快|急促)|烂苹果味|意识|嗜睡|口渴.*(严重|厉害)|尿多",)),
                min_data=("血糖值", "测量时间")),
    # ---- 妇产/儿科 ----
    RedFlagRule("RF-OB-BLEED", "obstetric", "妊娠相关腹痛/出血（含疑似宫外孕）", "E0",
                "立即拨打{emergency_number}或直接前往有产科急诊能力的医院；不要自行驾车，保存出血量信息。",
                all_of=((r"(怀孕|妊娠|孕妇|孕\d+周|停经|宫外孕)",),
                        (r"(阴道)?出血|见红|腹痛|肚子疼|肚子(坠|胀)痛|晕厥|晕倒|眼前发黑|肩痛",))),
    RedFlagRule("RF-OB-POSTPARTUM", "obstetric", "产后出血/严重头痛视物异常", "E0",
                "立即拨打{emergency_number}，记录出血量（多久湿透多少片 pads）并告知急救人员。",
                all_of=((r"(产后|月子|刚生完|顺产|剖宫产)",),
                        (r"大出血|出血(很多|多)|恶露.*(大量|增多)|头痛(厉害|剧烈)|看不清|视物模糊|抽搐|意识",))),
    # ---- 外伤/出血/血栓 ----
    RedFlagRule("RF-TRAUMA-HEAD-ANTICOAG", "trauma", "抗凝/抗血小板者头部外伤", "E1",
                "尽快（今天内）到急诊做评估，即使当时清醒、没有明显伤口也要去；出现意识改变、呕吐立即拨打{emergency_number}。",
                all_of=((r"(摔到|撞到|磕到|撞了|摔了).{0,8}(头|脑袋)|头部(外伤|受伤|撞伤|摔伤)",),),
                requires_modifier="anticoagulant"),
    RedFlagRule("RF-TRAUMA-BLEED", "trauma", "严重外伤/活动性出血", "E0",
                "立即拨打{emergency_number}，直接压迫止血、不要拔除刺入物；必要时请旁人协助呼救。",
                any_of=(r"大出血|出血不止|止不住血|动脉出血|喷射(性|状)出血",
                        r"(车祸|摔下|高处坠落|重物砸|被撞).{0,12}(不能动|剧痛|变形|意识|大量出血)"),
                none_of=(r"(没有|未)(大量出血|出血不止)",)),
    RedFlagRule("RF-THROMBOSIS-PE", "vascular", "下肢肿痛合并肺栓塞征象", "E1",
                "尽快（今天内）到急诊评估；出现呼吸困难、胸痛、晕厥立即拨打{emergency_number}，不要按摩患肢。",
                all_of=((r"(一条?腿|单侧下肢|小腿|大腿)(肿|胀|痛|发红|发热)",),
                        (r"呼吸困难|胸痛|胸闷|晕|咳血|气短",))),
    # ---- 肿瘤/血液/移植 ----
    RedFlagRule("RF-ONC-FEBRILE-NEUTROPENIA", "oncology", "化疗后发热（疑似粒缺伴发热）", "E0",
                "立即联系肿瘤科/急诊并按{emergency_number}求助，不要在家退烧观察；带上最近的化疗和血常规记录。",
                all_of=((r"(化疗|放疗|靶向|免疫治疗|抗肿瘤)",),
                        (r"发热|发烧|体温\s*3[89]|寒战|乏力(明显|加重)",)),
                requires_modifier="immunosuppressed"),
    RedFlagRule("RF-ONC-CORD", "oncology", "肿瘤患者新发背痛伴神经症状", "E1",
                "尽快（今天内）到急诊/肿瘤科评估（疑似脊髓压迫），不要推拿；大小便失禁或腿无力加重立即拨打{emergency_number}。",
                all_of=((r"(肿瘤|癌症|癌|化疗|放疗)",),
                        (r"(新发|突然|夜间|进行性)腰背痛|背痛|腰痛",
                         r"腿(无力|麻|不听使唤)|走路(不稳|困难)|大小便(失禁|困难)|尿不出|会阴(麻木)",)),
                requires_modifier="cancer"),
    RedFlagRule("RF-TRANSPLANT-INFECTION", "transplant", "移植后感染/排异征象", "E1",
                "尽快（今天内）联系移植中心或到急诊评估；高热、呼吸困难、意识改变立即拨打{emergency_number}。",
                all_of=((r"移植(后|术后)|肾移植|肝移植|骨髓移植|造血干细胞移植",),
                        (r"发热|发烧|寒战|咳嗽|呼吸困难|尿少|水肿|皮疹|黄疸|腹泻|乏力",)),
                requires_modifier="transplant"),
    RedFlagRule("RF-HEME-BLEED", "hematology", "血液病/血小板异常伴出血", "E1",
                "尽快（今天内）到急诊/血液科评估；活动性大出血或意识改变立即拨打{emergency_number}。",
                all_of=((r"(白血病|淋巴瘤|骨髓瘤|血小板(低|减少)|血友病|贫血很重)",),
                        (r"出血|瘀斑|牙龈出血|鼻血止不住|黑便|血尿|皮下出血点",))),
    # ---- 精神健康/安全 ----
    RedFlagRule("RF-MH-SUICIDE", "mental", "自伤/自杀风险（当前意图或计划）", "E0",
                "现在最重要的事是保证安全：请立即拨打{emergency_number}或请身边人陪同前往急诊；不要独处，"
                "把可能伤害自己的物品交给家人保管。",
                any_of=(r"想(自杀|死|结束生命)|不想活|活着没意思|自杀(计划|想法|念头)|割腕|跳楼|喝药(自尽|自杀)|"
                        r"我已经想好(怎么|了)|遗书|告别",
                        r"想(伤害|杀了)自己|控制不住想死"),
                none_of=(r"(没有|从未|否认)(想自杀|自杀念头)",)),
    # ---- 眼耳时敏器官 ----
    RedFlagRule("RF-EYE-VISION-LOSS", "eye", "突发视力丧失/视网膜脱离征象", "E1",
                "尽快（今天内）到眼科急诊评估，不要揉眼、不要自行滴药；突发无痛单眼视力丧失按急诊处理。",
                any_of=(r"(突然|突发|一下子)[^，。]{0,4}眼[^，。]{0,6}(看不见|看不到|视力下降|失明|黑影|闪光)",
                        r"(眼前|视野)(黑影|闪光|遮挡|幕布)|视网膜脱离")),
    RedFlagRule("RF-ENT-AIRWAY", "ent", "急性气道梗阻/严重颈部感染", "E0",
                "立即拨打{emergency_number}；保持坐位、不要强行吞咽，出现窒息立即请旁人协助急救。",
                any_of=(r"咽不下去|吞口水都(疼|困难)|张不开嘴|脖子肿(得厉害|大)|呼吸(困难|费力)伴咽痛",
                        r"异物(卡|堵)在(喉咙|气管)|说不出话.*喉咙"),
                none_of=(r"(没有|未)(呼吸困难|窒息)",)),
)


# 常见低风险路径的“必须就医/升级”提示（供提示词参考；不是执业规则）
GENERAL_ESCALATION_HINTS: tuple[str, ...] = (
    "症状较前明显加重，或出现新的不适",
    "呼吸困难、意识改变、晕厥、大出血等危险表现",
    "儿童、老人、孕产妇、免疫抑制等人群症状变化快",
    "超过建议观察时限仍未改善，或反复发作",
)


def screen_summary(res: ScreenResult) -> str:
    if not res.matched:
        return "未命中已知红旗规则"
    return "；".join(f"{m['rule_id']} {m['title']}({m['urgency']})" for m in res.matched)
