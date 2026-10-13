"""结构化问诊场景（与 `app/wecom/` 平级的业务模块）。

对话页顶部导航「🩺 问诊」进入（`/?scenario=consult`）。**这是一个显式工作流，不是通用 Agent 的场景**：
一次运行 = 患者发一条消息 → 回一条，流程由图决定、判断由专门的提示词决定、安全边界由代码渲染。

    现病史采集（多轮）→ 分诊中间提示 → 鉴别问询（多轮）→ 诊断意见
      → 患者报告病情变化 → 追问变化 → 更新版诊断 → 结束

模块划分：
- `gates.py`      10 项现病史要素、轮次门槛、动作白名单与 `clamp_action`、问题数量硬截断、安全边界常量
- `state.py`      `consult.json` 读写（含对话记录），服务重启不丢
- `prompts.py`    `consult-*` 默认提示词（判断类 JSON / 生成类带 max_chars）与 `PROMPT_TIER`
- `llm.py`        `call_json`（判断）/ `stream_text`（生成，流式）
- `graph.py`      StateGraph 节点、路由、`run_stream()`（对话页 SSE）
- `render.py`     分诊 / 诊断 / 结束语的代码渲染（免责声明、急诊提示、无药物剂量由代码保证）

与 `app/wecom/` 的区别：企微群是独立子系统（自己的 LangGraph + MySQL 表 + worker + 定时任务进程）；
问诊也是工作流，但没有数据库、没有后台任务，状态就是一个 JSON 文件。

注意：`graph` / `llm` 这两个子模块**延迟导入**。因为 `app/prompts/manager.py` 会把
`app.consult.prompts` 合并进 DEFAULT_PROMPTS，如果在 `__init__` 里急切导入 graph，
就会出现 `manager → consult.prompts → consult.__init__ → graph → llm → manager` 的循环导入。
"""
from __future__ import annotations

from typing import Any

from app.consult import render
from app.consult.gates import (
    ACTIONS,
    DISCLAIMER,
    EMERGENCY_HINT,
    MAX_QUESTIONS_PER_TURN,
    MIN_DIFFERENTIAL_ROUNDS,
    MIN_INTAKE_ROUNDS,
    REQUIRED_HISTORY,
    STAGES,
    URGENCY,
    clamp_action,
    is_question,
    limit_questions,
    missing_history,
)
from app.consult.prompts import CONSULT_PROMPTS, PROMPT_TIER
from app.consult.state import (
    load_state,
    new_state,
    reset_state,
    save_state,
    session_dir,
    session_dir_name,
)

# 延迟导入：避免与 app.prompts.manager 形成循环（见模块 docstring）
_LAZY: dict[str, str] = {
    "run_stream": "app.consult.graph",
    "build_graph": "app.consult.graph",
    "get_graph": "app.consult.graph",
    "call_json": "app.consult.llm",
    "stream_text": "app.consult.llm",
    "compile_prompt": "app.consult.llm",
    "model_for": "app.consult.llm",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_name), name)


__all__ = [
    # 常量与门禁
    "STAGES", "REQUIRED_HISTORY", "URGENCY", "ACTIONS", "MIN_INTAKE_ROUNDS",
    "MIN_DIFFERENTIAL_ROUNDS", "MAX_QUESTIONS_PER_TURN", "DISCLAIMER", "EMERGENCY_HINT",
    "missing_history", "clamp_action", "limit_questions", "one_line", "is_question",
    # 状态
    "load_state", "save_state", "reset_state", "new_state", "session_dir", "session_dir_name",
    # 提示词与渲染
    "CONSULT_PROMPTS", "PROMPT_TIER", "render",
    # 工作流（延迟导入）
    "run_stream", "build_graph", "get_graph", "call_json", "stream_text", "compile_prompt", "model_for",
]
