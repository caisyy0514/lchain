"""AI 生图工具：把 skills/imagegen 的 CLI（scripts/image_gen.py，原样保留不修改）接入 Agent。

- generate_image  文生图（可带参考风格描述），保存到报告工作区，返回可在对话里直接显示的 Markdown 图片
- edit_image      编辑工作区里已有的图片（换背景、去掉元素、改风格等）

配置（.env）：IMAGEGEN_API_KEY / IMAGEGEN_BASE_URL / IMAGEGEN_MODEL，调用 CLI 时映射为子进程的
OPENAI_API_KEY / OPENAI_BASE_URL —— 只作用于生图子进程，不影响 web 进程里对话模型的配置。
未配置 IMAGEGEN_API_KEY 时不注册这两个工具，imagegen skill 也会因 requires 不满足而自动隐藏。

图片位置：有 PPT 流程的会话放 threads/ppt-<会话>/images/（可直接写进 PPT spec），否则放
threads/chat-<会话>/images/；通过 /api/workspace/files/<相对路径> 在网页里显示。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from langchain.tools import ToolRuntime, tool

from app.agent import ppt_workflow as wf
from app.core.config import get_settings

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "imagegen" / "scripts" / "image_gen.py"
SIZES = {"square": "1024x1024", "landscape": "1536x1024", "portrait": "1024x1536"}
QUALITIES = ("low", "medium", "high")


def imagegen_enabled() -> bool:
    s = get_settings()
    return bool(s.imagegen_api_key and SCRIPT.is_file())


def _image_dir(session_id: str | None) -> Path:
    from app.agent.report_tools import workspace

    if session_id:
        safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id)[:64] or "default"
        if wf.load_state(session_id):
            d = wf.session_dir(session_id) / "images"
        else:
            d = workspace() / "threads" / f"chat-{safe}" / "images"
    else:
        d = workspace() / "threads" / "imagegen" / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def file_url(path: Path) -> str:
    from urllib.parse import quote

    from app.agent.report_tools import workspace

    return "/api/workspace/files/" + quote(str(path.resolve().relative_to(workspace())))


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", name or "").strip("_")[:40]
    return s or f"image_{int(time.time())}"


def _unique(path: Path) -> Path:
    """不覆盖已有图片：重名时加 -v2、-v3。"""
    if not path.exists():
        return path
    for i in range(2, 1000):
        p = path.with_name(f"{path.stem}-v{i}{path.suffix}")
        if not p.exists():
            return p
    raise ValueError("同名图片过多")


def _env() -> dict[str, str]:
    s = get_settings()
    env = {**os.environ, "OPENAI_API_KEY": s.imagegen_api_key, "PYTHONIOENCODING": "utf-8"}
    if s.imagegen_base_url:
        env["OPENAI_BASE_URL"] = s.imagegen_base_url
    else:
        env.pop("OPENAI_BASE_URL", None)
    return env


async def _run_cli(args: list[str]) -> tuple[int, str, str]:
    s = get_settings()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(SCRIPT), *args, cwd=str(ROOT), env=_env(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=s.imagegen_timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        proc.kill()  # 超时或对话被取消：结束子进程，避免后台继续消耗额度
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _error_text(stderr: str) -> str:
    lines = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    err = next((ln for ln in reversed(lines) if ln.startswith(("Error:", "openai.")) or "Error code" in ln), "")
    msg = err or (lines[-1] if lines else "未知错误")
    if "429" in msg or "Overloaded" in msg or "RateLimit" in msg:
        msg += "（生图服务繁忙，可以稍后重试）"
    if "content_policy" in msg or "moderation" in msg.lower() or "safety" in msg.lower():
        msg += "（提示词触发了内容安全策略，请调整描述）"
    return msg[:400]


def _record(session_id: str | None, entry: dict) -> None:
    if not session_id or not wf.load_state(session_id):
        return
    f = wf.session_dir(session_id) / "assets.json"
    items = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else []
    items.append(entry)
    f.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _result(paths: list[Path], prompt: str, elapsed: float, mode: str) -> str:
    lines = [f"✅ 已{'生成' if mode == 'generate' else '编辑'} {len(paths)} 张图片（耗时 {elapsed:.0f} 秒，模型 {get_settings().imagegen_model}）："]
    for p in paths:
        rp = wf.repo_path(p)
        lines.append(f"- {rp}\n  在回复中展示：![{p.stem}]({file_url(p)})")
    lines.append(f"实际使用的提示词：{prompt[:600]}")
    lines.append("用于 PPT/Word/HTML 时直接用上面的 /repo 路径，caption 或 source 注明「AI 生成示意图」。")
    return "\n".join(lines)


@tool
async def generate_image(prompt: str, name: str, size: str = "landscape", quality: str = "medium",
                         n: int = 1, background: str = "", runtime: ToolRuntime = None) -> str:
    """用 AI 生成图片（照片、插画、场景图、产品图、海报、图标、信息图等位图），保存到工作区并返回可展示的 Markdown。
    prompt：按 imagegen skill 的结构化写法（Use case / Asset type / Primary request / Style / Composition / Text (verbatim) /
    Constraints / Avoid…），中英文均可；图中需要的文字写在 Text (verbatim) 里并加引号。
    name：简短文件名；size：landscape 横图 1536×1024（PPT 默认）/ square 1024×1024 / portrait 竖图 1024×1536；
    quality：low 草稿更快 / medium 默认 / high 最终稿（更慢）；n：同一提示词的变体数 1~4；
    background：留空，需要透明背景时填 transparent（服务不支持会报错）。
    生成较慢（通常 1~5 分钟），不同内容的多张图请分别调用，可以在同一步里并行发起。"""
    s = get_settings()
    size = SIZES.get(size, size)
    if size not in SIZES.values():
        return f"❌ size 可选 {list(SIZES)}"
    if quality not in QUALITIES:
        return f"❌ quality 可选 {QUALITIES}"
    if not 1 <= int(n) <= 4:
        return "❌ n 取 1~4"
    if not prompt.strip():
        return "❌ prompt 不能为空"
    sid = wf.runtime_session(runtime)
    out = _unique(_image_dir(sid) / f"ai_{_slug(name)}.png")
    args = ["generate", "--model", s.imagegen_model, "--prompt", prompt, "--size", size, "--quality", quality,
            "--n", str(int(n)), "--no-augment"]
    if background:
        args += ["--background", background, "--output-format", "png"]
    if int(n) > 1:
        out_dir = out.with_suffix("")
        args += ["--out-dir", str(out_dir)]
    else:
        args += ["--out", str(out)]
    started = time.time()
    try:
        code, stdout, stderr = await _run_cli(args)
    except asyncio.TimeoutError:
        return f"❌ 生图超时（{s.imagegen_timeout} 秒），服务可能繁忙，请稍后重试或降低 quality"
    if code != 0:
        logger.warning("生图失败：%s", stderr[-800:])
        return f"❌ 生图失败：{_error_text(stderr)}"
    paths = [Path(m) for m in re.findall(r"^Wrote (.+)$", stdout, re.M) if Path(m).is_file()]
    if not paths:
        return f"❌ 生图完成但没有找到输出文件：{(stdout + stderr)[-300:]}"
    elapsed = time.time() - started
    _record(sid, {"kind": "ai_image", "paths": [wf.repo_path(p) for p in paths], "prompt": prompt,
                  "model": s.imagegen_model, "size": size, "quality": quality})
    return _result(paths, prompt, elapsed, "generate")


@tool
async def edit_image(image_path: str, prompt: str, name: str = "", quality: str = "medium",
                     runtime: ToolRuntime = None) -> str:
    """编辑工作区里已有的图片（之前生成的图或已下载的图）：替换背景、去掉/替换元素、调整光线、改变风格等。
    image_path：/repo/... 路径；prompt：写清楚「只改什么、其他保持不变」（Constraints: change only X; keep Y unchanged）。
    结果另存为新文件，不覆盖原图。"""
    from app.agent.report_tools import _resolve

    s = get_settings()
    if quality not in QUALITIES:
        return f"❌ quality 可选 {QUALITIES}"
    try:
        src = _resolve(image_path, must_exist=True)
    except (ValueError, FileNotFoundError) as e:
        return f"❌ 找不到图片：{e}"
    if src.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        return "❌ 只支持编辑 PNG / JPEG / WebP 图片"
    sid = wf.runtime_session(runtime)
    out = _unique(_image_dir(sid) / f"{_slug(name or src.stem + '-edited')}.png")
    args = ["edit", "--model", s.imagegen_model, "--image", str(src), "--prompt", prompt, "--quality", quality,
            "--no-augment", "--out", str(out)]
    started = time.time()
    try:
        code, stdout, stderr = await _run_cli(args)
    except asyncio.TimeoutError:
        return f"❌ 编辑超时（{s.imagegen_timeout} 秒），请稍后重试"
    if code != 0:
        logger.warning("图片编辑失败：%s", stderr[-800:])
        return f"❌ 图片编辑失败：{_error_text(stderr)}"
    paths = [Path(m) for m in re.findall(r"^Wrote (.+)$", stdout, re.M) if Path(m).is_file()]
    if not paths:
        return f"❌ 编辑完成但没有找到输出文件：{(stdout + stderr)[-300:]}"
    _record(sid, {"kind": "ai_image_edit", "source": wf.repo_path(src), "paths": [wf.repo_path(p) for p in paths],
                  "prompt": prompt, "model": s.imagegen_model})
    return _result(paths, prompt, time.time() - started, "edit")


IMAGE_TOOLS = [generate_image, edit_image]
