"""报告类 Skill（html-report / word-report / ppt-report）的配套工具：在受限的工作区里写文件、运行生成脚本。

这三个 Skill 来自 medex，原本运行在 /repo 沙箱中。本项目的对应关系：
- 工作区根目录（原 /repo）      = REPORT_WORKSPACE（默认 data/report_workspace）
  - threads/<会话>/               中间 spec、骨架、素材
  - o_outcome/                    最终报告（原 /repo/o_outcome）
- Skill 脚本（原 /repo/skill/common/<skill>/scripts/...）= skills/<skill>/scripts/...

模型可用的工具：
- report_write_file(path, content)            在工作区写文本文件（spec.json、骨架 .md 等）
- report_run_generator(skill, spec_path, ...)  运行 skill 的生成脚本，输出写到 o_outcome/
- report_list_outputs()                        列出已生成的报告
- report_read_file(path)                       读取工作区文本文件

安全：所有路径都限制在工作区内（拒绝 .. 与符号链接逃逸），只能运行这三个 skill 的固定脚本，参数由代码拼装（不走 shell）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.core.config import get_settings

ROOT = Path(__file__).resolve().parents[2]
GENERATORS = {
    "html-report": "skills/html-report/scripts/generate_html.py",
    "word-report": "skills/word-report/scripts/generate_word.py",
    "ppt-report": "skills/ppt-report/scripts/generate_ppt.py",
}
MAX_WRITE_BYTES = 4 * 1024 * 1024


def workspace() -> Path:
    p = Path(get_settings().report_workspace)
    p = p if p.is_absolute() else ROOT / p
    (p / "o_outcome").mkdir(parents=True, exist_ok=True)
    (p / "threads").mkdir(parents=True, exist_ok=True)
    return p.resolve()


def _resolve(path: str, *, must_exist: bool = False) -> Path:
    """把模型给的路径解析到工作区内。支持相对路径，以及以 /repo 开头的 medex 风格路径。"""
    ws = workspace()
    raw = path.strip()
    if raw.startswith("/repo/") or raw == "/repo":
        raw = raw[len("/repo"):].lstrip("/")
    p = Path(raw)
    p = (p if p.is_absolute() else ws / p).resolve(strict=must_exist)
    if not p.is_relative_to(ws):
        raise ValueError(f"路径必须在报告工作区内：{ws}（收到 {path}）")
    return p


def generator_env() -> dict[str, str]:
    """生成脚本的环境变量：工作区映射为 /repo，并配置 PPT 校验器。"""
    s = get_settings()
    ws = workspace()
    env = {**os.environ, "REPORT_REPO_ROOT": str(ws), "REPORT_OUTPUT_DIR": str(ws / "o_outcome"),
           "PYTHONIOENCODING": "utf-8"}
    validator = Path(s.ppt_openxml_sdk_validator)
    validator = validator if validator.is_absolute() else ROOT / validator
    env["PPT_REPORT_OPENXML_SDK_VALIDATOR"] = str(validator)
    env["PPT_REPORT_SDK_VALIDATOR_OWNER"] = s.ppt_sdk_validator_owner
    env["PPT_REPORT_SDK_VALIDATION"] = s.ppt_sdk_validation
    return env


def _map_repo_paths(node, ws: Path):
    """把 spec 中 medex 风格的 /repo/... 图片路径换成工作区里的真实绝对路径（html/word 生成器按字面路径读文件）。"""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "path" and isinstance(v, str) and (v == "/repo" or v.startswith("/repo/")):
                out[k] = str(ws / v[len("/repo"):].lstrip("/"))
            else:
                out[k] = _map_repo_paths(v, ws)
        return out
    if isinstance(node, list):
        return [_map_repo_paths(x, ws) for x in node]
    return node


def _prepare_spec(spec_file: Path, ws: Path, skill: str = "") -> tuple[Path, list[str]]:
    """生成交给脚本的 spec：
    - /repo/... 图片路径映射到工作区真实路径（html/word 生成器按字面路径读文件）；
    - spec.style（预设名 / 覆盖对象）解析为完整样式写入 _style，生成脚本只认 _style 的具体数值。
    有改动时写到同目录 .resolved/ 下（文件名不变，报告默认命名不受影响），否则原样返回。返回 (路径, 样式提示)。"""
    import json

    from app.agent.report_styles import describe, resolve_style

    text = spec_file.read_text(encoding="utf-8")
    try:
        spec = json.loads(text)
    except json.JSONDecodeError:
        return spec_file, []  # 交给生成脚本报出具体的 JSON 错误
    if not isinstance(spec, dict):
        return spec_file, []
    notes: list[str] = []
    changed = False
    if '"/repo' in text:
        spec = _map_repo_paths(spec, ws)
        changed = True
    spec.pop("_style", None)  # 只接受由 style 解析出的 _style，不允许直接注入
    if "style" in spec:
        resolved, notes = resolve_style(spec.get("style"), skill)
        spec["_style"] = resolved
        notes = [f"样式：{describe(resolved)}", *notes]
        changed = True
    if not changed and "_style" not in text:
        return spec_file, notes
    tmp_dir = spec_file.parent / ".resolved"
    tmp_dir.mkdir(exist_ok=True)
    out = tmp_dir / spec_file.name
    out.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return out, notes


def run_generator(skill: str, spec_path: str | None = None, markdown_path: str | None = None,
                  references_path: str | None = None, extra_args: list[str] | None = None,
                  timeout: int = 300) -> tuple[int, str]:
    if skill not in GENERATORS:
        raise ValueError(f"未知 skill：{skill}，可选 {list(GENERATORS)}")
    ws = workspace()
    cmd = [sys.executable, str(ROOT / GENERATORS[skill])]
    style_notes: list[str] = []
    if markdown_path:
        if skill != "html-report":
            raise ValueError("只有 html-report 支持 markdown_path")
        cmd += ["--markdown", str(_resolve(markdown_path, must_exist=True))]
        if references_path:
            cmd += ["--references", str(_resolve(references_path, must_exist=True))]
    elif spec_path:
        spec_file, style_notes = _prepare_spec(_resolve(spec_path, must_exist=True), ws, skill)
        cmd += ["--spec", str(spec_file)]
    else:
        raise ValueError("需要 spec_path（或 html-report 的 markdown_path）")
    cmd += ["--out-dir", str(ws / "o_outcome")]
    allowed = {"--title", "--author", "--toc", "--no-toc"}
    for a in extra_args or []:
        if a.split("=", 1)[0] not in allowed:
            raise ValueError(f"不允许的参数：{a}（可用 {sorted(allowed)}）")
        cmd.append(a)
    r = subprocess.run(cmd, cwd=str(ws), env=generator_env(), capture_output=True, text=True, timeout=timeout)
    out = (r.stdout + ("\n" + r.stderr if r.stderr else "")).strip()
    if style_notes and r.returncode == 0:
        out += "\n" + "\n".join(style_notes)
    return r.returncode, out.replace(str(ws), "/repo")


# ---------------------------------------------------------------------- tools
@tool
def report_write_file(path: str, content: str) -> str:
    """在报告工作区写入一个文本文件（如 JSON spec、报告骨架 Markdown）。path 可以是相对路径
    （如 threads/2026-report/spec.json）或 /repo/... 形式。文件已存在时覆盖。"""
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        return f"内容过大（{len(data)} 字节），上限 {MAX_WRITE_BYTES}"
    try:
        p = _resolve(path)
    except ValueError as e:
        return str(e)
    if p.parent == workspace() / "o_outcome":
        return "不能直接写 o_outcome/，最终报告必须由 report_run_generator 生成"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return f"已写入 /repo/{p.relative_to(workspace())}（{len(data)} 字节）"


@tool
def report_read_file(path: str) -> str:
    """读取报告工作区中的文本文件（最多 20000 字符）。"""
    try:
        p = _resolve(path, must_exist=True)
    except (ValueError, FileNotFoundError) as e:
        return f"读取失败：{e}"
    return p.read_text(encoding="utf-8", errors="replace")[:20000]


@tool
def report_run_generator(skill: str, spec_path: str = "", markdown_path: str = "", references_path: str = "",
                         title: str = "", author: str = "", runtime: ToolRuntime = None) -> str:
    """运行报告 Skill 的生成脚本。skill：html-report / word-report / ppt-report；
    spec_path：已用 report_write_file 写好的 JSON spec 路径；html-report 也可改用 markdown_path（可选 references_path）。
    成功时输出中的 Output: 即报告路径（位于 /repo/o_outcome/）。失败时按报错修改 spec 后重试。
    ppt-report 在对话中必须先经用户确认大纲（ppt_confirm_outline），生成后需调用 ppt_verify 验证。"""
    from app.agent import ppt_workflow as wf

    extra = []
    if title:
        extra.append(f"--title={title}")
    if author:
        extra.append(f"--author={author}")
    # PPT 流程门禁：对话中调用时（有会话 id）必须存在用户确认过的最新大纲，spec 要与大纲一致。
    # 脚本/测试直接调用（没有 runtime）不受影响。
    session_id = wf.runtime_session(runtime) if skill == "ppt-report" else None
    warnings: list[str] = []
    spec_file: Path | None = None
    if session_id:
        try:
            spec_file = _resolve(spec_path, must_exist=True) if spec_path else None
        except (ValueError, FileNotFoundError) as e:
            return f"运行失败：{e}"
        if spec_file is None:
            return "运行失败：ppt-report 需要 spec_path"
        turn = wf.latest_user_turn(runtime)
        reason, warnings = wf.before_generate(session_id, spec_file, turn)
        if reason:
            return reason
    try:
        code, out = run_generator(skill, spec_path or None, markdown_path or None, references_path or None, extra)
    except (ValueError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        return f"运行失败：{e}"
    result = f"{'成功' if code == 0 else f'失败（退出码 {code}）'}\n{out}"
    if session_id and spec_file is not None:
        m = re.search(r"^Output:\s*(\S.*)$", out, re.M)
        err = "\n".join(line for line in out.splitlines() if line.startswith("ERROR")) or out[-300:]
        wf.after_generate(session_id, spec_file, m.group(1).strip() if m else None, code == 0,
                          error="" if code == 0 else err, turn=turn)
        if warnings:
            result += "\n\n⚠️ 与大纲对照的提醒：\n- " + "\n- ".join(warnings)
        if code == 0:
            result += "\n\n下一步：必须调用 ppt_verify 渲染验证，验证通过后才能告诉用户已完成。"
    return result


@tool
def report_list_outputs() -> str:
    """列出报告工作区 o_outcome/ 下已生成的报告文件。"""
    out = sorted((workspace() / "o_outcome").glob("*.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = [p for p in out if not p.name.startswith(".")]
    if not out:
        return "o_outcome/ 下还没有报告"
    return "\n".join(f"/repo/o_outcome/{p.name}  {p.stat().st_size} 字节  下载：/api/reports/{p.name}" for p in out[:50])


REPORT_TOOLS = [report_write_file, report_read_file, report_run_generator, report_list_outputs]
