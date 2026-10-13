"""PPT 制作工作流：需求整理 → 大纲确认（可多轮修改）→ 生成 → 渲染验证。

为什么需要代码层面的流程控制：
- ppt-report Skill 只是一份说明书，模型可能跳过「问需求、确认大纲」直接生成；
- 所以把流程做成按会话持久化的状态机，关键步骤由工具门禁保证：
  - `report_run_generator(skill="ppt-report")` 只有在「用户确认过的最新大纲」存在时才放行；
  - `ppt_confirm_outline` 只接受大纲提出**之后**用户新发的、表达确认的消息（按消息 id 判断，模型无法自己确认）；
  - 大纲有任何修改都会生成新版本并撤销之前的确认。
- 状态存放在工作区 `threads/ppt-<会话>/workflow.json`，服务重启不丢（对话记忆 InMemorySaver 会丢，但流程状态还在）。

阶段（stage）：
    requirements   需求整理中（有待用户回答的问题）
    outline_review 大纲待确认（等待用户确认或提出修改）
    approved       大纲已确认，可以写 spec 并生成
    generated      已生成，待渲染验证
    needs_fix      验证发现问题，需修正后重新生成
    delivered      验证通过，已交付

`PptWorkflowMiddleware` 在每次调用模型前把当前阶段和下一步要求追加到系统提示词，模型不必记住流程。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

STAGES = {
    "requirements": "需求整理中",
    "outline_review": "大纲待确认",
    "approved": "大纲已确认，可生成",
    "generated": "已生成，待验证",
    "needs_fix": "验证发现问题，待修复",
    "delivered": "已验证交付",
}
LAYOUTS = {"title", "section", "bullets", "text", "image", "chart", "table", "columns"}
VISUAL_KINDS = {"none", "image", "diagram", "chart", "table"}
# 图文密度：内容页中带图片/示意图/图表/表格的最低比例
VISUAL_DENSITY = {"rich": 0.5, "balanced": 0.3, "minimal": 0.0}
# 标题单行可容纳的宽度（1 个汉字 = 1），按生成器的字号和标题框宽度估算并略留余量：
# 封面 50pt / 章节 40pt / 内容页 35pt。超出会在生成时报「Title 文字超出可读区域」
TITLE_CAPACITY = {"title": 14.0, "section": 18.0, "content": 21.0}
REQUIRED_FIELDS = {"topic": "主题", "audience": "受众", "purpose": "用途/场景", "slide_count": "页数或时长"}
_lock = threading.Lock()


# ------------------------------------------------------------------ 会话与存储
def session_dir_name(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "")[:64]
    if not safe:
        raise ValueError("会话 id 无效")
    return f"ppt-{safe}"


def session_dir(session_id: str) -> Path:
    from app.agent.report_tools import workspace

    d = workspace() / "threads" / session_dir_name(session_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def repo_path(p: Path) -> str:
    from app.agent.report_tools import workspace

    return "/repo/" + str(p.resolve().relative_to(workspace()))


def _state_file(session_id: str) -> Path:
    return session_dir(session_id) / "workflow.json"


def load_state(session_id: str) -> dict[str, Any] | None:
    f = _state_file(session_id)
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("PPT 工作流状态文件损坏：%s", f)
        return None


def save_state(session_id: str, state: dict[str, Any]) -> None:
    f = _state_file(session_id)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)  # 原子替换，避免写一半


def _new_state(session_id: str) -> dict[str, Any]:
    return {"session_id": session_id, "stage": "requirements", "requirements": None, "outlines": [],
            "approved_version": None, "outputs": [], "last_verify": None, "history": [],
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}


def _log(state: dict[str, Any], event: str) -> None:
    state.setdefault("history", []).append({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event})
    state["history"] = state["history"][-50:]


# ------------------------------------------------------------------ 用户回复识别
_CONFIRM = re.compile(r"确认|确定|同意|可以|没问题|没有问题|就这样|按这个|照这个|开始生成|开始做|生成吧|做吧|通过|好的|好了|行|ok|OK|Ok|👍|yes|Yes|lgtm|LGTM")
_REVISE = re.compile(r"不|改|调整|修改|增加|加上|加一|删|去掉|换|但|再|还要|还需|另外|补充|少一|多一|合并|拆|别|要是|能否|能不能|是否|吗|？|\?")
# 含否定字但实际表达认可的说法，判断修改意图前先去掉
_POSITIVE_NEG = re.compile(r"不错|不用改|不需要改|无需修改|不用调整|不需要调整|没什么要改|不用了|挺不错|不用动")


def classify_reply(text: str) -> str:
    """把用户对大纲的回复分成 confirm / revise / unclear。宁可判成 unclear 让模型再问一句，也不误判为确认。"""
    t = (text or "").strip()
    if not t:
        return "unclear"
    cleaned = _POSITIVE_NEG.sub("", t)
    wants_change = bool(_REVISE.search(cleaned))
    confirms = bool(_CONFIRM.search(t)) or bool(_POSITIVE_NEG.search(t))
    if confirms and not wants_change and len(t) <= 60:
        return "confirm"
    if wants_change or len(t) > 60:
        return "revise"
    return "unclear"


@dataclass
class UserTurn:
    """当前对话里用户的最新一条消息（来自 agent state）。"""
    message_id: str | None
    text: str
    count: int


def _msg_text(m) -> str:
    c = m.content
    if isinstance(c, str):
        return c
    return "".join(x.get("text", "") for x in c if isinstance(x, dict))


def latest_user_turn(runtime: ToolRuntime | None) -> UserTurn:
    msgs = (runtime.state or {}).get("messages", []) if runtime is not None else []
    humans = [m for m in msgs if isinstance(m, HumanMessage)]
    if not humans:
        return UserTurn(None, "", 0)
    last = humans[-1]
    return UserTurn(getattr(last, "id", None) or f"idx:{len(humans)}", _msg_text(last), len(humans))


def runtime_session(runtime: ToolRuntime | None) -> str | None:
    if runtime is None:
        return None
    cfg = getattr(runtime, "config", None) or {}
    return (cfg.get("configurable") or {}).get("thread_id")


# ------------------------------------------------------------------ 需求
def _as_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "、".join(str(x) for x in v if str(x).strip())
    return str(v).strip()


def normalize_requirements(req: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """返回 (规范化后的需求, 缺失的必填项)。必填项可以由用户给出，也可以写在 assumptions 里由用户在大纲阶段确认。"""
    out = {k: v for k, v in (req or {}).items() if v not in (None, "", [])}
    density = str(out.get("visual_density") or "rich").strip().lower()
    if density not in VISUAL_DENSITY:
        density = "rich"
    out["visual_density"] = density
    out["open_questions"] = [q for q in (out.get("open_questions") or []) if str(q).strip()]
    out["assumptions"] = [a for a in (out.get("assumptions") or []) if str(a).strip()]
    assumed = "".join(out["assumptions"])
    missing = [label for key, label in REQUIRED_FIELDS.items()
               if not _as_text(out.get(key)) and label not in assumed]
    return out, missing


def save_requirements(session_id: str, req: dict[str, Any], turn: UserTurn) -> str:
    with _lock:
        state = load_state(session_id) or _new_state(session_id)
        norm, missing = normalize_requirements(req)
        norm["saved_after_message"] = turn.message_id
        changed_after_approval = state.get("approved_version") is not None
        state["requirements"] = norm
        state["stage"] = "requirements"
        if state.get("outlines") or changed_after_approval:
            # 需求变了，旧大纲的确认失效，必须重新出大纲并确认
            state["approved_version"] = None
        _log(state, "需求已更新" + ("（原大纲确认已失效）" if changed_after_approval else ""))
        save_state(session_id, state)
    lines = [f"已记录需求（会话目录 {repo_path(session_dir(session_id))}）。"]
    if missing:
        lines.append(f"⚠️ 以下必填项既没有用户答复也没有写入 assumptions：{'、'.join(missing)}。"
                     "请向用户询问，或在 assumptions 中写明你的默认假设（如「受众：假设为临床医生」）。")
    if norm["open_questions"]:
        lines.append("还有待用户回答的问题：" + "；".join(norm["open_questions"])
                     + "。请把这些问题连同需求摘要一起发给用户，**本轮不要出大纲**；"
                       "用户答复后再次调用 ppt_save_requirements（open_questions 置空）。")
    elif not missing:
        lines.append("需求已齐全，下一步：调用 ppt_propose_outline 提交大纲。")
    return "\n".join(lines)


# ------------------------------------------------------------------ 大纲
def _visual(slide: dict[str, Any]) -> dict[str, Any]:
    v = slide.get("visual")
    if isinstance(v, str):
        v = {"kind": v}
    return v if isinstance(v, dict) else {"kind": "none"}


def title_width(text: str) -> float:
    import unicodedata

    w = 0.0
    for ch in str(text):
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 1.0
        elif ch in "ilI.,:;'!| ":
            w += 0.35
        else:
            w += 0.64
    return w


def validate_outline(outline: dict[str, Any], requirements: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    """返回 (错误, 提示)。错误必须修正后才能提交给用户。"""
    errors: list[str] = []
    notes: list[str] = []
    if not isinstance(outline, dict):
        return ["大纲必须是 JSON 对象"], notes
    if not str(outline.get("title") or "").strip():
        errors.append("缺少 title（PPT 总标题）")
    slides = outline.get("slides")
    if not isinstance(slides, list) or not slides:
        return errors + ["slides 必须是非空数组"], notes
    if len(slides) > 60:
        errors.append("大纲超过 60 页，请精简或拆成多份")
    content_pages = visual_pages = 0
    titles: set[str] = set()
    for i, s in enumerate(slides, 1):
        where = f"第 {i} 页"
        if not isinstance(s, dict):
            errors.append(f"{where} 必须是对象")
            continue
        title = str(s.get("title") or "").strip()
        layout = str(s.get("layout") or "").strip()
        if not title:
            errors.append(f"{where} 缺少 title")
        elif normalize_title(title) in titles:
            errors.append(f"{where} 标题「{title}」重复；每页标题需唯一，便于和 spec 对照")
        titles.add(normalize_title(title))
        cap = TITLE_CAPACITY.get(layout, TITLE_CAPACITY["content"])
        if title and title_width(title) > cap:
            errors.append(f"{where} 标题「{title}」过长（约 {title_width(title):.0f} 字宽，"
                          f"{'封面' if layout == 'title' else '章节页' if layout == 'section' else '内容页'}标题单行最多约 {cap:.0f} 个汉字），"
                          "生成时会溢出；请缩短，补充说明放到副标题或正文")
        if layout not in LAYOUTS:
            errors.append(f"{where} layout 必须是 {sorted(LAYOUTS)} 之一（收到 {layout or '空'}）")
            continue
        if layout in ("title", "section"):
            continue
        content_pages += 1
        if not str(s.get("key_message") or "").strip():
            errors.append(f"{where}「{title}」缺少 key_message（本页要传达的核心观点）")
        points = s.get("points")
        if not isinstance(points, list) or not any(str(p).strip() for p in points):
            errors.append(f"{where}「{title}」缺少 points（本页实际要讲的要点）")
        v = _visual(s)
        kind = str(v.get("kind") or "none")
        if kind not in VISUAL_KINDS:
            errors.append(f"{where} visual.kind 必须是 {sorted(VISUAL_KINDS)} 之一")
            continue
        if kind != "none":
            visual_pages += 1
            if not str(v.get("description") or "").strip():
                errors.append(f"{where}「{title}」visual.description 需说明画面/图表内容")
            if kind in ("image", "chart", "table") and not str(v.get("source") or "").strip():
                errors.append(f"{where}「{title}」visual.source 需说明素材/数据来源（联网图片、用户资料、已核验文献等）")
        # 版式与配图一致，避免“大纲说有图、spec 里没有位置放图”
        need = {"image": {"image", "diagram"}, "chart": {"chart"}, "table": {"table"}}
        if layout in need and kind not in need[layout]:
            errors.append(f"{where}「{title}」layout={layout} 需要 visual.kind 为 {'/'.join(sorted(need[layout]))}")
        if layout == "columns" and kind == "none":
            errors.append(f"{where}「{title}」columns 图文混排页需要配图/图表/表格；纯文字请用 bullets/text")
        if layout in ("bullets", "text") and kind != "none":
            notes.append(f"{where}「{title}」有配图但 layout={layout}，已按 columns（左文右图）处理更合适")
    density = (requirements or {}).get("visual_density", "rich")
    need_ratio = VISUAL_DENSITY.get(density, 0.5)
    if content_pages and visual_pages / content_pages < need_ratio:
        errors.append(f"图文密度不足：{content_pages} 个内容页中只有 {visual_pages} 页带图片/示意图/图表/表格，"
                      f"当前要求（{density}）至少 {need_ratio:.0%}。请为讲解「所见、流程、结构、趋势、对比」的页面补充视觉内容；"
                      "若用户明确只要文字，把需求 visual_density 设为 minimal。")
    return errors, notes


def render_outline_md(outline: dict[str, Any], requirements: dict[str, Any] | None, version: int) -> str:
    req = requirements or {}
    head = [f"### 📑 PPT 大纲（第 {version} 版）：{outline.get('title', '')}"]
    if outline.get("subtitle"):
        head.append(f"副标题：{outline['subtitle']}")
    info = [f"{label}：{_as_text(req.get(key))}" for key, label in
            [("topic", "主题"), ("audience", "受众"), ("purpose", "用途"), ("slide_count", "页数/时长"),
             ("style", "风格"), ("language", "语言")] if _as_text(req.get(key))]
    density_label = {"rich": "丰富（≥50% 内容页配图/图表）", "balanced": "适中（≥30%）", "minimal": "以文字为主"}
    info.append(f"图文密度：{density_label.get(req.get('visual_density', 'rich'), '丰富')}")
    info.append("视觉样式：" + (req.get("theme_desc") or "academic 学术蓝绿（默认）"))
    head.append("**需求摘要**：" + "；".join(info))
    if req.get("assumptions"):
        head.append("**我做的假设（请确认）**：" + "；".join(req["assumptions"]))
    icon = {"none": "—", "image": "🖼️ 图片", "diagram": "🧭 示意图", "chart": "📊 图表", "table": "📋 表格"}
    layout_label = {"title": "封面", "section": "章节页", "bullets": "要点", "text": "文字", "image": "大图",
                    "chart": "图表", "table": "表格", "columns": "图文混排"}
    rows = ["| 页 | 标题 | 版式 | 核心内容 | 配图 / 图表 |", "| --- | --- | --- | --- | --- |"]
    for i, s in enumerate(outline.get("slides", []), 1):
        v = _visual(s)
        kind = v.get("kind", "none")
        pts = "；".join(str(p) for p in (s.get("points") or []))
        body = (f"**{s.get('key_message')}**<br>{pts}" if s.get("key_message") else pts) or (s.get("subtitle") or "")
        vis = icon.get(kind, kind)
        if kind != "none":
            vis += f"：{v.get('description', '')}"
            if v.get("source"):
                vis += f"（来源：{v['source']}）"
        cell = lambda x: str(x).replace("|", "／").replace("\n", " ")  # noqa: E731
        rows.append(f"| {i} | {cell(s.get('title', ''))} | {layout_label.get(s.get('layout'), s.get('layout'))} "
                    f"| {cell(body)} | {cell(vis)} |")
    tail = ("\n\n请确认这份大纲：回复 **「确认大纲」** 开始生成；如需调整，直接告诉我要改哪一页、怎么改"
            "（增删页面、调整顺序、换配图、改重点都可以）。")
    return "\n\n".join(head) + "\n\n" + "\n".join(rows) + tail


def propose_outline(session_id: str, outline: dict[str, Any], turn: UserTurn) -> str:
    with _lock:
        state = load_state(session_id)
        if not state or not state.get("requirements"):
            return "❌ 还没有记录需求。请先与用户确认需求并调用 ppt_save_requirements。"
        req = state["requirements"]
        if req.get("open_questions") and req.get("saved_after_message") == turn.message_id:
            return ("❌ 需求里还有待用户回答的问题，本轮不能出大纲。请先把问题发给用户；"
                    "用户答复后用 ppt_save_requirements 更新需求（open_questions 置空）再出大纲。")
        _, missing = normalize_requirements(req)
        if missing:
            return f"❌ 需求缺少：{'、'.join(missing)}。请询问用户或写入 assumptions 后再出大纲。"
        errors, notes = validate_outline(outline, req)
        if errors:
            return "❌ 大纲未通过检查，请修正后重新提交：\n- " + "\n- ".join(errors)
        version = len(state.get("outlines", [])) + 1
        d = session_dir(session_id)
        md = render_outline_md(outline, req, version)
        (d / f"outline_v{version}.json").write_text(json.dumps(outline, ensure_ascii=False, indent=2), encoding="utf-8")
        (d / f"outline_v{version}.md").write_text(md, encoding="utf-8")
        state.setdefault("outlines", []).append({
            "version": version, "title": outline.get("title"), "slides": len(outline.get("slides", [])),
            "proposed_after_message": turn.message_id, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        state["approved_version"] = None
        state["stage"] = "outline_review"
        _log(state, f"提交大纲 v{version}")
        save_state(session_id, state)
    extra = ("\n提示：" + "；".join(notes)) if notes else ""
    return (f"✅ 大纲 v{version} 已保存（{repo_path(d / f'outline_v{version}.md')}）。"
            "请把下面的大纲**原样**发给用户，然后结束本轮回复，等待用户确认或提出修改；"
            f"不要在本轮写 spec 或生成 PPT。{extra}\n\n{md}")


def confirm_outline(session_id: str, version: int, turn: UserTurn) -> str:
    with _lock:
        state = load_state(session_id)
        if not state or not state.get("outlines"):
            return "❌ 还没有大纲，请先调用 ppt_propose_outline。"
        latest = state["outlines"][-1]
        if int(version) != latest["version"]:
            return f"❌ 只能确认最新的大纲 v{latest['version']}（收到 v{version}）。"
        if state.get("approved_version") == latest["version"]:
            return f"大纲 v{latest['version']} 已经确认过，可以继续写 spec 并生成。"
        if turn.message_id is None or turn.message_id == latest.get("proposed_after_message"):
            return ("❌ 用户还没有对这版大纲作出回复。请把大纲发给用户并结束本轮，"
                    "等用户明确回复「确认」后再调用本工具。不能替用户确认。")
        verdict = classify_reply(turn.text)
        if verdict != "confirm":
            hint = ("用户的回复包含修改意见：请按意见修改后用 ppt_propose_outline 提交新版本，再次请用户确认。"
                    if verdict == "revise" else
                    "无法从用户回复中确定其已确认大纲：请直接问用户「是否确认这版大纲？」。")
            return f"❌ 未确认。用户最新回复：「{turn.text[:100]}」。{hint}"
        state["approved_version"] = latest["version"]
        state["approved_by_message"] = turn.text[:200]
        state["approved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        state["stage"] = "approved"
        _log(state, f"用户确认大纲 v{latest['version']}")
        save_state(session_id, state)
    d = session_dir(session_id)
    return (f"✅ 用户已确认大纲 v{latest['version']}。下一步：\n"
            f"1. 读取已确认大纲：report_read_file('{repo_path(d / f'outline_v{latest['version']}.json')}')；\n"
            "2. 按大纲准备素材：联网图片用 ppt_search_images + ppt_download_image，流程/时间线/结构类用 ppt_make_diagram；\n"
            f"3. 把 spec 写到 {repo_path(d)}/spec.json，每页 title 与大纲一致，按大纲的版式和配图写入；\n"
            "4. report_run_generator(skill='ppt-report', spec_path=...) 生成；5. ppt_verify 渲染验证。")


# ------------------------------------------------------------------ 生成门禁（由 report_run_generator 调用）
def normalize_title(title: str) -> str:
    t = re.sub(r"[（(]\s*\d+\s*/\s*\d+\s*[)）]\s*$", "", str(title or ""))
    return re.sub(r"\s+", "", t).lower()


def _spec_visual(slide: dict[str, Any]) -> bool:
    kind = slide.get("type", "bullets")
    if kind in ("image", "chart", "table"):
        return True
    if kind == "columns":
        blocks = list(slide.get("left") or []) + list(slide.get("right") or [])
        return any(isinstance(b, dict) and b.get("type") in ("image", "chart", "table") for b in blocks)
    return False


def check_spec_against_outline(spec: dict[str, Any], outline: dict[str, Any],
                               requirements: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    slides = [s for s in spec.get("slides", []) if isinstance(s, dict)]
    spec_titles = {normalize_title(s.get("title", "")): s for s in slides}
    for i, o in enumerate(outline.get("slides", []), 1):
        s = spec_titles.get(normalize_title(o.get("title", "")))
        if s is None:
            errors.append(f"大纲第 {i} 页「{o.get('title')}」在 spec 中找不到同名页")
            continue
        kind = _visual(o).get("kind", "none")
        if kind != "none" and o.get("layout") not in ("title", "section") and not _spec_visual(s):
            warnings.append(f"「{o.get('title')}」大纲计划配{kind}，spec 中没有图片/图表/表格")
    outline_titles = {normalize_title(o.get("title", "")) for o in outline.get("slides", [])}
    extra = [s.get("title") for s in slides if normalize_title(s.get("title", "")) not in outline_titles]
    if extra:
        warnings.append("spec 中有大纲外的页面：" + "、".join(str(t) for t in extra[:8]))
    content = [s for s in slides if s.get("type", "bullets") not in ("title", "section")]
    need = VISUAL_DENSITY.get((requirements or {}).get("visual_density", "rich"), 0.5)
    have = sum(_spec_visual(s) for s in content)
    if content and have / len(content) < need:
        warnings.append(f"spec 图文密度 {have}/{len(content)} 低于要求 {need:.0%}")
    return errors, warnings


MAX_FAILS_PER_TURN = 6  # 同一轮用户消息里生成失败的次数上限，超过后先向用户汇报，避免耗尽 Agent 步数


def before_generate(session_id: str, spec_file: Path, turn: UserTurn | None = None) -> tuple[str | None, list[str]]:
    """返回 (拒绝原因 or None, 警告)。"""
    state = load_state(session_id)
    if state and turn is not None and turn.message_id:
        if state.get("stage") == "delivered" and state.get("delivered_after_message") == turn.message_id:
            # 验证已通过：本轮必须先交付，是否采纳优化建议由用户决定（避免模型自行反复重做、耗尽步数）
            return ("❌ 本轮的 PPT 已通过验证，不要再自行重新生成。请现在就把下载地址、预览图和验证结论交付给用户；"
                    "视觉评审里的优化建议可以列出来，请用户决定是否调整。"), []
        fails = state.get("fail_count") if state.get("fail_turn") == turn.message_id else 0
        if (fails or 0) >= MAX_FAILS_PER_TURN:
            last = state.get("last_error", "")
            return (f"❌ 本轮已连续生成失败 {fails} 次（最近一次：{last[:200]}）。请停止重试，"
                    "向用户说明卡在哪一页、什么原因，并给出调整建议（如精简该页文字、拆页、换版式），等用户回复后再继续。"), []
    if not state or state.get("approved_version") is None:
        stage = STAGES.get((state or {}).get("stage", ""), "尚未开始")
        return (f"❌ PPT 大纲尚未经用户确认（当前阶段：{stage}），不能生成。"
                "请按流程：整理需求 → ppt_propose_outline 提交大纲 → 用户确认 → ppt_confirm_outline → 再生成。"), []
    latest = state["outlines"][-1]["version"]
    if state["approved_version"] != latest:
        return f"❌ 最新大纲 v{latest} 尚未确认，请先让用户确认。", []
    d = session_dir(session_id)
    if not spec_file.resolve().is_relative_to(d.resolve()):
        return f"❌ spec 必须放在本会话目录 {repo_path(d)}/ 下（收到 {spec_file.name}）。", []
    try:
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
        outline = json.loads((d / f"outline_v{latest}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return f"❌ 读取 spec / 大纲失败：{e}", []
    errors, warnings = check_spec_against_outline(spec, outline, state.get("requirements"))
    theme = (state.get("requirements") or {}).get("theme")
    if theme and "style" not in spec:
        # 用户在大纲阶段确认过样式：spec 没写时自动补上，避免模型漏写导致样式丢失
        spec["style"] = theme
        spec_file.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        warnings.append("spec 未写 style，已按确认的需求自动补上样式")
    elif theme and spec.get("style") != theme:
        warnings.append("spec.style 与需求中确认的样式不同；若不是用户要求的调整，请改回确认的样式")
    if errors:
        return ("❌ spec 与用户确认的大纲不一致：\n- " + "\n- ".join(errors)
                + "\n请按已确认大纲补齐；如果确实需要改变结构，先用 ppt_propose_outline 提交新版本并请用户确认。"), warnings
    return None, warnings


def after_generate(session_id: str, spec_file: Path, output: str | None, ok: bool,
                   error: str = "", turn: UserTurn | None = None) -> None:
    with _lock:
        state = load_state(session_id)
        if not state:
            return
        turn_id = turn.message_id if turn else None
        if ok and output:
            state.setdefault("outputs", []).append({
                "file": output, "spec": repo_path(spec_file), "outline_version": state.get("approved_version"),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            state["stage"] = "generated"
            state["last_verify"] = None
            state["fail_count"], state["fail_turn"] = 0, turn_id
            _log(state, f"生成 {Path(output).name}")
        else:
            state["fail_count"] = (state.get("fail_count", 0) if state.get("fail_turn") == turn_id else 0) + 1
            state["fail_turn"] = turn_id
            state["last_error"] = error[:500]
            _log(state, f"生成失败：{error[:160]}")
        save_state(session_id, state)


def record_verify(session_id: str, result: dict[str, Any], turn: UserTurn | None = None) -> None:
    with _lock:
        state = load_state(session_id)
        if not state:
            return
        state["last_verify"] = result
        state["stage"] = "delivered" if result.get("passed") else "needs_fix"
        if result.get("passed"):
            state["delivered_after_message"] = turn.message_id if turn else None
        _log(state, f"验证{'通过' if result.get('passed') else '发现问题'}：{Path(result.get('file', '')).name}")
        save_state(session_id, state)


# ------------------------------------------------------------------ 状态描述（给模型看）
def status_text(session_id: str) -> str:
    state = load_state(session_id)
    d = repo_path(session_dir(session_id))
    if not state:
        return (f"本会话还没有开始制作 PPT。会话目录：{d}\n"
                "第一步：整理用户需求（主题、受众、用途、页数/时长、风格、已有素材、图文偏好），"
                "缺失的关键信息向用户提问，调用 ppt_save_requirements 记录。")
    stage = state.get("stage", "requirements")
    lines = [f"当前阶段：{STAGES.get(stage, stage)}（{stage}）；会话目录：{d}"]
    req = state.get("requirements") or {}
    if req:
        lines.append("需求：" + json.dumps({k: v for k, v in req.items() if k != "saved_after_message"},
                                         ensure_ascii=False)[:800])
    if state.get("outlines"):
        o = state["outlines"][-1]
        ok = "已确认" if state.get("approved_version") == o["version"] else "未确认"
        lines.append(f"最新大纲：v{o['version']}《{o.get('title')}》{o.get('slides')} 页，{ok}；"
                     f"文件 {d}/outline_v{o['version']}.json")
    if state.get("outputs"):
        lines.append(f"最近生成：{state['outputs'][-1]['file']}")
    if state.get("fail_count") and stage in ("approved", "generated", "needs_fix"):
        lines.append(f"本轮已生成失败 {state['fail_count']} 次（上限 {MAX_FAILS_PER_TURN}），最近原因：{state.get('last_error', '')[:200]}")
    if state.get("last_verify"):
        v = state["last_verify"]
        lines.append(f"最近验证：{'通过' if v.get('passed') else '未通过'}；{v.get('summary', '')[:300]}")
    lines.append("下一步：" + NEXT_STEP[stage])
    return "\n".join(lines)


NEXT_STEP = {
    "requirements": "若仍有待用户回答的问题，先把问题发给用户并结束本轮；需求齐全后调用 ppt_propose_outline 提交大纲。",
    "outline_review": ("等待用户对大纲的回复：用户明确确认 → 调用 ppt_confirm_outline；用户提出修改 → 修改后用 "
                       "ppt_propose_outline 提交新版本并再次请用户确认（可反复多轮）。禁止在确认前写 spec 或生成。"),
    "approved": "按已确认大纲准备配图/示意图，写 spec 到会话目录，调用 report_run_generator(skill='ppt-report') 生成。",
    "generated": "必须调用 ppt_verify 渲染并逐页检查，未验证不能告诉用户已完成。",
    "needs_fix": "按验证问题修正 spec（精简文字、拆页、换图等）后重新生成并再次 ppt_verify；若需改结构，先提交新大纲请用户确认。",
    "delivered": ("已交付：本轮立即把下载地址、预览图、页数/配图数和验证结论告诉用户，视觉评审建议只列出来由用户决定，"
                  "本轮不要再自行重新生成。用户后续若要求修改内容/结构，"
                  "先用 ppt_propose_outline 提交修改后的大纲并确认；纯排版微调可直接改 spec 重生成并验证。"),
}


# ------------------------------------------------------------------ 工具
def _session_or_error(runtime: ToolRuntime | None) -> tuple[str | None, str | None]:
    sid = runtime_session(runtime)
    if not sid:
        return None, "❌ 无法获取会话 id（该工具只能在对话中调用）"
    try:
        session_dir(sid)
    except ValueError as e:
        return None, f"❌ {e}"
    return sid, None


@tool
def ppt_workflow_status(runtime: ToolRuntime) -> str:
    """查看本会话 PPT 制作流程的当前阶段、需求、大纲版本和下一步要求。"""
    sid, err = _session_or_error(runtime)
    return err or status_text(sid)


@tool
def ppt_save_requirements(topic: str = "", audience: str = "", purpose: str = "", slide_count: str = "",
                          style: str = "", language: str = "中文", materials: str = "", key_points: str = "",
                          visual_density: str = "rich", theme: dict | str | None = None,
                          assumptions: list[str] | None = None,
                          open_questions: list[str] | None = None, runtime: ToolRuntime = None) -> str:
    """记录/更新 PPT 需求。topic 主题、audience 受众、purpose 用途/场景、slide_count 页数或演讲时长、style 风格、
    materials 用户已有素材/数据、key_points 用户强调的重点；visual_density 图文密度：rich（默认，≥50% 内容页配图）/
    balanced（≥30%）/ minimal（以文字为主，用户明确要求时才用）。
    theme：视觉样式，写法同 spec.style（预设名如 "business"，或 {"preset":..., "colors":{...}, ...}），按用户描述的
    风格/品牌色/用途选择（可先 report_list_styles 查看），会在大纲中展示给用户确认；style 记录风格的文字描述。
    assumptions：用户没说、由你默认的项（会在大纲里请用户确认）；open_questions：必须先问用户的问题，非空时本轮不能出大纲。
    需求有变化时再次调用即可（会撤销已确认的大纲）。"""
    sid, err = _session_or_error(runtime)
    if err:
        return err
    req = {"topic": topic, "audience": audience, "purpose": purpose, "slide_count": slide_count, "style": style,
           "language": language, "materials": materials, "key_points": key_points,
           "visual_density": visual_density, "assumptions": assumptions or [], "open_questions": open_questions or []}
    if theme:
        from app.agent.report_styles import describe, resolve_style

        if isinstance(theme, str) and theme.strip().startswith("{"):
            try:
                theme = json.loads(theme)
            except json.JSONDecodeError as e:
                return f"❌ theme 不是合法 JSON：{e}"
        try:
            resolved, _ = resolve_style(theme, "ppt-report")
        except ValueError as e:
            return f"❌ 样式无效：{e}"
        req["theme"] = theme
        req["theme_desc"] = describe(resolved)
    return save_requirements(sid, req, latest_user_turn(runtime))


@tool
def ppt_propose_outline(outline: dict, runtime: ToolRuntime) -> str:
    """提交 PPT 大纲（新版本），返回渲染好的大纲 Markdown，需原样展示给用户确认。outline 结构：
    {"title": "总标题", "subtitle": "可选",
     "slides": [{"title": "页标题（唯一）", "layout": "title|section|bullets|text|image|chart|table|columns",
                 "key_message": "本页核心观点（内容页必填）", "points": ["要讲的要点", ...],
                 "visual": {"kind": "none|image|diagram|chart|table", "description": "画面/图表内容",
                            "source": "素材或数据来源：联网图片 / 用户资料 / 示意图生成 / 已核验文献"}}]}
    用户每次要求修改大纲，都用本工具提交修改后的完整新版本。"""
    sid, err = _session_or_error(runtime)
    if err:
        return err
    if isinstance(outline, str):
        try:
            outline = json.loads(outline)
        except json.JSONDecodeError as e:
            return f"❌ outline 不是合法 JSON：{e}"
    return propose_outline(sid, outline, latest_user_turn(runtime))


@tool
def ppt_confirm_outline(version: int, runtime: ToolRuntime) -> str:
    """在用户明确回复确认大纲后调用，记录确认，之后才能生成 PPT。version 为用户确认的大纲版本号。
    会校验用户最新消息确实是在大纲提出之后发出的确认；用户提出修改意见时不要调用。"""
    sid, err = _session_or_error(runtime)
    if err:
        return err
    return confirm_outline(sid, version, latest_user_turn(runtime))


WORKFLOW_TOOLS = [ppt_workflow_status, ppt_save_requirements, ppt_propose_outline, ppt_confirm_outline]


# ------------------------------------------------------------------ 中间件：每次调用模型时注入流程状态
_START_HINT = (
    "\n\n## PPT 制作流程（强制）\n"
    "用户要求制作 PPT / 演示文稿 / 幻灯片时：先 `load_skill('ppt-report')` 阅读说明，然后严格按流程：\n"
    "1) 整理需求（缺关键信息先问用户）→ `ppt_save_requirements`；2) `ppt_propose_outline` 提交大纲并原样展示，"
    "**结束本轮等待用户确认**；用户要修改就改完再提交新版本，可多轮；3) 用户确认后 `ppt_confirm_outline`；"
    "4) 准备图文素材、写 spec、`report_run_generator` 生成；5) `ppt_verify` 渲染验证，有问题修正重生成。"
    "未经用户确认大纲，系统会拒绝生成。"
)


class PptWorkflowMiddleware(AgentMiddleware):
    """把本会话 PPT 流程状态追加到系统提示词。agent 按请求构建，所以直接在构造时传入 session_id。"""

    def __init__(self, session_id: str):
        super().__init__()
        self.session_id = session_id

    def _extra(self) -> str:
        try:
            state = load_state(self.session_id)
        except ValueError:
            return ""
        if not state:
            return _START_HINT
        return _START_HINT + "\n\n### 本会话 PPT 流程状态\n" + status_text(self.session_id)

    def _patch(self, request):
        extra = self._extra()
        if not extra:
            return request
        base = request.system_prompt or ""
        return request.override(system_message=SystemMessage(content=base + extra))

    def wrap_model_call(self, request, handler):
        return handler(self._patch(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._patch(request))
