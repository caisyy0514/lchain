"""企微群回复 Agent 的评测用例库：从 `eval/wecom/**/*.md` 加载。

**为什么把用例放成文件而不是写在 Python 里**
- 一个用例一个文件，加/改用例不用碰代码，diff 也看得清
- **目录按场景分类**（医疗咨询 / 找医生用药 / 安全急救 / 公共信息 / 非医疗事务 / 无需回复），
  文件名 = `<序号>-<场景描述>.md`
- 用例库只有这一份：测试（`tests/agent/test_wecom_eval.py`）、真实模型跑批（`/wecom-eval` 页面、
  `app/wecom/eval.py`）都从这里读

一个用例 = 「群档案 + 可选的群历史 + 本轮患者消息」，期望项写在 `expect` 里：
期望回复分支（intent）、是否需要回复、必须/禁止出现的关键词、字数上限、是否推找医生卡片、
是否 @病历经理、是否推引导语。

解析失败的文件会带路径写 ERROR 日志并跳过（不让一个写坏的用例把服务带崩），
用 `PYTHONPATH=. .venv/bin/python -m app.wecom.cases` 可以单独校验所有用例（有错时退出码 1）。
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)

# 必填字段（少一个就认为是写坏了）
REQUIRED_FIELDS = ("name", "message", "expect")
ROLES = ("patient", "family", "case_manager", "doctor")
# 期望字段的默认值；None 表示「不校验这一项」
DEFAULT_EXPECT: dict[str, Any] = {
    "intent": "", "intent_any": [], "need_reply": True,
    "must_include": [], "must_include_any": [], "must_not_include": [],
    "max_chars": 0, "push_card": None, "notify_manager": None, "mention_manager": None,
    "push_guide": None, "merged": False,
}
# `pre`：跑这条用例前先给临时群打的标记（目前只支持“群里已经推过找医生卡片”）
PRE_KEYS = ("card_pushed",)


def eval_dir() -> Path:
    """用例根目录（`EVAL_DIR`/wecom，默认项目下的 `eval/wecom`）。"""
    from app.core.config import get_settings

    p = Path(get_settings().eval_dir).expanduser()
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[2] / p
    return p / "wecom"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    raise ValueError(f"应该是字符串或列表，得到 {type(v).__name__}")


def _history(v: Any) -> list[dict[str, str]]:
    """群历史消息：[{role, content}]，role 取患者/家属/病历经理/医生/助手（assistant）。"""
    if v is None:
        return []
    if not isinstance(v, list):
        raise ValueError("history 应该是列表")
    out = []
    for i, item in enumerate(v, 1):
        if not isinstance(item, dict) or not str(item.get("content") or "").strip():
            raise ValueError(f"history 第 {i} 项要有 role 和 content")
        role = str(item.get("role") or "patient").strip()
        if role not in (*ROLES, "assistant"):
            raise ValueError(f"history 第 {i} 项 role={role!r} 不合法（{ROLES + ('assistant',)}）")
        out.append({"role": role, "content": str(item["content"]).strip(),
                    "sender_name": str(item.get("sender_name") or "").strip()})
    return out


def _expect(v: Any) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise ValueError("expect 应该是键值对")
    unknown = set(v) - set(DEFAULT_EXPECT)
    if unknown:
        raise ValueError(f"expect 里有不认识的字段：{sorted(unknown)}（可用：{sorted(DEFAULT_EXPECT)}）")
    out = {**DEFAULT_EXPECT}
    out.update({k: v[k] for k in DEFAULT_EXPECT if k in v})
    out["intent"] = str(out["intent"] or "").strip()
    out["intent_any"] = _as_list(out["intent_any"])
    out["need_reply"] = bool(out["need_reply"])
    out["must_include"] = _as_list(out["must_include"])
    out["must_include_any"] = _as_list(out["must_include_any"])
    out["must_not_include"] = _as_list(out["must_not_include"])
    out["max_chars"] = int(out["max_chars"] or 0)
    for key in ("push_card", "notify_manager", "mention_manager", "push_guide"):
        if out[key] is not None:
            out[key] = bool(out[key])
    out["merged"] = bool(out["merged"])
    if not (out["intent"] or out["intent_any"] or out["push_card"] is not None or not out["need_reply"]):
        raise ValueError("expect 至少要有 intent / intent_any / need_reply=false / push_card 中的一项期望")
    return out


def parse_case_file(path: Path) -> dict[str, Any]:
    """解析一个用例文件，返回用例字典；格式不对时抛 ValueError（带路径）。"""
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
    missing = [k for k in REQUIRED_FIELDS if not meta.get(k)]
    if missing:
        raise ValueError(f"{path}：缺少必填字段 {missing}")
    role = str(meta.get("sender_role") or "patient").strip()
    if role not in ROLES:
        raise ValueError(f"{path}：sender_role={role!r} 不合法（{ROLES}）")
    message = str(meta["message"]).strip()
    followup = str(meta.get("followup") or "").strip()
    if followup and not meta.get("expect", {}).get("merged"):
        raise ValueError(f"{path}：写了 followup 就要把 expect.merged 设为 true（这条用例是在测打断合并）")
    pre = meta.get("pre") or {}
    if pre and not isinstance(pre, dict):
        raise ValueError(f"{path}：pre 应该是键值对")
    try:
        source = str(path.relative_to(_project_root()))
    except ValueError:  # 自定义目录（测试用）不一定在项目下
        source = str(path)
    try:
        cid = str(meta.get("id") or path.stem.split("-")[0]).strip()
        return {
            "id": cid,
            "name": str(meta["name"]).strip(),
            "set": path.parent.name,
            "set_label": path.parent.name,
            "message": message,
            "followup": followup,
            "sender_role": role,
            "sender_name": str(meta.get("sender_name") or "").strip(),
            "profile": str(meta.get("profile") or "").strip(),
            "case_manager_name": str(meta.get("case_manager_name") or "李经理").strip(),
            "history": _history(meta.get("history")),
            "expect": _expect(meta["expect"]),
            "pre": {k: bool(pre[k]) for k in PRE_KEYS if k in pre},
            "notes": m.group(2).strip(),
            "source": source,
            "path": path,
        }
    except ValueError as e:
        raise ValueError(f"{path}：{e}") from e


def load_cases(set_keys: list[str] | None = None, path: Path | None = None) -> list[dict[str, Any]]:
    """加载用例；`set_keys` 只保留这些目录（集合），`path` 换个目录（测试用）。空 = 全部。"""
    root = path or eval_dir()
    keys = {k for k in (set_keys or []) if k and k != "all"}
    cases: list[dict[str, Any]] = []
    if not root.is_dir():
        logger.error("用例目录不存在：%s", root)
        return cases
    for f in sorted(root.rglob("*.md")):
        if keys and f.parent.name not in keys:
            continue
        try:
            cases.append(parse_case_file(f))
        except ValueError as e:
            logger.error("用例文件解析失败，已跳过：%s", e)
        except Exception as e:  # noqa: BLE001  单个坏文件不能带崩服务
            logger.exception("用例文件读取失败，已跳过：%s（%s）", f, e)
    cases.sort(key=lambda c: (c["set"], c["id"], c["name"]))
    return cases


def list_sets(path: Path | None = None) -> list[dict[str, Any]]:
    """可选的测试集合（= `eval/wecom` 下的目录）与用例数。"""
    cases = load_cases(path=path)
    counts: dict[str, int] = {}
    for c in cases:
        counts[c["set"]] = counts.get(c["set"], 0) + 1
    return [{"key": k, "label": k, "count": v} for k, v in sorted(counts.items())]


CASES: list[dict[str, Any]] = load_cases()


def main() -> int:
    """命令行校验所有用例文件：`python -m app.wecom.cases`（有错退出码 1）。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    root = eval_dir()
    files = sorted(root.rglob("*.md"))
    bad = 0
    per_set: dict[str, int] = {}
    for f in files:
        try:
            case = parse_case_file(f)
        except (ValueError, Exception) as e:  # noqa: BLE001
            print(f"✗ {e}")
            bad += 1
            continue
        per_set[case["set"]] = per_set.get(case["set"], 0) + 1
    print(f"\n用例目录：{root}")
    for k, v in sorted(per_set.items()):
        print(f"  {k}：{v} 例")
    print(f"共 {len(files) - bad} 个用例可用，{bad} 个文件有问题")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
