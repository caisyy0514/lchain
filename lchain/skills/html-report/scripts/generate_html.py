#!/usr/bin/env python3
"""Render a structured JSON spec or Markdown file into a self-contained HTML report.

Pure standard library: no external dependencies. Produces a single .html file
with inline CSS, a Chinese-friendly font stack, an optional table of contents,
print styles, and images inlined as base64 so the report is fully portable.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import re
import struct
import sys
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import unquote, urlsplit

CSS = """
:root{
  --fg:#263441; --heading:#19364d; --muted:#61717d; --border:#dce3e7; --rule:#b8c7d0;
  --bg:#ffffff; --alt:#f4f7f8; --accent:#007f7a; --accent-bg:#eaf5f3;
  --code:#263441; --code-bg:#f3f6f7; --quote:#3e5662; --quote-bg:#f7faf9;
  --chart-bg:#ffffff; --chart-ink:#465e6f; --chart-grid:#e2e8ec; --chart-axis:#8298a6;
  --series-1:#007f7a; --series-2:#0072b2; --series-3:#b1812f;
  --series-4:#876ba5; --series-5:#617e88; --series-6:#c66a3c;
}
@media (prefers-color-scheme: dark){
  :root{
    --fg:#dce5ec; --heading:#e4edf5; --muted:#a5b6c3; --border:#344753; --rule:#496171;
    --bg:#111b24; --alt:#172630; --accent:#76c9bf; --accent-bg:#193f40;
    --code:#dce5ec; --code-bg:#172630; --quote:#c0d5dd; --quote-bg:#152b31;
    --chart-bg:#111b24; --chart-ink:#c0d0dc; --chart-grid:#304552; --chart-axis:#7e9aa9;
    --series-1:#73c7b9; --series-2:#71b9e4; --series-3:#d1b56c;
    --series-4:#b49bd2; --series-5:#9ab8c2; --series-6:#e9a07a;
  }
}
*{box-sizing:border-box;}
html{-webkit-text-size-adjust:100%;}
body{
  font-family:"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Source Han Sans SC",
    "Noto Sans CJK SC","WenQuanYi Micro Hei",Helvetica,Arial,sans-serif;
  color:var(--fg); background:var(--bg); line-height:1.85; margin:0;
  font-size:16px; text-rendering:optimizeLegibility; font-kerning:normal;
}
.report{max-width:1064px; margin:0 auto; padding:64px 48px 88px; overflow-wrap:anywhere;}
header.cover{border-top:3px solid var(--heading); padding-top:22px; margin-bottom:26px;}
.cover-label{font-size:11px; font-weight:650; letter-spacing:.16em; color:var(--accent); margin-bottom:18px;}
header.cover h1{
  font-family:"Songti SC","STSong","Noto Serif CJK SC","Source Han Serif SC",Georgia,serif;
  color:var(--heading); font-weight:700; font-size:40px; letter-spacing:.015em; line-height:1.4; margin:0 0 14px;
}
header.cover .subtitle{font-size:17px; color:var(--muted); line-height:1.85; max-width:50em; margin:0 0 20px;}
header.cover .meta{display:flex; gap:6px 24px; flex-wrap:wrap; font-size:12px; color:var(--muted);
  border-top:1px solid var(--border); padding-top:14px;}
header.cover .meta div{min-width:0;}
.meta-key{color:var(--heading); margin-right:6px; font-weight:550;}
h1,h2,h3,h4,h5,h6{color:var(--heading); line-height:1.5; scroll-margin-top:24px; font-weight:650;}
h1{font-size:27px; margin:44px 0 20px; padding-bottom:10px; border-bottom:1px solid var(--rule);}
h2{font-size:23px; margin:34px 0 16px;}
h3{font-size:19px; margin:28px 0 12px;}
h4,h5,h6{font-size:17px; margin:24px 0 10px;}
header.cover h1{padding:0; border:0;}
p{margin:0 0 18px;}
ul,ol{margin:0 0 18px; padding-left:1.6em;}
li{margin:6px 0;}
li::marker{color:var(--muted);}
li > ul,li > ol{margin:4px 0;}
a{color:var(--accent); text-decoration:none; text-underline-offset:.18em;}
a:hover{text-decoration:underline;}
a:focus-visible,.table-wrap:focus-visible,.chart-scroll:focus-visible{outline:2px solid var(--accent); outline-offset:4px;}
strong{font-weight:650; color:var(--heading);}
code{font-family:"SF Mono",Menlo,Consolas,"PingFang SC",monospace;
  font-size:.9em; color:var(--code); background:var(--code-bg); padding:2px 5px; border-radius:2px;}
pre{background:var(--code-bg); padding:16px 18px; overflow-x:auto; border-left:2px solid var(--rule);}
pre code{background:none; padding:0; font-size:.88em;}
blockquote{margin:24px 0; padding:10px 0 10px 20px; color:var(--quote);
  border-left:2px solid var(--accent);}
blockquote p:last-child{margin-bottom:0;}
hr{border:0; border-top:1px solid var(--rule); margin:32px 0;}
table{width:100%; border-collapse:collapse; margin:0; font-size:14px; line-height:1.75;
  border-top:1.5px solid var(--heading); border-bottom:1.5px solid var(--heading); font-variant-numeric:tabular-nums;}
caption{text-align:left; color:var(--heading); font-size:14px; font-weight:600;
  padding:0 0 11px; caption-side:top; line-height:1.65;}
