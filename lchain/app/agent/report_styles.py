"""报告样式（主题）：html-report / word-report / ppt-report 共用的自定义样式协议。

spec 顶层可以写 `style`，三种报告同一套结构：

    "style": "business"                                   # 直接用预设名（或用户保存的样式名）
    "style": {"preset": "business",                       # 以预设为基础，再覆盖部分字段
              "colors": {"primary": "#C8102E", "accent": "#1F4E79"},
              "fonts": {"cn": "思源黑体"},
              "table": "header_fill", "cover": "band",
              "brand": {"label": "XX 医院 · 质控报告", "footer": "XX 医院医务部"}}

字段：
- colors：primary 主色（标题栏、表头、章节色带）、accent 强调色（引用、链接、项目符号）、text 正文、heading 标题、
  muted 次要文字、border 分隔线、alt 浅底（斑马纹、代码底）、highlight 点缀色（封面细线）、background 背景、
  on_primary 主色上的文字色（缺省自动按对比度选黑/白）、chart 图表系列色（2~6 个）
- fonts：cn 中文字体、en 西文字体、heading_cn 标题中文字体（缺省同 cn）
- font_scale：正文字号缩放 0.85~1.25（Word / HTML；PPT 版式按固定字号测量，不支持缩放）
- table：auto 按格式默认 / three_line 三线表 / header_fill 表头填充主色 / grid 全框线
- cover：classic 经典 / band 主色色带封面
- brand：label 封面小标签（HTML/PPT 页脚）、footer 页脚/结尾说明

本模块负责：预设库、用户保存的样式（工作区 styles/*.json）、把输入解析成**完整**的样式对象并做校验
（颜色格式、字体名、正文/背景对比度等）。生成脚本只接收解析后的具体数值，不认识预设名，
这样对 medex 原脚本的改动最小。
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

HEX_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")
FONT_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff \-]{0,39}$")
NAME_RE = re.compile(r"^[\w\u4e00-\u9fff-]{1,32}$")
COLOR_KEYS = ("primary", "accent", "text", "heading", "muted", "border", "alt", "highlight", "background", "on_primary")
TABLE_STYLES = {"auto": "按格式默认（Word/HTML 三线表，PPT 表头填充）", "three_line": "三线表",
                "header_fill": "表头填充主色", "grid": "全框线"}
COVER_STYLES = {"classic": "经典（白底居中）", "band": "主色色带"}
SKILLS = ("html-report", "word-report", "ppt-report")

_BASE: dict[str, Any] = {
    "colors": {"primary": "19364D", "accent": "007F7A", "text": "253746", "heading": "162D40", "muted": "617180",
               "border": "DAE3E8", "alt": "F5F8FA", "highlight": "B58948", "background": "FFFFFF",
               "chart": ["007F7A", "3869A8", "B87924", "8461A0", "617E88", "C66A3C"]},
    "fonts": {"cn": "微软雅黑", "en": "Calibri", "heading_cn": ""},
    "font_scale": 1.0,
    "table": "auto",
    "cover": "classic",
    "brand": {"label": "", "footer": ""},
}


def _preset(label: str, desc: str, colors: dict | None = None, **kw) -> dict[str, Any]:
    p = copy.deepcopy(_BASE)
    p["colors"].update(colors or {})
    for k, v in kw.items():
        p[k] = {**p[k], **v} if isinstance(v, dict) and isinstance(p.get(k), dict) else v
    p["label"], p["description"] = label, desc
    return p


PRESETS: dict[str, dict[str, Any]] = {
    "academic": _preset("学术蓝绿", "深蓝 + 青绿，克制严谨，适合医学/科研汇报（原默认样式）"),
    "business": _preset("商务蓝", "藏青 + 亮蓝 + 金色点缀，表头填充，适合工作汇报、方案评审", {
        "primary": "1F3A5F", "accent": "2E75B6", "text": "262F3A", "heading": "14263D", "muted": "5F6B7A",
        "border": "D6DEE8", "alt": "F2F6FA", "highlight": "C9A227",
        "chart": ["2E75B6", "1F3A5F", "C9A227", "6C8EBF", "8C9BAB", "D9822B"]},
        table="header_fill", cover="band"),
    "tech": _preset("科技蓝紫", "深蓝底色调 + 电光蓝 + 紫色，现代感强，适合技术/产品介绍", {
        "primary": "0F2447", "accent": "0091D5", "text": "1F2A3C", "heading": "0B1B36", "muted": "5A6B82",
        "border": "D3DDEB", "alt": "F1F5FB", "highlight": "7B61FF",
        "chart": ["0091D5", "7B61FF", "00B39F", "F2A33A", "5A6B82", "E5577C"]},
        fonts={"cn": "微软雅黑", "en": "Segoe UI"}, table="header_fill", cover="band"),
    "fresh": _preset("清新绿", "森绿 + 嫩绿，明亮温和，适合健康科普、社区宣教", {
        "primary": "2E6B47", "accent": "3BA55C", "text": "25352B", "heading": "1D4A30", "muted": "5E7266",
        "border": "D5E5DA", "alt": "F2F8F4", "highlight": "E0A526",
        "chart": ["3BA55C", "2E6B47", "E0A526", "4A90A4", "8AA398", "D66A4E"]},
        table="header_fill"),
    "warm": _preset("暖橙", "棕红 + 橙色，亲和有温度，适合患者教育、活动总结", {
        "primary": "7A3419", "accent": "D9692E", "text": "3A2A22", "heading": "5C2610", "muted": "7A6558",
        "border": "EADCD2", "alt": "FBF5F0", "highlight": "E8A838",
        "chart": ["D9692E", "7A3419", "E8A838", "5B8C85", "9C8577", "B5475B"]},
        table="header_fill", cover="band"),
    "hospital": _preset("医院红", "中国红 + 深灰，庄重醒目，适合医院/机构正式报告", {
        "primary": "9E1B32", "accent": "C8102E", "text": "2B2B2B", "heading": "7A1426", "muted": "666666",
        "border": "E3D5D7", "alt": "FAF4F5", "highlight": "C9A227",
        "chart": ["C8102E", "4D4D4D", "C9A227", "3A6EA5", "999999", "E07A5F"]},
        table="header_fill", cover="band"),
    "elegant": _preset("典雅紫", "黛紫 + 金色，标题宋体，适合学术讲座、年度报告", {
        "primary": "3D2C5A", "accent": "7E5BB5", "text": "2E2A36", "heading": "2A1E40", "muted": "6B6477",
        "border": "E0DAE8", "alt": "F7F4FA", "highlight": "B8975A",
        "chart": ["7E5BB5", "3D2C5A", "B8975A", "4F8A8B", "9C94A8", "C9656B"]},
        fonts={"heading_cn": "宋体"}, cover="band"),
    "minimal": _preset("极简黑白", "黑白灰，无彩色干扰，适合打印、正式文件", {
        "primary": "222222", "accent": "444444", "text": "222222", "heading": "111111", "muted": "666666",
        "border": "D9D9D9", "alt": "F5F5F5", "highlight": "888888",
        "chart": ["333333", "7F7F7F", "B2B2B2", "555555", "999999", "CCCCCC"]}),
    "dark": _preset("深色", "深蓝黑背景 + 青色高亮，适合投屏演示（仅 PPT / HTML，Word 不支持）", {
        "primary": "1C3A57", "accent": "3FC1C9", "text": "DDE6EE", "heading": "FFFFFF", "muted": "9AAFC2",
        "border": "2E465E", "alt": "182C40", "highlight": "E3B341", "background": "0F1C2A",
        "chart": ["3FC1C9", "7AA7FF", "E3B341", "C38BFF", "9AAFC2", "FF8A65"]},
        table="header_fill"),
}
DEFAULT_PRESET = "academic"


# ------------------------------------------------------------------ 颜色工具
def norm_hex(value: Any, field: str) -> str:
    m = HEX_RE.match(str(value or "").strip())
    if not m:
        raise ValueError(f"颜色 {field} 必须是 6 位十六进制，如 #1F3A5F（收到 {value!r}）")
    return m.group(1).upper()


def _rgb(h: str) -> tuple[int, int, int]:
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def luminance(h: str) -> float:
    def ch(c: int) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(h)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def blend(a: str, b: str, t: float) -> str:
    """a 与 b 按 t（0=a，1=b）混合。"""
    ra, ga, ba = _rgb(a)
    rb, gb, bb = _rgb(b)
    return "".join(f"{round(x + (y - x) * t):02X}" for x, y in ((ra, rb), (ga, gb), (ba, bb)))


# ------------------------------------------------------------------ 用户保存的样式
def styles_dir() -> Path:
    from app.agent.report_tools import workspace

    d = workspace() / "styles"
    d.mkdir(exist_ok=True)
    return d


def saved_styles() -> dict[str, dict[str, Any]]:
    out = {}
    for f in sorted(styles_dir().glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out[f.stem] = data
    return out


def save_style(name: str, style: Any) -> dict[str, Any]:
    if not NAME_RE.match(name or ""):
        raise ValueError("样式名只能包含中英文、数字、下划线、短横线，最多 32 个字符")
    if name in PRESETS:
        raise ValueError(f"「{name}」是内置预设名，请换一个名字")
    resolved, _ = resolve_style(style, "html-report", allow_dark=True)
    resolved.pop("warnings", None)
    (styles_dir() / f"{name}.json").write_text(json.dumps(resolved, ensure_ascii=False, indent=2), encoding="utf-8")
    return resolved


# ------------------------------------------------------------------ 解析
def _base_for(name: str) -> dict[str, Any]:
    if name in PRESETS:
        return copy.deepcopy(PRESETS[name])
    saved = saved_styles()
    if name in saved:
        return {**copy.deepcopy(_BASE), **copy.deepcopy(saved[name])}
    raise ValueError(f"未知样式「{name}」。可用预设：{'、'.join(PRESETS)}"
                     + (f"；已保存的样式：{'、'.join(saved)}" if saved else ""))


def resolve_style(style: Any, skill: str, *, allow_dark: bool = False) -> tuple[dict[str, Any], list[str]]:
    """把 spec.style（预设名 / 覆盖对象）解析成完整样式。返回 (样式, 提示)。不合法时抛 ValueError。"""
    notes: list[str] = []
    if style is None or style == "":
        style = DEFAULT_PRESET
    if isinstance(style, str):
        style = {"preset": style.strip()}
    if not isinstance(style, dict):
        raise ValueError("style 必须是预设名字符串或对象")
    unknown = set(style) - {"preset", "name", "colors", "fonts", "font_scale", "table", "cover", "brand",
                            "label", "description"}
    if unknown:
        raise ValueError(f"style 不支持的字段：{'、'.join(sorted(unknown))}")
    preset = str(style.get("preset") or DEFAULT_PRESET)
    out = _base_for(preset)
    out["name"] = str(style.get("name") or (preset if len(style) <= 1 else f"{preset}（自定义）"))[:40]

    colors = style.get("colors") or {}
    if not isinstance(colors, dict):
        raise ValueError("style.colors 必须是对象")
    for k, v in colors.items():
        if k == "chart":
            if not isinstance(v, list) or not 2 <= len(v) <= 6:
                raise ValueError("style.colors.chart 需要 2~6 个颜色")
            out["colors"]["chart"] = [norm_hex(c, "chart") for c in v]
        elif k in COLOR_KEYS:
            out["colors"][k] = norm_hex(v, k)
        else:
            raise ValueError(f"style.colors 不支持 {k}，可用：{'、'.join(COLOR_KEYS)}、chart")
    c = out["colors"]
    for k in COLOR_KEYS:
        if k != "on_primary":
            c[k] = norm_hex(c[k], k)
    # 只改了主色/强调色时，让标题色、图表色跟着联动（否则会出现“紫色主色 + 预设绿色标题/图表”的混搭）
    if "primary" in colors and "heading" not in colors:
        dark_bg = luminance(c["background"]) < 0.2
        c["heading"] = blend(c["primary"], "FFFFFF" if dark_bg else "000000", 0.25)
    if ("primary" in colors or "accent" in colors) and "chart" not in colors:
        preset_chart = [x for x in c["chart"] if x not in (c["primary"], c["accent"])]
        c["chart"] = [c["accent"], c["primary"], *preset_chart][:6]
    c["chart"] = [norm_hex(x, "chart") for x in c.get("chart") or _BASE["colors"]["chart"]]
    while len(c["chart"]) < 6:  # 补足 6 个系列色：用已有颜色与背景混合出浅一档
        c["chart"].append(blend(c["chart"][len(c["chart"]) % 2], c["background"], 0.35))
    # 主色用作文字（章节标题、小标题）时，与背景对比度不足就改用标题色，避免深色背景上看不见
    c["primary_text"] = c["primary"] if contrast(c["primary"], c["background"]) >= 3.0 else c["heading"]
    if not colors.get("on_primary"):
        c["on_primary"] = "FFFFFF" if contrast("FFFFFF", c["primary"]) >= contrast("1A1A1A", c["primary"]) else "1A1A1A"
    else:
        c["on_primary"] = norm_hex(c["on_primary"], "on_primary")

    fonts = style.get("fonts") or {}
    if not isinstance(fonts, dict):
        raise ValueError("style.fonts 必须是对象")
    for k, v in fonts.items():
        if k not in ("cn", "en", "heading_cn"):
            raise ValueError(f"style.fonts 不支持 {k}，可用：cn、en、heading_cn")
        v = str(v or "").strip()
        if v and not FONT_RE.match(v):
            raise ValueError(f"字体名「{v}」不合法（只允许中英文、数字、空格、短横线，最多 40 字）")
        out["fonts"][k] = v
    out["fonts"]["heading_cn"] = out["fonts"].get("heading_cn") or out["fonts"]["cn"]

    if "font_scale" in style:
        try:
            scale = float(style["font_scale"])
        except (TypeError, ValueError) as e:
            raise ValueError("style.font_scale 必须是数字") from e
        if not 0.85 <= scale <= 1.25:
            raise ValueError("style.font_scale 取值 0.85~1.25")
        out["font_scale"] = round(scale, 3)
    if skill == "ppt-report" and out["font_scale"] != 1.0:
        notes.append("PPT 按固定字号测量排版，已忽略 font_scale")
        out["font_scale"] = 1.0
    for key, allowed in (("table", TABLE_STYLES), ("cover", COVER_STYLES)):
        if key in style:
            if style[key] not in allowed:
                raise ValueError(f"style.{key} 取值：{'、'.join(f'{k}（{v}）' for k, v in allowed.items())}")
            out[key] = style[key]
    brand = style.get("brand") or {}
    if not isinstance(brand, dict):
        raise ValueError("style.brand 必须是对象")
    for k, limit in (("label", 30), ("footer", 80)):
        if k in brand:
            v = str(brand[k] or "").strip()
            if len(v) > limit:
                raise ValueError(f"style.brand.{k} 最多 {limit} 个字")
            out["brand"][k] = v

    # 可读性：按 WCAG 对比度检查，避免用户给的颜色组合导致文字看不清
    dark = luminance(c["background"]) < 0.2
    if dark and skill == "word-report" and not allow_dark:
        raise ValueError("Word 报告不支持深色背景（打印和编辑都会看不清），请换用浅色样式")
    checks = [("text", "正文", 4.5), ("heading", "标题", 4.5), ("muted", "次要文字", 3.0), ("accent", "强调色", 3.0)]
    bad = [f"{label}（{c[k]}）与背景（{c['background']}）对比度 {contrast(c[k], c['background']):.1f}，至少需要 {need}"
           for k, label, need in checks if contrast(c[k], c["background"]) < need]
    if contrast(c["on_primary"], c["primary"]) < 4.5:
        bad.append(f"主色（{c['primary']}）上的文字（{c['on_primary']}）对比度不足 4.5")
    if bad:
        raise ValueError("样式颜色可读性不足：" + "；".join(bad) + "。请调深/调浅相应颜色")
    out.pop("label", None)
    out.pop("description", None)
    return out, notes


def describe(style: dict[str, Any]) -> str:
    c = style["colors"]
    f = style["fonts"]
    return (f"{style.get('name', '')}：主色 #{c['primary']}、强调 #{c['accent']}、背景 #{c['background']}；"
            f"字体 {f['cn']}/{f['en']}" + (f"（标题 {f['heading_cn']}）" if f["heading_cn"] != f["cn"] else "")
            + f"；表格 {TABLE_STYLES[style['table']]}；封面 {COVER_STYLES[style['cover']]}")


def list_styles_text() -> str:
    lines = ["内置预设（spec 里写 \"style\": \"预设名\"，或 {\"preset\": \"预设名\", ...覆盖字段}）："]
    for name, p in PRESETS.items():
        c = p["colors"]
        lines.append(f"- {name}「{p['label']}」：{p['description']}。主色 #{c['primary']} 强调 #{c['accent']}"
                     f" 背景 #{c['background']}；表格 {TABLE_STYLES[p['table']]}；封面 {COVER_STYLES[p['cover']]}")
    saved = saved_styles()
    if saved:
        lines.append("用户保存的样式：")
        for name, s in saved.items():
            try:
                lines.append(f"- {name}：{describe({**copy.deepcopy(_BASE), **s})}")
            except (KeyError, TypeError):
                lines.append(f"- {name}")
    return "\n".join(lines)


# ------------------------------------------------------------------ 预览图
def render_preview(style: dict[str, Any], out: Path, title: str = "样式预览") -> Path:
    """画一张样式样张（封面条、标题、正文、表格、图表色），便于用户在对话里确认观感。"""
    from PIL import Image, ImageDraw

    from app.agent.ppt_media import _font

    c = {k: "#" + v for k, v in style["colors"].items() if k != "chart"}
    series = ["#" + x for x in style["colors"]["chart"]]
    W, H = 1200, 675
    img = Image.new("RGB", (W, H), c["background"])
    d = ImageDraw.Draw(img)
    band = style.get("cover") == "band"
    if band:
        d.rectangle((0, 0, W, 150), fill=c["primary"])
    d.text((60, 42), title, font=_font(46, bold=True), fill=c["on_primary"] if band else c["heading"])
    d.rectangle((60, 118, 200, 124), fill=c["highlight"])
    if style["brand"].get("label"):
        d.text((W - 60, 60), style["brand"]["label"], font=_font(20), fill=c["on_primary"] if band else c["muted"],
               anchor="ra")
    y = 185
    d.text((60, y), "一、章节标题示例", font=_font(32, bold=True), fill=c.get("primary_text", c["primary"]))
    d.rectangle((60, y + 46, 140, y + 50), fill=c["accent"])
    body = "正文示例：这里展示正文颜色与字号，引用标注"
    d.text((60, y + 70), body, font=_font(24), fill=c["text"])
    bw = d.textlength(body, font=_font(24))
    d.text((60 + bw + 2, y + 66), "[1]", font=_font(16), fill=c["accent"])
    d.text((60 + bw + 30, y + 70), " 使用强调色。", font=_font(24), fill=c["text"])
    d.text((60, y + 108), "次要文字 / 图注 / 来源：示例数据，仅用于展示样式", font=_font(20), fill=c["muted"])
    # 表格
    tx, ty, cw, rh = 60, 360, [170, 150, 170], 44
    heads, rows = ["指标", "数值", "说明"], [["示例 A", "12.5", "达标"], ["示例 B", "8.3", "关注"], ["示例 C", "15.0", "达标"]]
    style_t = style["table"] if style["table"] != "auto" else "three_line"
    if style_t == "header_fill":
        d.rectangle((tx, ty, tx + sum(cw), ty + rh), fill=c["primary"])
    x = tx
    for i, h in enumerate(heads):
        d.text((x + 14, ty + 10), h, font=_font(22, bold=True),
               fill=c["on_primary"] if style_t == "header_fill" else c["heading"])
        x += cw[i]
    for r, row in enumerate(rows):
        yy = ty + rh * (r + 1)
        if style_t == "header_fill" and r % 2 == 1:
            d.rectangle((tx, yy, tx + sum(cw), yy + rh), fill=c["alt"])
        x = tx
        for i, v in enumerate(row):
            d.text((x + 14, yy + 10), v, font=_font(22), fill=c["text"])
            x += cw[i]
    if style_t == "three_line":
        for yy, w in ((ty, 3), (ty + rh, 1), (ty + rh * 4, 3)):
            d.rectangle((tx, yy, tx + sum(cw), yy + w - 1), fill=c["heading"])
    elif style_t == "grid":
        for r in range(5):
            d.line((tx, ty + rh * r, tx + sum(cw), ty + rh * r), fill=c["border"], width=2)
        x = tx
        for w in [0, *cw]:
            x += w
            d.line((x, ty, x, ty + rh * 4), fill=c["border"], width=2)
    else:
        d.line((tx, ty + rh * 4, tx + sum(cw), ty + rh * 4), fill=c["border"], width=2)
    # 柱状图
    bx, by, bh = 680, 600, 230
    d.line((bx, by, bx + 460, by), fill=c["muted"], width=2)
    vals = [0.55, 0.8, 0.65, 0.95, 0.45, 0.7]
    for i, (v, col) in enumerate(zip(vals, series)):
        x0 = bx + 20 + i * 74
        d.rectangle((x0, by - int(bh * v), x0 + 50, by - 1), fill=col)
    d.text((bx, by - bh - 50), "图表系列色", font=_font(22, bold=True), fill=c["heading"])
    f = style["fonts"]
    d.text((60, H - 50), f"{style.get('name', '')} · 字体 {f['cn']} / {f['en']}"
           + (f" · 标题 {f['heading_cn']}" if f["heading_cn"] != f["cn"] else "")
           + f" · {TABLE_STYLES[style['table']].split('（')[0]} · {COVER_STYLES[style['cover']]}",
           font=_font(18), fill=c["muted"])
    if style["brand"].get("footer"):
        d.text((W - 60, H - 50), style["brand"]["footer"], font=_font(18), fill=c["muted"], anchor="ra")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG", optimize=True)
    return out


# ------------------------------------------------------------------ Agent 工具
from langchain.tools import ToolRuntime, tool  # noqa: E402


def _preview_path(runtime: ToolRuntime | None, name: str) -> tuple[Path, str]:
    """预览图放到会话目录（有会话时）或 styles/_preview，返回 (文件, 可访问 URL)。"""
    from app.agent import ppt_workflow as wf

    safe = re.sub(r"[^\w-]", "_", name)[:40] or "style"
    sid = wf.runtime_session(runtime)
    if sid:
        d = wf.session_dir(sid) / "preview" / "styles"
        f = d / f"{safe}.png"
        return f, f"/api/ppt/preview/{wf.session_dir_name(sid)}/styles/{safe}.png"
    d = styles_dir() / "_preview"
    return d / f"{safe}.png", ""


@tool
def report_list_styles() -> str:
    """列出报告可用的样式：内置预设（学术、商务、科技、清新、暖橙、医院红、典雅、极简、深色）和用户保存的样式。
    Word / PPT / HTML 三种报告通用：在 spec 顶层写 "style"。"""
    return list_styles_text() + (
        "\n\n自定义写法：\"style\": {\"preset\": \"business\", \"colors\": {\"primary\": \"#C8102E\"}, "
        "\"fonts\": {\"cn\": \"思源黑体\"}, \"table\": \"header_fill|three_line|grid\", \"cover\": \"classic|band\", "
        "\"font_scale\": 1.1, \"brand\": {\"label\": \"封面小标签\", \"footer\": \"页脚说明\"}}")


@tool
def report_preview_style(style: dict | str, runtime: ToolRuntime = None) -> str:
    """生成样式样张图（标题、正文、表格、图表配色），让用户在生成报告前确认观感。
    style 与 spec.style 写法相同：预设名字符串，或 {"preset": ..., "colors": {...}, "fonts": {...}, ...}。
    颜色组合不可读（对比度不足）时会报错，请按提示调整。"""
    if isinstance(style, str) and style.strip().startswith("{"):
        try:
            style = json.loads(style)
        except json.JSONDecodeError as e:
            return f"❌ style 不是合法 JSON：{e}"
    try:
        resolved, notes = resolve_style(style, "html-report", allow_dark=True)
    except ValueError as e:
        return f"❌ {e}"
    f, url = _preview_path(runtime, resolved["name"])
    try:
        render_preview(resolved, f)
    except Exception as e:  # noqa: BLE001
        return f"样式有效（{describe(resolved)}），但预览图生成失败：{e}"
    dark = luminance(resolved["colors"]["background"]) < 0.2
    lines = [f"✅ 样式有效：{describe(resolved)}"]
    if dark:
        lines.append("注意：深色背景只适用于 PPT / HTML，Word 报告不支持。")
    lines += notes
    if url:
        lines.append(f"预览图（在回复中用 Markdown 展示给用户确认）：![样式预览]({url})")
    return "\n".join(lines)


@tool
def report_save_style(name: str, style: dict | str) -> str:
    """把用户确认的样式保存为命名样式，之后三种报告都可以直接用 "style": "<name>"。
    name：中英文/数字/下划线/短横线，最多 32 字，不能与内置预设重名；同名会覆盖。"""
    if isinstance(style, str) and style.strip().startswith("{"):
        try:
            style = json.loads(style)
        except json.JSONDecodeError as e:
            return f"❌ style 不是合法 JSON：{e}"
    try:
        resolved = save_style(name, style)
    except ValueError as e:
        return f"❌ {e}"
    return f"✅ 已保存样式「{name}」：{describe(resolved)}。以后在 spec 中写 \"style\": \"{name}\" 即可复用。"


STYLE_TOOLS = [report_list_styles, report_preview_style, report_save_style]
