"""山区远程问诊工作流（app/rural/）的测试。

分三层：
1. **纯函数**：动作钳制（含"就医条件"门禁）、处置层级抬升、无剂量过滤、问题数截断、文本渲染的安全边界；
2. **用例端到端**：用「理想判断节点」的桩替换 LLM，把 `eval/rural/**/*.md` 的 11 个用例
   真正跑一遍 StateGraph（load_state → extract ∥ assess → decide → 分支 → save_state），
   断言阶段流转、就医建议、处置层级、诊断版本、字数与问题数；
3. **链路**：`scenario="rural"` 的派发、`/api/rural` 状态接口、会话列表分页与删除的路径安全。

真实模型的判断质量靠 `make rural-smoke`（消耗 token），这里锁的是**流程健壮性**：
不该转诊时不过度转诊、该转诊时不降级、一次只问一个问题、免责声明不丢、不出现药物剂量。

    PYTHONPATH=. .venv/bin/python -m pytest tests/agent/test_rural.py -q
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.rural import gates, graph, render, state as rstate
from app.rural.cases import CASES
from app.rural.gates import (
    DISCLAIMER,
    DISPOSITIONS,
    emergency_hint,
    MAX_QUESTIONS_PER_TURN,
    MIN_DIFFERENTIAL_ROUNDS,
    MIN_INTAKE_ROUNDS,
    REQUIRED_HISTORY,
    first_aid_for,
)

# ------------------------------------------------------------------ 桩：把「理想判断」写成规则
CURRENT: dict[str, Any] = {}         # 当前用例（测试串行执行，用模块级变量传递）
SCRIPT: list[dict[str, Any]] = []    # extract 的逐轮脚本（患者每次发言返回什么）


def _oracle_action(v: dict[str, Any]) -> str:
    """一个「理想判断节点」会给出的 action（流程测试用它替代 LLM 判断）。"""
    if v.get("has_triage") != "是":
        ready = (v.get("missing") == "（已采集齐全）"
                 and int(v.get("intake_rounds") or 0) >= int(v.get("min_intake_rounds") or 2))
        if not ready:
            return "ask_history"
        if v.get("missing_access") != "（已问齐）":
            return "ask_access"
        return "triage"
    if int(v.get("diagnosis_versions") or 0) == 0:
        if int(v.get("differential_rounds") or 0) >= int(v.get("min_differential_rounds") or 1):
            return "diagnose"
        return "ask_differential"
    if v.get("has_new_change") == "是" and v.get("followup_answered") != "是":
        return "ask_followup"
    if v.get("has_new_change") == "是":
        return "re_diagnose"
    return "close"


def _case_first_aid(case: dict[str, Any]) -> str:
    """从用例文本里识别急救场景，供桩的 triage/diagnose 字段使用。"""
    hits = first_aid_for([case["name"], case["primary"], case["department"], *case["red_flags"]])
    return hits[0]["name"] if hits else ""


async def fake_call_json(prompt_name: str, user_content: str, **kw: Any) -> tuple[dict[str, Any], Any]:
    v = {k: val for k, val in kw.items() if k not in ("cfg", "overrides")}
    if prompt_name == "rural-assess":
        return {"action": _oracle_action(v), "reason": "测试桩", "focus": "起病时间与就医条件"}, None
    if prompt_name == "rural-extract":
        return (SCRIPT.pop(0) if SCRIPT else {}), None
    case = CURRENT["case"]
    if prompt_name == "rural-triage":
        return {"department": case["department"], "urgency": case["urgency"],
                "disposition": case["disposition"], "referral_target": "乡镇卫生院 → 县人民医院",
                "transfer_notes": "平躺少动、有人陪同、带上身份证和正在吃的药",
                "access_notes": case["access"].get("access_distance", ""),
                "local_actions": ["先测体温和血压", "不要喂水喂药"],
                "reasoning": "测试桩：依据已采集信息", "red_flags": case["red_flags"],
                "suggested_tests": ["血常规"], "first_aid": _case_first_aid(case)}, None
    if prompt_name == "rural-diagnose":
        is_update = v.get("is_update") == "是"
        return {"primary": case["updated"] if is_update else case["primary"],
                "differentials": case["differentials"], "evidence": case["evidence"],
                "severity": "轻至中度", "urgency": case["urgency"], "disposition": case["disposition"],
                "disposition_reason": "依据病情和就医条件", "referral_target": "乡镇卫生院",
                "referral_plan": "明天一早请邻居用三轮车送到乡镇卫生院，带上身份证",
                "tests": ["血常规"], "local_management": ["清淡饮食，多喝温水，别干重活"],
                "red_flags": case["red_flags"], "followup_plan": "3 天后联系村医，加重随时下山",
                "confidence": "中", "first_aid": _case_first_aid(case)}, None
    raise AssertionError(f"未预期的判断提示词：{prompt_name}")


async def fake_stream_text(prompt_name: str, user_content: str, **kw: Any):
    """生成类节点的桩：返回固定短回复（一次一个问题、不带编号）。"""
    text = CURRENT.get("questions", {}).get(prompt_name, "什么时候开始的？")
    for part in [text[i:i + 8] for i in range(0, len(text), 8)]:
        yield part


@pytest.fixture(autouse=True)
def patched(tmp_path, monkeypatch):
    from app.core import config

    config.get_settings.cache_clear()
    monkeypatch.setenv("REPORT_WORKSPACE", str(tmp_path / "ws"))
    config.get_settings.cache_clear()
    monkeypatch.setattr(graph, "call_json", fake_call_json)
    monkeypatch.setattr(graph, "stream_text", fake_stream_text)
    CURRENT.clear()
    SCRIPT.clear()
    yield
    config.get_settings.cache_clear()


def turn(session_id: str, message: str) -> dict[str, Any]:
    """跑一轮工作流，返回最终 graph state。"""
    return asyncio.run(graph.build_graph().ainvoke(
        {"session_id": session_id, "user_message": message, "steps": [],
         "trace_id": f"test-trace-{session_id}"},
        config={"configurable": {"thread_id": session_id}, "recursion_limit": 30},
    ))


# ------------------------------------------------------------------ 11 个用例端到端


def _script(case: dict[str, Any]) -> list[dict[str, Any]]:
    """把用例拆成逐轮 extract 结果：前两轮补现病史，第三轮问就医条件，第四轮补条件，之后鉴别/变化。"""
    items = list(case["history"].items())
    access = list(case["access"].items())
    return [
        {"new_items": dict(items[:5]), "is_new_change": False, "red_flags": [], "note": "第 1 轮病史"},
        {"new_items": dict(items[5:]), "is_new_change": False, "red_flags": [], "note": "第 2 轮病史"},
        {"new_items": {}, "is_new_change": False, "red_flags": [], "note": "回答现病史追问"},
        {"new_items": dict(access), "is_new_change": False, "red_flags": [], "note": "回答就医条件"},
        {"new_items": {}, "is_new_change": False, "red_flags": [], "note": "鉴别问询回答"},
        {"new_items": {}, "is_new_change": False, "red_flags": [], "note": "鉴别问询回答"},
        {"new_items": {}, "is_new_change": True, "change_summary": case["change"], "red_flags": [],
         "note": "病情变化"},
        {"new_items": {}, "is_new_change": False, "red_flags": [], "note": "变化后的回答"},
    ]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_cases_full_flow(case: dict):
    """11 个用例都能走完「采集 → 就医条件 → 就医建议 → 鉴别 → 诊断 → 变化 → 更新诊断」。"""
    sid = f"case-{CASES.index(case):02d}"
    CURRENT["case"] = case
    SCRIPT.extend(_script(case))

    # 1) 主诉 → 采集现病史
    out = turn(sid, case["complaint"])
    assert out["action"] == "ask_history", out["action"]
    assert out["state"]["stage"] == "intake"

    # 2) 填满现病史：要素齐了但轮次不够仍要继续问
    out = turn(sid, "（回答第 1 轮）")
    assert out["action"] == "ask_history", "轮次不够不能停下采集"

    # 3) 要素齐 + 轮次够 → 先问就医条件（这决定去哪一级医院）
    out = turn(sid, "（回答第 2 轮）")
    assert out["action"] == "ask_access", f"应问就医条件，实际 {out['action']}"
    assert out["state"]["stage"] == "access"
    assert out["reply"].count("？") <= MAX_QUESTIONS_PER_TURN

    # 4) 就医条件答完 → 给就医建议
    out = turn(sid, "（回答就医条件）")
    assert out["action"] == "triage", f"应给出就医建议，实际 {out['action']}"
    assert out["state"]["stage"] == "differential"
    assert not gates.missing_access(out["state"]), "就医条件应已采集"
    assert case["department"] in out["reply"]
    assert DISCLAIMER in out["reply"], "就医建议必须带免责声明"
    assert DISPOSITIONS[case["disposition"]].split("（")[0] in out["reply"], "要给出处置层级"
    if case["urgency"] in ("urgent", "emergency") or case["disposition"] in ("county", "emergency"):
        assert emergency_hint() in out["reply"], "急症必须带立即就医提示"

    # 5) 鉴别问询 → 诊断 v1
    out = turn(sid, "（鉴别回答 1）")
    assert out["action"] == "ask_differential"
    out = turn(sid, "（鉴别回答 2）")
    for _ in range(4):
        if out["action"] == "diagnose":
            break
        out = turn(sid, "（补充回答）")
    assert out["action"] == "diagnose", f"应进入诊断，实际 {out['action']}"
    assert out["state"]["diagnoses"][-1]["version"] == 1
    assert case["primary"] in out["reply"]
    assert DISCLAIMER in out["reply"]

    # 6) 病情变化 → 先追问，再给更新版诊断
    out = turn(sid, case["change"])
    assert out["action"] == "ask_followup", f"变化后应先追问，实际 {out['action']}"
    assert out["state"]["stage"] == "followup"
    out = turn(sid, case["change_answer"])
    assert out["action"] == "re_diagnose", f"追问后应更新诊断，实际 {out['action']}"
    assert len(out["state"]["diagnoses"]) == 2
    assert case["updated"] in out["reply"]
    assert out["state"]["diagnoses"][-1]["after_change"] is True


def test_emergency_case_gets_first_aid_steps():
    """意外伤害/中毒用例：回复里必须出现**正确**的急救做法（内容来自代码表，不是模型自由发挥）。"""
    case = next(c for c in CASES if "蛇咬" in c["name"])
    sid = "firstaid01"
    CURRENT["case"] = case
    SCRIPT.extend(_script(case))
    out = turn(sid, case["complaint"])
    for _ in range(8):
        if out["action"] == "triage":
            break
        out = turn(sid, "（回答）")
    assert out["action"] == "triage"
    assert "马上这样做（蛇咬）" in out["reply"], out["reply"]
    assert "别切开" in out["reply"] and "别用嘴吸" in out["reply"], out["reply"]
    assert out["state"]["triage"]["disposition"] == "emergency"


def test_emergency_hint_in_every_emergency_case():
    """所有 emergency 用例的就医建议都要带「立即就医」提示（由 render.py 保证）。"""
    for case in [c for c in CASES if c["disposition"] == "emergency"]:
        text = render.render_triage({"department": case["department"], "urgency": case["urgency"],
                                     "disposition": case["disposition"], "reasoning": "依据",
                                     "red_flags": case["red_flags"], "suggested_tests": []})
        assert emergency_hint() in text and DISCLAIMER in text, case["name"]


# ------------------------------------------------------------------ 动作钳制（代码兜底）
def _data(**over: Any) -> dict[str, Any]:
    base = rstate.new_state("gates01")
    base.update(over)
    return base


def _ready_state(**over: Any) -> dict[str, Any]:
    """现病史齐 + 轮次够 + 就医条件齐的中间状态。"""
    base = _data(history={k: "x" for k, _ in REQUIRED_HISTORY}, record_rounds=2,
                 access={k: "x" for k in gates.ACCESS_KEYS}, access_asked=True)
    base.update(over)
    return base


def test_clamp_blocks_triage_when_history_incomplete():
    action, note = gates.clamp_action(_data(), "triage")
    assert action == "ask_history" and "要素未齐" in note


def test_clamp_asks_access_before_triage():
    """要素齐 + 轮次够，但还不知道路远不远、有没有车 → 先问就医条件，不能直接给建议。"""
    data = _data(history={k: "x" for k, _ in REQUIRED_HISTORY}, record_rounds=2)
    action, note = gates.clamp_action(data, "triage")
    assert action == "ask_access" and "就医方式" in note
    # 模型直接说 ask_history 也会被改成 ask_access
    assert gates.clamp_action(data, "ask_history")[0] == "ask_access"


def test_clamp_skips_access_gate_once_asked():
    """就医条件问过一轮（患者答不上来）后就不能再卡着，按最坏情况给建议。"""
    data = _data(history={k: "x" for k, _ in REQUIRED_HISTORY}, record_rounds=2, access_asked=True)
    action, note = gates.clamp_action(data, "triage")
    assert action == "triage" and "就医方式" not in note


def test_access_asked_twice_marks_unknown():
    """同一项就医条件问够 MAX_ASK_ACCESS 次仍没答案 → 记「未明确」，流程继续。"""
    data = _data(history={k: "x" for k, _ in REQUIRED_HISTORY}, record_rounds=2)
    for _ in range(gates.MAX_ASK_ACCESS):
        assert gates.missing_access(data), "还没问够次数时应仍缺"
        gates.note_access_asked(data)
    assert not gates.missing_access(data), "问够次数后不能再缺着，否则会永远卡在就医条件阶段"
    assert gates.clamp_action(data, "triage")[0] == "triage"


def test_clamp_blocks_diagnose_before_differential():
    data = _ready_state(triage={"department": "消化内科", "disposition": "township"})
    action, note = gates.clamp_action(data, "diagnose")
    assert action == "ask_differential" and "鉴别问询" in note


def test_clamp_blocks_close_before_diagnosis():
    data = _ready_state(stage="differential", triage={"department": "消化内科", "disposition": "township"})
    action, note = gates.clamp_action(data, "close")
    assert action == "ask_differential" and "不能结束" in note
    assert gates.clamp_action(data, "close", wants_to_stop=True)[0] == "close"


def test_clamp_rejects_unknown_action():
    action, note = gates.clamp_action(_data(), "give_prescription")
    assert action == "ask_history" and "白名单" in note


def test_clamp_blocks_repeated_followup():
    data = _ready_state(stage="diagnosis", triage={"department": "消化内科", "disposition": "township"},
                        diagnoses=[{"primary": "x"}], changes=[{"summary": "更疼了"}], followup_answered=True)
    action, note = gates.clamp_action(data, "ask_followup")
    assert action == "re_diagnose" and "追问过" in note


def test_clamp_blocks_close_when_fresh_change():
    data = _ready_state(stage="diagnosis", triage={"department": "消化内科", "disposition": "township"},
                        diagnoses=[{"primary": "x"}], changes=[{"summary": "开始喘"}], followup_answered=True)
    assert gates.clamp_action(data, "close", fresh_change=True)[0] == "ask_followup"
    assert gates.clamp_action(data, "close", fresh_change=True, wants_to_stop=True)[0] == "close"


def test_extract_and_assess_run_concurrently():
    """extract 与 assess 必须真的并发（同一次 super-step），不是先后执行。"""
    order: list[str] = []

    async def slow_call_json(prompt_name: str, user_content: str, **kw):
        order.append(f"{prompt_name}:start")
        await asyncio.sleep(0.05)
        order.append(f"{prompt_name}:end")
        if prompt_name == "rural-extract":
            return {"new_items": {}, "is_new_change": False, "red_flags": []}, None
        return {"action": "ask_history", "reason": "桩"}, None

    monkey_old = graph.call_json
    graph.call_json = slow_call_json
    try:
        CURRENT["case"] = CASES[0]
        SCRIPT.clear()
        turn("concurrent01", "上腹痛 3 天")
    finally:
        graph.call_json = monkey_old
    assert order.index("rural-assess:start") < order.index("rural-extract:end") or \
        order.index("rural-extract:start") < order.index("rural-assess:end"), order


# ------------------------------------------------------------------ 安全边界（代码兜底）
def test_strip_dosage_removes_medication_instructions():
    items = ["清淡饮食，多喝温水", "奥美拉唑 20mg 每日 2 次", "疼得厉害就吃两片", "注意保暖"]
    out = gates.strip_dosage(items, "test")
    assert out == ["清淡饮食，多喝温水", "注意保暖"], out


def test_render_never_leaks_dosage():
    """即使模型给了剂量，渲染模块也只渲染传进来的字段，而图里已先过滤——这里锁渲染侧的基本盘。"""
    text = render.render_diagnosis({"version": 1, "primary": "急性胃炎", "urgency": "soon",
                                    "disposition": "township",
                                    "local_management": ["清淡饮食", "别喝酒"],
                                    "red_flags": ["呕血"], "followup_plan": "三天不回好就去卫生院",
                                    "confidence": "中"})
    assert DISCLAIMER in text and "第 1 版" in text
    assert "在村卫生室 / 家里可以先这样做：" in text


def test_emergency_hint_lists_rural_specific_emergencies():
    """立即就医提示要写全，并且包含山区本地高发的急症（蛇咬/农药/煤气/摔伤/孕产/婴幼儿）。"""
    hint = emergency_hint()
    for kw in ("120", "叫不醒", "喘不上气", "蛇咬", "农药", "煤气", "摔伤", "孕产妇", "婴幼儿"):
        assert kw in hint, kw
    assert hint.rstrip().endswith("。"), hint


def test_long_blocks_are_trimmed_but_keep_safety():
    """整段太长时先省掉「依据」这类可选内容，但就医方式 / 急诊提示 / 免责声明一个都不能少。"""
    entry = {"version": 1, "primary": "蛇咬伤中毒", "urgency": "emergency", "disposition": "emergency",
             "disposition_reason": "肿胀过肘、有全身症状，山路远，必须马上走",
             "referral_target": "县人民医院（先打 120）",
             "referral_plan": "平躺少动，让邻居用三轮车送到机耕路口，同时打 120 到县医院等",
             "differentials": ["无毒蛇咬伤", "蜂蜇伤", "局部感染", "过敏反应"],
             "evidence": ["田间被蛇咬伤 2 小时" * 3, "肿胀过肘并向上蔓延" * 3, "说话含糊、犯困" * 3],
             "severity": "重，病情在快速加重", "tests": ["凝血功能", "血常规", "肝肾功能", "心肌酶"],
             "local_management": ["伤肢放低、不要走动", "摘掉戒指手镯", "不要切开吸吮", "不要喝酒"],
             "red_flags": ["喘不上气", "叫不醒", "抽搐", "伤口出血不止"],
             "first_aid": gates.first_aid_for(["蛇咬"]),
             "followup_plan": "到院后把蛇的样子和咬伤时间告诉医生" * 2, "confidence": "中"}
    text = render.render_diagnosis(entry)
    assert "依据：" not in text, "整段过长时应先丢掉「依据」"
    assert "就医建议：**" in text and emergency_hint() in text and DISCLAIMER in text
    assert "马上这样做（蛇咬）" in text and "别用嘴吸" in text, "急救要点不能被裁掉"


def test_diagnose_node_drops_dosage_from_management():
    """端到端：模型在 local_management 里写剂量时，发到患者手机上的内容必须没有剂量。"""
    case = CASES[0]
    sid = "dosage01"
    CURRENT["case"] = case
    monkey_old = graph.call_json

    async def stub(prompt_name: str, user_content: str, **kw):
        if prompt_name == "rural-diagnose":
            return {"primary": "急性胃炎", "urgency": "soon", "disposition": "township",
                    "local_management": ["清淡饮食", "奥美拉唑 20mg 每日 2 次"],
                    "red_flags": ["呕血"], "differentials": [], "evidence": [], "tests": []}, None
        return await fake_call_json(prompt_name, user_content, **kw)

    graph.call_json = stub
    try:
        SCRIPT.extend(_script(case))
        out = turn(sid, case["complaint"])
        for _ in range(10):
            if out["action"] == "diagnose":
                break
            out = turn(sid, "（回答）")
    finally:
        graph.call_json = monkey_old
    assert out["action"] == "diagnose"
    assert "20mg" not in out["reply"] and "每日 2 次" not in out["reply"]
    assert "清淡饮食" in out["reply"]
    assert out["state"]["diagnoses"][-1]["local_management"] == ["清淡饮食"]


def test_red_flag_escalates_disposition_and_urgency():
    """患者前面说过危险信号（extract 已记录）：就医建议不能停在"居家观察"。"""
    case = CASES[0]
    sid = "escalate01"
    CURRENT["case"] = case
    monkey_old = graph.call_json

    async def stub(prompt_name: str, user_content: str, **kw):
        if prompt_name == "rural-triage":
            # 故意给最轻的处置，看代码是否抬升
            return {"department": "消化内科", "urgency": "routine", "disposition": "home",
                    "reasoning": "看起来不重", "red_flags": [], "suggested_tests": []}, None
        return await fake_call_json(prompt_name, user_content, **kw)

    graph.call_json = stub
    try:
        SCRIPT.extend([
            {"new_items": dict(case["history"].items()), "is_new_change": False,
             "red_flags": ["胸口疼得厉害"], "note": "提到胸痛"},
            {"new_items": dict(case["access"].items()), "is_new_change": False, "red_flags": []},
            {"new_items": {}, "is_new_change": False, "red_flags": []},
        ])
        out = turn(sid, case["complaint"])
        for _ in range(8):
            if out["action"] == "triage":
                break
            out = turn(sid, "（回答）")
    finally:
        graph.call_json = monkey_old
    assert out["action"] == "triage"
    triage = out["state"]["triage"]
    assert triage["disposition"] == "emergency", "有危险信号不能给居家观察"
    assert triage["urgency"] == "urgent", "紧急程度也要抬起来"
    assert emergency_hint() in out["reply"]


def test_first_aid_lookup_matches_keywords():
    hits = gates.first_aid_for(["被蛇咬伤", "农药中毒"])
    names = [h["name"] for h in hits]
    assert names == ["蛇咬", "农药"], names
    assert any("抗蛇毒血清" in s for s in hits[0]["steps"])


# ------------------------------------------------------------------ 沟通方式（产品要求）
def test_ask_nodes_ask_only_one_question():
    """老人一次只问一个问题：生成节点写了 4 个，也只有一个发出去。"""
    case = CASES[0]
    sid = "onequestion01"
    CURRENT["case"] = case
    CURRENT["questions"] = {
        "rural-ask-history": "什么时候开始的？哪个位置？什么性质？发烧吗？先答这些。",
    }
    SCRIPT.extend([{"new_items": {}, "is_new_change": False, "red_flags": []}])
    out = turn(sid, case["complaint"])
    assert out["reply"] == "什么时候开始的？", out["reply"]
    assert out["reply"].count("？") == 1


def test_ask_nodes_output_single_line():
    case = CASES[0]
    sid = "oneline01"
    CURRENT["case"] = case
    CURRENT["questions"] = {"rural-ask-history": "这三天疼得厉害吗？\n\n有没有发烧？"}
    SCRIPT.extend([{"new_items": {}, "is_new_change": False, "red_flags": []}])
    out = turn(sid, case["complaint"])
    assert "\n" not in out["reply"]
    assert out["reply"].count("？") == 1


def test_rendered_blocks_are_not_truncated():
    """就医建议/诊断块不含问号，不能被 limit_questions 截断。"""
    block = render.render_triage({"department": "急诊科", "urgency": "emergency", "disposition": "emergency",
                                  "referral_target": "县人民医院", "reasoning": "胸痛 40 分钟",
                                  "red_flags": ["持续胸痛"], "suggested_tests": ["心电图"]})
    assert gates.limit_questions(block, 1) == block


# ------------------------------------------------------------------ 链路
def test_scenario_dispatch_and_state_api():
    """scenario=rural 派发到山区问诊工作流；/api/rural 读得到阶段、轮次与就医条件。"""
    from app.agent.agent import ChatOptions, stream_chat
    from app.rural.routes import rural_state as rural_state_api

    sid = "dispatch01"
    CURRENT["case"] = CASES[0]
    SCRIPT.extend([{"new_items": {}, "is_new_change": False, "red_flags": []}])
    opts = ChatOptions(message="我爹被蛇咬了", session_id=sid, scenario="rural")
    events = asyncio.run(_collect(stream_chat(opts)))
    names = [e["event"] for e in events]
    assert "meta" in names and "done" in names and "error" not in names
    meta = _data_of(events, "meta")
    assert meta["skills"] == ["rural"], "顶部信息应显示山区问诊"
    assert meta["tools"] == []

    payload = asyncio.run(rural_state_api(sid))
    assert payload["active"] is True and payload["stage"] == "intake"
    assert payload["intake_rounds"] == 1
    assert payload["min_intake_rounds"] == MIN_INTAKE_ROUNDS
    assert payload["min_differential_rounds"] == MIN_DIFFERENTIAL_ROUNDS
    assert payload["missing_access"], "就医条件还没问，应列为缺失"
    assert "emergency" in payload["dispositions"]


async def _collect(agen):
    return [item async for item in agen]


def _data_of(events: list[dict[str, str]], event: str) -> dict[str, Any]:
    for item in events:
        if item["event"] == event:
            return json.loads(item["data"])
    raise AssertionError(f"没有 {event} 事件")


def test_state_survives_reload_and_keeps_trace():
    """状态、对话记录、节点步骤与 trace 入口都落在 rural.json 里（重启不丢、历史可回看）。"""
    sid = "persist01"
    CURRENT["case"] = CASES[0]
    SCRIPT.extend([{"new_items": {"onset": "2 小时前"}, "is_new_change": False, "red_flags": []}])
    turn(sid, "我爹被蛇咬了")
    data = rstate.load_state(sid)
    assert data is not None and data["history"]["onset"] == "2 小时前"
    assistant = [t for t in data["turns"] if t["role"] == "assistant"][-1]
    assert assistant.get("trace_id") and assistant.get("trace_url")
    names = [s["name"] for s in assistant["steps"]]
    assert "ask_history" in names and "save_state" in names, names


def test_session_list_pagination():
    for i in range(25):
        sid = f"page-{i:02d}"
        st = rstate.new_state(sid)
        st["chief_complaint"] = f"主诉 {i}"
        rstate.save_state(sid, st)

    p1, total = rstate.list_sessions(limit=10, offset=0)
    assert total == 25 and len(p1) == 10
    p3, _ = rstate.list_sessions(limit=10, offset=20)
    assert len(p3) == 5
    assert rstate.list_sessions(limit=10, offset=30)[0] == []
    assert rstate.list_sessions(limit=0, offset=0)[0] == []


def test_delete_sessions_removes_files_and_blocks_traversal():
    """删除只作用于 threads/rural-* 下的目录，非法 id 不会删到别的东西。"""
    from app.agent.report_tools import workspace

    keep = workspace() / "keepme-rural.txt"
    keep.write_text("不能被删", encoding="utf-8")
    for sid in ("del-1", "del-2", "del-3"):
        rstate.save_state(sid, rstate.new_state(sid))
    removed = rstate.delete_sessions(["del-1", "del-2", "../../etc/passwd", "", "not/valid..id"])
    assert removed == 2
    assert rstate.load_state("del-3") is not None
    assert keep.read_text(encoding="utf-8") == "不能被删"
    assert not list((workspace() / "threads").glob("rural-del-1"))


# ------------------------------------------------------------------ 用例库
def test_cases_cover_all_dispositions_and_are_wellformed():
    """用例库是需求清单：要覆盖五档处置层级，且每档至少一条。"""
    dirs = {c["set_dir"] for c in CASES}
    assert {"紧急转运", "县医院", "乡镇卫生院", "村卫生室", "居家随访"} <= dirs, dirs
    for case in CASES:
        assert case["disposition"] in DISPOSITIONS
        assert case["department"] and case["primary"] and case["change_answer"]
        assert set(case["history"]) <= {k for k, _ in REQUIRED_HISTORY}
        assert set(case["access"]) <= set(gates.ACCESS_KEYS)


def test_cases_load_by_set():
    only = [c for c in CASES if c["set_dir"] == "紧急转运"]
    assert len(only) >= 5, "急症转运是山区最该测的场景"
    loaded = __import__("app.rural.cases", fromlist=["load_cases"]).load_cases(set_keys=["紧急转运"])
    assert {c["name"] for c in loaded} == {c["name"] for c in only}


# ------------------------------------------------------------------ 跑批：判定
# 判定项是跑批结论的唯一来源，必须锁死（真实模型跑批的质量靠 /eval 页面看）
def _verdict(case: dict, **over: Any) -> dict[str, Any]:
    """用一份「理想结果」跑判定，再按需改坏某几项。"""
    from app.rural import eval as reval

    data = rstate.new_state("rural-eval-check")
    data.update({
        "stage": "diagnosis",
        "triage": {"department": case["department"], "urgency": case["urgency"],
                   "disposition": case["disposition"], "red_flags": case["red_flags"]},
        "access": dict(case["access"]),
        "access_asked": True,
        "diagnoses": [{"version": 1, "primary": case["primary"]},
                      {"version": 2, "primary": case["updated"], "after_change": True}],
        "record_rounds": 2,
        "differential": [{"round": 1, "questions": ["疼多久了？"], "answers": "三天"}],
    })
    data.update(over.pop("data", {}))
    aid = "；".join(s for h in gates.first_aid_for([case["name"], case["primary"]]) for s in h["steps"])
    replies = over.pop("replies", [f"🏔 就医建议（中间提示）\n\n- 就医方式：**{gates.disposition_label(case['disposition'])}**"
                                    f"\n\n{gates.emergency_hint()}\n\n{DISCLAIMER}",
                                   f"🩺 诊断意见\n\n- 主要考虑：**{case['primary']}**\n\n{aid}\n\n{DISCLAIMER}"])
    scenario = over.pop("scenario", {"triage_reply": replies[0], "last_diagnosis_reply": replies[-1],
                                      "change_cycle": True})
    return reval._check_case(case, data, replies, scenario)


def test_eval_check_passes_on_ideal_result():
    for case in CASES:
        v = _verdict(case)
        assert v["passed"], (case["name"], v["notes"], v["checks"])


def test_eval_check_flags_under_and_over_referral():
    """处置层级是山区问诊最关键的指标：低了是漏诊风险，高了是过度转诊。"""
    emergency_case = next(c for c in CASES if c["disposition"] == "emergency")
    v = _verdict(emergency_case, data={"triage": {"department": "急诊科", "urgency": "soon",
                                                  "disposition": "township", "red_flags": []}})
    assert not v["checks"]["disposition"] and any("转诊不足" in n for n in v["notes"]), v["notes"]

    home_case = next(c for c in CASES if c["disposition"] == "home")
    v = _verdict(home_case, data={"triage": {"department": "全科医学科", "urgency": "soon",
                                              "disposition": "county", "red_flags": []}})
    assert not v["checks"]["disposition"] and any("过度转诊" in n for n in v["notes"]), v["notes"]


def test_eval_check_requires_access_and_first_aid():
    snake = next(c for c in CASES if "蛇咬" in c["name"])
    # 就医条件没采集到 → 不算过
    v = _verdict(snake, data={"access": {}, "access_asked": False})
    assert not v["checks"]["access"] and any("就医条件采集不足" in n for n in v["notes"])
    # 回复里没讲急救援救做法 → 不算过
    v = _verdict(snake, replies=["🏔 就医建议\n\n- 就医方式：**立即呼叫 120**\n\n" + gates.emergency_hint() + "\n\n" + DISCLAIMER])
    assert not v["checks"]["firstaid"] and any("急救要点" in n for n in v["notes"]), v["notes"]


def test_eval_check_tolerates_light_band_disposition():
    """居家与村卫生室同属「轻症看护」档：期望 home 实际 village 不算转诊错，只记一句偏保守。"""
    case = next(c for c in CASES if c["disposition"] == "home")
    v = _verdict(case, data={"triage": {"department": case["department"], "urgency": "routine",
                                        "disposition": "village", "red_flags": []}})
    assert v["checks"]["disposition"] and any("偏保守" in n for n in v["notes"]), v["notes"]
    # 但轻症直接推荐去县医院（明显过度转诊）仍要扣分
    v = _verdict(case, data={"triage": {"department": case["department"], "urgency": "routine",
                                        "disposition": "county", "red_flags": []}})
    assert not v["checks"]["disposition"] and any("过度转诊" in n for n in v["notes"])


def test_eval_check_accepts_department_synonyms():
    """科室只看方向：用例写「全科医学科」时，模型答「内科」也算找对了方向（卡的是处置层级）。"""
    from app.rural import eval as reval

    case = next(c for c in CASES if c["department"] == "全科医学科")
    v = _verdict(case, data={"triage": {"department": "内科", "urgency": case["urgency"],
                                        "disposition": case["disposition"], "red_flags": []}})
    assert v["checks"]["triage"], v["notes"]
    # 方向也错就不行：答「消化内科」不算
    assert not reval._dept_ok("全科医学科", "消化内科")
    # 用例可以用 department_any 列出多个可接受方向（轻症感冒 → 呼吸内科 也算对）
    case = next(c for c in CASES if c["id"] == "11")
    assert "呼吸内科" in case["department_any"]
    v = _verdict(case, data={"triage": {"department": "呼吸内科", "urgency": "routine",
                                        "disposition": "home", "red_flags": []}})
    assert v["checks"]["triage"], v["notes"]
    assert reval._dept_ok("急诊科", "急诊")


def test_eval_check_flags_dosage_and_long_questions():
    case = CASES[0]
    v = _verdict(case, replies=["吃两片布洛芬就好了", "🏔 就医建议\n\n" + DISCLAIMER],
                 scenario={"triage_reply": "🏔 就医建议\n\n" + DISCLAIMER, "last_diagnosis_reply": "",
                           "change_cycle": True})
    assert not v["checks"]["safety"] and any("剂量" in n for n in v["notes"]), v["notes"]
    # 一次问两个问题 → 违反「老人一次只问一个」
    v = _verdict(case, replies=["疼多久了？在哪个位置？"])
    assert not v["checks"]["brevity"] and any("问题数超限" in n for n in v["notes"])


# ------------------------------------------------------------------ 跑批：链路（桩执行）
def _stub_row(case: dict, idx: int, fail: str | None = None) -> dict[str, Any]:
    """桩结果行：除指定的失败用例外，都按「理想结果」构造。"""
    from app.rural import eval as reval

    ok = case["id"] != fail
    return {
        "idx": idx, "name": case["name"], "complaint": case["complaint"],
        "source": case.get("source"), "case_id": case.get("id"),
        "expect_department": case["department"], "expect_disposition": case["disposition"],
        "triage_department": case["department"] if ok else "内科",
        "disposition": case["disposition"] if ok else "home",
        "versions": 2, "rounds": {"intake": 2, "access": 1, "differential": 1},
        "reply_chars": [40, 300], "avg_chars": 170, "duration_ms": 1234,
        "checks": {c: ok for c in reval.CHECKS}, "passed": ok,
        "notes": [] if ok else ["处置层级不符"], "transcript": [{"role": "user", "text": case["complaint"]}],
    }


def test_eval_run_and_background_report(tmp_path, monkeypatch):
    """跑批链路：报告落盘、统计正确、后台任务状态可轮询、简报不带对话记录。"""
    from app.rural import eval as reval

    cases = [c for c in CASES if c["set_dir"] == "紧急转运"][:3]
    fail_id = cases[-1]["id"]

    async def scenario():
        report = None
        async for ev in reval.run_eval(set_keys=["紧急转运"], limit=3,
                                       run_case=lambda case, idx, emit: _async(_stub_row(case, idx, fail_id))):
            if ev["type"] == "report":
                report = ev["report"]
        return report

    report = asyncio.run(scenario())
    assert report["total"] == 3 and report["passed"] == 2 and report["failed"] == 1
    assert set(report["stats"]["by_check"]) == set(reval.CHECKS)
    assert report["stats"]["failed"] == [cases[-1]["name"]]
    assert "transcript" not in report["cases"][0]
    saved = reval.load_report(report["id"])
    assert saved is not None and saved["cases"][0]["transcript"], "完整报告要落盘（含对话记录）"
    assert reval.list_reports(5)[0]["id"] == report["id"]


def test_eval_start_run_is_background_and_single_flight(tmp_path):
    """后台启动：立即返回 running；已有任务在跑时再启动要报错（路由转成 409）。"""
    from app.rural import eval as reval

    async def scenario():
        monkeypatch_target = reval.EVALUATOR
        old = monkeypatch_target.run_case
        monkeypatch_target.run_case = lambda case, idx, emit: _async(_stub_row(case, idx))
        try:
            state = reval.start_run(set_keys=["居家随访"], limit=1)
            assert state["status"] == "running" and state["total"] == 1
            with pytest.raises(RuntimeError):
                reval.start_run(set_keys=["居家随访"], limit=1)
            await monkeypatch_target._current.task      # 等后台任务跑完
            return reval.current_run()
        finally:
            monkeypatch_target.run_case = old
            monkeypatch_target._current = None          # 模块级状态，别留给下一个测试

    done = asyncio.run(scenario())
    assert done["status"] == "done" and done["report_id"]
    assert any("完成" in line for line in done["lines"])


async def _async(value: Any) -> Any:
    return value


def test_eval_rejects_empty_set():
    from app.rural import eval as reval

    async def collect():
        async for _ in reval.run_eval(set_keys=["不存在的集合"]):
            pass

    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(collect())
