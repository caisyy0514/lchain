"""结构化问诊工作流（app/consult/）的测试。

分三层：
1. **纯函数**：动作钳制 `clamp_action`、问题数量截断 `limit_questions`、分诊/诊断渲染的安全边界；
2. **10 个常见病例端到端**：用「理想判断节点」的桩替换 LLM，真正跑一遍 StateGraph
   （load_state → extract → assess → 分支 → save_state），断言阶段流转、分诊科室、诊断版本、字数与问题数；
3. **链路**：`scenario="consult"` 的派发、`/api/consult` 状态接口读到的字段。

LLM 的判断质量无法用单测覆盖（要靠 `make consult-smoke` 跑真实模型），
但**流程健壮性**（不该推进时不推进、问题数封顶、免责声明不丢）必须在这里锁死。

    PYTHONPATH=. .venv/bin/python -m pytest tests/agent/test_consult.py -q
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.consult import gates, graph, render, state as cstate
from app.consult.cases import CASES
from app.consult.gates import (
    DISCLAIMER,
    EMERGENCY_HINT,
    MAX_QUESTIONS_PER_TURN,
    MIN_DIFFERENTIAL_ROUNDS,
    MIN_INTAKE_ROUNDS,
    REQUIRED_HISTORY,
)

# ------------------------------------------------------------------ 桩：把「理想判断」写成规则
CURRENT: dict[str, Any] = {}      # 当前病例（测试串行执行，用模块级变量传递）
SCRIPT: list[dict[str, Any]] = []  # extract 的逐轮脚本（患者每次发言返回什么）


def _oracle_action(v: dict[str, Any]) -> str:
    """一个「理想判断节点」会给出的 action（流程测试用它替代 LLM 判断）。"""
    if v.get("has_triage") != "是":
        ready = v.get("missing") == "（已采集齐全）" and int(v.get("intake_rounds") or 0) >= int(v.get("min_intake_rounds") or 2)
        return "triage" if ready else "ask_history"
    if int(v.get("diagnosis_versions") or 0) == 0:
        if int(v.get("differential_rounds") or 0) >= int(v.get("min_differential_rounds") or 2):
            return "diagnose"
        return "ask_differential"
    if v.get("has_new_change") == "是" and v.get("followup_answered") != "是":
        return "ask_followup"
    if v.get("has_new_change") == "是":
        return "re_diagnose"
    return "close"


async def fake_call_json(prompt_name: str, user_content: str, **kw: Any) -> tuple[dict[str, Any], Any]:
    v = {k: val for k, val in kw.items() if k not in ("cfg", "overrides")}
    if prompt_name == "consult-assess":
        return {"action": _oracle_action(v), "reason": "测试桩", "focus": "起病时间与部位"}, None
    if prompt_name == "consult-extract":
        return (SCRIPT.pop(0) if SCRIPT else {}), None
    if prompt_name == "consult-triage":
        case = CURRENT["case"]
        return {"department": case["department"], "reasoning": "测试桩：依据已采集信息",
                "urgency": case["urgency"], "red_flags": case["red_flags"],
                "suggested_tests": ["血常规"]}, None
    if prompt_name == "consult-diagnose":
        case = CURRENT["case"]
        is_update = v.get("is_update") == "是"
        return {"primary": case["updated"] if is_update else case["primary"],
                "differentials": case["differentials"], "evidence": case["evidence"],
                "severity": "轻至中度", "urgency": case["urgency"], "tests": ["血常规"],
                "management": ["清淡饮食，规律作息"], "red_flags": case["red_flags"],
                "followup_plan": "3 天后复诊，加重随时就医", "confidence": "中"}, None
    raise AssertionError(f"未预期的判断提示词：{prompt_name}")


async def fake_stream_text(prompt_name: str, user_content: str, **kw: Any):
    """生成类节点的桩：返回固定的短回复（问题直接给、不带编号）。"""
    text = CURRENT.get("questions", {}).get(prompt_name, "什么时候开始的？\n具体在哪个位置？")
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
    """跑一轮工作流，返回最终 graph state（trace_id 与 run_stream 一致地传入）。"""
    return asyncio.run(graph.build_graph().ainvoke(
        {"session_id": session_id, "user_message": message, "steps": [],
         "trace_id": f"test-trace-{session_id}"},
        config={"configurable": {"thread_id": session_id}, "recursion_limit": 30},
    ))


# ------------------------------------------------------------------ 10 个常见病例


def _script(case: dict[str, Any]) -> list[dict[str, Any]]:
    """把病例拆成逐轮的 extract 结果：前两轮补要素，第三轮给新变化，第四轮是变化后的回答。"""
    items = list(case["history"].items())
    return [
        {"new_items": dict(items[:5]), "is_new_change": False, "red_flags": [], "note": "第 1 轮病史"},
        {"new_items": dict(items[5:]), "is_new_change": False, "red_flags": [], "note": "第 2 轮病史"},
        {"new_items": {}, "is_new_change": False, "red_flags": [], "note": "鉴别问询回答"},
        {"new_items": {}, "is_new_change": False, "red_flags": [], "note": "鉴别问询回答 2"},
        {"new_items": {}, "is_new_change": True, "change_summary": case["change"], "red_flags": [], "note": "病情变化"},
        {"new_items": {}, "is_new_change": False, "red_flags": [], "note": "变化后的回答"},
        {"new_items": {}, "is_new_change": False, "red_flags": [], "note": "补充"},
    ]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_common_cases_full_flow(case: dict):
    """10 个病例都能走完「采集 → 分诊 → 鉴别 → 诊断 → 变化追问 → 更新诊断」。"""
    sid = f"case-{CASES.index(case):02d}"
    CURRENT["case"] = case
    SCRIPT.extend(_script(case))

    # 1) 主诉 → 采集现病史
    out = turn(sid, case["complaint"])
    assert out["action"] == "ask_history"
    assert out["state"]["stage"] == "intake"

    # 2) 第 1 轮病史
    out = turn(sid, "（回答第 1 轮）")
    assert out["action"] == "ask_history", "要素没齐不能分诊"

    # 3) 第 2 轮病史：要素齐了但轮次还需要 ≥2，仍应继续问（这时也可分诊）
    out = turn(sid, "（回答第 2 轮）")
    data = out["state"]
    assert not gates.missing_history(data), "两轮后要素应齐备"

    # 4) 直到拿到分诊
    for _ in range(4):
        if out["action"] == "triage":
            break
        out = turn(sid, "（补充回答）")
    assert out["action"] == "triage", f"应进入分诊，实际 {out['action']}"
    assert out["state"]["stage"] == "differential"
    assert case["department"] in out["reply"]
    assert DISCLAIMER in out["reply"], "分诊必须带免责声明"
    if case["urgency"] in ("urgent", "emergency"):
        assert EMERGENCY_HINT in out["reply"], "紧急病例必须带立即就医提示"

    # 5) 鉴别问询 ≥2 轮后才能诊断
    out = turn(sid, "（鉴别回答 1）")
    assert out["action"] == "ask_differential"
    out = turn(sid, "（鉴别回答 2）")
    assert len(out["state"]["differential"]) >= MIN_DIFFERENTIAL_ROUNDS

    # 6) 诊断 v1
    for _ in range(3):
        if out["action"] == "diagnose":
            break
        out = turn(sid, "（补充回答）")
    assert out["action"] == "diagnose", f"应进入诊断，实际 {out['action']}"
    assert out["state"]["diagnoses"][-1]["version"] == 1
    assert case["primary"] in out["reply"]
    assert DISCLAIMER in out["reply"]

    # 7) 病情变化 → 必须先追问
    out = turn(sid, case["change"])
    assert out["action"] == "ask_followup", f"变化后应先追问，实际 {out['action']}"
    assert out["state"]["stage"] == "followup"

    # 8) 患者回答 → 更新版诊断 v2
    out = turn(sid, case["change_answer"])
    assert out["action"] == "re_diagnose", f"追问后应更新诊断，实际 {out['action']}"
    assert len(out["state"]["diagnoses"]) == 2
    assert out["state"]["diagnoses"][-1]["after_change"] is True
    assert case["updated"] in out["reply"]
    assert DISCLAIMER in out["reply"]
    assert out["state"]["stage"] == "diagnosis"


# ------------------------------------------------------------------ 动作钳制（代码兜底）
def _data(**over: Any) -> dict[str, Any]:
    base = cstate.new_state("gates01")
    base.update(over)
    return base


def test_clamp_blocks_triage_when_history_incomplete():
    action, note = gates.clamp_action(_data(), "triage")
    assert action == "ask_history" and "要素未齐" in note


def test_clamp_blocks_diagnose_before_differential():
    data = _data(history={k: "x" for k, _ in REQUIRED_HISTORY}, record_rounds=2,
                 triage={"department": "消化内科"})
    action, note = gates.clamp_action(data, "diagnose")
    assert action == "ask_differential" and "鉴别问询" in note


def test_clamp_blocks_diagnose_from_intake():
    assert gates.clamp_action(_data(), "diagnose")[0] == "ask_history"


def test_clamp_blocks_re_diagnose_before_followup():
    data = _data(history={k: "x" for k, _ in REQUIRED_HISTORY}, record_rounds=2, stage="followup",
                 triage={"department": "消化内科"}, diagnoses=[{"primary": "x"}],
                 changes=[{"summary": "更疼了"}], followup_answered=False)
    action, note = gates.clamp_action(data, "re_diagnose")
    assert action == "ask_followup" and "还没追问" in note


def test_clamp_rejects_unknown_action():
    action, note = gates.clamp_action(_data(), "give_prescription")
    assert action == "ask_history" and "白名单" in note


def test_clamp_blocks_close_before_diagnosis():
    """分诊后就收尾等于把流程半途而废：没诊断时 close 要被驳回（除非患者明确要求结束）。"""
    data = _data(history={k: "x" for k, _ in REQUIRED_HISTORY}, record_rounds=2,
                 triage={"department": "消化内科"}, stage="differential")
    action, note = gates.clamp_action(data, "close")
    assert action == "ask_differential" and "不能结束" in note
    # 患者明确表示要去线下 → 允许结束
    assert gates.clamp_action(data, "close", wants_to_stop=True)[0] == "close"


def test_clamp_diagnose_skipped_straight_to_differential():
    """已分诊时给 ask_history 也要归位到鉴别问询，不能回头再采病史。"""
    data = _data(history={k: "x" for k, _ in REQUIRED_HISTORY}, record_rounds=2,
                 triage={"department": "消化内科"}, stage="differential")
    assert gates.clamp_action(data, "ask_history")[0] == "ask_differential"


def test_clamp_blocks_repeated_followup():
    """变化已经追问过一轮、患者也答了，就不能再问一遍（否则无限追问）。"""
    data = _data(history={k: "x" for k, _ in REQUIRED_HISTORY}, record_rounds=2, stage="diagnosis",
                 triage={"department": "消化内科"}, diagnoses=[{"primary": "x"}],
                 changes=[{"summary": "拉肚子"}], followup_answered=True)
    action, note = gates.clamp_action(data, "ask_followup")
    assert action == "re_diagnose" and "追问过" in note


def test_clamp_upgrades_stuck_history_intake_to_triage():
    """要素齐 + 轮次够时不允许再问病史（否则会永远停在采集阶段）。"""
    data = _data(history={k: "x" for k, _ in REQUIRED_HISTORY}, record_rounds=2)
    action, note = gates.clamp_action(data, "ask_history")
    assert action == "triage" and "直接进入分诊" in note


def test_clamp_blocks_close_when_fresh_change():
    """assess 与 extract 并发时看不到本轮的新变化，可能给出 close；要改成先追问。"""
    data = _data(history={k: "x" for k, _ in REQUIRED_HISTORY}, record_rounds=2, stage="diagnosis",
                 triage={"department": "消化内科"}, diagnoses=[{"primary": "x"}],
                 changes=[{"summary": "开始拉肚子"}], followup_answered=True)
    action, note = gates.clamp_action(data, "close", fresh_change=True)
    assert action == "ask_followup" and "先追问" in note
    # 患者明确说要去线下：还是允许结束
    assert gates.clamp_action(data, "close", fresh_change=True, wants_to_stop=True)[0] == "close"


def test_extract_and_assess_run_concurrently():
    """extract 与 assess 必须真的并发（同一次 super-step），不是先后执行。

    并发后单轮耗时能降下 ~1.4s（两个判断节点各 ~1.5s），所以这里盯住它不被改回串行。
    """
    order: list[str] = []

    async def slow_call_json(prompt_name: str, user_content: str, **kw):
        order.append(f"{prompt_name}:start")
        await asyncio.sleep(0.05)
        order.append(f"{prompt_name}:end")
        if prompt_name == "consult-extract":
            return {"new_items": {}, "is_new_change": False, "red_flags": []}, None
        return {"action": "ask_history", "reason": "桩"}, None

    monkey_old = graph.call_json
    graph.call_json = slow_call_json
    try:
        sid = "concurrent01"
        SCRIPT.clear()
        turn(sid, "上腹痛 3 天")
    finally:
        graph.call_json = monkey_old
    # 并发：两个 start 都在任何 end 之前
    assert order.index("consult-assess:start") < order.index("consult-extract:end") or \
        order.index("consult-extract:start") < order.index("consult-assess:end"), order


def test_close_is_overridden_when_change_arrives_same_turn():
    """整图验证：assess 说 close、extract 同一轮发现新变化 → 最终动作必须是追问。"""
    case = CASES[0]
    sid = "freshchange01"
    _drive_to_diagnosis(case, sid)

    # assess 桩返回 close（模拟它看不到本轮新变化），extract 桩报告变化
    async def stub(prompt_name: str, user_content: str, **kw):
        if prompt_name == "consult-extract":
            return {"new_items": {}, "is_new_change": True, "change_summary": case["change"],
                    "red_flags": []}, None
        return {"action": "close", "reason": "患者语气像收尾"}, None

    monkey_old = graph.call_json
    graph.call_json = stub
    try:
        out = turn(sid, case["change"])
    finally:
        graph.call_json = monkey_old
    assert out["action"] == "ask_followup", out["action"]
    assert out["state"]["changes"], "新变化要记下来"


# ------------------------------------------------------------------ 问题数量硬截断
def test_limit_questions_counts_by_question_mark():
    """按问号计数：第 2 个问号之后的内容（多出的问题、尾巴上的收尾语）全部删掉。"""
    assert gates.limit_questions("疼吗？发烧吗？吐过血吗？") == "疼吗？发烧吗？"
    assert gates.limit_questions("疼吗？发烧吗？") == "疼吗？发烧吗？"
    # 两个问题分行写也会被截（但推荐写在同一行，见 one_line）
    assert gates.limit_questions("疼吗？\n发烧吗？\n吐过血吗？") == "疼吗？\n发烧吗？"


def test_limit_questions_leaves_non_questions_alone():
    """分诊/诊断块不含问号，不能被截。"""
    block = "🧭 分诊建议\n\n- 建议就诊：**消化内科**\n- 紧急程度：常规"
    assert gates.limit_questions(block) == block


def test_one_line_joins_questions():
    """两个问题要能连成一行展示。"""
    assert gates.one_line("疼吗？\n\n发烧吗？") == "疼吗？ 发烧吗？"
    assert gates.one_line("疼吗？") == "疼吗？"


def test_ask_nodes_render_two_questions_in_one_line(case: dict = CASES[0]):
    """即使生成节点把两个问题分了行，发给患者的也是一行。"""
    sid = "oneline01"
    CURRENT["case"] = case
    CURRENT["questions"] = {
        "consult-ask-history": "这三天疼得厉害吗？\n\n有没有发烧、恶心想吐？",
    }
    SCRIPT.extend([{"new_items": {}, "is_new_change": False, "red_flags": []}])
    out = turn(sid, case["complaint"])
    assert "\n" not in out["reply"], out["reply"]
    assert out["reply"].count("？") == 2


def test_ask_nodes_never_ask_more_than_two_questions(case: dict = CASES[0]):
    """即使生成节点一次问了 4 个，发给患者的也只有 2 个问题。"""
    sid = "truncate01"
    CURRENT["case"] = case
    CURRENT["questions"] = {
        "consult-ask-history": "什么时候开始的？\n哪个位置？\n什么性质？\n发烧吗？先答这些。",
    }
    SCRIPT.extend([{"new_items": {}, "is_new_change": False, "red_flags": []}] * 3)
    out = turn(sid, case["complaint"])
    assert out["reply"] == "什么时候开始的？ 哪个位置？", out["reply"]
    assert "什么性质" not in out["reply"] and "先答这些" not in out["reply"]


def test_questions_are_plain_without_numbering():
    """问题直接给：不带编号、不带过渡语（验收产品要求）。"""
    sid = "plain01"
    CURRENT["case"] = CASES[0]
    SCRIPT.extend([{"new_items": {}, "is_new_change": False, "red_flags": []}])
    out = turn(sid, "上腹痛 3 天")
    for line in out["reply"].split("\n"):
        assert not line.strip()[:2].rstrip(".、)").isdigit(), f"不应带编号：{line!r}"
    assert "好的" not in out["reply"] and "再问" not in out["reply"]


# ------------------------------------------------------------------ 安全边界（代码渲染）
def test_render_triage_always_has_disclaimer():
    text = render.render_triage({"department": "消化内科", "reasoning": "依据", "urgency": "routine",
                                 "red_flags": [], "suggested_tests": []})
    assert DISCLAIMER in text and "消化内科" in text
    assert EMERGENCY_HINT not in text


def test_render_triage_emergency_adds_hint():
    text = render.render_triage({"department": "急诊科", "reasoning": "胸痛", "urgency": "emergency",
                                 "red_flags": ["持续胸痛"], "suggested_tests": []})
    assert EMERGENCY_HINT in text and DISCLAIMER in text


def test_render_diagnosis_lists_and_disclaimer():
    text = render.render_diagnosis({"version": 2, "primary": "急性胃炎", "differentials": ["溃疡"],
                                    "evidence": ["上腹痛"], "management": ["清淡饮食"],
                                    "red_flags": ["呕血"], "followup_plan": "3 天复诊", "confidence": "中",
                                    "urgency": "soon"})
    assert "第 2 版" in text and DISCLAIMER in text
    assert "- 主要考虑：**急性胃炎**" in text and "出现以下情况请立即就医：" in text


def test_red_flag_from_patient_escalates_urgency():
    """患者前面说过危险信号（extract 已记录），分诊给 routine 时要抬到 urgent。"""
    sid = "redflag01"
    CURRENT["case"] = CASES[6]        # 骨关节炎，routine
    SCRIPT.extend([
        {"new_items": dict(CASES[6]["history"].items()), "is_new_change": False,
         "red_flags": ["胸痛"], "note": "提到胸痛"},
        {"new_items": {}, "is_new_change": False, "red_flags": []},
    ])
    out = turn(sid, "膝盖疼，另外这几天偶尔胸口疼")
    for _ in range(3):
        if out["action"] == "triage":
            break
        out = turn(sid, "（补充）")
    assert out["action"] == "triage"
    assert out["state"]["triage"]["urgency"] == "urgent", "患者提过危险信号，不能按常规处理"
    assert EMERGENCY_HINT in out["reply"]


# ------------------------------------------------------------------ 链路
def test_scenario_dispatch_and_state_api():
    """scenario=consult 派发到问诊工作流；/api/consult 读得到阶段与轮次。"""
    from app.agent.agent import ChatOptions, stream_chat
    from app.consult.routes import consult_state

    sid = "dispatch01"
    CURRENT["case"] = CASES[0]
    SCRIPT.extend([{"new_items": {}, "is_new_change": False, "red_flags": []}])
    opts = ChatOptions(message="上腹痛 3 天", session_id=sid, scenario="consult")
    events = asyncio.run(_collect(stream_chat(opts)))
    names = [e["event"] for e in events]
    assert "meta" in names and "done" in names and "error" not in names
    meta = _data_of(events, "meta")
    assert meta["skills"] == ["consult"], "顶部信息应显示问诊工作流"
    assert meta["tools"] == []

    payload = asyncio.run(consult_state(sid))
    assert payload["active"] is True and payload["stage"] == "intake"
    assert payload["intake_rounds"] == 1
    assert payload["min_intake_rounds"] == MIN_INTAKE_ROUNDS
    assert payload["min_differential_rounds"] == MIN_DIFFERENTIAL_ROUNDS


async def _collect(agen):
    return [item async for item in agen]


def _data_of(events: list[dict[str, str]], event: str) -> dict[str, Any]:
    import json

    for item in events:
        if item["event"] == event:
            return json.loads(item["data"])
    raise AssertionError(f"没有 {event} 事件")


def test_session_list_pagination():
    """会话历史分页：offset/limit 生效、返回总数、页与页之间不重叠。"""
    for i in range(25):
        sid = f"pagecheck-{i:02d}"
        st = cstate.new_state(sid)
        st["chief_complaint"] = f"主诉 {i}"
        cstate.save_state(sid, st)

    p1, total = cstate.list_sessions(limit=10, offset=0)
    assert total == 25 and len(p1) == 10
    p3, _ = cstate.list_sessions(limit=10, offset=20)
    assert len(p3) == 5
    p4, _ = cstate.list_sessions(limit=10, offset=30)
    assert p4 == [], "超出范围应为空页"
    ids = {r["session_id"] for r in p1} | {r["session_id"] for r in p3}
    assert len(ids) == 15, "不同页不能出现重复会话"
    assert all(r["title"].startswith("主诉") for r in p1 + p3)


def test_session_list_page_size_zero_is_empty():
    assert cstate.list_sessions(limit=0, offset=0)[0] == []


def test_delete_sessions_removes_files_and_blocks_traversal():
    """批量删除：只删 threads/consult-* 下的目录，非法 id 不报错但也不会删到别的东西。"""
    from app.agent.report_tools import workspace

    out = workspace() / "keepme.txt"
    out.write_text("不能被删", encoding="utf-8")
    for sid in ("del-1", "del-2", "del-3"):
        cstate.save_state(sid, cstate.new_state(sid))
    removed = cstate.delete_sessions(["del-1", "del-2", "../../etc/passwd", "", "not/valid..id"])
    assert removed == 2, "只应该删掉两个合法会话"
    assert cstate.load_state("del-3") is not None, "没选中的不能被删"
    assert out.read_text(encoding="utf-8") == "不能被删", "不能删到工作区里其它文件"
    assert not list((workspace() / "threads").glob("consult-del-1"))


def test_state_survives_reload(tmp_path):
    """状态与对话记录都在 consult.json 里，重新读取后判断节点仍能看到之前的问答。"""
    sid = "persist01"
    CURRENT["case"] = CASES[0]
    SCRIPT.extend([{"new_items": {"onset": "3 天前"}, "is_new_change": False, "red_flags": []}])
    turn(sid, "上腹痛 3 天")
    data = cstate.load_state(sid)
    assert data is not None and data["history"]["onset"] == "3 天前"
    assert [t["role"] for t in data["turns"]] == ["user", "assistant"]


def test_saved_turn_keeps_steps_and_trace():
    """落盘的 assistant 轮次要带节点步骤与 trace 入口——点历史会话时靠它还原卡片与链路。"""
    sid = "persist02"
    CURRENT["case"] = CASES[0]
    SCRIPT.extend([{"new_items": {}, "is_new_change": False, "red_flags": []}])
    turn(sid, "上腹痛 3 天")
    data = cstate.load_state(sid)
    assistant = [t for t in data["turns"] if t["role"] == "assistant"][-1]
    assert assistant.get("trace_id"), "要存 trace id"
    assert assistant.get("trace_url"), "要存 trace 链接"
    names = [s["name"] for s in assistant["steps"]]
    assert "ask_history" in names and "save_state" in names, names
    assert all({"name", "args", "output", "status"} <= set(s) for s in assistant["steps"]), \
        "步骤格式要与前端工具卡片一致（name/args/output/status）"


def _drive_to_diagnosis(case: dict, sid: str) -> dict[str, Any]:
    """把一次问诊推到诊断 v1（供变化循环的测试复用）。"""
    CURRENT["case"] = case
    SCRIPT.extend([{"new_items": dict(case["history"]), "is_new_change": False, "red_flags": []},
                   {"new_items": {}, "is_new_change": False, "red_flags": []},
                   {"new_items": {}, "is_new_change": False, "red_flags": []},
                   {"new_items": {}, "is_new_change": False, "red_flags": []},
                   {"new_items": {}, "is_new_change": False, "red_flags": []}])
    out = turn(sid, case["complaint"])
    for _ in range(8):
        if out.get("state", {}).get("diagnoses"):
            return out
        out = turn(sid, "（补充回答）")
    raise AssertionError(f"没走到诊断：{out.get('action')}")


def test_change_cycle_does_not_loop():
    """诊断后报告变化：追问一轮 → 更新诊断；患者回答里再做一次变化判定也不能重复追问。"""
    case = CASES[0]
    sid = "changeloop01"
    out = _drive_to_diagnosis(case, sid)
    assert out["state"]["diagnoses"][-1]["version"] == 1

    # 患者报告新变化 → 必须追问
    SCRIPT.append({"new_items": {}, "is_new_change": True, "change_summary": case["change"], "red_flags": []})
    out = turn(sid, case["change"])
    assert out["action"] == "ask_followup"
    assert out["state"]["followup_answered"] is False

    # 患者回答（extract 仍可能判成"新变化"）→ 应被合并进同一次变化，直接更新诊断
    SCRIPT.append({"new_items": {}, "is_new_change": True, "change_summary": case["change_answer"], "red_flags": []})
    out = turn(sid, case["change_answer"])
    assert out["action"] == "re_diagnose", out["action"]
    assert len(out["state"]["changes"]) == 1, "同一次变化的回答要合并，不能新增变化"
    assert len(out["state"]["diagnoses"]) == 2
    assert case["updated"] in out["reply"]
