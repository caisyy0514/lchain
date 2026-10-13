"""Agent 工具集。新增工具：写一个带 @tool 装饰器的函数，然后加到 ALL_TOOLS 里即可。"""
from __future__ import annotations

import ast
import math
import operator
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.tools import tool


@tool
def get_current_time(timezone: str = "Asia/Shanghai") -> str:
    """获取指定时区的当前日期和时间。timezone 为 IANA 时区名，例如 Asia/Shanghai、America/New_York。"""
    try:
        now = datetime.now(ZoneInfo(timezone))
    except Exception:  # noqa: BLE001
        return f"未知时区: {timezone}"
    return now.strftime(f"%Y-%m-%d %H:%M:%S %A ({timezone})")


_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}
_FUNCS = {k: getattr(math, k) for k in ("sqrt", "sin", "cos", "tan", "log", "log10", "exp", "floor", "ceil")}
_FUNCS.update(abs=abs, round=round)
_CONSTS = {"pi": math.pi, "e": math.e}


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        return _FUNCS[node.func.id](*[_safe_eval(a) for a in node.args])
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    raise ValueError(f"不支持的表达式: {ast.dump(node)[:60]}")


@tool
def calculator(expression: str) -> str:
    """计算数学表达式，支持 + - * / // % **、括号以及 sqrt/sin/cos/log/exp/abs/round 等函数和 pi/e 常量。"""
    try:
        return str(_safe_eval(ast.parse(expression, mode="eval")))
    except Exception as e:  # noqa: BLE001
        return f"计算失败: {e}"


@tool
def get_weather(city: str) -> str:
    """查询城市天气（示例工具，返回模拟数据，可替换为真实天气 API）。"""
    import random

    rnd = random.Random(city + datetime.now().strftime("%Y%m%d"))
    cond = rnd.choice(["晴", "多云", "阴", "小雨", "雷阵雨"])
    return f"{city} 今日天气：{cond}，气温 {rnd.randint(12, 30)}°C，湿度 {rnd.randint(30, 90)}%（模拟数据）"


BASIC_TOOLS = [get_current_time, calculator, get_weather]


def build_tools(*, use_tools: bool = True, web_search: bool = True, skills: bool = True) -> list:
    """按开关组装工具集。新增工具：在对应分组里添加即可。"""
    from app.agent.imagegen import IMAGE_TOOLS, imagegen_enabled
    from app.agent.ppt_media import MEDIA_TOOLS
    from app.agent.ppt_workflow import WORKFLOW_TOOLS
    from app.agent.report_tools import REPORT_TOOLS
    from app.agent.report_styles import STYLE_TOOLS
    from app.agent.search import SEARCH_TOOLS
    from app.agent.skills import load_skill

    if not use_tools:
        return []
    tools = list(BASIC_TOOLS)
    if web_search:
        tools += SEARCH_TOOLS
    if skills:
        tools.append(load_skill)
        tools += REPORT_TOOLS  # 报告类 Skill（html/word/ppt-report）写 spec、运行生成脚本
        tools += STYLE_TOOLS  # 报告自定义样式：列出预设、样张预览、保存命名样式
        tools += WORKFLOW_TOOLS + MEDIA_TOOLS  # PPT 制作流程（需求→大纲确认→生成→验证）与图文素材
        # 结构化问诊是独立工作流（app/consult），不再作为工具注册到通用对话 Agent
        if imagegen_enabled():
            tools += IMAGE_TOOLS  # AI 生图 / 改图（skills/imagegen），未配置 IMAGEGEN_API_KEY 时不注册
    return tools


ALL_TOOLS = build_tools()
