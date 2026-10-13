"""工作流用例跑批的通用机制：后台任务 + 报告存储 + 统计 + DeepSeek 分析。

`app/consult/eval.py`（城市结构化问诊）与 `app/rural/eval.py`（山区远程问诊）共用这一套：
**跑批是后台任务，不跟着浏览器连接走**——一轮评测要几分钟，如果跟着 SSE 连接跑，用户一刷新
就把任务取消了（本项目真实踩过）。这里把任务放进 `asyncio.Task`，状态留在进程内存里，
前端只负责「POST 启动 + 轮询状态」，刷新/换页面都不影响任务本身。

各工作流只提供三样领域相关的东西：
- `run_case(case, idx, emit)`：怎么跑一条用例（AI 扮演患者 ↔ 工作流），返回一条结果 dict
- `check_case(case, data, replies, scenario)`：怎么判定这条用例通过（在各自的 eval 模块里）
- `checks`：判定项 key 列表（统计与页面展示用）与 AI 分析的背景说明 `intro`

注意：状态在**进程内存**里（多 web 节点部署时只有发起的那台能看到进度；报告本身落盘，
所以 `/eval` 页在任何节点都能看历史）。这是与企微群那套 MySQL 存储的区别——问诊类跑批是
开发者/运营自己触发的，不需要跨节点。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.llm import build_llm

logger = logging.getLogger(__name__)

MAX_ROUNDS = 14             # 单个用例最多对话轮数（患者模拟 + 工作流来回）
LONG_REPLY_CHARS = 600      # 非问句块超过这个字数算「回复过长」（结构化块另有宽限）


def evals_dir() -> Path:
    """报告目录：工作区下的 `evals/`（与报告类 Skill 同一工作区）。"""
    from app.agent.report_tools import workspace

    d = workspace() / "evals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def stats_of(rows: list[dict[str, Any]], checks: tuple[str, ...]) -> dict[str, Any]:
    """整轮统计：通过率、分项通过率、轮次/字数/耗时均值、失败用例名。"""
    n = len(rows) or 1
    ok = [r for r in rows if r.get("passed")]
    with_replies = [r for r in rows if r.get("reply_chars")]
    return {
        "pass_rate": round(len(ok) / n * 100),
        "by_check": {c: round(sum(1 for r in rows if (r.get("checks") or {}).get(c)) / n * 100) for c in checks},
        "avg_rounds": round(sum(sum((r.get("rounds") or {}).values()) for r in rows) / n, 1),
        "avg_reply_chars": round(sum(r.get("avg_chars", 0) for r in with_replies) / max(len(with_replies), 1)),
        "max_reply_chars": max([max(r.get("reply_chars") or [0]) for r in rows] or [0]),
        "avg_duration_ms": round(sum(r.get("duration_ms", 0) for r in rows) / n),
        "failed": [r["name"] for r in rows if not r.get("passed")],
    }


def brief_of(report: dict[str, Any]) -> dict[str, Any]:
    """列表/进度用的简报：去掉每个用例的完整对话记录（避免响应过大）。"""
    return {**{k: v for k, v in report.items() if k != "cases"},
            "cases": [{k: v for k, v in c.items() if k != "transcript"} for c in report.get("cases") or []]}


# ------------------------------------------------------------------ 跑批任务（与浏览器连接解耦）
@dataclass
class RunState:
    id: str
    set_label: str
    total: int
    started_at: str
    done: int = 0
    current: str = ""
    lines: list[str] = field(default_factory=list)
    status: str = "running"          # running / done / failed / cancelled
    report_id: str | None = None
    error: str | None = None
    task: Any = None
    _t0: float = field(default_factory=time.perf_counter)

    def brief(self) -> dict[str, Any]:
        return {"id": self.id, "status": self.status, "sets": self.set_label, "total": self.total,
                "done": self.done, "current": self.current, "report_id": self.report_id,
                "error": self.error, "started_at": self.started_at,
                "elapsed_ms": int((time.perf_counter() - self._t0) * 1000),
                "lines": self.lines[-60:]}


def line_of(event: dict[str, Any]) -> str | None:
    """把跑批事件转成前端进度框里的一行日志。"""
    kind = event.get("type")
    if kind == "start":
        return f"开始：{event['total']} 个用例（{event.get('model')}）"
    if kind == "case_start":
        return f"\n[{event['case'] + 1}/{event.get('total', '?')}] {event['name']}…"
    if kind == "round":
        return f"　r{event['round']} {event['action']} {event['chars']}字"
    if kind == "case_done":
        notes = "；".join(event.get("notes") or [])
        return f"　{'✓' if event['passed'] else '✗'} {event['name']}{' — ' + notes if notes else ''}"
    if kind == "report":
        r = event["report"]
        return f"\n✅ 完成：{r['passed']}/{r['total']} 通过（{round(r['duration_ms'] / 1000)}s）"
    if kind == "error":
        return f"❌ {event['message']}"
    return None


class WorkflowEvaluator:
    """一个工作流的跑批器：报告读写 + 后台任务 + 统计 + AI 分析。

    - `prefix`：报告文件名前缀（`consult-` / `rural-`），也决定 `/evals` 列表能看到哪些报告
    - `checks`：判定项 key（与 `check_case` 返回的 dict 对齐），统计与页面共用
    - `load_cases(set_keys)`：怎么从 eval 目录加载用例（各工作流自己的加载器）
    - `intro`：AI 分析提示词里的一句话背景（告诉模型这轮评测在评什么）
    """

    def __init__(self, *, prefix: str, label: str, checks: tuple[str, ...],
                 load_cases: Callable[[list[str] | None], list[dict[str, Any]]],
                 run_case: Callable[..., Any], intro: str, max_rounds: int = MAX_ROUNDS) -> None:
        self.prefix = prefix
        self.label = label
        self.checks = checks
        self.load_cases = load_cases
        self.run_case = run_case
        self.intro = intro
        self.max_rounds = max_rounds
        self._current: RunState | None = None

    # ---------------------------------------------------------------- 报告读写
    def save_report(self, report: dict[str, Any]) -> Path:
        f = evals_dir() / f"{self.prefix}-{report['id']}.json"
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(f)
        return f

    def load_report(self, eval_id: str) -> dict[str, Any] | None:
        safe = "".join(c for c in (eval_id or "") if c.isalnum() or c in "-_")
        f = evals_dir() / f"{self.prefix}-{safe}.json"
        if not f.is_file():
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("评测报告损坏：%s", f)
            return None

    def list_reports(self, limit: int = 30) -> list[dict[str, Any]]:
        files = sorted(evals_dir().glob(f"{self.prefix}-*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
        out: list[dict[str, Any]] = []
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append({"id": data.get("id"), "created_at": data.get("created_at"), "mode": data.get("mode"),
                        "total": data.get("total"), "passed": data.get("passed"),
                        "duration_ms": data.get("duration_ms"), "model": data.get("model"),
                        "set": data.get("set"), "set_label": data.get("set_label"),
                        "has_analysis": bool(data.get("analysis"))})
        return out

    # ---------------------------------------------------------------- 后台任务
    def current_run(self) -> dict[str, Any]:
        """当前/最近一次跑批的状态（没有跑过返回 idle）；前端刷新后靠它恢复界面。"""
        return {"status": "idle"} if self._current is None else self._current.brief()

    def start_run(self, *, set_keys: list[str] | None = None, limit: int | None = None) -> dict[str, Any]:
        """在后台起一轮跑批（已有任务在跑时抛 RuntimeError，由路由返回 409）。"""
        if self._current is not None and self._current.status == "running":
            raise RuntimeError("已有一轮测试在跑，等它结束或先取消")
        cases = self.load_cases(set_keys)
        if limit:
            cases = cases[:limit]
        if not cases:
            raise ValueError("选中的测试集合里没有用例")
        state = RunState(id=datetime.now().strftime("%Y%m%d-%H%M%S"),
                         set_label="、".join(set_keys or []) or "全部", total=len(cases),
                         started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self._current = state
        state.task = asyncio.create_task(self._runner(state, set_keys, limit))
        return state.brief()

    def cancel_run(self) -> bool:
        if self._current is not None and self._current.status == "running" and self._current.task is not None:
            self._current.task.cancel()
            return True
        return False

    def load_cases(self, set_keys: list[str] | None) -> list[dict[str, Any]]:  # noqa: D102  （构造时注入）
        raise NotImplementedError

    async def _runner(self, state: RunState, set_keys: list[str] | None, limit: int | None) -> None:
        try:
            async for event in self.run_eval(set_keys=set_keys, limit=limit, on_event=lambda _e: None):
                line = line_of(event)
                if line:
                    state.lines.append(line)
                if event.get("type") == "case_start":
                    state.current = event["name"]
                elif event.get("type") == "case_done":
                    state.done += 1
                    state.current = ""
                elif event.get("type") == "report":
                    state.report_id = event["report"]["id"]
            state.status = "done"
        except asyncio.CancelledError:
            state.status = "cancelled"
            state.lines.append("⏹ 已取消")
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("[%s] 跑批失败", self.label)
            state.status = "failed"
            state.error = f"{type(e).__name__}: {e}"
            state.lines.append(f"❌ {state.error}")

    # ---------------------------------------------------------------- 跑一轮评测
    async def run_eval(self, *, on_event: Callable[[dict[str, Any]], None] | None = None,
                       limit: int | None = None, set_key: str | None = None,
                       set_keys: list[str] | None = None,
                       run_case: Callable[..., Any] | None = None) -> AsyncIterator[dict[str, Any]]:
        """跑一轮评估，产出进度事件；最后一个事件是完整报告（type=report）。

        set_key / set_keys：只跑某些测试集合（目录名，**支持多选**；`all` 或空 = 全部）。
        limit：只跑前 N 个用例（快速验证用）。run_case：测试用桩（覆盖默认实现）。
        """
        emit = on_event or (lambda _e: None)
        eval_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        picked = sorted({*(set_keys or []), *([set_key] if set_key else [])} - {"", "all"})
        available = self.load_cases(picked or None)
        cases = available[:limit] if limit else available
        if not cases:
            raise ValueError(f"测试集合 {picked or 'all'} 里没有用例")
        model = getattr(self._settings(), "llm_model", "")
        t0 = time.perf_counter()
        rows: list[dict[str, Any]] = []
        yield {"type": "start", "id": eval_id, "total": len(cases), "model": model,
               "set": picked or ["all"], "set_label": "、".join(picked) or "全部"}
        for idx, case in enumerate(cases):
            emit({"type": "case_start", "case": idx, "name": case["name"]})
            yield {"type": "case_start", "case": idx, "name": case["name"], "total": len(cases)}
            try:
                row = await (run_case or self.run_case)(case, idx, emit)
                row["error"] = None
            except Exception as e:  # noqa: BLE001  单个用例失败不影响整轮
                logger.exception("[%s] 用例执行失败 case=%s", self.label, case["name"])
                row = {"idx": idx, "name": case["name"], "complaint": case.get("complaint", ""),
                       "source": case.get("source"), "checks": {}, "passed": False,
                       "notes": [f"执行异常：{type(e).__name__}: {e}"], "error": str(e),
                       "reply_chars": [], "avg_chars": 0, "rounds": {}, "versions": 0, "transcript": []}
            rows.append(row)
            yield {"type": "case_done", "case": idx, "name": case["name"], "passed": row["passed"],
                   "notes": row["notes"], "triage": row.get("triage_department")}

        passed = sum(1 for r in rows if r["passed"])
        report = {
            "id": eval_id, "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "mode": "live",
            "model": model, "scenario": self.prefix, "total": len(rows), "passed": passed,
            "failed": len(rows) - passed,
            "sets": picked or ["all"], "set_label": "、".join(picked) or "全部",
            "partial": bool(limit and limit < len(available)),
            "duration_ms": int((time.perf_counter() - t0) * 1000),
            "stats": stats_of(rows, self.checks), "cases": rows, "analysis": None,
        }
        self.save_report(report)
        yield {"type": "report", "report": brief_of(report)}

    @staticmethod
    def _settings():
        from app.core.config import get_settings

        return get_settings()

    # ---------------------------------------------------------------- AI 分析
    async def analyze_report(self, report: dict[str, Any]) -> str:
        """用 DeepSeek 分析报告（结论由调用方写回报告的 analysis 字段）。"""
        payload = {
            "通过率": f"{report['passed']}/{report['total']}",
            "统计": report.get("stats"),
            "用例": [{"名称": c["name"], "通过": c["passed"],
                      "分诊": c.get("triage_department"), "期望科室": c.get("expect_department"),
                      "处置层级": c.get("disposition"), "期望处置": c.get("expect_disposition"),
                      "诊断版本": c.get("versions"), "轮次": c.get("rounds"),
                      "平均字数": c.get("avg_chars"),
                      "最小/最大字数": [min(c.get("reply_chars") or [0]), max(c.get("reply_chars") or [0])],
                      "未通过项": [k for k, v in (c.get("checks") or {}).items() if not v],
                      "备注": c.get("notes"), "错误": c.get("error")}
                     for c in report["cases"]],
        }
        llm = build_llm(temperature=0.3)
        resp = await llm.ainvoke([
            SystemMessage(content="你是严谨的 AI 产品评测专家，只根据给定数据说话。"),
            HumanMessage(content=ANALYZE_PROMPT.format(intro=self.intro, label=self.label,
                                                       payload=json.dumps(payload, ensure_ascii=False, indent=2)))])
        return resp.text.strip()


ANALYZE_PROMPT = """你是 AI 产品评测专家。下面是一轮「{intro}」的自动化评测报告（{label}）。
请用中文分析，指出问题与具体改进建议。报告数据：

{payload}

输出要求（Markdown，控制在 500 字以内，不要客套）：
## 结论
一句话总结这轮评测是否达标（通过率、主要短板）。

## 失败用例归因
逐个说明失败的用例可能是什么原因（判断错 / 没走完流程 / 安全问题 / 回复过长 / 执行异常），
如果是提示词问题就指出是哪一类判断（信息提取、本轮动作、生成），不要泛泛而谈。

## 改进建议
给 3 条以内、可以直接改提示词或代码的建议；每条一句话，写清楚改哪个环节。

## 指标观察
轮次、字数、耗时有没有异常（例如某个用例轮次特别多、回复特别长）。"""
