"""在企微群板块生成一套「分级诊疗样例」：每个群完整跑一遍多轮问诊并得到结论。

用法（需要 web 服务与 MySQL 已启动）：
    PYTHONPATH=. .venv/bin/python scripts/seed_fd_samples.py                       # 生成/覆盖全部样例群
    PYTHONPATH=. .venv/bin/python scripts/seed_fd_samples.py --clean               # 先删除样例群再生成
    PYTHONPATH=. .venv/bin/python scripts/seed_fd_samples.py --only=e2-fever       # 只跑一个（群 id 包含关键字）
    BASE=http://127.0.0.1:8000 .venv/bin/python scripts/seed_fd_samples.py

覆盖：E0 立即急救（胸痛/卒中/疑似肺栓塞）、E1 尽快急诊（晕厥）、E2 当日就医（高热）、
E3 限时面诊（上腹痛）、E4 居家观察（上呼吸道/腹泻，带主动追问任务）、E5 稳定复诊（高血压）、
EU 暂不能安全分级（透析乏力，关键信息不足 → 人工复核）。

每个群会依次发送患者消息，直到家庭医生给出结论（或消息用完）：
- 结论包含：病情说明、可能的方向、需要优先排除的风险、机构级别/科室、建议检查、确诊方式；
- 结论已提交平台真人医生审核（不再引导患者去线上找医生）；
- 轻症结论会创建居家观察任务，由定时任务到点主动追问。

注意：会调用真实模型（每个群 4–10 次 LLM 调用）；临床等级以实际运行结果为准。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "http://127.0.0.1:8000"
NP = {"http": None, "https": None}

# (group_id, 群名称, 患者档案, 发送者, [患者依次发送的消息...])
SCENARIOS: list[tuple[str, str, str, tuple[str, str], list[str]]] = [
    ("sample-e0-chest", "样例·E0 立即急救｜胸痛伴冷汗",
     "患者：张某，男，56 岁；高血压 10 年，吸烟 30 年。", ("patient_zhang", "张先生"),
     ["昨晚开始胸痛，出冷汗，喘不上气，现在还闷得厉害"]),
    ("sample-e0-stroke", "样例·E0 立即急救｜突发口角歪斜",
     "患者：王某，女，72 岁；房颤，长期服用利伐沙班。", ("family_wang", "王女士女儿"),
     ["我妈今天早上突然口角歪斜，说话不清楚，右手抬不起来"]),
    ("sample-e1-pe", "样例·E0 立即急救｜疑似肺栓塞（腿肿痛+气短）",
     "患者：陈某，女，58 岁；2 型糖尿病；半月前做过膝关节手术。", ("patient_chen", "陈女士"),
     ["左小腿又肿又痛三天了，今天突然觉得胸闷气短"]),
    ("sample-e1-syncope", "样例·E1 尽快急诊｜晕倒过一次",
     "患者：刘某，男，45 岁；无基础病。", ("patient_liu", "刘先生"),
     ["今天上午突然晕倒了一次，现在醒了，还有点头晕乏力"]),
    ("sample-e2-fever", "样例·E2 当日就医｜高热三天不退",
     "患者：赵某，男，30 岁；无基础病。住址：示例市朝阳区。", ("patient_zhao", "赵先生"),
     ["发烧 38.9 三天了，一直退不下来，咳嗽也加重了，没有别的症状",
      "没有呼吸困难，也没有胸痛，吃饭喝水还可以",
      "平时身体挺好，没有基础病，也没吃什么药",
      "你就按现在的情况告诉我该怎么办吧"]),
    ("sample-e3-abdomen", "样例·E3 限时面诊｜反复上腹痛两周",
     "患者：孙某，女，41 岁；无基础病。住址：示例市朝阳区。", ("patient_sun", "孙女士"),
     ["右上腹隐痛两周了，饭后明显，没有发烧也没有呕吐",
      "没有黑便、便血，也没有皮肤或眼睛发黄",
      "没做过检查，也没吃止痛药或别的药",
      "平时没有反酸烧心，大便也正常，家里没有胆囊病史",
      "你就按现在的情况告诉我该怎么办吧"]),
    ("sample-e4-uri", "样例·E4 居家观察｜轻症上呼吸道感染",
     "患者：周某，女，34 岁；无基础病。", ("patient_zhou", "周女士"),
     ["这两天嗓子疼、流鼻涕，低烧 37.8，没有别的症状",
      "没有呼吸困难、胸痛，精神还可以",
      "平时身体挺好，没有基础病",
      "没怀孕也没吃什么药，家里有退烧药",
      "你就按现在的情况告诉我该怎么办吧"]),
    ("sample-e4-gi", "样例·E4 居家观察｜轻度腹泻",
     "患者：吴某，男，28 岁；无基础病。", ("patient_wu", "吴先生"),
     ["拉肚子两天了，一天三四次，没有发烧，还能正常喝水吃饭",
      "没有血便黑便，也没有剧烈腹痛和呕吐",
      "没有基础病，也没吃药",
      "家里有口服补液盐，也能正常喝水",
      "你就按现在的情况告诉我该怎么办吧"]),
    ("sample-e5-hypertension", "样例·E5 稳定复诊｜高血压随访",
     "患者：郑某，男，62 岁；高血压 3 年，规律服药。", ("patient_zheng", "郑先生"),
     ["高血压一直吃氨氯地平 5mg，最近血压 135/85 左右，上次复诊是三个月前，想问下次复诊什么时候",
      "没有头晕头痛胸闷，也没有其他不舒服",
      "复查结果都正常，也没有换过药",
      "你就按现在的情况告诉我下次什么时候复诊吧"]),
    ("sample-eu-l", "样例·EU 暂不能分级｜透析患者乏力（需人工复核）",
     "患者：李某，男，60 岁；慢性肾衰，每周透析三次。住址：示例市海淀区。", ("patient_li", "李先生"),
     ["我每周做三次透析，最近两天有点乏力，需要提前去医院吗",
      "我也说不太清楚，就是感觉和平时不太一样",
      "反正就是没精神，别的我也说不上来",
      "你就按现在知道的告诉我该怎么办吧",
      "我也没什么能补充的了"]),
]

MANAGER = {"case_manager_id": "mgr_li", "case_manager_name": "李经理"}
MODE_ZH = {"emergency": "立即急救", "in_person": "线下就医", "self_care": "居家观察",
           "stable_followup": "稳定复诊", "initial_navigation": "人工/线下评估",
           "expert_second_opinion": "二诊", "mdt": "MDT"}


def _send(group_id: str, name: str, profile: str, sender: tuple[str, str], content: str) -> dict:
    r = requests.post(f"{BASE}/api/wecom/reply", timeout=300, proxies=NP, json={
        "group_id": group_id, "sender_id": sender[0], "sender_name": sender[1], "sender_role": "patient",
        "content": content,
        "group": {"name": name, "profile": profile, **MANAGER},
    })
    return r.json()


def _plan(group_id: str) -> dict:
    p = requests.get(f"{BASE}/api/wecom/groups/{group_id}/fd-plan", timeout=30, proxies=NP).json()
    return {"case": p.get("case") or {}, "plan": (p.get("case") or {}).get("plan") or {},
            "observation": p.get("active_observation") or {}}


def _clean(group_ids: list[str]) -> None:
    requests.post(f"{BASE}/api/wecom/groups/delete", json={"group_ids": group_ids}, timeout=60, proxies=NP)
    sql = ("DELETE FROM wecom_fd_observations WHERE group_id IN (" +
           ",".join(f"'{g}'" for g in group_ids) + ");"
           "DELETE FROM wecom_fd_cases WHERE group_id IN (" +
           ",".join(f"'{g}'" for g in group_ids) + ");")
    subprocess.run(["docker", "exec", "lchain-mysql", "mysql", "-ulchain", "-plchain", "lchain", "-e", sql],
                   capture_output=True, check=False)


def _seed(sc: tuple[str, str, str, tuple[str, str], list[str]]) -> dict:
    gid, name, profile, sender, messages = sc
    rounds = 0
    for i, msg in enumerate(messages):
        r = _send(gid, name, profile, sender, msg)
        rounds = i + 1
        info = _plan(gid)
        if info["plan"].get("urgency") or r.get("route") in ("severe",):
            break
        if i < len(messages) - 1:
            time.sleep(1)   # 模拟患者看消息后再回答
    info = _plan(gid)
    pl, obs = info["plan"], info["observation"]
    short = name.split("｜")[-1]
    # 群名称按实际判级回写，避免“标题说 E4、实际判 E3”的误导
    if pl.get("urgency"):
        new_name = f"样例·{pl['urgency']} {MODE_ZH.get(pl.get('care_mode'), pl.get('care_mode') or '')}｜{short}"
    else:
        new_name = f"样例·问诊中｜{short}"
    requests.put(f"{BASE}/api/wecom/groups/{gid}", json={"name": new_name}, timeout=30, proxies=NP)
    return {"group_id": gid, "name": new_name, "rounds": rounds,
            "urgency": pl.get("urgency"), "care_mode": pl.get("care_mode"),
            "observation": obs.get("state"), "next_review_at": obs.get("next_review_at"),
            "department": (pl.get("destination") or {}).get("department"),
            "facility_level": pl.get("facility_level"),
            "condition_summary": pl.get("condition_summary")}


def main() -> None:
    only = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--only=")), "")
    scenarios = [s for s in SCENARIOS if only.lower() in s[0].lower()]
    if not scenarios:
        print(f"没有匹配的样例群：{only}（可选：{', '.join(s[0] for s in SCENARIOS)}）")
        return
    if "--clean" in sys.argv:
        _clean([s[0] for s in scenarios])
        print(f"已清除旧样例群：{', '.join(s[0] for s in scenarios)}")
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(4, len(scenarios))) as ex:
        for row in ex.map(_seed, scenarios):
            rows.append(row)
            print(f"  {row['group_id']:<24} {str(row['urgency']):<4} {str(row['care_mode']):<20} "
                  f"{row['rounds']} 轮" + (f" / 观察 {row['observation']}" if row["observation"] else ""),
                  flush=True)
    with open("/tmp/fd_samples_summary.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"\n完成：{len(rows)} 个样例群。打开 http://127.0.0.1:8000/wecom 查看。")


if __name__ == "__main__":
    main()
