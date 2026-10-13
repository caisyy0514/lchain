"""企微群回复评测（`app/wecom/cases.py` + `app/wecom/eval.py`）的测试。

三层：
1. **用例加载**：`eval/wecom/**/*.md` 都能解析，坏文件只跳过并报错，不能把服务带崩；
2. **判定**：`_check_case` 的每一项（分支、该不该回、卡片、转人工、关键词/禁用词、字数、合并）——
   这是跑批结果的正确性来源，必须锁死；
3. **跑批链路**：用桩执行的「一轮结果」跑完整 `run_eval`，验证报告结构、统计与**临时群清理**
   （真实模型的质量靠 `/wecom-eval` 页面跑真模型，这里测的是链路与判定）。

    PYTHONPATH=. .venv/bin/python -m pytest tests/agent/test_wecom_eval.py -q
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.wecom import cases as wcases
from app.wecom import eval as weval

# ---------------------------------------------------------------- 1. 用例加载
def test_builtin_cases_load() -> None:
    """内置用例库能全部解析，字段齐、集合 = 目录名。"""
    assert len(wcases.CASES) >= 10, "内置用例太少，至少覆盖各意图"
    for c in wcases.CASES:
        assert c["name"] and c["message"], c
        assert c["sender_role"] in wcases.ROLES, c
        assert c["set"] == c["set_label"], c
        assert c["source"].startswith("eval/wecom/"), c
        assert c["path"].is_file(), c
        assert isinstance(c["expect"], dict) and c["expect"], c


def test_builtin_cases_cover_intents() -> None:
    """用例要覆盖各回复分支与「无需回复」，否则评测没有意义。"""
    intents = set()
    for c in wcases.CASES:
        if c["expect"]["intent"]:
            intents.add(c["expect"]["intent"])
        intents.update(c["expect"]["intent_any"])
    for want in ("severe", "find_doctor", "simple_medical", "clarify", "non_medical"):
        assert want in intents, f"没有覆盖 {want} 分支的用例"
    assert any(not c["expect"]["need_reply"] for c in wcases.CASES), "缺少「无需回复」用例"
    assert any(c["expect"]["merged"] for c in wcases.CASES), "缺少打断合并用例"


def test_list_sets_matches_dirs() -> None:
    sets = {s["key"]: s["count"] for s in wcases.list_sets()}
    assert sets and sum(sets.values()) == len(wcases.CASES)


def test_bad_case_file_is_skipped(tmp_path) -> None:
    """写坏的用例文件只跳过 + 报错，不影响其它用例。"""
    (tmp_path / "场景A").mkdir()
    (tmp_path / "场景A" / "01-好用例.md").write_text(
        "---\nname: 好用例\nmessage: 你好\nexpect:\n  intent: simple_medical\n---\n说明\n", encoding="utf-8")
    (tmp_path / "场景A" / "02-缺字段.md").write_text("---\nname: 只有名字\n---\n", encoding="utf-8")
    (tmp_path / "场景A" / "03-没有frontmatter.md").write_text("就一段正文", encoding="utf-8")
    (tmp_path / "场景A" / "04-字段不认识.md").write_text(
        "---\nname: 坏字段\nmessage: 你好\nexpect:\n  intentt: severe\n---\n", encoding="utf-8")
    loaded = weval.load_cases(path=tmp_path)
    assert [c["name"] for c in loaded] == ["好用例"]
    assert weval.load_cases(set_keys=["场景A"], path=tmp_path) == loaded
    assert weval.load_cases(set_keys=["不存在的集合"], path=tmp_path) == []


def test_followup_requires_merged() -> None:
    """写了 followup 却没说在测打断合并 -> 认为写坏了。"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.md"
        p.write_text("---\nname: 连发两条\nmessage: 一\nfollowup: 二\nexpect:\n  intent: simple_medical\n---\n",
                     encoding="utf-8")
        with pytest.raises(ValueError, match="merged"):
            wcases.parse_case_file(p)


# ---------------------------------------------------------------- 2. 判定
def _case(case_id: str) -> dict:
    return {c["id"]: c for c in wcases.CASES}[case_id]


def _ev(**kw) -> dict:
    base = {"status": "done", "route": "", "need_reply": True, "replies": [], "guides": [],
            "card": False, "notify": False, "mention_manager": False, "message_ids": [1]}
    base.update(kw)
    return base


def test_check_case_pass_and_fail() -> None:
    case = _case("01")     # simple_medical，禁用词：停药/加量
    good = _ev(route="simple_medical", replies=["血糖 8.9 偏高，建议记录两天并找医生看看"])
    assert weval._check_case(case, good)["passed"] is True
    bad = _ev(route="severe", replies=["先停药观察几天"])
    verdict = weval._check_case(case, bad)
    assert verdict["passed"] is False
    joined = " ".join(verdict["notes"])
    assert "回复分支不符" in joined and "禁止词" in joined and "关键词" in joined


def test_check_case_no_reply_case() -> None:
    """无需回复的用例：回了就判失败，没回才过。"""
    case = _case("11")
    assert weval._check_case(case, _ev(replies=[]))["passed"] is True
    v = weval._check_case(case, _ev(route="simple_medical", replies=["好的，不客气"]))
    assert v["passed"] is False and "不该回复" in " ".join(v["notes"])