.table-wrap{min-width:0; overflow-x:auto; margin:0 0 14px;}
.table-wrap table{min-width:360px;}
th,td{border:0; border-bottom:1px solid var(--border); padding:11px 14px; text-align:left; vertical-align:top;}
th{color:var(--heading); border-bottom:1px solid var(--heading); font-weight:650; background:transparent;}
tbody tr:last-child td{border-bottom:0;}
th:first-child,td:first-child{padding-left:0;}
th:last-child,td:last-child{padding-right:0;}
th.num,td.num{text-align:right; font-variant-numeric:tabular-nums lining-nums;}
figure{margin:8px 0 24px; text-align:left;}
figure img{display:block; width:auto; max-width:100%; max-height:640px; height:auto; object-fit:contain;
  margin:0 auto; border:1px solid var(--border); background:#fff;}
figcaption{font-size:13px; color:var(--muted); margin-top:13px; line-height:1.65; text-align:left;}
.caption-title{display:block; font-weight:600; font-size:14px; color:var(--heading);}
.caption-meta{display:block; margin-top:4px;}
.source,.block-note{font-size:12px; color:var(--muted); text-align:left; margin:5px 0 20px; line-height:1.7;}
figcaption .source{display:inline; text-align:inherit; margin:0;}
.citations{font-size:.8em; vertical-align:super; white-space:normal; margin-left:.25em; line-height:1;}
.citations a{white-space:nowrap; margin-right:.25em;}
.caption-meta .citations,.block-note .citations{font-size:.95em; vertical-align:baseline;}
.columns{display:grid; grid-template-columns:minmax(0,var(--left,1fr)) minmax(0,var(--right,1fr));
  gap:36px; align-items:start; margin:22px 0 28px;}
.column{min-width:0;}
.column>:first-child{margin-top:0;}
.column>:last-child{margin-bottom:0;}
.chart-scroll{overflow-x:auto; max-width:100%; background:var(--chart-bg);}
.chart-scroll-hint{display:none; font-size:11px; color:var(--muted); text-align:left; margin-bottom:7px;}
.column .chart-scroll-hint{display:block;}
.chart-svg{display:block; width:100%; min-width:560px; height:auto;
  font-family:"Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",sans-serif; font-variant-numeric:tabular-nums;}
.chart-legend{display:flex; gap:7px 24px; flex-wrap:wrap; list-style:none; padding:0; margin:10px 0 14px;
  text-align:left; font-size:13px; line-height:1.6; print-color-adjust:exact; -webkit-print-color-adjust:exact;}
.chart-legend li{display:flex; align-items:center; gap:8px; min-width:0; margin:0;}
.chart-legend .swatch{width:22px; height:9px; flex-shrink:0;}
.chart-legend .swatch-line{width:36px; height:3px; position:relative;}
.swatch-line::after{content:""; position:absolute; left:15px; top:-2px; width:7px; height:7px;
  border-radius:50%; background:var(--legend-color);}
.legend-label{min-width:0;}
.chart-data{font-size:13px; text-align:left; margin-top:16px; margin-bottom:28px;}
.chart-data table{font-size:13px;}
.chart-data caption{font-size:12px; font-weight:400; color:var(--muted);}
.references{margin-top:42px; border-top:1px solid var(--rule); padding-top:20px; font-size:13px; line-height:1.75;}
.references h2{font-size:20px; margin:0 0 18px;}
.references ol{padding-left:2.1em;}
.references li{margin:0 0 13px; padding-left:.25em; scroll-margin-top:20px;}
.references li::marker{color:var(--muted); font-variant-numeric:tabular-nums;}
.references li:target{background:var(--accent-bg);}
.reference-link{margin-left:.4em; font-size:.95em;}
.toc{display:grid; grid-template-columns:100px minmax(0,1fr); gap:12px 22px;
  border-top:1px solid var(--border); border-bottom:1px solid var(--border);
  padding:18px 0; margin:26px 0 36px;}
.toc h2{font-size:12px; margin:2px 0 0; padding:0; letter-spacing:.12em; color:var(--muted); font-weight:500;}
.toc ol{display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:6px 24px;
  list-style:none; padding:0; margin:0; counter-reset:toc;}
.toc li{margin:0; counter-increment:toc; font-size:13px; line-height:1.75;}
.toc a{display:flex; gap:12px; color:var(--heading);}
.toc a::before{content:counter(toc,decimal-leading-zero); flex-shrink:0;
  font-size:10px; color:var(--accent); font-variant-numeric:tabular-nums; padding-top:3px;}
.toc .numbered a::before{display:none;}
.toc .lvl-2{padding-left:.7em;}
.toc .lvl-3{padding-left:1.4em;}
.page-break{break-after:page; height:0;}
footer.report-foot{margin-top:40px; padding-top:16px; border-top:1px solid var(--border);
  font-size:11px; color:var(--muted); line-height:1.7;}
@media (max-width:700px){
  .report{padding:28px 20px 52px;}
  header.cover{padding-top:16px;}
  header.cover h1{font-size:29px;}
  header.cover .subtitle{font-size:15px;}
  h1{font-size:24px; margin-top:34px;}
  h2{font-size:21px;}
  .toc{grid-template-columns:minmax(0,1fr); gap:10px;}
  .toc ol{grid-template-columns:minmax(0,1fr);}
  .columns{grid-template-columns:minmax(0,1fr); gap:22px; margin:18px 0 24px;}
  .chart-scroll-hint{display:block;}
  th,td{padding:9px 10px;}
}
@media print{
  :root{--fg:#192832; --heading:#19364d; --muted:#52626d; --border:#d5dce1; --rule:#a8bac7;
    --bg:#fff; --alt:#fff; --accent:#006963; --code-bg:#f4f6f7;
    --chart-bg:#fff; --chart-ink:#465e6f; --chart-grid:#dce4e9; --chart-axis:#8298a6;
    --series-1:#007f7a; --series-2:#0072b2; --series-3:#b1812f;
    --series-4:#876ba5; --series-5:#617e88; --series-6:#c66a3c;}
  @page{size:A4; margin:16mm;}
  .report{max-width:none; padding:0;}
  body{font-size:10.5pt; line-height:1.75; background:#fff; color:var(--fg);}
  header.cover{padding-top:15px; margin-bottom:18px;}
  header.cover h1{font-size:25pt;}
  header.cover .subtitle{font-size:11pt; margin-bottom:12px;}
  .cover-label{font-size:8pt; margin-bottom:12px;}
  h1{font-size:18pt; margin:26px 0 14px;}
  h2{font-size:15pt; margin:24px 0 12px;}
  h3{font-size:12pt;}
  .toc{margin:20px 0 24px; padding:12px 0;}
  .columns{display:block; margin:16px 0 20px;}
  .column+.column{margin-top:14px;}
  .chart-scroll-hint,.column .chart-scroll-hint{display:none;}
  .toc,figure,.chart-scroll{break-inside:avoid;}
  figure{display:inline-block; width:100%; vertical-align:top; page-break-inside:avoid; margin:4px 0 18px;}
  figcaption{break-before:avoid; break-after:auto; margin-top:10px;}
  .caption-title{font-size:10pt;}
  .caption-meta,.source,.block-note{font-size:8.5pt;}
  figure img{max-height:165mm; max-width:100%;}
  .column figure img{max-height:95mm;}
  .chart-scroll,.table-wrap,pre{overflow:visible;}
  .chart-svg,.table-wrap table{min-width:0; width:100%;}
  .chart-legend{font-size:9pt; margin:8px 0 10px;}
  table,.chart-data table{table-layout:fixed; font-size:9pt;}
  th,td{padding:6px 9px; overflow-wrap:anywhere;}
  thead{display:table-header-group;}
  tr{break-inside:avoid;}
  caption,h1,h2,h3,h4,h5,h6{break-after:avoid;}
  p,li{orphans:3; widows:3;}
  pre{white-space:pre-wrap; overflow-wrap:anywhere;}
  a{color:var(--accent); text-decoration:underline;}
  .toc a{text-decoration:none;}
  .references.compact{break-inside:avoid;}
  .references li{break-inside:avoid;}
}
""".strip()


# [LChain] 自定义样式：spec._style 为 app/agent/report_styles.py 解析后的完整样式（颜色为 6 位 HEX，无 #）。
#   没有 _style 时输出与 medex 原版完全一致。
_CSS_FONT_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff \-]{0,39}$")
_HEX_RE = re.compile(r"^[0-9A-Fa-f]{6}$")


def _style_color(style: dict, key: str) -> str:
    value = str((style.get("colors") or {}).get(key, ""))
    if not _HEX_RE.match(value):
        raise ValueError(f"style.colors.{key} 无效: {value!r}")
    return "#" + value.lower()


def _mix(a: str, b: str, t: float) -> str:
    ra, ga, ba = (int(a[i:i + 2], 16) for i in (1, 3, 5))
    rb, gb, bb = (int(b[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in ((ra, rb), (ga, gb), (ba, bb)))


def _font_stack(*names: str) -> str:
    fonts = [f'"{n}"' for n in names if n and _CSS_FONT_RE.match(n)]
    return ",".join(fonts)


def style_css(style: dict | None) -> str:
    """把解析后的样式转成追加在默认 CSS 之后的覆盖规则（CSS 变量 + 少量结构规则）。"""
    if not style:
        return ""
    col = {k: _style_color(style, k) for k in ("primary", "accent", "text", "heading", "muted", "border",
                                                "alt", "highlight", "background", "on_primary")}
    series = [("#" + str(c).lower()) for c in (style.get("colors") or {}).get("chart", []) if _HEX_RE.match(str(c))]
    fonts = style.get("fonts") or {}
    base = ('"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Source Han Sans SC","Noto Sans CJK SC",'
            '"WenQuanYi Micro Hei",Helvetica,Arial,sans-serif')
    body_stack = ",".join(x for x in (_font_stack(fonts.get("en", ""), fonts.get("cn", "")), base) if x)
    head_stack = ",".join(x for x in (_font_stack(fonts.get("en", ""), fonts.get("heading_cn", "")), base) if x)
    scale = float(style.get("font_scale") or 1.0)
    variables = {
        "--fg": col["text"], "--heading": col["heading"], "--muted": col["muted"], "--border": col["border"],
        "--rule": _mix(col["border"], col["heading"], 0.25), "--bg": col["background"], "--alt": col["alt"],
        "--accent": col["accent"], "--accent-bg": _mix(col["accent"], col["background"], 0.88),
        "--code": col["text"], "--code-bg": col["alt"], "--quote": _mix(col["text"], col["muted"], 0.5),
        "--quote-bg": col["alt"], "--chart-bg": col["background"], "--chart-ink": _mix(col["text"], col["muted"], 0.5),
        "--chart-grid": col["border"], "--chart-axis": col["muted"], "--primary": col["primary"],
        "--on-primary": col["on_primary"], "--highlight": col["highlight"],
        "--primary-text": "#" + str((style.get("colors") or {}).get("primary_text") or col["primary"][1:]).lower(),
    }
    variables.update({f"--series-{i + 1}": c for i, c in enumerate(series[:6])})
    decl = ";".join(f"{k}:{v}" for k, v in variables.items())
    rules = [
        # 用户指定了样式：不再跟随系统深色模式切换（否则用户选的颜色会被覆盖）
        f":root{{{decl}}}",
        f"@media (prefers-color-scheme: dark){{:root{{{decl}}}}}",
        f"@media print{{:root{{{decl}}}}}",
        f"body{{font-family:{body_stack}; font-size:{16 * scale:.1f}px;}}",
        f"h1,h2,h3,h4,h5,h6,header.cover h1{{font-family:{head_stack};}}",
        "h1{border-bottom-color:var(--primary-text);} h1,h2{color:var(--primary-text);}",
        "header.cover{border-top-color:var(--primary);}",
        ".cover-rule{width:96px; height:3px; background:var(--highlight); margin:0 0 18px;}",
    ]
    if style.get("cover") == "band":
        rules.append("header.cover{border-top:0; background:var(--primary); color:var(--on-primary);"
                     " padding:40px 44px 34px; margin:-64px -48px 34px; }"
                     "header.cover h1,header.cover .subtitle,header.cover .meta,header.cover .meta-key,"
                     "header.cover .cover-label{color:var(--on-primary);}"
                     "header.cover .subtitle,header.cover .meta{opacity:.88;}"
                     "@media (max-width:700px){header.cover{margin:-28px -20px 26px; padding:26px 22px;}}"
                     "@media print{header.cover{margin:0 0 24px; print-color-adjust:exact; -webkit-print-color-adjust:exact;}}")
    table = style.get("table", "auto")
    if table == "header_fill":
        rules.append("table{border-top:0; border-bottom:2px solid var(--primary);}"
                     "th{background:var(--primary); color:var(--on-primary); border-bottom:0;"
                     " print-color-adjust:exact; -webkit-print-color-adjust:exact;}"
                     "th:first-child,td:first-child{padding-left:14px;} th:last-child,td:last-child{padding-right:14px;}"
                     "tbody tr:nth-child(even) td{background:var(--alt);}")
    elif table == "grid":
        rules.append("table{border:1px solid var(--border);} th,td{border:1px solid var(--border);}"
                     "th{background:var(--alt);} th:first-child,td:first-child{padding-left:14px;}"
                     " th:last-child,td:last-child{padding-right:14px;} tbody tr:last-child td{border-bottom:1px solid var(--border);}")
    return "\n".join(rules)


def safe_url(value: str, *, reference: bool = False) -> str:
    """Allow web/anchor links, never executable or external-resource schemes."""
    url = str(value).strip()
    if not url or re.search(r"[\x00-\x20\x7f\\]", url):
        raise ValueError(f"链接为空或含无效字符: {value!r}")
    parts = urlsplit(url)
    if parts.scheme.lower() in {"http", "https"} and parts.hostname:
        return url
    if not reference and url.startswith("#") and len(url) > 1:
        return url
    raise ValueError(f"链接仅支持 http/https{' 或 #锚点' if not reference else ''}: {url}")


def slugify(text: str) -> str:
    return re.sub(r"[^\w一-鿿-]+", "-", str(text).strip()).strip("-").lower() or "section"


def finite_number(value) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def build_toc(headings: list[tuple]) -> str:
    items = []
    for heading in headings:
        level, text = heading[:2]
        anchor = heading[2] if len(heading) > 2 else slugify(text)
        numbered = " numbered" if re.match(r"^(?:\d+(?:\.\d+)*[.、)]?|第[一二三四五六七八九十百\d]+[章节])\s+", text) else ""
        items.append(f'<li class="lvl-{min(level, 3)}{numbered}"><a href="#{escape(anchor)}">{escape(text)}</a></li>')
    return '<nav class="toc" aria-label="报告目录"><h2>内容导览</h2><ol>' + "".join(items) + "</ol></nav>"


class RenderContext:
    """Own references, anchors, and relative assets for one report."""

    def __init__(self, references=None, base_dir: Path | None = None):
        self.base_dir = base_dir or Path.cwd()
        self.references: list[dict] = []
        self.ref_numbers: dict[str, int] = {}
        self.headings: list[tuple[int, str, str]] = []
        self.heading_counts: dict[str, int] = {}
        self.figure_count = 0
        self.chart_count = 0
        self._images: dict[Path, str] = {}
        identities, definitions = {}, {}
        if references is None:
            references = []
        if not isinstance(references, list):
            raise ValueError("references 必须是数组")
        for raw in references:
            if not isinstance(raw, dict):
                raise ValueError("每条参考文献必须是对象")
            ref = dict(raw)
            rid, title = ref.get("id"), ref.get("title")
            if not isinstance(rid, str) or not rid.strip() or not isinstance(title, str) or not title.strip():
                raise ValueError("参考文献必须有非空 id 和 title")
            ref["id"], ref["title"] = rid.strip(), title.strip()
            rid = ref["id"]
            authors = ref.get("authors", "")
            if isinstance(authors, list) and all(isinstance(a, str) for a in authors):
                authors = ", ".join(authors)
            if not isinstance(authors, str):
                raise ValueError(f"参考文献 {rid}: authors 须为字符串或字符串数组")
            ref["authors"] = authors.strip()
            for field in ("year", "source", "url", "doi"):
                value = ref.get(field) or ""
                if not isinstance(value, (str, int)) or isinstance(value, bool):
                    raise ValueError(f"参考文献 {rid}: {field} 须为文本")
                ref[field] = str(value).strip()
            if ref["url"]:
                ref["url"] = safe_url(ref["url"], reference=True)
            if ref["doi"]:
                ref["doi"] = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", ref["doi"], flags=re.I)
                if not re.fullmatch(r'10\.\d{4,9}/[^\s<>"]+', ref["doi"], re.I):
                    raise ValueError(f"参考文献 {rid}: DOI 格式无效")
            if rid in definitions and definitions[rid] != ref:
                raise ValueError(f"参考文献 id 冲突: {rid}")
            definitions[rid] = dict(ref)
            keys = []
            if ref["doi"]:
                keys.append(("doi", ref["doi"].lower()))
            if ref["url"]:
                keys.append(("url", ref["url"].rstrip("/")))
                url_doi = re.match(r"https?://(?:dx\.)?doi\.org/(.+)$", ref["url"], re.I)
                if url_doi:
                    keys.append(("doi", unquote(url_doi.group(1)).lower()))
            if not keys:
                keys.append(("text", ref["title"].casefold(), ref["authors"].casefold(), ref["year"]))
            matches = {identities[key] for key in keys if key in identities}
            if len(matches) > 1:
                raise ValueError(f"参考文献标识冲突（DOI/URL 指向不同条目）: {rid}")
            if not matches:
                self.references.append(dict(ref))
                number = len(self.references)
            else:
                number = matches.pop()
                canonical = self.references[number - 1]
                if canonical["doi"] and ref["doi"] and canonical["doi"].lower() != ref["doi"].lower():
                    raise ValueError(f"参考文献 DOI 冲突: {rid}")
                for field in ("authors", "source", "year", "doi", "url"):
                    if not canonical[field]:
                        canonical[field] = ref[field]
            for key in keys:
                identities[key] = number
            self.ref_numbers[rid] = number

    def cite(self, ids) -> str:
        if ids is None:
            return ""
        if not isinstance(ids, list) or any(not isinstance(r, str) for r in ids):
            raise ValueError("citations 必须是参考文献 id 字符串数组")
        numbers = []
        for rid in ids:
            if rid not in self.ref_numbers:
                raise ValueError(f"未知参考文献 id: {rid}")
            if self.ref_numbers[rid] not in numbers:
                numbers.append(self.ref_numbers[rid])
        return ('<span class="citations">' + "".join(
            f'<a href="#ref-{n}" role="doc-biblioref" aria-label="参考文献 {n}">[{n}]</a>'
            for n in numbers) + "</span>") if numbers else ""

    def heading(self, level: int, text: str) -> str:
        key = "section-" + slugify(text)
        count = self.heading_counts.get(key, 0) + 1
        self.heading_counts[key] = count
        anchor = key if count == 1 else f"{key}-{count}"
        self.headings.append((level, text, anchor))
        return anchor

    def image(self, path: str) -> str:
        if not isinstance(path, str) or not path.strip() or urlsplit(path).scheme or path.startswith("//"):
            raise ValueError("image.path 必须是本地图片；远程资源须先保存到本地")
        asset = (self.base_dir / Path(path).expanduser()).resolve()
        if asset not in self._images:
            self._images[asset] = inline_image(str(asset))
        self.figure_count += 1
        return self._images[asset]

    def bibliography(self) -> str:
        if not self.references:
            return ""
        items = []
        for number, ref in enumerate(self.references, 1):
            text = escape(". ".join(ref[k] for k in ("authors", "title", "source", "year") if ref.get(k)))
            if ref["doi"]:
                text += f' <a class="reference-link" href="{escape("https://doi.org/" + ref["doi"])}">DOI: {escape(ref["doi"])}</a>'
            if ref["url"]:
                text += f' <a class="reference-link" href="{escape(ref["url"])}">{escape(ref["url"])}</a>'
            items.append(f'<li id="ref-{number}">{text}</li>')
        compact = " compact" if len(self.references) <= 4 else ""
        return f'<section class="references{compact}" role="doc-bibliography"><h2>参考文献</h2><ol>' + "".join(items) + "</ol></section>"


def inline_image(path: str) -> str:
    """Embed image bytes; malformed/missing assets fail before delivery."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"图片不存在: {p}")
    if p.stat().st_size > 30 * 1024 * 1024:
        raise ValueError(f"图片超过 30 MiB，请保留清晰度后压缩: {p}")
    data, mime = p.read_bytes(), ""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        offset, chunks = 8, []
        while offset + 12 <= len(data):
            length = struct.unpack(">I", data[offset:offset + 4])[0]
            chunk = data[offset + 4:offset + 8 + length]
            end = offset + 12 + length
            if end > len(data) or zlib.crc32(chunk) & 0xffffffff != struct.unpack(">I", data[end - 4:end])[0]:
                raise ValueError(f"PNG 数据损坏: {p}")
            chunks.append(chunk[:4])
            offset = end
        if chunks and chunks[0] == b"IHDR" and b"IDAT" in chunks and chunks[-1] == b"IEND" and offset == len(data):
            mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff") and data.rstrip().endswith(b"\xff\xd9"):
        mime = "image/jpeg"
    elif data[:6] in (b"GIF87a", b"GIF89a") and len(data) > 13 and data.endswith(b";"):
        mime = "image/gif"
    elif len(data) > 20 and data[:4] == b"RIFF" and data[8:12] == b"WEBP" and struct.unpack("<I", data[4:8])[0] + 8 == len(data):
        mime = "image/webp"
    elif data.lstrip().startswith((b"<svg", b"<?xml")):
        try:
            root = ET.fromstring(data)
        except ET.ParseError as exc:
            raise ValueError(f"SVG 无法解析: {p}") from exc
        allowed = {"svg", "g", "defs", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
                   "text", "tspan", "title", "desc", "linearGradient", "radialGradient", "stop", "clipPath", "marker", "pattern", "use"}
        if root.tag.split("}")[-1] != "svg":
            raise ValueError(f"图片内容不是 SVG: {p}")
        for element in root.iter():
            if element.tag.split("}")[-1] not in allowed:
                raise ValueError(f"SVG 含不支持的元素，请转为本地 PNG: {p}")
            for key, value in element.attrib.items():
                local = key.split("}")[-1].lower()
                external_urls = [u.strip(" '\"") for u in re.findall(r"url\(([^)]*)\)", value, flags=re.I)]
                if local.startswith("on") or (local == "href" and not value.startswith("#")) \
                        or any(not u.startswith("#") for u in external_urls) or "@import" in value.lower():
                    raise ValueError(f"SVG 含脚本或外部资源: {p}")
        mime = "image/svg+xml"
    if not mime:
        raise ValueError(f"图片格式无效或不支持（PNG/JPEG/GIF/WebP/自包含 SVG）: {p}")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


_INLINE = re.compile(r"\`([^\`]+)\`|!\[([^\]]*)\]\(([^)]+)\)|\[@([^\]]+)\]|\[([^\]]+)\]\(([^)]+)\)|\*\*([^*]+)\*\*|\*([^*]+)\*")


def image_target(value: str) -> str:
    value = re.sub(r'\s+["\'][^"\']*["\']\s*$', "", value.strip())
    return unquote(value[1:-1] if value.startswith("<") and value.endswith(">") else value)


def render_inline(text: str, context: RenderContext | None = None) -> str:
    """Escaped Markdown with local images, safe links and [@id] citations."""
    context = context or RenderContext()
    text, output, start = str(text), [], 0
    for match in _INLINE.finditer(text):
        output.append(escape(text[start:match.start()]))
        code, alt, path, citation, label, url, bold, italic = match.groups()
        if code is not None:
            output.append(f"<code>{escape(code)}</code>")
        elif path is not None:
            src = context.image(image_target(path))
            output.append(f'<img class="inline-image" src="{src}" alt="{escape(alt)}" style="max-width:100%;height:auto;object-fit:contain"/>')
        elif citation is not None:
            output.append(context.cite([rid.strip().lstrip("@") for rid in citation.split(";")]))
        elif url is not None:
            output.append(f'<a href="{escape(safe_url(url))}">{escape(label)}</a>')
        elif bold is not None:
            output.append(f"<strong>{render_inline(bold, context)}</strong>")
        else:
            output.append(f"<em>{render_inline(italic, context)}</em>")
        start = match.end()
    output.append(escape(text[start:]))
    return "".join(output)


def block_note(block: dict, context: RenderContext, *, caption: bool = False) -> str:
    parts = []
    title = ""
    if caption and (block.get("caption") or block.get("title")):
        title = '<span class="caption-title">' + render_inline(block.get("caption") or block["title"], context) + "</span>"
    if block.get("source"):
        parts.append('<span class="source">来源：' + render_inline(block["source"], context) + "</span>")
    citation = context.cite(block.get("citations"))
    if citation:
        parts.append(citation)
    metadata = " ".join(parts)
    if caption:
        return title + (f'<span class="caption-meta">{metadata}</span>' if metadata else "")
    return metadata


def numeric_table_columns(rows: list[list], count: int) -> list[bool]:
    """Align a complete quantitative column without changing its displayed values."""
    missing = {"", "-", "–", "—", "缺测", "未测", "NA", "N/A"}
    numeric = re.compile(r"[+−-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?:\s*(?:%|％|[A-Za-zµμ][A-Za-z0-9µμ/·^²³%-]*))?")
    result = []
    for col in range(count):
        values = [row[col] for row in rows if str(row[col]).strip() not in missing]
        result.append(bool(values) and all(finite_number(value) or bool(numeric.fullmatch(str(value).strip())) for value in values))
    return result


def render_table(block: dict, context: RenderContext, *, css_class: str = "") -> str:
    headers, rows = block.get("headers", []), block.get("rows", [])
    if not isinstance(headers, list) or not isinstance(rows, list) or any(not isinstance(r, list) for r in rows):
        raise ValueError("table.headers 和 table.rows 必须是数组")
    count = len(headers) or (len(rows[0]) if rows else 0)
    if not count or any(len(row) != count for row in rows):
        raise ValueError("表格须至少一列，每行单元格数量须与表头一致")
    numeric = numeric_table_columns(rows, count)
    title = f'<caption>{render_inline(block["caption"], context)}</caption>' if block.get("caption") else ""
    thead = ("<thead><tr>" + "".join(f'<th scope="col"' + (' class="num"' if numeric[i] else '') + f'>{render_inline(h, context)}</th>' for i, h in enumerate(headers)) + "</tr></thead>") if headers else ""
    tbody = "".join("<tr>" + "".join("<td" + (' class="num"' if numeric[i] else '') + f">{render_inline(c, context)}</td>" for i, c in enumerate(row)) + "</tr>" for row in rows)
    note = block_note(block, context)
    result = f'<div class="table-wrap {css_class}" tabindex="0" role="region" aria-label="{escape(str(block.get("caption") or "数据表"))}"><table>{title}{thead}<tbody>{tbody}</tbody></table></div>'
    return result + (f'<div class="block-note">{note}</div>' if note else "")


def render_chart(block: dict, context: RenderContext) -> str:
    chart_type = block.get("chart_type", "bar")
    categories, series = block.get("categories"), block.get("series")
    if chart_type not in ("bar", "line"):
        raise ValueError("chart_type 仅支持 bar 或 line")
    if not isinstance(categories, list) or not 1 <= len(categories) <= 12 or any(not isinstance(c, str) for c in categories):
        raise ValueError("图表须包含 1–12 个字符串类别，更多类别请拆分图表")
    if any(not c.strip() or len(c) > 36 for c in categories):
        raise ValueError("图表类别名须为 1–36 字符；请使用简短标签并在正文解释")
    if len({c.strip() for c in categories}) != len(categories):
        raise ValueError("图表 categories 不可重复")
    if chart_type == "line" and len(categories) < 2:
        raise ValueError("折线图至少需要两个类别")
    if not isinstance(block.get("unit"), str) or not block["unit"].strip():
        raise ValueError("图表须提供 unit；无量纲指标请写比值、人数等实际单位")
    if not (isinstance(block.get("source"), str) and block["source"].strip()) and not block.get("citations"):
        raise ValueError("图表须提供 source 或 citations，说明数据来源")
    if not isinstance(series, list) or not 1 <= len(series) <= 6:
        raise ValueError("图表须包含 1–6 个系列，更多系列请拆分图表")
    numbers = []
    names = set()
    for entry in series:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"].strip():
            raise ValueError("每个图表系列须有非空 name")
        if entry["name"].strip() in names:
            raise ValueError("图表系列 name 不可重复")
        names.add(entry["name"].strip())
        values = entry.get("values")
        if not isinstance(values, list) or len(values) != len(categories):
            raise ValueError("图表每个 series.values 长度必须与 categories 一致")
        if any(v is not None and not finite_number(v) for v in values):
            raise ValueError("图表数值必须是有限数字或 null（缺测），不可使用字符串/NaN/Infinity")
        if all(v is None for v in values):
            raise ValueError("每个图表系列至少需要一个实际数值")
        numbers.extend(v for v in values if v is not None)
    if not numbers:
        raise ValueError("图表至少需要一个实际数值，不能把全部缺测值绘制为零")
    low, high = min(numbers), max(numbers)
    if chart_type == "bar":
        low, high = min(low, 0), max(high, 0)
    if low == high:
        delta = abs(low) * .1 or 1
        low, high = low - delta, high + delta
        if chart_type == "bar" and min(numbers) >= 0:
            low = 0
    span = high - low
    if not math.isfinite(span) or span == 0:
        raise ValueError("图表数值范围超出可绘制精度，请换算单位")
    if high > 0:
        high += span * .08
    if low < 0:
        low -= span * .08
    if not math.isfinite(high - low) or (high - low) / 4 == 0:
        raise ValueError("图表数值范围超出可绘制精度，请换算单位")
    # Nice ticks avoid implying arbitrary precision in the measurements.
    rough = (high - low) / 4
    magnitude = 10 ** math.floor(math.log10(rough))
    if magnitude == 0:
        raise ValueError("图表数值过小，请换算单位")
    tick_step = next(factor for factor in (1, 2, 2.5, 5, 10) if factor >= rough / magnitude) * magnitude
    low = math.floor(low / tick_step) * tick_step
    high = math.ceil(high / tick_step) * tick_step
    if not math.isfinite(high - low):
        raise ValueError("图表坐标轴范围超出可绘制精度，请换算单位")
    width, left, right, top, plot_h = 760, 86, 24, 42, 258
    plot_w = width - left - right
    step_w = plot_w / len(categories)
    chars = max(3, min(9, int(step_w / 16)))
    label_lines = max(math.ceil(len(category) / chars) for category in categories)
    if label_lines > 4:
        raise ValueError("图表类别标签过长，请缩短标签或拆分图表")
    height = top + plot_h + 31 + label_lines * 20
    def y(value):
        return top + (high - value) / (high - low) * plot_h
    context.chart_count += 1
    ident = f"chart-{context.chart_count}"
    title = str(block.get("caption") or block.get("title") or ("数值对比" if chart_type == "bar" else "指标趋势"))
    description = str(block.get("alt") or f"{title}。{len(categories)} 个类别，{len(series)} 个系列。完整数值见下方数据表，缺测显示为缺测。")
    svg = [f'<svg class="chart-svg" viewBox="0 0 {width} {height}" role="img" aria-labelledby="{ident}-title {ident}-desc" xmlns="http://www.w3.org/2000/svg">',
           f'<title id="{ident}-title">{escape(title)}</title><desc id="{ident}-desc">{escape(description)}</desc>',
           '<rect width="100%" height="100%" fill="var(--chart-bg)"/>']
    for step in range(round((high - low) / tick_step) + 1):
        value = low + tick_step * step
        if abs(value) < tick_step * 1e-10:
            value = 0
        pos = y(value)
        svg.append(f'<line x1="{left}" x2="{width-right}" y1="{pos:.2f}" y2="{pos:.2f}" stroke="var(--chart-grid)"/>')
        svg.append(f'<text x="{left-12}" y="{pos+5:.2f}" text-anchor="end" font-size="16" fill="var(--chart-ink)">{value:.4g}</text>')
    if low <= 0 <= high:
        zero = y(0)
        svg.append(f'<line class="zero-baseline" x1="{left}" x2="{width-right}" y1="{zero:.2f}" y2="{zero:.2f}" stroke="var(--chart-axis)" stroke-width="1.25"/>')
    unit = str(block.get("unit") or "")
    if len(unit) > 40:
        raise ValueError("图表 unit 超过 40 字符，请把解释移至图注")
    svg.append(f'<text x="{left}" y="21" font-size="16" fill="var(--chart-ink)">{escape(unit)}</text>')
    xs = [left + step_w * (index + .5) for index in range(len(categories))]
    for index, category in enumerate(categories):
        for row, start in enumerate(range(0, len(category), chars)):
            # Bounded labels are wrapped; the data table retains their complete text.
            label = category[start:start+chars]
            svg.append(f'<text x="{xs[index]:.2f}" y="{top+plot_h+27+row*20}" text-anchor="middle" font-size="16" fill="var(--chart-ink)">{escape(label)}</text>')
    palette = [f"var(--series-{index+1})" for index in range(6)]
    dashes = ["", "7 4", "3 3", "9 3 2 3", "12 4", "2 4"]
    for index, entry in enumerate(series):
        color = palette[index]
        if chart_type == "bar":
            group_width = min(step_w * .68, len(series) * 48 + max(0, len(series)-1) * 9)
            bar_w = group_width / len(series)
            for category_index, value in enumerate(entry["values"]):
                if value is None:
                    continue
                x = xs[category_index] - group_width / 2 + (index + .065) * bar_w
                ypos, zero = y(value), y(0)
                svg.append(f'<rect x="{x:.2f}" y="{min(ypos,zero):.2f}" width="{bar_w*.87:.2f}" height="{abs(ypos-zero):.2f}" fill="{color}"><title>{escape(entry["name"])} · {escape(categories[category_index])}: {value:g} {escape(unit)}</title></rect>')
        else:
            segment, segments = [], []
            for category_index, value in enumerate(entry["values"]):
                if value is None:
                    if segment:
                        segments.append(segment)
                        segment = []
                else:
                    segment.append((xs[category_index], y(value)))
            if segment:
                segments.append(segment)
            for segment in segments:
                if len(segment) > 1:
                    points = " ".join(f"{x:.2f},{pos:.2f}" for x, pos in segment)
                    svg.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="3" stroke-dasharray="{dashes[index]}"/>')
            for category_index, value in enumerate(entry["values"]):
                if value is not None:
                    svg.append(f'<circle cx="{xs[category_index]:.2f}" cy="{y(value):.2f}" r="4" fill="{color}"><title>{escape(entry["name"])} · {escape(categories[category_index])}: {value:g} {escape(unit)}</title></circle>')
    svg.append("</svg>")
    legend_items = []
    for index, entry in enumerate(series):
        color, background = palette[index], palette[index]
        if chart_type == "line" and dashes[index]:
            stops, cursor = [], 0
            for segment, length in enumerate(map(int, dashes[index].split())):
                shade = color if segment % 2 == 0 else "transparent"
                stops.append(f"{shade} {cursor}px {cursor+length}px")
                cursor += length
            background = "repeating-linear-gradient(90deg," + ",".join(stops) + ")"
        marker_class = "swatch swatch-line" if chart_type == "line" else "swatch"
        legend_items.append(f'<li><span class="{marker_class}" aria-hidden="true" style="--legend-color:{color};background:{background}"></span><span class="legend-label">{escape(entry["name"])}</span></li>')
    legend = '<ul class="chart-legend" aria-label="数据系列">' + "".join(legend_items) + "</ul>"
    data = {"headers": ["类别"] + [entry["name"] + (f"（{unit}）" if unit else "") for entry in series],
            "rows": [[category] + [("缺测" if entry["values"][i] is None else entry["values"][i]) for entry in series] for i, category in enumerate(categories)],
            "caption": "图表数据（缺测值不连线，不按零处理）"}
    note = block_note({**block, "caption": title}, context, caption=True)
    return '<figure class="chart"><div class="chart-scroll-hint">左右滑动查看完整图表；下方附完整数据表。</div><div class="chart-scroll" tabindex="0" role="region" aria-label="图表，可横向滚动">' + "".join(svg) + "</div>" + legend + f"<figcaption>{note}</figcaption></figure>" + render_table(data, context, css_class="chart-data")


def render_block(block: dict, context: RenderContext | None = None, *, depth: int = 0) -> str:
    context = context or RenderContext()
    if not isinstance(block, dict):
        raise ValueError("blocks 的每一项必须是对象")
    btype = block.get("type")
    if btype == "heading":
        level = max(1, min(6, int(block.get("level", 1))))
        text = str(block.get("text", ""))
        anchor = context.heading(level, text)
        return f'<h{level} id="{anchor}">{render_inline(text, context)}{context.cite(block.get("citations"))}</h{level}>'
    if btype == "paragraph":
        content = f"<p>{render_inline(block.get('text', ''), context)}{context.cite(block.get('citations'))}</p>"
        return content + (f'<div class="source">来源：{render_inline(block["source"], context)}</div>' if block.get("source") else "")
    if btype in ("bullets", "numbered"):
        tag = "ol" if btype == "numbered" else "ul"
        items = block.get("items", []) or []
        if not isinstance(items, list):
            raise ValueError("列表 items 必须是数组")
        content = f"<{tag}>" + "".join(f"<li>{render_inline(item, context)}</li>" for item in items) + f"</{tag}>"
    elif btype == "table":
        return render_table(block, context)
    elif btype == "chart":
        return render_chart(block, context)
    elif btype == "image":
        src = context.image(block.get("path", ""))
        note = block_note(block, context, caption=True)
        alt = block.get("alt") or block.get("caption") or Path(block["path"]).name
        return f'<figure><img src="{src}" alt="{escape(str(alt))}"/>' + (f"<figcaption>{note}</figcaption>" if note else "") + "</figure>"
    elif btype == "columns":
        if depth >= 2:
            raise ValueError("columns 最多允许两层，请把复杂内容拆成连续章节")
        ratio = block.get("ratio", [1, 1])
        if not isinstance(ratio, list) or len(ratio) != 2 or any(not finite_number(v) or not .2 <= v <= 5 for v in ratio):
            raise ValueError("columns.ratio 须为两个 0.2–5 的有限正数")
        columns = []
        for side in ("left", "right"):
            children = block.get(side, [])
            if not isinstance(children, list):
                raise ValueError(f"columns.{side} 必须是 blocks 数组")
            columns.append('<div class="column">' + "".join(render_block(b, context, depth=depth+1) for b in children) + "</div>")
        content = f'<div class="columns" style="--left:{ratio[0]:g}fr;--right:{ratio[1]:g}fr">' + "".join(columns) + "</div>"
    elif btype == "quote":
        content = f"<blockquote><p>{render_inline(block.get('text', ''), context)}</p></blockquote>"
    elif btype == "code":
        content = f'<pre><code class="language-{escape(str(block.get("lang", "")))}">{escape(str(block.get("text", "")))}</code></pre>'
    elif btype == "page_break":
        content = '<div class="page-break"></div>'
    else:
        raise ValueError(f"不支持的 block type: {btype!r}")
    note = block_note(block, context)
    return content + (f'<div class="block-note">{note}</div>' if note else "")


# --- Minimal Markdown renderer ---------------------------------------------

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_FENCE = re.compile(r"^```(\w*)\s*$")
_MD_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_MD_TABLE_SEP = re.compile(r"^\s*\|\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|\s*$")
_MD_HR = re.compile(r"^(\*\*\*|---|___)\s*$")
_MD_IMAGE = re.compile(r"^\s*!\[([^\]]*)\]\(([^)]+)\)\s*(\[@[^\]]+\])?\s*$")


def render_markdown(md: str, context: RenderContext | None = None) -> tuple[str, list[tuple]]:
    context = context or RenderContext()
    lines = md.splitlines()
    body: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if m := _MD_HEADING.match(line):
            level = len(m.group(1))
            text = m.group(2).strip()
            body.append(render_block({"type": "heading", "level": level, "text": text}, context))
            i += 1
            continue
        if m := _MD_IMAGE.match(line):
            body.append(render_block({"type": "image", "path": image_target(m.group(2)),
                                      "alt": m.group(1), "caption": m.group(1) + (m.group(3) or "")}, context))
            i += 1
            continue
        if _MD_FENCE.match(line):
            lang = _MD_FENCE.match(line).group(1)
            i += 1
            code: list[str] = []
            while i < n and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            body.append(f'<pre><code class="language-{escape(lang)}">{escape(chr(10).join(code))}</code></pre>')
            continue
        if _MD_HR.match(line):
            body.append("<hr/>")
            i += 1
            continue
        if _MD_TABLE_ROW.match(line) and i + 1 < n and _MD_TABLE_SEP.match(lines[i + 1]):
            def _split_row(r: str) -> list[str]:
                r = r.strip()
                if r.startswith("|"):
                    r = r[1:]
                if r.endswith("|"):
                    r = r[:-1]
                return [c.strip() for c in r.split("|")]
            headers = _split_row(line)
            i += 2
            rows: list[list[str]] = []
            while i < n and _MD_TABLE_ROW.match(lines[i]):
                rows.append(_split_row(lines[i]))
                i += 1
            body.append(render_table({"headers": headers, "rows": rows}, context))
            continue
        if line.startswith(">"):
            quote: list[str] = []
            while i < n and lines[i].startswith(">"):
                quote.append(lines[i].lstrip(">").strip())
                i += 1
            body.append(f"<blockquote><p>{render_inline(' '.join(quote), context)}</p></blockquote>")
            continue
        if re.match(r"^\s*([-*+]\s+)", line):
            items: list[str] = []
            while i < n and (m := re.match(r"^\s*([-*+]\s+)(.*)$", lines[i])):
                items.append(m.group(2))
                i += 1
            body.append("<ul>" + "".join(f"<li>{render_inline(it, context)}</li>" for it in items) + "</ul>")
            continue
        if re.match(r"^\s*(\d+\.\s+)", line):
            items = []
            while i < n and (m := re.match(r"^\s*(\d+\.\s+)(.*)$", lines[i])):
                items.append(m.group(2))
                i += 1
            body.append("<ol>" + "".join(f"<li>{render_inline(it, context)}</li>" for it in items) + "</ol>")
            continue
        if line.strip() == "":
            i += 1
            continue
        para: list[str] = []
        while i < n and lines[i].strip() != "" and not _MD_HEADING.match(lines[i]) \
                and not _MD_FENCE.match(lines[i]) and not _MD_HR.match(lines[i]) \
                and not (_MD_TABLE_ROW.match(lines[i]) and i + 1 < n and _MD_TABLE_SEP.match(lines[i+1])) \
                and not _MD_IMAGE.match(lines[i]) and not re.match(r"^\s*([-*+]|\d+\.)\s+", lines[i]) \
                and not lines[i].startswith(">"):
            para.append(lines[i].strip())
            i += 1
        body.append(f"<p>{render_inline(' '.join(para), context)}</p>")
    return "".join(body), context.headings


def build_html(spec: dict, body: str, headings: list[tuple], *,
               force_toc: bool, no_toc: bool) -> str:
    title = str(spec.get("title") or "报告")
    subtitle = spec.get("subtitle")
    author = spec.get("author")
    meta = spec.get("meta") or {}

    style = spec.get("_style") or None
    brand = (style or {}).get("brand") or {}
    label = brand.get("label") if style else "MEDEX · 医学报告"
    cover = ['<header class="cover">'] + ([f'<div class="cover-label">{escape(str(label))}</div>'] if label else []) \
        + [f"<h1>{escape(title)}</h1>"] + (['<div class="cover-rule"></div>'] if style else [])
    if subtitle:
        cover.append(f'<p class="subtitle">{escape(str(subtitle))}</p>')
    if author or meta:
        cover.append('<div class="meta">')
        if author:
            cover.append(f'<div><span class="meta-key">作者</span>{escape(str(author))}</div>')
        for k, v in meta.items():
            cover.append(f'<div><span class="meta-key">{escape(str(k))}</span>{escape(str(v))}</div>')
        cover.append("</div>")
    cover.append("</header>")

    toc = ""
    if not no_toc:
        if force_toc or len(headings) >= 3:
            toc = build_toc(headings)

    foot = (
        '<footer class="report-foot">本报告由 Medex 生成，仅供医学参考，'
        "不构成最终诊疗意见。</footer>"
    )
    if style:
        foot = f'<footer class="report-foot">{escape(str(brand["footer"]))}</footer>' if brand.get("footer") else ""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8"/>'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>'
        f"<title>{escape(title)}</title>"
        f"<style>{CSS}\n{style_css(style)}</style></head><body>"
        f'<main class="report">{"".join(cover)}{toc}{body}{foot}</main>'
        "</body></html>"
    )


def render_report(spec: dict, *, base_dir: Path | None = None, force_toc: bool = False,
                  no_toc: bool = False) -> tuple[str, RenderContext]:
    """Render and validate every block before any final output is written."""
    context = RenderContext(spec.get("references"), base_dir=base_dir)
    blocks = spec.get("blocks", [])
    if not isinstance(blocks, list):
        raise ValueError("blocks 必须是数组")
    body = "".join(render_block(block, context) for block in blocks) + context.bibliography()
    return build_html(spec, body, context.headings, force_toc=force_toc, no_toc=no_toc), context


def load_spec(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("spec 顶层必须是 JSON 对象")
    return data


def _sanitize_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name)).strip("_")
    return name or "report"


def resolve_output(args, spec: dict, src_path: Path) -> Path:
    """Resolve output: /repo/o_outcome/{name}_{yyyy}_{mm}_{dd}_v{N}.html."""
    if args.out:
        out = args.out.expanduser().resolve()
        if out.exists() and not args.force:
            raise FileExistsError(f"output exists (use --force to overwrite): {out}")
        return out
    name = _sanitize_name(args.name or spec.get("title") or src_path.stem)
    date_tag = (args.date or datetime.now().strftime("%Y-%m-%d")).replace("-", "_")
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.version is not None:
        out = out_dir / f"{name}_{date_tag}_v{args.version}.html"
        if out.exists() and not args.force:
            raise FileExistsError(f"output exists (use --force to overwrite): {out}")
        return out
    version = _next_version(out_dir, name, date_tag, "html")
    return out_dir / f"{name}_{date_tag}_v{version}.html"


def _next_version(out_dir: Path, name: str, date_tag: str, ext: str) -> int:
    """Return a version number strictly greater than any existing report with
    the same name/date/extension. Guarantees every run produces a brand-new file
    that never collides with or overwrites a historical report."""
    pattern = re.compile(rf"^{re.escape(name)}_{date_tag}_v(\d+)\.{re.escape(ext)}$")
    highest = 0
    try:
        for entry in out_dir.iterdir():
            m = pattern.match(entry.name)
            if m:
                highest = max(highest, int(m.group(1)))
    except OSError:
        pass
    return highest + 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Render a JSON spec or Markdown into a self-contained HTML report.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--spec", type=Path, help="JSON spec file path")
    src.add_argument("--markdown", type=Path, help="Markdown file path")
    ap.add_argument("--references", type=Path, help="Local reference JSON array (Markdown only; use [@id])")
    ap.add_argument("--out", type=Path, help="Explicit output .html path (overrides auto naming)")
    ap.add_argument("--name", help="Report base name; default: spec title or input stem")
    ap.add_argument("--version", type=int, help="Version number; default: auto-increment to avoid clash")
    ap.add_argument("--out-dir", type=Path, default=Path("/repo/o_outcome"),
                    help="Output directory (default: /repo/o_outcome)")
    ap.add_argument("--date", help="Date string YYYY-MM-DD (default: today)")
    ap.add_argument("--title", help="Override title")
    ap.add_argument("--author", help="Override author")
    ap.add_argument("--toc", action="store_true", help="Force table of contents")
    ap.add_argument("--no-toc", action="store_true", help="Disable table of contents")
    ap.add_argument("--force", action="store_true", help="Overwrite existing output")
    args = ap.parse_args()

    try:
        if args.spec:
            args.spec = args.spec.expanduser().resolve(strict=True)
            spec = load_spec(args.spec)
            src_path = args.spec
            if args.references:
                raise ValueError("--references 仅用于 --markdown；JSON spec 请使用顶层 references")
            context = RenderContext(spec.get("references"), base_dir=src_path.parent)
            blocks = spec.get("blocks", [])
            if not isinstance(blocks, list):
                raise ValueError("blocks 必须是数组")
            body = "".join(render_block(b, context) for b in blocks)
            headings = context.headings
        else:
            args.markdown = args.markdown.expanduser().resolve(strict=True)
            src_path = args.markdown
            md = args.markdown.read_text(encoding="utf-8")
            references = []
            if args.references:
                references = json.loads(args.references.read_text(encoding="utf-8"))
                if isinstance(references, dict):
                    if "references" not in references:
                        raise ValueError("--references 对象必须包含 references 数组")
                    references = references["references"]
            context = RenderContext(references, base_dir=src_path.parent)
            body, headings = render_markdown(md, context)
            spec = {"title": headings[0][1] if headings and headings[0][0] == 1 else args.markdown.stem}

        if args.title:
            spec["title"] = args.title
        if args.author:
            spec["author"] = args.author

        body += context.bibliography()
        html = build_html(spec, body, headings, force_toc=args.toc, no_toc=args.no_toc)
        out = resolve_output(args, spec, src_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation closes the race between concurrent automatic versions.
        with out.open("w" if args.force else "x", encoding="utf-8") as stream:
            stream.write(html)

        # stats
        n_head = len(headings)
        n_tbl = body.count("<table")
        n_img = context.figure_count
        print(f"Output: {out}")
        print(f"Headings: {n_head}")
        print(f"Tables: {n_tbl}")
        print(f"Figures: {n_img} (inlined: {n_img}, missing: 0)")
        print(f"Charts: {context.chart_count} (SVG with visible data tables)")
        print(f"References: {len(context.references)}")
        print(f"TOC: {'yes' if (not args.no_toc and (args.toc or n_head >= 3)) else 'no'}")
        print(f"Size: {out.stat().st_size} bytes")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
