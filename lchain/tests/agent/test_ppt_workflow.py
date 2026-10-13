"""PPT 制作工作流（app/agent/ppt_workflow.py）与生成门禁、验证工具的测试。

用临时工作区和伪造的 ToolRuntime 模拟多轮对话：需求 → 大纲 → 修改 → 确认 → 生成 → 验证。
运行：PYTHONPATH=. .venv/bin/python -m pytest tests/agent -q
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent import ppt_workflow as wf


class Chat:
    """伪造对话：维护消息列表，生成工具需要的 runtime（state.messages + config.thread_id）。"""

    def __init__(self, session_id: str = "testsession01"):
        self.session_id = session_id
        self.messages: list = []
        self._n = 0

    def user(self, text: str) -> "Chat":
        self._n += 1
        self.messages.append(HumanMessage(content=text, id=f"u{self._n}"))
        return self

    def ai(self, text: str) -> "Chat":
        self.messages.append(AIMessage(content=text))
        return self

    @property
    def runtime(self):
        return SimpleNamespace(state={"messages": list(self.messages)},
                               config={"configurable": {"thread_id": self.session_id}})


def call(t, chat: Chat, **args):
    """按 ToolNode 的方式调用工具（注入 runtime）。"""
    if getattr(t, "coroutine", None):
        return asyncio.run(t.coroutine(**args, runtime=chat.runtime))
    return t.func(**args, runtime=chat.runtime)


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    from app.core import config

    config.get_settings.cache_clear()
    monkeypatch.setenv("REPORT_WORKSPACE", str(tmp_path / "ws"))
    yield tmp_path / "ws"
    config.get_settings.cache_clear()


REQ = dict(topic="高血压居家管理", audience="社区居民", purpose="健康讲座", slide_count="6 页")


def outline(n_visual: int = 3, extra_title: str | None = None) -> dict:
    slides = [{"title": "高血压居家管理", "layout": "title"}]
    for i in range(4):
        visual = i < n_visual
        slides.append({
            "title": f"第{i + 1}部分", "layout": "columns" if visual else "bullets",
            "key_message": f"观点{i + 1}", "points": ["要点 A", "要点 B"],
            "visual": {"kind": "diagram", "description": "流程示意", "source": "示意图生成"} if visual else {"kind": "none"},
        })
    if extra_title:
        slides.append({"title": extra_title, "layout": "bullets", "key_message": "补充", "points": ["x"],
                       "visual": {"kind": "none"}})
    return {"title": "高血压居家管理", "slides": slides}


# ------------------------------------------------------------------ 回复识别
@pytest.mark.parametrize("text,expected", [
    ("确认", "confirm"), ("可以，开始生成吧", "confirm"), ("没问题", "confirm"), ("OK", "confirm"),
    ("挺不错，不用改了", "confirm"), ("好的", "confirm"),
    ("第3页换成示意图", "revise"), ("可以，但是再加一页饮食建议", "revise"), ("不行，太长了", "revise"),
    ("嗯", "unclear"), ("", "unclear"),
])
def test_classify_reply(text, expected):
    assert wf.classify_reply(text) == expected


# ------------------------------------------------------------------ 大纲校验
def test_outline_validation_rules():
    errors, _ = wf.validate_outline(outline(n_visual=1), {"visual_density": "rich"})
    assert any("图文密度不足" in e for e in errors)
    errors, _ = wf.validate_outline(outline(n_visual=1), {"visual_density": "minimal"})
    assert errors == []
    bad = outline()
    bad["slides"][1]["visual"] = {"kind": "none"}  # columns 没有配图
    bad["slides"][2]["title"] = bad["slides"][3]["title"]  # 标题重复
    errors, _ = wf.validate_outline(bad, {"visual_density": "minimal"})
    assert any("columns" in e for e in errors) and any("重复" in e for e in errors)
    long_title = outline()
    long_title["slides"][0]["title"] = "血压稳，日子安——高血压居家管理"
    errors, _ = wf.validate_outline(long_title, None)
    assert any("标题" in e and "过长" in e for e in errors)
    bad = outline()
    bad["slides"][1]["visual"]["source"] = ""
    bad["slides"][1]["visual"]["kind"] = "image"
    errors, _ = wf.validate_outline(bad, None)
    assert any("visual.source" in e for e in errors)


# ------------------------------------------------------------------ 完整流程
def test_full_flow_with_revision_and_gates(workspace):
    from app.agent.report_tools import report_run_generator

    chat = Chat().user("帮我做一个高血压科普 PPT")
    # 1. 需求有待回答的问题 -> 本轮不能出大纲
    out = call(wf.ppt_save_requirements, chat, topic="高血压", open_questions=["听众是谁？", "讲多久？"])
    assert "待用户回答" in out
    assert "❌" in call(wf.ppt_propose_outline, chat, outline=outline())

    # 用户回答后更新需求，出大纲
    chat.ai("请问听众是谁？讲多久？").user("社区居民，6 页左右")
    assert "需求已齐全" in call(wf.ppt_save_requirements, chat, **REQ)
    out = call(wf.ppt_propose_outline, chat, outline=outline())
    assert "大纲 v1" in out and "| 页 |" in out

    # 2. 同一轮不能确认；未确认不能生成
    assert "还没有对这版大纲作出回复" in call(wf.ppt_confirm_outline, chat, version=1)
    spec_dir = wf.session_dir(chat.session_id)
    spec = {"title": "高血压居家管理", "slides": [{"type": "title", "title": "高血压居家管理"}]}
    (spec_dir / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    spec_path = wf.repo_path(spec_dir / "spec.json")
    out = call(report_run_generator, chat, skill="ppt-report", spec_path=spec_path)
    assert "尚未经用户确认" in out

    # 3. 用户提出修改 -> 不能确认，提交 v2
    chat.ai("大纲如上").user("可以，但再加一页饮食建议")
    assert "修改意见" in call(wf.ppt_confirm_outline, chat, version=1)
    out = call(wf.ppt_propose_outline, chat, outline=outline(extra_title="饮食建议"))
    assert "大纲 v2" in out
    chat.ai("v2 大纲").user("确认")
    assert "只能确认最新" in call(wf.ppt_confirm_outline, chat, version=1)
    assert "✅" in call(wf.ppt_confirm_outline, chat, version=2)
    assert wf.load_state(chat.session_id)["stage"] == "approved"

    # 4. spec 缺大纲中的页 -> 拒绝；spec 不在会话目录 -> 拒绝
    out = call(report_run_generator, chat, skill="ppt-report", spec_path=spec_path)
    assert "与用户确认的大纲不一致" in out and "饮食建议" in out
    other = workspace / "threads" / "other" / "spec.json"
    other.parent.mkdir(parents=True)
    other.write_text("{}", encoding="utf-8")
    assert "本会话目录" in call(report_run_generator, chat, skill="ppt-report", spec_path="threads/other/spec.json")

    # 需求变化 -> 确认失效
    call(wf.ppt_save_requirements, chat, **{**REQ, "slide_count": "12 页"})
    state = wf.load_state(chat.session_id)
    assert state["approved_version"] is None and state["stage"] == "requirements"


def test_spec_outline_check_tolerates_continuation_titles():
    o = outline()
    slides = [{"type": "title", "title": "高血压居家管理"}]
    for i in range(4):
        slides.append({"type": "columns", "title": f"第{i + 1}部分（1/2）",
                       "left": [{"type": "text", "text": "x"}],
                       "right": [{"type": "image", "path": "/repo/a.png"}]})
    errors, warnings = wf.check_spec_against_outline({"slides": slides}, o, {"visual_density": "rich"})
    assert errors == [] and warnings == []


def test_middleware_injects_stage(workspace):
    chat = Chat("mwsession").user("做个 PPT")
    mw = wf.PptWorkflowMiddleware(chat.session_id)
    assert "PPT 制作流程（强制）" in mw._extra() and "流程状态" not in mw._extra()
    call(wf.ppt_save_requirements, chat, **REQ)
    assert "需求整理中" in mw._extra()


def test_generate_and_verify_end_to_end(workspace):
    """确认大纲 → 画示意图 → 生成真实 PPTX → ppt_verify。需要本机有 PPT 校验器与中文字体，缺失时跳过。"""
    from app.agent import ppt_media
    from app.agent.report_tools import report_run_generator

    if not ppt_media.cjk_font_path():
        pytest.skip("没有中文字体")
    chat = Chat("e2esession").user("做 PPT")
    call(wf.ppt_save_requirements, chat, **REQ)
    call(wf.ppt_propose_outline, chat, outline=outline())
    chat.ai("大纲").user("确认大纲")
    assert "✅" in call(wf.ppt_confirm_outline, chat, version=1)
    img = call(ppt_media.ppt_make_diagram, chat, kind="flow", name="steps",
               items=[{"label": "静坐", "detail": "5 分钟"}, {"label": "测量", "detail": "上臂"},
                      {"label": "记录", "detail": "早晚"}])
    assert "✅" in img
    path = img.split("示意图 ", 1)[1].split("（", 1)[0]
    slides = [{"type": "title", "title": "高血压居家管理"}]
    for i in range(4):
        if i < 3:
            slides.append({"type": "columns", "title": f"第{i + 1}部分",
                           "left": [{"type": "bullets", "bullets": ["要点 A", "要点 B"]}],
                           "right": [{"type": "image", "path": path, "caption": "图：流程（示意图）",
                                      "source": "示意图"}]})
        else:
            slides.append({"type": "bullets", "title": f"第{i + 1}部分", "bullets": ["要点 A"]})
    d = wf.session_dir(chat.session_id)
    (d / "spec.json").write_text(json.dumps({"title": "高血压居家管理", "slides": slides}, ensure_ascii=False),
                                 encoding="utf-8")
    out = call(report_run_generator, chat, skill="ppt-report", spec_path=wf.repo_path(d / "spec.json"))
    if "失败" in out.splitlines()[0]:
        pytest.skip(f"生成环境不完整：{out[-300:]}")
    assert wf.load_state(chat.session_id)["stage"] == "generated"
    out = call(ppt_media.ppt_verify, chat)
    state = wf.load_state(chat.session_id)
    assert state["last_verify"]["stats"]["pictures"] == 3
    if state["last_verify"]["rendered"]:
        assert "✅ 验证通过" in out, out
        assert state["stage"] == "delivered"


def _approve(chat: Chat) -> None:
    call(wf.ppt_save_requirements, chat, **REQ)
    call(wf.ppt_propose_outline, chat, outline=outline())
    chat.ai("大纲").user("确认")
    assert "✅" in call(wf.ppt_confirm_outline, chat, version=1)


def test_no_regenerate_after_verified_in_same_turn():
    """验证通过后，同一轮不允许再生成（避免模型按优化建议反复重做、耗尽步数）；用户新消息后可以。"""
    chat = Chat("deliversession").user("做 PPT")
    _approve(chat)
    d = wf.session_dir(chat.session_id)
    spec = d / "spec.json"
    spec.write_text("{}", encoding="utf-8")
    turn = wf.latest_user_turn(chat.runtime)
    wf.after_generate(chat.session_id, spec, "/repo/o_outcome/x.pptx", True, turn=turn)
    wf.record_verify(chat.session_id, {"file": "x.pptx", "passed": True, "summary": ""}, turn)
    reason, _ = wf.before_generate(chat.session_id, spec, turn)
    assert reason and "不要再自行重新生成" in reason
    chat.ai("已交付").user("第 3 页字大一点")
    reason, _ = wf.before_generate(chat.session_id, spec, wf.latest_user_turn(chat.runtime))
    assert reason is None or "不要再自行重新生成" not in reason


def test_generate_failure_limit_per_turn():
    chat = Chat("failsession").user("做 PPT")
    _approve(chat)
    spec = wf.session_dir(chat.session_id) / "spec.json"
    turn = wf.latest_user_turn(chat.runtime)
    for _ in range(wf.MAX_FAILS_PER_TURN):
        wf.after_generate(chat.session_id, spec, None, False, error="ERROR: 表格超出", turn=turn)
    reason, _ = wf.before_generate(chat.session_id, spec, turn)
    assert reason and "连续生成失败" in reason and "表格超出" in reason
    chat.ai("卡住了").user("那就拆成两页")
    reason, _ = wf.before_generate(chat.session_id, spec, wf.latest_user_turn(chat.runtime))
    assert reason is None or "连续生成失败" not in reason


def test_recursion_limit_returns_friendly_notice_and_repairs_history(monkeypatch):
    """步数耗尽时不返回原始异常，给出已有成果；下一轮对话不会因为悬空的 tool_calls 报错。"""
    from langchain_core.language_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult

    from app.agent import agent as ag

    class LoopModel(BaseChatModel):
        """每次都要求调用工具的假模型，用来触发步数上限。"""
        loop: bool = True
        n: int = 0

        @property
        def _llm_type(self):
            return "loop"

        def bind_tools(self, tools, **kw):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            self.n += 1
            msg = (AIMessage(content="", tool_calls=[{"name": "calculator", "args": {"expression": "1+1"},
                                                      "id": f"c{self.n}"}])
                   if self.loop else AIMessage(content="好的"))
            return ChatResult(generations=[ChatGeneration(message=msg)])

    monkeypatch.setattr(ag, "build_llm", lambda **kw: LoopModel())
    monkeypatch.setattr(ag, "_run_config", lambda o, p, t: {"configurable": {"thread_id": o.session_id},
                                                            "callbacks": [], "metadata": {}, "recursion_limit": 6})
    chat = Chat("loopsession")
    call(wf.ppt_save_requirements, chat.user("x"), **REQ)

    async def run(msg):
        events = []
        async for e in ag.stream_chat(ag.ChatOptions(message=msg, session_id="loopsession", web_search=False)):
            events.append(e)
        return events

    events = asyncio.run(run("算一下"))
    kinds = [e["event"] for e in events]
    assert "error" not in kinds and "done" in kinds
    assert any("单轮上限" in e["data"] for e in events if e["event"] == "token")
    # 第二轮：历史中的悬空 tool_calls 已补齐，不会报错
    monkeypatch.setattr(ag, "build_llm", lambda **kw: LoopModel(loop=False))
    events = asyncio.run(run("继续"))
    assert "error" not in [e["event"] for e in events]
    asyncio.run(ag.clear_session("loopsession"))