def test_check_case_card_and_mention() -> None:
    """找医生用例：新标准服务不推卡片（结论由平台医生审核）；非医疗用例：该 @病历经理。

    卡片检查器本身仍要可用，所以这里用 override 临时把期望改为 push_card=true。
    """
    base = _case("04")
    card_case = {**base, "expect": {**base["expect"], "push_card": True}}
    assert weval._check_case(card_case, _ev(route="find_doctor", replies=["找医生看看"], card=True))["passed"] is True
    v = weval._check_case(card_case, _ev(route="find_doctor", replies=["找医生看看"], card=False))
    assert v["passed"] is False and "卡片" in " ".join(v["notes"])

    staff_case = _case("09")          # non_medical，mention_manager: true
    assert weval._check_case(staff_case, _ev(route="non_medical", replies=["已转病历经理"], mention_manager=True))["passed"] is True
    v = weval._check_case(staff_case, _ev(route="non_medical", replies=["已转病历经理"], mention_manager=False))
    assert v["passed"] is False and "转人工" in " ".join(v["notes"])


def test_check_case_merged_and_length() -> None:
    merged_case = _case("13")         # 连发两条：要合并成一条回复
    ok = _ev(route="find_doctor", replies=["先别停药，找医生评估"], card=False, message_ids=[1, 2])
    assert weval._check_case(merged_case, ok)["passed"] is True
    two = _ev(route="find_doctor", replies=["一", "二"], card=False, message_ids=[1])
    assert weval._check_case(merged_case, two)["passed"] is False

    length_case = _case("02")         # clarify，上限 80 字
    long_reply = _ev(route="clarify", replies=["很长的回复" * 40])
    v = weval._check_case(length_case, long_reply)
    assert v["passed"] is False and "超字数" in " ".join(v["notes"])


def test_check_case_records_applicable_checks() -> None:
    """没写期望的项不参与判定（不进分母），否则分项通过率会虚高。"""
    verdict = weval._check_case(_case("01"), _ev(route="simple_medical", replies=["血糖偏高，找医生看看"]))
    assert "intent" in verdict["applicable"] and "notify" in verdict["applicable"]
    assert "guide" not in verdict["applicable"] and "merged" not in verdict["applicable"]
    assert all(isinstance(v, bool) for v in verdict["checks"].values())


# ---------------------------------------------------------------- 3. 跑批链路（桩执行 + 真 MySQL）
def test_run_eval_with_stub_executor(tmp_path, monkeypatch) -> None:
    """用桩结果跑完整跑批：报告落盘、统计正确、临时群被清理。

    整个流程必须跑在**同一个事件循环**里（SQLAlchemy async 引擎的连接是绑循环的），
    所以这里用一次 asyncio.run 把 init_db + 跑批 + 清理检查一起跑完。
    """
    monkeypatch.setattr(weval, "evals_dir", lambda: tmp_path)
    result = asyncio.run(_stub_scenario())
    if result is None:
        pytest.skip("MySQL 未启动（make mysql-up），跳过跑批链路测试")
    report, created = result
    assert report["total"] == 3
    assert report["passed"] == 2 and report["failed"] == 1, report["stats"]["failed"]
    assert report["stats"]["failed"][0]["name"] == _case("02")["name"]
    assert set(report["stats"]["by_check"]) == set(weval.CHECKS)
    assert (tmp_path / f"wecom-{report['id']}.json").is_file()
    assert report["cases"][0]["steps"] == [] and report["cases"][0]["message"]
    assert len(set(created)) == 3, "每条用例应该用独立的临时群 id"


async def _stub_scenario() -> tuple[dict, list[str]] | None:
    """返回 (报告, 用过的临时群 id)；MySQL 不可用时返回 None。"""
    from app.core.db import db_ready, engine, init_db
    from app.wecom import store

    FAIL_ID = "02"                        # 指定这一条用例「跑坏」，用来验证失败统计
    await init_db()
    if not db_ready():
        return None
    created: list[str] = []

    async def stub(case: dict, gid: str) -> dict:
        """「理想一轮」的桩：除了指定的失败用例，其它都能满足用例里写的每一条期望。"""
        created.append(gid)
        assert gid.startswith(store.EVAL_GROUP_PREFIX)
        exp = case["expect"]
        ok = case["id"] != FAIL_ID
        if ok:
            route = exp["intent"] or (exp["intent_any"][0] if exp["intent_any"] else "")
            # 用用例自己的关键词拼一条「合规回复」：包含必须出现的关键词、且不超上限
            parts = [*exp["must_include"], *(exp["must_include_any"][:1] or ["医生"])]
            reply = "，".join(parts) or "好的"
            if exp["max_chars"]:
                reply = reply[:exp["max_chars"]]
        else:
            route, reply = "severe", "先停药观察几天"      # 分支错 + 命中禁用词
        return _ev(route=route, replies=([] if not exp["need_reply"] else [reply]),
                   guides=(["正在查询"] if exp["push_guide"] else []),
                   card=exp["push_card"] is True, notify=exp["notify_manager"] is True,
                   mention_manager=exp["mention_manager"] is True,
                   message_ids=[1, 2] if exp["merged"] else [1])

    report = None
    async for ev in weval.run_eval(set_keys=["医疗咨询"], executor=stub):
        if ev["type"] == "report":
            report = weval.load_report(ev["report"]["id"])
    for gid in created:               # 跑批必须把临时群清干净
        assert (await store.get_group(gid)) is None
    await engine.dispose()            # 断开连接：别把绑在本循环上的连接留给下一个测试
    assert report is not None
    return report, created


def test_run_eval_rejects_empty_set() -> None:
    async def collect() -> None:
        async for _ in weval.run_eval(set_keys=["不存在的集合"]):
            pass

    with pytest.raises(ValueError):
        asyncio.run(collect())


def test_report_brief_drops_transcript() -> None:
    report = {"id": "x", "passed": 1, "total": 1, "cases": [{"name": "a", "transcript": [{"role": "patient"}]}]}
    brief = weval._brief(report)
    assert "transcript" not in brief["cases"][0]
    assert len(json.dumps(brief)) < len(json.dumps(report))
