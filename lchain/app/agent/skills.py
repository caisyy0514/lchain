"""Skills：可插拔的「能力说明书」，采用渐进式加载（progressive disclosure）。

目录结构：
    skills/
      web-research/
        SKILL.md          # 必需：YAML frontmatter(name, description) + Markdown 正文
        reference.md      # 可选：附加资料，模型可通过 load_skill(name, file) 读取

工作方式：
1. 系统提示词里只注入每个 skill 的 name + description（很短）
2. 模型判断任务匹配某个 skill 时，调用 load_skill 读取完整 SKILL.md
3. 按 skill 中的步骤调用已有工具（web_search / fetch_webpage ...）完成任务

新增 skill 只需在 skills/ 下新建目录并写 SKILL.md，无需改代码（服务会按文件修改时间自动重新加载）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from langchain_core.tools import tool

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    requires: list[str] = field(default_factory=list)   # 依赖的工具名，缺失时不启用
    files: list[str] = field(default_factory=list)       # 目录下的附加文件


def _skills_root() -> Path:
    p = Path(get_settings().skills_dir)
    return p if p.is_absolute() else Path.cwd() / p


_cache: tuple[float, dict[str, Skill]] | None = None


def _dir_mtime(root: Path) -> float:
    return max((f.stat().st_mtime for f in root.rglob("*") if f.is_file()), default=0.0)


def load_skills() -> dict[str, Skill]:
    """扫描 skills 目录；文件有改动时自动重新加载。"""
    global _cache
    root = _skills_root()
    if not root.is_dir():
        return {}
    mtime = _dir_mtime(root)
    if _cache and _cache[0] == mtime:
        return _cache[1]

    skills: dict[str, Skill] = {}
    for md in sorted(root.glob("*/SKILL.md")):
        m = _FRONTMATTER.match(md.read_text(encoding="utf-8"))
        if not m:
            logger.warning("跳过 %s：缺少 YAML frontmatter", md)
            continue
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError as e:
            logger.warning("跳过 %s：frontmatter 解析失败 %s", md, e)
            continue
        name, desc = str(meta.get("name") or md.parent.name), str(meta.get("description") or "").strip()
        if not desc:
            logger.warning("跳过 %s：缺少 description", md)
            continue
        files = sorted(str(f.relative_to(md.parent)) for f in md.parent.rglob("*")
                       if f.is_file() and f.name != "SKILL.md" and not f.name.startswith("."))
        skills[name] = Skill(name=name, description=desc, body=m.group(2).strip(), path=md.parent,
                             requires=list(meta.get("requires") or []), files=files)
    _cache = (mtime, skills)
    logger.info("已加载 skills: %s", ", ".join(skills) or "(无)")
    return skills


def available_skills(tool_names: set[str]) -> list[Skill]:
    """只返回依赖工具都已启用的 skill。"""
    return [s for s in load_skills().values() if set(s.requires) <= tool_names]


def skills_prompt(skills: list[Skill]) -> str:
    if not skills:
        return ""
    items = "\n".join(f"- **{s.name}**：{s.description}" for s in skills)
    return (
        "\n\n## 可用技能（Skills）\n"
        "以下技能包含完成特定任务的详细方法。当用户的请求与某个技能匹配时，"
        "**先调用 `load_skill` 读取该技能的完整说明，再严格按说明执行**；不匹配时正常回答即可。\n"
        f"{items}"
    )


def _safe_join(base: Path, rel: str) -> Path | None:
    p = (base / rel).resolve()
    return p if p.is_relative_to(base.resolve()) and p.is_file() else None


@tool
def load_skill(name: str, file: str | None = None) -> str:
    """读取某个技能（Skill）的完整说明。name 为技能名；file 可选，用于读取技能目录下的附加文件（如 reference.md）。"""
    skills = load_skills()
    s = skills.get(name)
    if not s:
        return f"未找到技能「{name}」。可用技能：{', '.join(skills) or '无'}"
    if file:
        p = _safe_join(s.path, file)
        return p.read_text(encoding="utf-8")[:20000] if p else f"技能 {name} 下没有文件 {file}。可用文件：{s.files}"
    extra = f"\n\n---\n本技能目录下的附加文件（可用 load_skill(name, file) 读取）：{', '.join(s.files)}" if s.files else ""
    return f"# Skill: {s.name}\n\n{s.body}{extra}"
