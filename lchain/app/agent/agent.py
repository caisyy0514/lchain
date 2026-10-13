"""Agent 核心：LangChain v1 `create_agent`（底层是 LangGraph）+ Langfuse 追踪。"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError

from app.agent.ppt_workflow import PptWorkflowMiddleware
from app.agent.skills import available_skills, skills_prompt
from app.agent.tools import ALL_TOOLS, build_tools
from app.core.config import get_settings
from app.core.langfuse_client import get_langfuse, make_callback_handler, new_trace_id, trace_url
from app.core.llm import build_llm
from app.prompts.manager import ResolvedPrompt, resolve_prompt, resolve_prompt_async

logger = logging.getLogger(__name__)

# 会话记忆：按 session_id(thread_id) 保存多轮消息。生产环境可换成 PostgresSaver / RedisSaver
_checkpointer = InMemorySaver()


@dataclass
class ChatOptions:
    message: str
    session_id: str
    user_id: str = "anonymous"
    model: str | None = None
    temperature: float | None = None
    prompt_name: str = "agent-system"
    prompt_label: str | None = None
    prompt_version: int | None = None
    prompt_override: str | None = None       # 前端临时编辑的提示词
    prompt_variables: dict[str, Any] = field(default_factory=dict)
    use_tools: bool = True
    web_search: bool = True                  # 是否启用联网搜索
    use_skills: bool = True                  # 是否启用 Skills
    scenario: str | None = None              # 场景提示：consult（问诊）/ rural（山区问诊）/ ppt（做 PPT）/ None（通用对话）
    tags: list[str] = field(default_factory=list)


def _build_agent(opts: ChatOptions, prompt: ResolvedPrompt):
    s = get_settings()
    # 温度优先级：请求参数 > Langfuse 提示词 config > 全局默认
    temperature = opts.temperature
    if temperature is None:
        temperature = prompt.config.get("temperature", s.llm_temperature)
    model = opts.model or prompt.config.get("model") or s.llm_model
    llm = build_llm(model=model, temperature=temperature)
    tools = build_tools(use_tools=opts.use_tools, web_search=opts.web_search, skills=opts.use_skills)
    tool_names = {t.name for t in tools}
    skills = available_skills(tool_names) if "load_skill" in tool_names else []
    if not skills:  # 没有可用 skill 时不暴露 load_skill，避免模型乱调用
        tools = [t for t in tools if t.name != "load_skill"]
    system_prompt = prompt.text + _tools_hint(tool_names) + skills_prompt(skills)
    middleware = []
    if "web_search" in tool_names:
        # 超出次数后该工具返回错误提示，模型会基于已有信息作答（exit_behavior=continue）
        middleware += [
            ToolCallLimitMiddleware(tool_name="web_search", run_limit=s.search_max_calls),
            ToolCallLimitMiddleware(tool_name="fetch_webpage", run_limit=s.fetch_max_calls),
        ]
    for name in ("generate_image", "edit_image"):
        if name in tool_names:
            middleware.append(ToolCallLimitMiddleware(tool_name=name, run_limit=s.imagegen_max_calls))
    if "ppt_propose_outline" in tool_names and "ppt-report" in {sk.name for sk in skills}:
        # 每次调用模型前注入本会话 PPT 流程状态（需求 / 大纲版本 / 是否确认 / 下一步）
        middleware.append(PptWorkflowMiddleware(opts.session_id))
    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
        middleware=middleware,
        checkpointer=_checkpointer,
        name="lchain-agent",
    )
    return agent, model, temperature, system_prompt, [t.name for t in tools], [sk.name for sk in skills]


def _tools_hint(tool_names: set[str]) -> str:
    """根据启用的工具追加使用规范（与 Langfuse 中托管的业务提示词解耦）。"""
    if "web_search" not in tool_names:
        return ""
    return (
        "\n\n## 联网搜索\n"
        "你可以使用 `web_search` 搜索互联网、使用 `fetch_webpage` 阅读网页原文。\n"
        "- 涉及实时信息（新闻、价格、天气、比分、版本发布等）、你不确定或可能过时的知识时，**先搜索再回答**；"
        "常识性问题无需搜索。\n"
        "- 可以并行发起多个搜索；摘要不够时用 `fetch_webpage` 读原文。\n"
        "- 回答中引用搜索到的信息时，在句末用 [1]、[2] 标注，并在末尾列出「参考来源」（标题 + 链接）。\n"
        "- 搜索不到可靠信息时如实说明，不要编造。"
    )


def _run_config(opts: ChatOptions, prompt: ResolvedPrompt, trace_id: str | None) -> dict[str, Any]:
    handler = make_callback_handler(trace_id)
    metadata: dict[str, Any] = {
        # 以下 langfuse_* 字段会被 CallbackHandler 识别为 trace 属性
        "langfuse_session_id": opts.session_id,
        "langfuse_user_id": opts.user_id,
        "langfuse_tags": ["agent", f"prompt:{prompt.source}", *opts.tags],
        "prompt_name": prompt.name,
        "prompt_version": prompt.version,
        "prompt_source": prompt.source,
    }
    if prompt.client is not None:
        # 把 generation 关联到 Langfuse 中的提示词版本 -> 可在 Prompt 页面看到各版本的调用量 / 延迟 / 成本 / 评分
        metadata["langfuse_prompt"] = prompt.client
    return {
        "configurable": {"thread_id": opts.session_id},
        "callbacks": [handler] if handler else [],
        "metadata": metadata,
        "run_name": "agent-chat",
        "recursion_limit": 160,  # PPT 需要：读说明 → 找图/画示意图 → 写 spec → 生成 → 验证 → 修正重试
    }


async def _repair_dangling_tool_calls(agent, config: dict[str, Any], notice: str | None = None) -> None:
    """上一轮被中断（步数上限/异常）时，最后一条 AI 消息可能带着没有结果的 tool_calls，
    下一轮直接追加用户消息会被 OpenAI 兼容接口拒绝（tool_calls 后必须跟 tool 消息）。这里补上占位结果。"""
    try:
        state = await agent.aget_state(config)
    except Exception:  # noqa: BLE001
        return
    msgs = (state.values or {}).get("messages", []) if state else []
    answered = {m.tool_call_id for m in msgs if isinstance(m, ToolMessage)}
    last_ai = next((m for m in reversed(msgs) if isinstance(m, AIMessage)), None)
    patch: list = []
    if last_ai is not None:
        patch = [ToolMessage(content="（本轮已中止，未执行）", tool_call_id=tc["id"], name=tc["name"], status="error")
                 for tc in last_ai.tool_calls or [] if tc["id"] not in answered]
    if notice:
        patch.append(AIMessage(content=notice.strip()))
    if patch:
        await agent.aupdate_state(config, {"messages": patch})


def _recursion_notice(session_id: str) -> str:
    """步数耗尽时给用户的说明；若 PPT 流程已有成品，直接给出下载地址。"""
    from app.agent import ppt_workflow as wf

    msg = "\n\n---\n⚠️ 本轮操作步骤较多，已达到单轮上限，先停在这里。"
    try:
        state = wf.load_state(session_id)
    except ValueError:
        state = None
    if state and state.get("outputs"):
        name = Path(state["outputs"][-1]["file"]).name
        verify = state.get("last_verify") or {}
        passed = verify.get("file") == name and verify.get("passed")
        msg += (f"\n\n已生成的 PPT：[{name}](/api/reports/{name})"
                f"（{'已通过渲染验证' if passed else '尚未完成验证'}）。")
        if verify.get("preview") and verify.get("file") == name:
            msg += f"\n\n![PPT 预览]({verify['preview']})"
        msg += "\n\n如需继续调整，直接告诉我要改哪里即可。"
    elif state and state.get("last_error"):
        msg += f"\n\n生成还没有成功，最近一次的原因：{state['last_error'][:200]}。回复「继续」我会接着处理，也可以告诉我怎么精简。"
    else:
        msg += "回复「继续」我会接着处理。"
    return msg


def _sse(event: str, data: Any) -> dict[str, str]:
    return {"event": event, "data": json.dumps(data, ensure_ascii=False, default=str)}


async def stream_chat(opts: ChatOptions) -> AsyncIterator[dict[str, str]]:
    """流式对话，产出 SSE 事件：meta / token / replace / tool_start / tool_end / done / error。

    `scenario="consult"` 时**不经过通用对话 Agent**，直接派发到问诊工作流
    （`app/consult/graph.py`）——流程由图和判断提示词决定，不由模型自由发挥；
    `scenario="rural"` 同理派发到山区远程问诊工作流（`app/rural/graph.py`）。
    SSE 事件格式保持一致，前端对话页与进度条无需改动。
    """
    if opts.scenario == "consult":
        from app.consult.graph import run_stream as consult_stream

        logger.info("转到结构化问诊工作流 session=%s", opts.session_id)
        async for event in consult_stream(session_id=opts.session_id, message=opts.message,
                                         overrides=None, user_id=opts.user_id):
            yield event
        return

    if opts.scenario == "rural":
        from app.rural.graph import run_stream as rural_stream

        logger.info("转到山区远程问诊工作流 session=%s", opts.session_id)
        async for event in rural_stream(session_id=opts.session_id, message=opts.message,
                                       overrides=None, user_id=opts.user_id):
            yield event
        return

    # 走线程池解析（Langfuse 拉取是同步 HTTP，不能卡事件循环）
    prompt = await resolve_prompt_async(
        opts.prompt_name, label=opts.prompt_label, version=opts.prompt_version,
        override_text=opts.prompt_override, variables=opts.prompt_variables,
    )
    _last_extra: dict[str, Any] = {}   # 本轮的工具调用 / trace / usage，落盘时用
    agent, model, temperature, system_prompt, tool_names, skill_names = _build_agent(opts, prompt)
    trace_id = new_trace_id()
    config = _run_config(opts, prompt, trace_id)
    config["metadata"].update({"tools": tool_names, "skills": skill_names})
    if "web_search" in tool_names:
        config["metadata"]["langfuse_tags"].append("web-search")

    yield _sse("meta", {
        "trace_id": trace_id, "trace_url": trace_url(trace_id), "model": model,
        "temperature": temperature, "prompt": prompt.meta(), "system_prompt": system_prompt,
        "tools": tool_names, "skills": skill_names,
    })

    await _repair_dangling_tool_calls(agent, config)
    # 本轮的工具调用（供落盘：重开历史会话时能还原工具卡片）
    collected: list[dict[str, Any]] = []
    try:
        # messages 模式拿逐 token 输出；updates 模式拿每一步完整的工具调用参数和结果
        async for mode, data in agent.astream(
            {"messages": [{"role": "user", "content": opts.message}]},
            config=config, stream_mode=["messages", "updates"],
        ):
            if mode == "messages":
                chunk, _ = data
                if isinstance(chunk, AIMessageChunk) and isinstance(chunk.content, str) and chunk.content:
                    yield _sse("token", {"content": chunk.content})
                continue
            for update in (data or {}).values():
                for m in (update or {}).get("messages", []) if isinstance(update, dict) else []:
                    if isinstance(m, AIMessage):
                        for tc in m.tool_calls or []:
                            collected.append({"id": tc["id"], "name": tc["name"], "args": tc["args"],
                                              "output": "", "status": "success"})
                            yield _sse("tool_start", {"id": tc["id"], "name": tc["name"], "args": tc["args"]})
                    elif isinstance(m, ToolMessage):
                        row = next((c for c in collected if c["id"] == m.tool_call_id), None)
                        if row is not None:
                            row["output"] = str(m.content)[:3000]
                            row["status"] = m.status or "success"
                        yield _sse("tool_end", {"id": m.tool_call_id, "name": m.name,
                                                "output": str(m.content)[:3000], "status": m.status})

        state = await agent.aget_state(config)
        last_ai = next((m for m in reversed(state.values.get("messages", [])) if isinstance(m, AIMessage)), None)
        usage = getattr(last_ai, "usage_metadata", None) if last_ai else None
        _last_extra.update({"tools": collected or None, "trace_id": trace_id,
                            "trace_url": trace_url(trace_id), "usage": usage or None})
        yield _sse("done", {"trace_id": trace_id, "trace_url": trace_url(trace_id), "usage": usage})
    except GraphRecursionError:
        # 步数用完（多见于 PPT 生成反复修正）：不抛原始异常，告诉用户已有的成果和下一步
        logger.warning("agent 步数达到上限 session=%s", opts.session_id)
        notice = _recursion_notice(opts.session_id)
        await _repair_dangling_tool_calls(agent, config, notice)  # 记入对话，下一轮模型知道停在哪里
        yield _sse("token", {"content": notice})
        yield _sse("done", {"trace_id": trace_id, "trace_url": trace_url(trace_id), "usage": None,
                            "truncated": True})
    except Exception as e:  # noqa: BLE001
        logger.exception("agent 执行失败")
        yield _sse("error", {"message": f"{type(e).__name__}: {e}", "trace_id": trace_id})
    finally:
        await _persist_session(opts.session_id, agent, config, extra=_last_extra or None)
        lf = get_langfuse()
        if lf:
            await asyncio.to_thread(lf.flush)   # 同步等待上报，放线程里


async def get_history(session_id: str) -> list[dict[str, Any]]:
    """会话历史：优先读落盘的记录（服务重启也在），没有再回退到内存 checkpointer。"""
    from app.agent.sessions import load_session

    saved = load_session(session_id)
    if saved and saved.get("messages"):
        # 把落盘时记下的工具调用 / trace 入口一并返回，前端能还原工具卡片
        return [{"role": m["role"], "content": m["content"], "tools": m.get("tools"),
                 "trace_id": m.get("trace_id"), "trace_url": m.get("trace_url"),
                 "usage": m.get("usage")} for m in saved["messages"]]
    agent = create_agent(model=build_llm(), tools=ALL_TOOLS, checkpointer=_checkpointer)
    state = await agent.aget_state({"configurable": {"thread_id": session_id}})
    out = []
    for m in state.values.get("messages", []) if state and state.values else []:
        if m.type in ("human", "ai") and m.content:
            out.append({"role": "user" if m.type == "human" else "assistant", "content": m.content})
    return out


async def _persist_session(session_id: str, agent, config: dict[str, Any],
                           extra: dict[str, Any] | None = None) -> None:
    """把本轮对话同步到磁盘（会话列表 / 重启后看历史都靠它）。"""
    from app.agent.sessions import save_session

    try:
        state = await agent.aget_state(config)
        msgs = (state.values or {}).get("messages", []) if state else []
        rows = [{"role": "user" if m.type == "human" else "assistant", "content": m.content}
                for m in msgs if m.type in ("human", "ai") and isinstance(m.content, str) and m.content]
        if rows:
            save_session(session_id, rows, extra_last_assistant=extra)
    except Exception:  # noqa: BLE001  落盘失败不影响对话本身
        logger.warning("会话记录落盘失败 session=%s", session_id, exc_info=True)


async def clear_session(session_id: str) -> None:
    await _checkpointer.adelete_thread(session_id)
