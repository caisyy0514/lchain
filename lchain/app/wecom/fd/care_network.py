"""就医网络与费用测算（演示数据，待真实接入）。

- 机构目录、医保参数、常用检查价格都是 `data/care_network.json` 里的演示数据，
  上线前必须替换为当地医保目录 + 机构能力目录，并经商务/医学组审核；
- 只根据结构化计划做确定性推荐：机构级别（基层/二级/三级）+ 科室 + 检查能力 + 生活区域；
- 费用只是「按示例政策的估算」（起付线/报销比例/价格），卡片会附免责说明，
  不声称已核实号源，也不替代医院结算。

推荐原则：能力匹配的前提下，优先「同区 + 不过度升级」——本次检查与处理用什么级别
就推荐什么级别，并给出升级到更高级别机构的个人自付对照。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.wecom.fd.contracts import ActionablePlan, CareMode, CareOptions, FacilityOption, Urgency

_DATA_PATH = Path(__file__).parent / "data" / "care_network.json"

LEVEL_RANK = {"基层": 1, "二级": 2, "三级": 3}

# 检查名 → 价格表 key（按顺序匹配，命中即止）
_TEST_KEYS: tuple[tuple[str, str], ...] = (
    (r"血常规", "血常规"),
    (r"C反应蛋白|CRP", "C反应蛋白"),
    (r"胸部CT|肺CT", "胸部CT"),
    (r"胸部X线|胸片|胸部X光", "胸部X线"),
    (r"超声", "腹部超声"),
    (r"肝功能", "肝功能"),
    (r"肾功能|肌酐", "肾功能"),
    (r"电解质|血钾", "电解质"),
    (r"尿常规", "尿常规"),
    (r"心电图", "心电图"),
    (r"心肌酶|肌钙蛋白", "心肌酶"),
    (r"流感|新冠|病原", "病原检测"),
    (r"血脂", "血脂"),
    (r"血糖|糖化", "血糖"),
    (r"大便常规|便常规|潜血", "大便常规"),
    (r"血氧", "血氧"),
    (r"CT|核磁|MRI|磁共振|X线|影像|监测|查体", "其他检查"),
)


@lru_cache(maxsize=1)
def _data() -> dict[str, Any]:
    return json.loads(_DATA_PATH.read_text(encoding="utf-8"))


def network_version() -> str:
    return str(_data().get("version", ""))


def facility_count() -> int:
    return len(_data().get("facilities") or [])


def _rank(level_text: str | None) -> int:
    text = level_text or ""
    for name, rank in LEVEL_RANK.items():
        if name in text:
            return rank
    return 2  # 级别未知时按二级处理（不过度升级也不过度降级）


def detect_district(profile: str) -> str:
    """从自由文本档案里找生活区域（区/县名）；找不到用默认区域。"""
    for d in _data()["districts"]:
        name = str(d.get("name") or "")
        if name and name in (profile or ""):
            return name
    return next((str(d["name"]) for d in _data()["districts"] if d.get("default")), "朝阳区")


def _matched_keys(tests: list[str]) -> list[str]:
    keys: list[str] = []
    for t in tests or []:
        for pattern, key in _TEST_KEYS:
            if re.search(pattern, t):
                if key not in keys:
                    keys.append(key)
                break
    return keys


def _travel(facility: dict[str, Any], district: str) -> int | None:
    minutes = (facility.get("travel_minutes") or {}).get(district)
    return int(minutes) if minutes else None


def registration_fee(level: str) -> float:
    """挂号费（按机构级别）。"""
    rules = _data()["policy"]["levels"].get(level or "二级") or _data()["policy"]["levels"]["二级"]
    return float(rules["registration"])


def tests_cost(level: str, tests: list[str]) -> float:
    """一组检查项在该级别的价格合计（未知项按“其他检查”计价）。"""
    prices = _data()["policy"]["prices"]
    level = level if level in _data()["policy"]["levels"] else "二级"
    return float(sum(float(prices.get(k, prices["其他检查"])[level]) for k in _matched_keys(tests)))


def _estimate(policy: dict[str, Any], facility: dict[str, Any], keys: list[str]) -> tuple[float, float, float]:
    level = str(facility.get("level") or "二级")
    rules = policy["levels"][level]
    prices = policy["prices"]
    items = [float(prices.get(k, prices["其他检查"])[level]) for k in keys]
    total = float(rules["registration"]) + sum(items)
    reimbursed = round(max(0.0, total - float(rules["deductible"])) * float(rules["reimbursement"]), 2)
    return total, reimbursed, round(total - reimbursed, 2)


def _option(policy: dict[str, Any], facility: dict[str, Any], district: str, dept: str, keys: list[str],
            *, recommended: bool, required: int, verified: bool = False,
            covers_needs: bool = True) -> FacilityOption:
    total, reimbursed, out_of_pocket = _estimate(policy, facility, keys)
    travel = _travel(facility, district)
    reasons: list[str] = []
    if verified:
        reasons.append("已在本计划中确认")
    if facility.get("district") == district:
        reasons.append("与您同区")
    elif travel:
        reasons.append(f"跨区约 {travel} 分钟")
    if _rank(facility.get("level")) == required and required >= 1:
        reasons.append(f"{facility.get('level')}即可完成本次检查与处理")
    else:
        reasons.append(f"{facility.get('level')}具备所需能力")
    if facility.get("insurance"):
        reasons.append(str(facility["insurance"]))
    # 科室取机构自己挂出来的对应科室名（“发热门诊或呼吸内科” → “呼吸内科”），标题才准确
    depts = facility.get("departments") or []
    matched_dept = next((d for d in depts if dept and (dept in d or d in dept)), "")
    return FacilityOption(
        facility_id=str(facility.get("id") or ""),
        name=str(facility.get("name") or ""),
        level=str(facility.get("level") or ""),
        level_rank=_rank(facility.get("level")),
        district=str(facility.get("district") or ""),
        department=matched_dept or dept or str(depts[0] if depts else ""),
        address=str(facility.get("address") or ""),
        entry_method=str(facility.get("entry_method") or ""),
        insurance=str(facility.get("insurance") or ""),
        travel_minutes=travel,
        reason="；".join(reasons),
        recommended=recommended,
        covers_needs=covers_needs,
        total=total,
        reimbursed=reimbursed,
        out_of_pocket=out_of_pocket,
    )


def recommend(plan: ActionablePlan, profile: str = "") -> CareOptions | None:
    """按患者区域/级别/科室/检查能力推荐机构，并给出费用测算与升级对照。

    急症（E0/E1）、居家观察、健康教育、线上复诊不适用（返回 None）。
    """
    if plan.urgency in (Urgency.E0.value, Urgency.E1.value):
        return None
    if plan.care_mode in (CareMode.SELF_CARE.value, CareMode.HEALTH_EDUCATION.value,
                          CareMode.EXPERT_SECOND_OPINION.value):
        return None
    if (plan.destination.care_setting or "") == "online":
        return None

    data = _data()
    policy = data["policy"]
    district = detect_district(profile)
    keys = _matched_keys(plan.tests_needed)
    required = max(1, _rank(plan.facility_level))
    dept = (plan.destination.department or "").strip()
    facilities: list[dict[str, Any]] = data["facilities"]

    def covers(f: dict[str, Any]) -> bool:
        # 未识别到具体能力的检查归入“其他检查”，不应因它把机构排除
        caps = f.get("capabilities") or []
        return all(k in caps for k in keys if k != "其他检查")

    def has_dept(f: dict[str, Any]) -> bool:
        if not dept:
            return True
        return any(dept in d or d in dept for d in (f.get("departments") or []))

    pool = [f for f in facilities if covers(f) and has_dept(f) and _rank(f.get("level")) >= required]
    if not pool:  # 科室名不完全一致时放宽科室，只按能力+级别
        pool = [f for f in facilities if covers(f) and _rank(f.get("level")) >= required]
    if not pool and not keys:  # 无检查项时只按科室/级别
        pool = [f for f in facilities if has_dept(f) and _rank(f.get("level")) >= required]
    if not pool:
        return None

    pool.sort(key=lambda f: (f.get("district") != district, _rank(f.get("level")) - required,
                             _travel(f, district) or 99, str(f.get("name") or "")))
    verified_name = plan.destination.facility_name if plan.destination.verified else None
    rec = next((f for f in pool if verified_name and f.get("name") == verified_name), pool[0])

    def covers_needs(f: dict[str, Any]) -> bool:
        caps = f.get("capabilities") or []
        return all(k in caps for k in keys if k != "其他检查")

    options = [_option(policy, rec, district, dept, keys, recommended=True, required=required,
                       verified=bool(verified_name and rec.get("name") == verified_name),
                       covers_needs=covers_needs(rec))]

    alt = next((f for f in pool if _rank(f.get("level")) > _rank(rec.get("level"))), None)
    if alt is not None:
        options.append(_option(policy, alt, district, dept, keys, recommended=False, required=required,
                               covers_needs=covers_needs(alt)))
    saving = None
    if len(options) == 2:
        delta = round(options[1].out_of_pocket - options[0].out_of_pocket, 2)
        saving = delta if delta > 0 else None
    needs = ([options[0].department] if options[0].department else []) + keys
    return CareOptions(region=f"示例市{district}", insurance=str(policy.get("insurance") or ""),
                       options=options, needs=needs, saving=saving,
                       plan_note="升级到更高级别医院不会改变本次检查与处理方案，但按示例医保政策个人自付更高。",
                       disclaimer=str(data.get("disclaimer") or ""))
