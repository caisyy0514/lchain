"""山区问诊测试用例库：从 `eval/rural/**/*.md` 加载。

格式与 `eval/consult/**/*.md` 同一套（一个用例一个文件，front matter 结构化字段 + 正文说明），
区别在**目录按期望的处置层级分**（紧急转运 / 县医院 / 乡镇卫生院 / 村卫生室 / 居家随访）——
山区问诊最该被验证的能力不是"分到哪个科"，而是"该不该今晚下山"。

一个用例必填字段：
- 病人：`name` `complaint` `patient`（患者开场白与身份）
- 期望结论：`department`（科室）`urgency`（紧急程度）`disposition`（处置层级：
  home / village / township / county / emergency）
- 期望诊断：`primary`（v1）`updated`（变化后 v2）`differentials` `evidence`
- 变化闭环：`change` `change_answer`
- 采集内容：`history`（10 项现病史要素，key 固定）、`access`（就医条件：access_distance /
  access_transport / access_companion / local_conditions）

正文写「在验什么 / 判定要点」，给维护的人看。
解析失败的文件带路径写 ERROR 日志并跳过（不让一个写坏的用例把服务带崩）；
`python -m app.rural.cases` 可以单独校验所有用例文件（有错时退出码 1）。
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from app.consult.gates import REQUIRED_HISTORY
from app.rural.gates import ACCESS_KEYS, DISPOSITIONS, URGENCY  # noqa: F401  (URGENCY 供用例字段校验)

logger = logging.getLogger(__name__)

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)

REQUIRED_FIELDS = ("name", "complaint", "patient", "department", "urgency", "disposition",
                   "red_flags", "primary", "updated", "differentials", "evidence",
                   "change", "change_answer", "history", "access")
_HISTORY_KEYS = {key for key, _ in REQUIRED_HISTORY}


def eval_dir() -> Path:
    """用例根目录（`EVAL_DIR`，默认项目下的 `eval/`）。"""
    from app.core.config import get_settings

    p = Path(get_settings().eval_dir).expanduser()
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[2] / p
    return p


def _parse(path: Path) -> dict[str, Any]:
    """解析一个用例文件；格式不对时抛 ValueError（带文件路径）。"""
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
    access = meta["access"]
    if not isinstance(access, dict) or not access:
        raise ValueError(f"{path}：access 必须是非空字典（就医条件）")
    bad_access = [k for k in access if k not in ACCESS_KEYS]
    if bad_access:
        raise ValueError(f"{path}：access 里有未知字段 {'、'.join(bad_access)}；"
                         f"可用：{', '.join(ACCESS_KEYS)}")
    if meta["urgency"] not in ("routine", "soon", "urgent", "emergency"):
        raise ValueError(f"{path}：urgency 取 routine / soon / urgent / emergency")
    if meta["disposition"] not in DISPOSITIONS:
        raise ValueError(f"{path}：disposition 取 {' / '.join(DISPOSITIONS)}")

    # 目录名按处置层级分；写错位置不影响使用，但记一条日志（方便维护时发现放错文件）
    dir_name = path.parent.name
    if dir_name not in DISPOSITIONS.values() and not any(
            dir_name.startswith(v.split("（")[0]) for v in DISPOSITIONS.values()):
        logger.debug("用例目录名 %s 不在处置层级清单里（仅提示）：%s", dir_name, path)

    return {
        "id": str(meta.get("id") or path.stem.split("-")[0]),
        "name": str(meta["name"]),
        "set_dir": dir_name,           # 目录名 = 测试集合（期望处置层级）
        "source": str(path.relative_to(eval_dir())),
        "complaint": str(meta["complaint"]),
        "patient": str(meta["patient"]),
        "department": str(meta["department"]),
        # 可接受的科室清单（默认只有主期望）：轻症用例写多个方向，避免因为「全科 vs 呼吸内科」这种
        # 学名差异把流程判成失败——山区问诊真正要卡的是处置层级
        "department_any": [str(meta["department"]), *[str(x) for x in (meta.get("department_any") or [])]],
        "urgency": str(meta["urgency"]),
        "disposition": str(meta["disposition"]),
        "red_flags": [str(x) for x in meta["red_flags"]],
        "primary": str(meta["primary"]),
        "updated": str(meta["updated"]),
        "differentials": [str(x) for x in meta["differentials"]],
        "evidence": [str(x) for x in meta["evidence"]],
        "change": str(meta["change"]),
        "change_answer": str(meta["change_answer"]),
        "history": {str(k): str(v) for k, v in history.items()},
        "access": {str(k): str(v) for k, v in access.items()},
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
    """加载用例（按路径排序）。set_key/set_keys 只取某些测试集合（目录名，可多选）。"""
    keys = _normalize_keys(set_key, set_keys)
    root = eval_dir() / "rural"
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
            logger.error("跳过坏的山区问诊用例文件：%s", e)
            continue
        if keys and case["set_dir"] not in keys:
            continue
        cases.append(case)
    if not cases and not strict:
        raise RuntimeError(f"没有加载到任何山区问诊用例（sets={sorted(keys) if keys else 'all'}），"
                           f"检查 {root} 下的 .md 文件格式")
    return cases


def list_sets() -> list[dict[str, Any]]:
    """可选的测试集合：`all` + 用例目录（就是期望的处置层级）。"""
    groups: dict[str, int] = {}
    for c in CASES:
        groups[c["set_dir"]] = groups.get(c["set_dir"], 0) + 1
    sets = [{"key": "all", "label": "全部", "count": len(CASES)}]
    sets += [{"key": name, "label": name, "count": groups[name]} for name in sorted(groups)]
    return sets


CASES: list[dict[str, Any]] = load_cases()


if __name__ == "__main__":  # python -m app.rural.cases —— 校验所有用例文件
    from dotenv import load_dotenv

    load_dotenv()
    try:
        loaded = load_cases(strict=True)
    except ValueError as err:
        print(f"❌ {err}")
        sys.exit(1)
    print(f"✅ {len(loaded)} 个山区问诊用例全部合法（{eval_dir() / 'rural'}）")
    for s in list_sets():
        print(f"   集合 {s['label']}：{s['count']} 例")
    for c in loaded:
        print(f"   {c['source']}  →  处置 {DISPOSITIONS[c['disposition']]} / 科室 {c['department']}")
