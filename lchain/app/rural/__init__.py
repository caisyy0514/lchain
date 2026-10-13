"""山区远程问诊场景（与 `app/wecom/`、`app/consult/` 平级的业务模块）。

面向**偏远山区、医疗条件差地区的村民本人**（多为老人、低文化水平、可能说方言）。
顶部导航「🏔 山区问诊」进入（`/?scenario=rural`）。和结构化问诊一样是显式工作流：

    现病史采集（多轮）→ 问就医条件 → 就医建议（去哪个科、去哪一级、怎么去）
      → 鉴别问询 → 诊断意见（就地能做的处理 + 转诊安排）
      → 患者报告变化 → 追问 → 更新版诊断 → 结束

与 `app/consult/`（城市线上问诊）的差异，正是这个模块存在的理由：
- 多一问「就医条件」：到最近能看病的地方多远、有没有车、有没有人能陪 → 决定处置层级
- 结论落到**去哪一级医院**（居家 / 村卫生室 / 乡镇卫生院 / 县医院 / 打 120）与**路上怎么走**
- 建议只落在当地做得到的范围（`rural_local_resources`），做不了的写清到哪儿能做
- 一次只问一个问题、更短更口语；意外伤害/中毒场景自动追加**正确**的急救要点

模块划分：
- `gates.py`    现病史要素（复用 `app.consult.gates`）+ 就医条件要素、处置层级、`clamp_action`、
                无剂量兜底 `strip_dosage`、急救要点表 `FIRST_AID`
- `state.py`    `rural.json` 读写（含对话记录），服务重启不丢
- `prompts.py`  `rural-*` 默认提示词（判断类 JSON / 生成类带 max_chars）
- `llm.py`      `call_json`（判断）/ `stream_text`（生成），复用 `app/core/prompt_llm.py`
- `graph.py`    StateGraph 节点、路由、`run_stream()`（对话页 SSE）
- `render.py`   就医建议 / 诊断 / 结束语的代码渲染（免责声明、急诊提示、急救要点由代码保证）
- `cases.py`    从 `eval/rural/**/*.md` 加载用例

注意：`graph` / `llm` 延迟导入。因为 `app/prompts/manager.py` 会把 `app.rural.prompts` 合并进
DEFAULT_PROMPTS，如果在 `__init__` 里急切导入 graph，就会形成
`manager → rural.prompts → rural.__init__ → graph → llm → manager` 的循环导入。
"""
from __future__ import annotations

from typing import Any

from app.rural import render
from app.rural.gates import (
    ACCESS_KEYS,
    ACTIONS,
    DISPOSITIONS,
    DISCLAIMER,
    emergency_hint,
    FIRST_AID,
    MAX_QUESTIONS_PER_TURN,
    MIN_DIFFERENTIAL_ROUNDS,
    MIN_INTAKE_ROUNDS,
    REQUIRED_HISTORY,
    RURAL_FIELDS,
    RURAL_RED_FLAGS,
    STAGES,
    URGENCY,
    clamp_action,
    first_aid_for,
    is_question,
    limit_questions,
    missing_access,
    missing_history,
    strip_dosage,
)
from app.rural.prompts import PROMPT_TIER, RURAL_PROMPTS
from app.rural.state import (
    load_state,
    new_state,
    reset_state,
    save_state,
    session_dir,
    session_dir_name,
)

# 延迟导入：避免与 app.prompts.manager 形成循环（见模块 docstring）
_LAZY: dict[str, str] = {
    "run_stream": "app.rural.graph",
    "build_graph": "app.rural.graph",
    "get_graph": "app.rural.graph",
    "call_json": "app.rural.llm",
    "stream_text": "app.rural.llm",
    "compile_prompt": "app.rural.llm",
    "model_for": "app.rural.llm",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_name), name)


__all__ = [
    # 常量与门禁
    "STAGES", "REQUIRED_HISTORY", "RURAL_FIELDS", "ACCESS_KEYS", "DISPOSITIONS", "URGENCY",
    "ACTIONS", "MIN_INTAKE_ROUNDS", "MIN_DIFFERENTIAL_ROUNDS", "MAX_QUESTIONS_PER_TURN",
    "DISCLAIMER", "emergency_hint", "RURAL_RED_FLAGS", "FIRST_AID",
    "missing_history", "missing_access", "clamp_action", "limit_questions", "is_question",
    "strip_dosage", "first_aid_for",
    # 状态
    "load_state", "save_state", "reset_state", "new_state", "session_dir", "session_dir_name",
    # 提示词与渲染
    "RURAL_PROMPTS", "PROMPT_TIER", "render",
    # 工作流（延迟导入）
    "run_stream", "build_graph", "get_graph", "call_json", "stream_text", "compile_prompt", "model_for",
]
