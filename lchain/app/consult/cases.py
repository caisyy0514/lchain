"""问诊测试用例库：从 `eval/consult/**/*.md` 加载。

**为什么把用例放成文件而不是写在 Python 里**
- 一个用例一个文件，加/改用例不用碰代码，diff 也看得清
- **文件名带主诉与疾病**（`01-上腹痛3天-急性胃炎.md`），**目录按科室分**，按疾病或科室直接找
- 文件头是 YAML front matter（结构化字段），正文写给维护的人看（在验什么、判定要点）

格式与新增用例的步骤见 `eval/README.md`。病例库仍然只有这一份，
`tests/agent/test_consult.py`（流程测试）、`app/consult/eval.py`（真实模型跑批）、
`scripts/consult_smoke.py`（端到端冒烟）都从这里取。

解析失败的文件会带路径写 ERROR 日志并跳过（不让一个写坏的用例把服务带崩），
用 `python -m app.consult.cases` 可以单独校验所有用例文件（有错时退出码 1）。
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from app.consult.gates import REQUIRED_HISTORY

logger = logging.getLogger(__name__)

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)

# 必填字段（少一个就认为是写坏了）：跑批与测试都依赖这些
REQUIRED_FIELDS = ("name", "complaint", "patient", "department", "urgency",
                   "primary", "updated", "differentials", "evidence",
                   "change", "change_answer", "history", "red_flags")
_HISTORY_KEYS = {key for key, _ in REQUIRED_HISTORY}


def eval_dir() -> Path:
    """用例根目录（`EVAL_DIR`，默认项目下的 `eval/`）。"""
    from app.core.config import get_settings

    p = Path(get_settings().eval_dir).expanduser()
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[2] / p
    return p


def _parse(path: Path) -> dict[str, Any]:
    """解析一个用例文件，返回用例字典；格式不对时抛 ValueError（带文件路径）。"""
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER.match(text)
    if not m:
        raise ValueError(f"{path}：缺少 YAML front matter（文件要以 --- 开头）")
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"{path}：front matter 解析失败：{e}") from e
    if not isinstance(meta, dict):
        raise ValueError(f"{path}：front matter 必须是键值对")

    missing = [k for k in REQUIRED_FIELDS if meta.get(k) in (None, "", [])]
    if missing:
        raise ValueError(f"{path}：缺少必填字段 {'、'.join(missing)}")
    history = meta["history"]
    if not isinstance(history, dict) or not history:
        raise ValueError(f"{path}：history 必须是非空字典（要素 key -> 患者答复）")
    unknown = [k for k in history if k not in _HISTORY_KEYS]
    if unknown:
        raise ValueError(f"{path}：history 里有未知要素 {'、'.join(unknown)}；"
                         f"可用：{', '.join(sorted(_HISTORY_KEYS))}")
    if meta["urgency"] not in ("routine", "soon", "urgent", "emergency"):
        raise ValueError(f"{path}：urgency 取 routine / soon / urgent / emergency")

    return {
        "id": str(meta.get("id") or path.stem.split("-")[0]),
        "name": str(meta["name"]),
        # 目录名就是科室（按科室分类查找用），也在报告里显示
        "department_dir": path.parent.name,
        "source": str(path.relative_to(eval_dir())),
        "complaint": str(meta["complaint"]),
        "patient": str(meta["patient"]),
        "department": str(meta["department"]),
        "urgency": str(meta["urgency"]),
        "red_flags": [str(x) for x in meta["red_flags"]],
        "primary": str(meta["primary"]),
        "updated": str(meta["updated"]),
        "differentials": [str(x) for x in meta["differentials"]],
        "evidence": [str(x) for x in meta["evidence"]],
        "change": str(meta["change"]),
        "change_answer": str(meta["change_answer"]),
        "history": {str(k): str(v) for k, v in history.items()},
        "notes": m.group(2).strip(),
    }


def _normalize_keys(set_key: str | None, set_keys: list[str] | None) -> set[str] | None:
    """把「单个集合 / 多个集合」统一成一个集合；含 `all` 或为空 = 不筛选。"""
    keys = set(set_keys or [])
    if set_key:
        keys.add(set_key)
    keys.discard("")
    return None if (not keys or "all" in keys) else keys


def load_cases(*, strict: bool = False, set_key: str | None = None,
               set_keys: list[str] | None = None) -> list[dict[str, Any]]:
    """加载用例（按路径排序）。

    set_key / set_keys：只取某些测试集合（`list_sets()` 里的 key，就是 eval 目录下的**科室目录名**）；
    **可以多选**（set_keys 传多个，会合并）；含 `all` 或为空 = 全部。
    strict=True 时遇到坏文件直接抛错（校验用）。
    """
    keys = _normalize_keys(set_key, set_keys)
    root = eval_dir() / "consult"
    if not root.is_dir():
        logger.error("用例目录不存在：%s（检查 EVAL_DIR 配置）", root)
        return []
    cases: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.md")):
        if path.name.lower().startswith("readme"):
            continue
        try:
            case = _parse(path)
        except ValueError as e:
            if strict:
                raise
            logger.error("跳过坏的用例文件：%s", e)
            continue
        if keys and case["department_dir"] not in keys:
            continue
        cases.append(case)
    if not cases and not strict:
        raise RuntimeError(f"没有加载到任何问诊用例（sets={sorted(keys) if keys else 'all'}），检查 {root} 下的 .md 文件格式")
    return cases


def list_sets() -> list[dict[str, Any]]:
    """可选的测试集合：`all` + eval 目录下的每个科室目录（前端下拉框直接用）。"""
    groups: dict[str, int] = {}
    for c in CASES:
        groups[c["department_dir"]] = groups.get(c["department_dir"], 0) + 1
    sets = [{"key": "all", "label": "全部", "count": len(CASES)}]
    sets += [{"key": name, "label": name, "count": groups[name]} for name in sorted(groups)]
    return sets


CASES: list[dict[str, Any]] = load_cases()


if __name__ == "__main__":  # python -m app.consult.cases —— 校验所有用例文件
    from dotenv import load_dotenv

    load_dotenv()
    try:
        loaded = load_cases(strict=True)
    except ValueError as err:
        print(f"❌ {err}")
        sys.exit(1)
    print(f"✅ {len(loaded)} 个用例全部合法（{eval_dir() / 'consult'}）")
    for s in list_sets():
        print(f"   集合 {s['label']}：{s['count']} 例")
    for c in loaded:
        print(f"   {c['source']}  →  分诊 {c['department']} / {c['urgency']}")
