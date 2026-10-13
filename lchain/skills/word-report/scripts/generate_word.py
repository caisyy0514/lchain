#!/usr/bin/env python3
"""Generate a valid .docx (Office Open XML) from a structured JSON spec.

Builds self-contained OOXML parts with the standard library, including images,
editable charts/workbooks, mixed columns, scientific references and paginating
tables. No python-docx dependency. Existing Pillow is used opportunistically to
decode images. Supports East-Asian fonts and Word / WPS / Pages / LibreOffice.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
import sys
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

# Skills are distributed independently and tests may load this script by path.
# Resolve its sibling relative to __file__, never relative to the current cwd.
_asset_spec = importlib.util.spec_from_file_location("medex_word_report_assets", Path(__file__).with_name("report_assets.py"))
_assets = importlib.util.module_from_spec(_asset_spec)
_asset_spec.loader.exec_module(_assets)
ReferenceList = _assets.ReferenceList
build_chart = _assets.build_chart
positive_number = _assets.positive_number
read_image = _assets.read_image
xml_attr = _assets.xml_attr

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# [LChain] 自定义样式：默认值即 medex 原版配色/字体；spec._style（app/agent/report_styles.py 解析结果）存在时覆盖。
_DEFAULT_THEME = {
    "text": "27343D", "title": "172C3A", "subtitle": "64727D", "h1": "19364D", "h2": "19364D", "h3": "314B60",
    "caption": "314B60", "source": "64727D", "quote": "4D5D68", "biblio": "46545F", "cite": "314B60",
    "link": "007F7A", "table_rule": "314B60", "table_head_rule": "8597A5", "table_head_text": "243F54",
    "table_head_fill": "", "table_band": "", "table_grid": "", "code_fill": "F6F8FA", "code_text": "1F2328",
    "font_en": "Calibri", "font_cn": "等线", "font_heading_cn": "", "scale": 1.0, "cover_band": "",
    "cover_band_text": "", "chart": ["007F7A", "3869A8", "B87924", "8461A0"], "chart_ink": "4D5D68",
    "chart_axis": "97A6B0", "chart_grid": "E3E9ED", "brand_label": "", "brand_footer": "",
}
_T = dict(_DEFAULT_THEME)
_WORD_FONT_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff \-]{0,39}$")


def _sync_chart_theme() -> None:
    _assets.CHART_THEME.update(palette=_T["chart"], ink=_T["chart_ink"], axis=_T["chart_axis"],
                               grid=_T["chart_grid"], font_en=_T["font_en"], font_cn=_T["font_cn"])


def apply_style(style: dict | None) -> None:
    """按解析后的样式设置本次生成的主题；None 恢复原版默认。"""
    _T.clear()
    _T.update(_DEFAULT_THEME)
    if not style:
        _sync_chart_theme()
        return
    c = {k: str(v).upper() for k, v in (style.get("colors") or {}).items() if k != "chart"}
    for key in ("primary", "accent", "text", "heading", "muted", "border", "alt", "on_primary"):
        if not re.fullmatch(r"[0-9A-F]{6}", c.get(key, "")):
            raise ValueError(f"style.colors.{key} 无效: {c.get(key)!r}")
    fonts = style.get("fonts") or {}
    for k in ("cn", "en", "heading_cn"):
        if fonts.get(k) and not _WORD_FONT_RE.match(str(fonts[k])):
            raise ValueError(f"style.fonts.{k} 无效: {fonts[k]!r}")
    chart = [str(x).upper() for x in (style.get("colors") or {}).get("chart", []) if re.fullmatch(r"[0-9A-Fa-f]{6}", str(x))]
    _T.update({
        "text": c["text"], "title": c["heading"], "subtitle": c["muted"],
        "h1": c.get("primary_text", c["primary"]), "h2": c.get("primary_text", c["primary"]),
        "h3": c["heading"], "caption": c["heading"], "source": c["muted"], "quote": c["muted"], "biblio": c["text"],
        "cite": c["accent"], "link": c["accent"], "table_rule": c["heading"], "table_head_rule": c["muted"],
        "table_head_text": c["heading"], "code_fill": c["alt"], "code_text": c["text"],
        "font_en": fonts.get("en") or _T["font_en"], "font_cn": fonts.get("cn") or _T["font_cn"],
        "font_heading_cn": fonts.get("heading_cn") if fonts.get("heading_cn") not in ("", None, fonts.get("cn")) else "",
        "scale": float(style.get("font_scale") or 1.0),
        "chart": chart[:4] or _T["chart"], "chart_ink": c["muted"], "chart_axis": c["muted"], "chart_grid": c["border"],
    })
    table = style.get("table", "auto")
    if table == "header_fill":
        _T.update(table_head_fill=c["primary"], table_head_text=c["on_primary"], table_band=c["alt"],
                  table_rule=c["primary"], table_head_rule=c["primary"])
    elif table == "grid":
        _T.update(table_grid=c["border"], table_head_fill=c["alt"])
    if style.get("cover") == "band":
        _T.update(cover_band=c["primary"], cover_band_text=c["on_primary"])
    brand = style.get("brand") or {}
    _T.update(brand_label=str(brand.get("label") or "")[:30], brand_footer=str(brand.get("footer") or "")[:80])
    _sync_chart_theme()


def footer_xml() -> str:
    """页脚：品牌说明（左）+ 页码（右，域代码 PAGE）。"""
    text = escape(_T["brand_footer"])
    run = f'<w:rPr><w:color w:val="{_T["source"]}"/><w:sz w:val="{_sz(17)}"/><w:szCs w:val="{_sz(17)}"/></w:rPr>'
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:p><w:pPr><w:pBdr><w:top w:val="single" w:sz="4" w:space="4" w:color="{_T["table_head_rule"]}"/></w:pBdr>'
            f'<w:tabs><w:tab w:val="right" w:pos="{CONTENT_W}"/></w:tabs><w:spacing w:after="0"/></w:pPr>'
            f'<w:r>{run}<w:t xml:space="preserve">{text}</w:t></w:r><w:r>{run}<w:tab/></w:r>'
            f'<w:r>{run}<w:fldChar w:fldCharType="begin"/></w:r><w:r>{run}<w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
            f'<w:r>{run}<w:fldChar w:fldCharType="separate"/></w:r><w:r>{run}<w:t>1</w:t></w:r>'
            f'<w:r>{run}<w:fldChar w:fldCharType="end"/></w:r></w:p></w:ftr>')


def _sz(half_points: int) -> int:
    """按 font_scale 缩放字号（半磅）。"""
    return max(12, round(half_points * _T["scale"]))


def _table_borders() -> str:
    if _T["table_grid"]:
        return ("<w:tblBorders>" + "".join(
            f'<w:{e} w:val="single" w:sz="4" w:space="0" w:color="{_T["table_grid"]}"/>'
            for e in ("top", "left", "bottom", "right", "insideH", "insideV")) + "</w:tblBorders>")
    top = "nil" if _T["table_head_fill"] else "single"
    return ("<w:tblBorders>"
            f'<w:top w:val="{top}" w:sz="9" w:space="0" w:color="{_T["table_rule"]}"/>'
            '<w:left w:val="nil"/>'
            f'<w:bottom w:val="single" w:sz="8" w:space="0" w:color="{_T["table_rule"]}"/>'
            '<w:right w:val="nil"/><w:insideH w:val="nil"/><w:insideV w:val="nil"/>'
            "</w:tblBorders>")

# A4 portrait, 1-inch margins. Content width = 11906 - 2*1440 = 9026 twips.
PAGE_W = 11906
PAGE_H = 16838
MARGIN = 1440
CONTENT_W = PAGE_W - 2 * MARGIN
CONTENT_H = PAGE_H - 2 * MARGIN

# EMU (English Metric Units): 914400 per inch. Inline image sizing helpers.
EMU_PER_INCH = 914400
PIXEL_EMU = EMU_PER_INCH // 96  # 9525 EMU per pixel at 96 DPI
SUPPORTED_IMAGE_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
}


def xrun(text: str, *, bold: bool = False, italic: bool = False,
         code: bool = False, size: int | None = None, color: str | None = None,
         superscript: bool = False) -> str:
    """Build a <w:r> run with formatting properties."""
    rpr_parts: list[str] = []
    if code:
        rpr_parts.append(
            '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" '
            f'w:eastAsia="{escape(_T["font_cn"])}"/>'
        )
        rpr_parts.append(f'<w:color w:val="{_T["code_text"]}"/>')
        rpr_parts.append(f'<w:shd w:val="clear" w:color="auto" w:fill="{_T["code_fill"]}"/>')
    if bold:
        rpr_parts.append("<w:b/><w:bCs/>")
    if italic:
        rpr_parts.append("<w:i/><w:iCs/>")
    if color:
        rpr_parts.append(f'<w:color w:val="{color}"/>')
    if size is not None:
        rpr_parts.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
    if superscript:
        rpr_parts.append('<w:vertAlign w:val="superscript"/>')
    rpr = f"<w:rPr>{''.join(rpr_parts)}</w:rPr>" if rpr_parts else ""
    contents = "<w:br/>".join(f'<w:t xml:space="preserve">{escape(line)}</w:t>' for line in str(text).split("\n"))
    return f"<w:r>{rpr}{contents}</w:r>"


_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"\*([^*]+)\*")


def parse_inline(text: str) -> list[tuple[str, dict]]:
    """Parse inline markdown into (text, fmt) segments. fmt keys: bold/italic/code."""
    segments: list[tuple[str, dict]] = []
    # Tokenize by protecting code first.
    tokens: list[tuple[str, str | None]] = []  # (kind, value) kind in {text, code}
    pos = 0
    for m in _INLINE_CODE_RE.finditer(text):
        if m.start() > pos:
            tokens.append(("text", text[pos:m.start()]))
        tokens.append(("code", m.group(1)))
        pos = m.end()
    if pos < len(text):
        tokens.append(("text", text[pos:]))
    if not tokens:
        return [("", {})]
    for kind, value in tokens:
        if kind == "code":
            segments.append((value, {"code": True}))
            continue
        # split text by bold, then italic
        cursor = 0
        for bm in _BOLD_RE.finditer(value):
            if bm.start() > cursor:
                segments.extend(_split_italic(value[cursor:bm.start()], bold=False))
            segments.extend(_split_italic(bm.group(1), bold=True))
            cursor = bm.end()
        if cursor < len(value):
            segments.extend(_split_italic(value[cursor:], bold=False))
    return segments or [("", {})]


def _split_italic(text: str, *, bold: bool) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    cursor = 0
    for im in _ITALIC_RE.finditer(text):
        if im.start() > cursor:
            out.append((text[cursor:im.start()], {"bold": bold}))
        out.append((im.group(1), {"bold": bold, "italic": True}))
        cursor = im.end()
    if cursor < len(text):
        out.append((text[cursor:], {"bold": bold}))
    return out or [("", {"bold": bold})]


def runs_for_text(text: str, **base_fmt) -> str:
    segs = parse_inline(str(text))
    parts = []
    for seg_text, fmt in segs:
        merged = dict(base_fmt)
        # Inline parsing marks ordinary segments bold=False. Preserve explicit
        # header/label emphasis instead of letting that reset the base style.
        merged.update({key: value for key, value in fmt.items() if value or key not in merged})
        parts.append(xrun(seg_text, **merged))
    return "".join(parts)


def paragraph(text: str, *, style: str | None = None, jc: str | None = None,
              spacing: dict | None = None, keep_next: bool = False,
              keep_lines: bool = False, citation: str = "", **run_fmt) -> str:
    ppr_parts: list[str] = []
    if style:
        ppr_parts.append(f'<w:pStyle w:val="{style}"/>')
    if keep_next:
        ppr_parts.append('<w:keepNext/>')
    if keep_lines:
        ppr_parts.append('<w:keepLines/>')
    ppr_parts.append('<w:widowControl/>')
    if spacing:
        attrs = "".join(f' w:{k}="{v}"' for k, v in spacing.items())
        ppr_parts.append(f"<w:spacing{attrs}/>")
    if jc:
        ppr_parts.append(f'<w:jc w:val="{jc}"/>')
    ppr = f"<w:pPr>{''.join(ppr_parts)}</w:pPr>" if ppr_parts else ""
    body = runs_for_text(text, **run_fmt)
    if citation:
        body += xrun(citation, color=_T["cite"], superscript=True)
    return f"<w:p>{ppr}{body}</w:p>"


def heading(text: str, level: int, *, citation: str = "") -> str:
    level = max(1, min(3, level))
    return paragraph(text, style=f"Heading{level}", citation=citation)


def list_item(text: str, *, ordered: bool, num_id: int, citation: str = "") -> str:
    ppr = (
        f'<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num_id}"/></w:numPr></w:pPr>'
    )
    cite_run = xrun(citation, color=_T["cite"], superscript=True) if citation else ""
    return f"<w:p>{ppr}{runs_for_text(text)}{cite_run}</w:p>"


def text_lines(text: object, width: int, font_size: int = 22) -> int:
    """Conservative wrapped-line estimate, used for media and oversized rows."""
    capacity = max(1, width / (font_size * 10))
    return sum(max(1, math.ceil(sum(1 if unicodedata.east_asian_width(c) in "WF" else .55 for c in line) / capacity)) for line in str(text).split("\n"))


def column_widths(weights: list[float] | None, columns: int, width: int) -> list[int]:
    if weights is None:
        weights = [1] * columns
    if not isinstance(weights, list) or len(weights) != columns:
        raise ValueError("列宽权重的数量必须与列数一致")
    weights = [positive_number(v, "列宽/ratio") for v in weights]
    result = [int(width * value / sum(weights)) for value in weights]
    result[-1] += width - sum(result)
    if min(result) < 500:
        raise ValueError("列宽过窄，请减少列数、调整比例或改用全宽表格")
    return result


def table_alignment(rows: list[list], columns: int) -> list[str]:
    """Right-align measured values while preserving text and missing markers."""
    result = []
    numeric = re.compile(r"^[<>≤≥±+−-]?\s*\d[\d.,+−–eE/:%()\sA-Za-zμµ²³×^·-]*$")
    for col in range(columns):
        values = [str(row[col]).strip() for row in rows if len(row) > col and row[col] is not None and str(row[col]).strip() not in ("", "—", "-", "NA", "N/A")]
        result.append("right" if values and all(numeric.fullmatch(value) for value in values) else "left")
    return result


def table_width_weights(headers: list, rows: list[list], align: list[str], columns: int) -> list[float]:
    """Give explanatory columns space without letting one outlier dominate."""
    weights = []
    for col in range(columns):
        cells = ([headers[col]] if len(headers) > col else []) + [row[col] for row in rows if len(row) > col]
        lengths = sorted(sum(1 if unicodedata.east_asian_width(c) in "WF" else .55 for c in str(value)) for value in cells) or [3]
        representative = lengths[min(len(lengths) - 1, int(len(lengths) * .8))]
        header_length = sum(1 if unicodedata.east_asian_width(c) in "WF" else .55 for c in str(headers[col])) if col < len(headers) else 0
        weights.append(max(4, min(18 if align[col] == "left" else 9, max(header_length, representative) + 2)))
    return weights


def table(headers: list[str], rows: list[list[str]], *, width: int = CONTENT_W,
          widths: list[float] | None = None, align: list[str] | None = None) -> str:
    if not isinstance(headers, list) or not isinstance(rows, list) or any(not isinstance(r, list) for r in rows):
        raise ValueError("table headers/rows 必须是数组，rows 中每行必须是数组")
    cols = max(len(headers), max((len(r) for r in rows), default=0), 1)
    if align is not None and (not isinstance(align, list) or len(align) != cols or any(v not in ("left", "center", "right") for v in align)):
        raise ValueError("table align 需要每列一个 left/center/right")
    align = align or table_alignment(rows, cols)
    widths = column_widths(widths if widths is not None else table_width_weights(headers, rows, align, cols), cols, width)
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    tbl_pr = (
        "<w:tblPr>"
        f'<w:tblW w:w="{width}" w:type="dxa"/>'
        + _table_borders() +
        "<w:tblLayout w:type=\"fixed\"/>"
        '<w:tblCellMar><w:top w:w="120" w:type="dxa"/><w:left w:w="130" w:type="dxa"/><w:bottom w:w="120" w:type="dxa"/><w:right w:w="130" w:type="dxa"/></w:tblCellMar>'
        "</w:tblPr>"
    )
    tbl_grid = f"<w:tblGrid>{grid}</w:tblGrid>"

    def cell(text: str, index: int, *, header: bool = False, band: bool = False) -> str:
        fill = _T["table_head_fill"] if header else (_T["table_band"] if band else "")
        tc_pr = (
            "<w:tcPr>"
            f'<w:tcW w:w="{widths[index]}" w:type="dxa"/>'
            + (f'<w:tcBorders><w:bottom w:val="single" w:sz="4" w:color="{_T["table_head_rule"]}"/></w:tcBorders>'
               if header and not _T["table_grid"] else "")
            + (f'<w:shd w:val="clear" w:color="auto" w:fill="{fill}"/>' if fill else "")
            + '<w:vAlign w:val="center"/>'
            + "</w:tcPr>"
        )
        run_fmt = {"bold": True, "color": _T["table_head_text"]} if header else {}
        p = paragraph(text if text is not None else "", spacing={"after": "20", "line": "264", "lineRule": "auto"}, jc=align[index], size=_sz(21), **run_fmt)
        return f"<w:tc>{tc_pr}{p}</w:tc>"

    band_index = [0]

    def row(cells: list[str], *, header: bool = False) -> str:
        padded = list(cells) + [""] * (cols - len(cells))
        if max(text_lines(c, widths[i] - 260) * 520 + 320 for i, c in enumerate(padded)) > CONTENT_H - 1600:
            raise ValueError("表格单行超过一页，请把长说明移到正文或拆成多行")
        props = '<w:trPr><w:cantSplit/>' + ('<w:tblHeader/>' if header else '') + '</w:trPr>'
        band = False
        if not header:
            band = band_index[0] % 2 == 1
            band_index[0] += 1
        return "<w:tr>" + props + "".join(cell(c, i, header=header, band=band) for i, c in enumerate(padded[:cols])) + "</w:tr>"

    rows_xml = ""
    if headers:
        rows_xml += row(headers, header=True)
    for r in rows:
        rows_xml += row(r)
    return f"<w:tbl>{tbl_pr}{tbl_grid}{rows_xml}</w:tbl>"


def quote(text: str, *, citation: str = "") -> str:
    return paragraph(text, style="Quote", italic=True, citation=citation)


def code_block(text: str, *, citation: str = "") -> str:
    ppr = (
        "<w:pPr>"
        f'<w:shd w:val="clear" w:color="auto" w:fill="{_T["code_fill"]}"/>'
        '<w:ind w:left="240"/>'
        "</w:pPr>"
    )
    run = (
        f'<w:r><w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" '
        f'w:eastAsia="{escape(_T["font_cn"])}"/><w:color w:val="{_T["code_text"]}"/></w:rPr>'
        f'<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'
    )
    cite_run = xrun(citation, color=_T["cite"], superscript=True) if citation else ""
    return f"<w:p>{ppr}{run}{cite_run}</w:p>"


def page_break() -> str:
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def title_paragraph(title: str, subtitle: str | None) -> list[str]:
    if _T["cover_band"]:
        # 主色色带封面：单格表格铺底色，标题/副标题反白
        inner = (paragraph(_T["brand_label"], jc="center", spacing={"before": "120", "after": "0"}, size=_sz(18),
                           color=_T["cover_band_text"]) if _T["brand_label"] else "")
        inner += paragraph(title, style="Title", jc="center", spacing={"before": "240", "after": "120" if subtitle else "240"},
                           color=_T["cover_band_text"])
        if subtitle:
            inner += paragraph(subtitle, style="Subtitle", jc="center", spacing={"after": "240"}, color=_T["cover_band_text"])
        borders = "".join(f'<w:{e} w:val="nil"/>' for e in ("top", "left", "bottom", "right", "insideH", "insideV"))
        return [f'<w:tbl><w:tblPr><w:tblW w:w="{CONTENT_W}" w:type="dxa"/><w:tblBorders>{borders}</w:tblBorders>'
                f'<w:tblLayout w:type="fixed"/></w:tblPr><w:tblGrid><w:gridCol w:w="{CONTENT_W}"/></w:tblGrid>'
                f'<w:tr><w:trPr><w:cantSplit/></w:trPr><w:tc><w:tcPr><w:tcW w:w="{CONTENT_W}" w:type="dxa"/>'
                f'<w:shd w:val="clear" w:color="auto" w:fill="{_T["cover_band"]}"/>'
                '<w:tcMar><w:top w:w="200" w:type="dxa"/><w:left w:w="240" w:type="dxa"/><w:bottom w:w="200" w:type="dxa"/><w:right w:w="240" w:type="dxa"/></w:tcMar>'
                f'</w:tcPr>{inner}</w:tc></w:tr></w:tbl>',
                paragraph("", spacing={"after": "160"})]
    out = [paragraph(_T["brand_label"], style="SourceNote", jc="center", spacing={"after": "80"})] if _T["brand_label"] else []
    out += [paragraph(title, style="Title", jc="center", spacing={"before": "80", "after": "120" if subtitle else "280"}, keep_next=bool(subtitle))]
    if subtitle:
        out.append(paragraph(subtitle, style="Subtitle", jc="center",
                             spacing={"after": "260"}))
    return out


def meta_paragraphs(author: str | None, meta: dict) -> list[str]:
    out: list[str] = []
    if author:
        out.append(paragraph(author, style="SourceNote", jc="center", spacing={"after": "60"}))
    for k, v in meta.items():
        out.append(paragraph(f"{k}：{v}", style="SourceNote", jc="center", spacing={"after": "40"}))
    out.append(paragraph("", spacing={"after": "100", "line": "80", "lineRule": "exact"}))
    return out


# --- image embedding helpers ----------------------------------------------

class ImageMedia:
    """Deduplicates images by path and assigns unique relationship ids and
    media targets so the .docx is fully self-contained (downloadable, all
    referenced materials embedded)."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self._items: list[dict] = []
        self._by_path: dict[str, dict] = {}
        self._next_rid = 3  # rId1=styles, rId2=numbering reserved
        self._next_num = 1
        self._next_docpr = 1
        self.base_dir = base_dir or Path.cwd()
        self.charts: list[dict] = []
        self.links: list[dict] = []

    def add(self, path) -> dict:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.base_dir / p
        p = p.resolve()
        key = str(p)
        if key in self._by_path:
            return self._by_path[key]
        if not p.is_file():
            raise ValueError(f"图片文件不存在: {p}")
        ext = p.suffix.lower().lstrip(".")
        if ext not in SUPPORTED_IMAGE_TYPES:
            raise ValueError(f"不支持的图片格式: {p}；请转换为 PNG/JPEG/GIF/BMP")
        data, width, height = read_image(p)
        entry = {
            "path": key,
            "bytes": data,
            "ext": ext,
            "rid": f"rId{self._next_rid}",
            "target": f"media/image{self._next_num}.{ext}",
            "width": width,
            "height": height,
        }
        self._next_rid += 1
        self._next_num += 1
        self._items.append(entry)
        self._by_path[key] = entry
        return entry

    def add_chart(self, block: dict) -> dict:
        chart, workbook = build_chart(block)
        num = len(self.charts) + 1
        entry = {"rid": f"rId{self._next_rid}", "target": f"charts/chart{num}.xml", "xml": chart, "workbook": workbook, "num": num}
        self._next_rid += 1
        self.charts.append(entry)
        return entry

    def add_link(self, url: str) -> str:
        entry = {"rid": f"rId{self._next_rid}", "target": url}
        self._next_rid += 1
        self.links.append(entry)
        return entry["rid"]

    def next_docpr(self) -> int:
        v = self._next_docpr
        self._next_docpr += 1
        return v

    def items(self) -> list[dict]:
        return list(self._items)

    def extensions(self) -> set[str]:
        return {e["ext"] for e in self._items}


def figure_notes(block: dict, refs: ReferenceList) -> list[str]:
    caption = str(block.get("caption") or block.get("title") or "").strip()
    marker = refs.marker(block.get("citations"))
    notes = [caption + ("\u00a0" + marker if marker else "")] if caption else ([marker] if marker else [])
    if block.get("source"):
        notes.append("来源：" + str(block["source"]))
    if block.get("sample"):
        notes.append("样本：" + str(block["sample"]))
    return notes


def notes_xml(notes: list[str], *, keep_next: bool = False) -> str:
    paragraphs = []
    for i, note in enumerate(notes):
        is_source = note.startswith(("来源：", "样本：", "注："))
        citation = ""
        if not is_source:
            cite_match = re.search(r"\u00a0((?:\[\d+\])+)$", note)
            if cite_match:
                citation, note = cite_match.group(1), note[:cite_match.start()]
            note = re.sub(r"^((?:图|表)\s*\d+)([：:.]?\s*)", r"**\1** ", note)
        paragraphs.append(paragraph(note, style="SourceNote" if is_source else "Caption", keep_next=keep_next or i < len(notes) - 1, keep_lines=True, citation=citation))
    return "".join(paragraphs)


def figure_size(block: dict, width: int, notes: list[str], intrinsic: tuple[int, int]) -> tuple[int, int]:
    # CJK fallback fonts may use much taller line metrics than Latin fonts.
    # Reserve a heading/flow allowance as well: keepNext cannot save a group
    # whose combined heading + image + caption height exceeds the page.
    reserve = sum(text_lines(note, width, 20) * 440 + 120 for note in notes) + 1440
    available_h = CONTENT_H - reserve
    if available_h < 1440:
        raise ValueError("图注/来源过长，无法与图片同页；请缩短图注，把详细说明移入正文")
    max_w = min(width * 635, positive_number(block.get("width_in", width / 1440), "width_in") * EMU_PER_INCH)
    max_h = min(available_h * 635, positive_number(block.get("height_in", available_h / 1440), "height_in") * EMU_PER_INCH)
    original_w, original_h = intrinsic
    scale = min(max_w / original_w, max_h / original_h, 1 if block.get("type") == "image" else float("inf"))
    return max(1, int(original_w * scale)), max(1, int(original_h * scale))


def image_block(block: dict, media: ImageMedia, refs: ReferenceList | None = None, *, width: int = CONTENT_W) -> str:
    """Embed an image inline. Self-contained: the bytes live in word/media/."""
    if not block.get("path"):
        raise ValueError("image 必须包含 path")
    entry = media.add(block["path"])
    notes = figure_notes(block, refs or ReferenceList([]))
    w_emu, h_emu = figure_size(block, width, notes, (entry["width"] * PIXEL_EMU, entry["height"] * PIXEL_EMU))
    docpr = media.next_docpr()
    drawing = (
        '<w:p><w:pPr>' + ('<w:keepNext/>' if notes else '') + '<w:keepLines/><w:spacing w:before="100" w:after="60" w:line="240" w:lineRule="auto"/><w:jc w:val="center"/></w:pPr><w:r><w:drawing>'
        '<wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{w_emu}" cy="{h_emu}"/>'
        '<wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="{docpr}" name="Picture {docpr}" descr="{xml_attr(block.get("alt") or block.get("caption") or "图片")}"/>'
        '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        '<a:graphic>'
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:pic>'
        f'<pic:nvPicPr><pic:cNvPr id="{docpr}" name="Picture {docpr}"/>'
        '<pic:cNvPicPr><a:picLocks noChangeAspect="1"/></pic:cNvPicPr></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{entry["rid"]}"/>'
        '<a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{w_emu}" cy="{h_emu}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        '</pic:pic></a:graphicData></a:graphic>'
        '</wp:inline></w:drawing></w:r></w:p>'
    )
    return drawing + notes_xml(notes)


def chart_block(block: dict, media: ImageMedia, refs: ReferenceList, *, width: int = CONTENT_W) -> str:
    entry = media.add_chart(block)
    notes = figure_notes(block, refs)
    if any(v is None for series in block.get("series", []) for v in series.get("values", [])):
        notes.append("注：空缺表示缺测，不代表 0。")
    if width < CONTENT_W and (len(block.get("categories", [])) > 6 or len(block.get("series", [])) > 2):
        raise ValueError("双栏图表最多 6 个类别、2 个系列；请改用全宽 chart")
    w_emu, h_emu = figure_size(block, width, notes, (width * 635, int(width * 635 * .58)))
    label_width = (w_emu / 635 - 900) / len(block["categories"]) - 160
    if any(sum(1 if unicodedata.east_asian_width(c) in "WF" else .55 for c in str(label)) * 220 > label_width for label in block["categories"]):
        raise ValueError("chart 类别标签在当前宽度下过密，请缩短标签、减少类别或改用全宽图")
    docpr = media.next_docpr()
    return (
        '<w:p><w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="120" w:after="60" w:line="240" w:lineRule="auto"/><w:jc w:val="center"/></w:pPr><w:r><w:drawing>'
        '<wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{w_emu}" cy="{h_emu}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="{docpr}" name="Chart {docpr}" descr="{xml_attr(block.get("alt") or block.get("caption") or "数据图表")}"/>'
        '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">'
        f'<c:chart xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" r:id="{entry["rid"]}"/>'
        '</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>' + notes_xml(notes)
    )


# --- OOXML part templates --------------------------------------------------

def content_types_xml(media_exts: set[str], chart_count: int = 0) -> str:
    defaults = (
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
    )
    for ext in sorted(media_exts):
        ctype = SUPPORTED_IMAGE_TYPES[ext]
        defaults += f'<Default Extension="{ext}" ContentType="{ctype}"/>'
    if chart_count:
        defaults += '<Default Extension="xlsx" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"/>'
        defaults += ''.join(f'<Override PartName="/word/charts/chart{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>' for i in range(1, chart_count + 1))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        f'{defaults}'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
        + ('<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
           if _T["brand_footer"] else "") +
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '</Types>'
    )


RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""


def doc_rels_xml(media: ImageMedia) -> str:
    rels = (
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>'
    )
    for e in media.items():
        rels += (
            f'<Relationship Id="{e["rid"]}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
            f'Target="{e["target"]}"/>'
        )
    for e in media.charts:
        rels += f'<Relationship Id="{e["rid"]}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="{e["target"]}"/>'
    for e in media.links:
        rels += f'<Relationship Id="{e["rid"]}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="{xml_attr(e["target"])}" TargetMode="External"/>'
    if _T["brand_footer"]:
        rels += '<Relationship Id="rIdFooter1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>'
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'{rels}</Relationships>'
    )


def styles_xml() -> str:
    t, z = _T, _sz
    en, cn = escape(t["font_en"]), escape(t["font_cn"])
    head_fonts = f'<w:rFonts w:eastAsia="{escape(t["font_heading_cn"])}"/>' if t["font_heading_cn"] else ""

    def rpr(color: str, size: int, bold: bool = False, heading: bool = False) -> str:
        return (f"<w:rPr>{head_fonts if heading else ''}{'<w:b/><w:bCs/>' if bold else ''}"
                f'<w:color w:val="{color}"/><w:sz w:val="{z(size)}"/><w:szCs w:val="{z(size)}"/></w:rPr>')
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults>
<w:rPrDefault><w:rPr>
<w:rFonts w:ascii="{en}" w:hAnsi="{en}" w:eastAsia="{cn}" w:cs="{en}"/>
<w:color w:val="{t["text"]}"/><w:sz w:val="{z(22)}"/><w:szCs w:val="{z(22)}"/><w:lang w:val="en-CN" w:eastAsia="zh-CN" w:bidi="ar-SA"/>
</w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:widowControl/><w:snapToGrid w:val="0"/><w:spacing w:after="140" w:line="300" w:lineRule="auto"/></w:pPr></w:pPrDefault>
</w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>
<w:basedOn w:val="Normal"/><w:next w:val="Normal"/>
<w:pPr><w:keepLines/><w:spacing w:before="80" w:after="160"/><w:jc w:val="center"/></w:pPr>
{rpr(t["title"], 48, True, True)}</w:style>
<w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/>
<w:basedOn w:val="Normal"/><w:next w:val="Normal"/>
<w:pPr><w:spacing w:after="120"/><w:jc w:val="center"/></w:pPr>
{rpr(t["subtitle"], 24)}</w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>
<w:basedOn w:val="Normal"/><w:next w:val="Normal"/>
<w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="320" w:after="140"/></w:pPr>
{rpr(t["h1"], 32, True, True)}</w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>
<w:basedOn w:val="Normal"/><w:next w:val="Normal"/>
<w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="260" w:after="100"/></w:pPr>
{rpr(t["h2"], 27, True, True)}</w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/>
<w:basedOn w:val="Normal"/><w:next w:val="Normal"/>
<w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="200" w:after="80"/></w:pPr>
{rpr(t["h3"], 23, True, True)}</w:style>
<w:style w:type="paragraph" w:styleId="Caption"><w:name w:val="caption"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:pPr><w:keepLines/><w:spacing w:before="70" w:after="50" w:line="264" w:lineRule="auto"/></w:pPr>{rpr(t["caption"], 20)}</w:style>
<w:style w:type="paragraph" w:styleId="SourceNote"><w:name w:val="Source Note"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:pPr><w:keepLines/><w:spacing w:before="20" w:after="70" w:line="264" w:lineRule="auto"/></w:pPr>{rpr(t["source"], 18)}</w:style>
<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:pPr><w:spacing w:before="60" w:after="140"/><w:ind w:left="320" w:right="320"/></w:pPr><w:rPr><w:color w:val="{t["quote"]}"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Bibliography"><w:name w:val="Bibliography"/><w:basedOn w:val="Normal"/><w:next w:val="Bibliography"/><w:pPr><w:keepLines/><w:spacing w:after="120" w:line="276" w:lineRule="auto"/><w:ind w:left="420" w:hanging="420"/></w:pPr>{rpr(t["biblio"], 19)}</w:style>
</w:styles>"""


def numbering_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:abstractNum w:abstractNumId="0">
<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/>
<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="480" w:hanging="240"/></w:pPr></w:lvl>
</w:abstractNum>
<w:abstractNum w:abstractNumId="1">
<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/>
<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="480" w:hanging="240"/></w:pPr></w:lvl>
</w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>
</w:numbering>"""


def core_xml(title: str, author: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:dcterms="http://purl.org/dc/terms/"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{escape(title)}</dc:title>"
        f"<dc:creator>{escape(author)}</dc:creator>"
        "<cp:lastModifiedBy>Medex</cp:lastModifiedBy>"
        "</cp:coreProperties>"
    )


def app_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        "<Application>Medex Word Report Generator</Application>"
        "<Company>Medex</Company>"
        "</Properties>"
    )


def build_document(spec: dict, media: ImageMedia):
    apply_style(spec.get("_style") or None)
    title = spec.get("title") or "报告"
    subtitle = spec.get("subtitle")
    author = spec.get("author") or ""
    meta = spec.get("meta") or {}
    blocks = spec.get("blocks", []) or []
    refs = ReferenceList(spec.get("references") or [])

    body: list[str] = []
    body.extend(title_paragraph(title, subtitle))
    if author or meta:
        body.extend(meta_paragraphs(author, meta))

    stats = {key: 0 for key in ("heading", "paragraph", "bullets", "numbered", "table", "quote", "code", "image", "chart", "columns", "page_break")}

    def render(blocks: list, width: int = CONTENT_W, *, in_columns: bool = False) -> str:
        if not isinstance(blocks, list):
            raise ValueError("blocks/columns left/right 必须是数组")
        output = []
        for block in blocks:
            if not isinstance(block, dict):
                raise ValueError("每个 block 必须是对象")
            btype = block.get("type")
            if btype == "text":
                btype = "paragraph"
            if btype not in stats:
                raise ValueError(f"不支持的 block type: {btype}")
            stats[btype] += 1
            marker = refs.marker(block.get("citations"))
            text = str(block.get("text", ""))
            if btype == "columns":
                if in_columns:
                    raise ValueError("不支持嵌套 columns，请把复杂内容拆成连续的双栏或全宽块")
                if marker or block.get("source") or block.get("caption"):
                    raise ValueError("columns 的 citations/source/caption 应放在对应的 left/right 子块上")
                grid_widths = column_widths(block.get("ratio", [1, 1]), 2, width - 320)
                cells = []
                for i, side in enumerate(("left", "right")):
                    cell_width = grid_widths[i]
                    cell_content = render(block.get(side, []), cell_width - 160, in_columns=True)
                    cells.append(f'<w:tc><w:tcPr><w:tcW w:w="{cell_width}" w:type="dxa"/><w:tcMar><w:top w:w="80" w:type="dxa"/><w:left w:w="80" w:type="dxa"/><w:bottom w:w="80" w:type="dxa"/><w:right w:w="80" w:type="dxa"/></w:tcMar><w:vAlign w:val="top"/></w:tcPr>{cell_content}<w:p><w:pPr><w:spacing w:after="0" w:line="20" w:lineRule="exact"/></w:pPr></w:p></w:tc>')
                gap = '<w:tc><w:tcPr><w:tcW w:w="320" w:type="dxa"/></w:tcPr><w:p/></w:tc>'
                borders = ''.join(f'<w:{edge} w:val="nil"/>' for edge in ("top", "left", "bottom", "right", "insideH", "insideV"))
                # The outer row intentionally CAN split: long prose must continue
                # onto following pages, while each figure keeps its own notes.
                output.append(f'<w:tbl><w:tblPr><w:tblW w:w="{width}" w:type="dxa"/><w:tblBorders>{borders}</w:tblBorders><w:tblLayout w:type="fixed"/></w:tblPr><w:tblGrid><w:gridCol w:w="{grid_widths[0]}"/><w:gridCol w:w="320"/><w:gridCol w:w="{grid_widths[1]}"/></w:tblGrid><w:tr>{cells[0]}{gap}{cells[1]}</w:tr></w:tbl>')
                output.append(paragraph("", spacing={"after": "80"}))
            elif btype == "heading":
                output.append(heading(text, int(block.get("level", 1)), citation=marker))
            elif btype == "paragraph":
                output.append(paragraph(text, citation=marker))
            elif btype in ("bullets", "numbered"):
                items = block.get("items", [])
                if not isinstance(items, list):
                    raise ValueError("bullets/numbered items 必须是数组")
                for i, item in enumerate(items):
                    citation = marker if i == len(items) - 1 else ""
                    output.append(list_item(str(item), ordered=btype == "numbered", num_id=2 if btype == "numbered" else 1, citation=citation))
            elif btype == "table":
                output.append(notes_xml(figure_notes(block, refs), keep_next=True))
                output.append(table(block.get("headers", []) or [], block.get("rows", []) or [], width=width, widths=block.get("column_widths"), align=block.get("align")))
                output.append(paragraph("", spacing={"after": "80"}))
            elif btype == "quote":
                output.append(quote(text, citation=marker))
            elif btype == "code":
                output.append(code_block(text, citation=marker))
            elif btype == "image":
                output.append(image_block(block, media, refs, width=width))
            elif btype == "chart":
                output.append(chart_block(block, media, refs, width=width))
            elif btype == "page_break":
                if in_columns:
                    raise ValueError("columns 中不支持 page_break；请在 columns 块前后分页")
                if marker or block.get("source"):
                    raise ValueError("page_break 不承载来源或 citations，请放在相应内容块")
                output.append(page_break())
            if btype in ("heading", "paragraph", "bullets", "numbered", "quote", "code") and block.get("source"):
                output.append(notes_xml(["来源：" + str(block["source"])]))
        return ''.join(output)

    body.append(render(blocks))
    if refs.entries:
        body.append(heading(str(spec.get("references_title") or "参考文献"), 1))
        for number, item in enumerate(refs.entries, 1):
            ref_body = xrun(f"[{number}] ", bold=True, size=_sz(19), color=_T["cite"]) + xrun(refs.text(item), size=_sz(19))
            url = "https://doi.org/" + item["doi"] if item.get("doi") else item.get("url")
            if url:
                rid = media.add_link(url)
                label = "doi:" + item["doi"] if item.get("doi") else url
                ref_body += f'<w:hyperlink r:id="{rid}"><w:r><w:rPr><w:color w:val="{_T["link"]}"/><w:sz w:val="{_sz(19)}"/></w:rPr><w:t xml:space="preserve"> {escape(label)}</w:t></w:r></w:hyperlink>'
            body.append('<w:p><w:pPr><w:pStyle w:val="Bibliography"/></w:pPr>' + ref_body + '</w:p>')

    sect_pr = (
        "<w:sectPr>"
        + ('<w:footerReference w:type="default" r:id="rIdFooter1"/>' if _T["brand_footer"] else "") +
        f'<w:pgSz w:w="{PAGE_W}" w:h="{PAGE_H}"/>'
        f'<w:pgMar w:top="{MARGIN}" w:right="{MARGIN}" w:bottom="{MARGIN}" w:left="{MARGIN}"'
        ' w:header="720" w:footer="720" w:gutter="0"/>'
        '<w:cols w:space="720"/><w:docGrid w:linePitch="360"/>'
        "</w:sectPr>"
    )
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        ' xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
        ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
        ' xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<w:body>{"".join(body)}{sect_pr}</w:body>'
        "</w:document>"
    )
    return doc, stats, title, author


def _sanitize_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name)).strip("_")
    return name or "report"


def resolve_output(args, spec: dict, src_path: Path, ext: str) -> Path:
    """Resolve output path: /repo/o_outcome/{name}_{yyyy}_{mm}_{dd}_v{N}.{ext}.

    Honors explicit --out. Otherwise derives name from spec title or src stem,
    uses --date (default today), and auto-increments version to avoid collision
    unless --version is given (collision then requires --force).
    """
    if args.out:
        out = args.out.expanduser().resolve()
        if out.exists() and not args.force:
            raise FileExistsError(f"output exists (use --force to overwrite): {out}")
        return out

    name = _sanitize_name(args.name or spec.get("title") or src_path.stem)
    date = args.date or datetime.now().strftime("%Y-%m-%d")
    date_tag = date.replace("-", "_")
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.version is not None:
        version = args.version
        out = out_dir / f"{name}_{date_tag}_v{version}.{ext}"
        if out.exists() and not args.force:
            raise FileExistsError(f"output exists (use --force to overwrite): {out}")
        return out
    version = _next_version(out_dir, name, date_tag, ext)
    return out_dir / f"{name}_{date_tag}_v{version}.{ext}"


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
    ap = argparse.ArgumentParser(description="Generate a .docx report from a JSON spec.")
    ap.add_argument("--spec", type=Path, required=True, help="JSON spec file path")
    ap.add_argument("--out", type=Path, help="Explicit output .docx path (overrides auto naming)")
    ap.add_argument("--name", help="Report base name; default: spec title or spec stem")
    ap.add_argument("--version", type=int, help="Version number; default: auto-increment to avoid clash")
    ap.add_argument("--out-dir", type=Path, default=Path("/repo/o_outcome"),
                    help="Output directory (default: /repo/o_outcome)")
    ap.add_argument("--date", help="Date string YYYY-MM-DD (default: today)")
    ap.add_argument("--title", help="Override title")
    ap.add_argument("--author", help="Override author")
    ap.add_argument("--force", action="store_true", help="Overwrite existing output")
    args = ap.parse_args()

    try:
        spec_path = args.spec.expanduser().resolve(strict=True)
        with spec_path.open("r", encoding="utf-8") as f:
            spec = json.load(f)
        if not isinstance(spec, dict):
            raise ValueError("spec 顶层必须是 JSON 对象")
        if args.title:
            spec["title"] = args.title
        if args.author:
            spec["author"] = args.author

        out = resolve_output(args, spec, spec_path, "docx")
        media = ImageMedia(spec_path.parent)
        document_xml, stats, title, author = build_document(spec, media)
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w" if args.force else "x", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", content_types_xml(media.extensions(), len(media.charts)))
            z.writestr("_rels/.rels", RELS)
            z.writestr("word/_rels/document.xml.rels", doc_rels_xml(media))
            z.writestr("word/document.xml", document_xml)
            z.writestr("word/styles.xml", styles_xml())
            z.writestr("word/numbering.xml", numbering_xml())
            if _T["brand_footer"]:
                z.writestr("word/footer1.xml", footer_xml())
            for e in media.items():
                z.writestr(f"word/{e['target']}", e["bytes"])
            for e in media.charts:
                num = e["num"]
                z.writestr(f"word/{e['target']}", e["xml"])
                z.writestr(f"word/embeddings/chart{num}.xlsx", e["workbook"])
                z.writestr(f"word/charts/_rels/chart{num}.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/package" Target="../embeddings/chart' + str(num) + '.xlsx"/></Relationships>')
            z.writestr("docProps/core.xml", core_xml(title, author))
            z.writestr("docProps/app.xml", app_xml())

        print(f"Output: {out}")
        print(f"Blocks: {sum(stats.values())} "
              f"(heading {stats['heading']}, paragraph {stats['paragraph']}, "
              f"bullets {stats['bullets']}, numbered {stats['numbered']}, "
              f"table {stats['table']}, quote {stats['quote']}, "
              f"code {stats['code']}, image {stats['image']}, chart {stats['chart']}, columns {stats['columns']}, "
              f"page_break {stats['page_break']})")
        print(f"Images: {len(media.items())} embedded")
        print(f"Charts: {len(media.charts)} embedded with editable workbooks")
        print(f"References: {len(ReferenceList(spec.get('references') or []).entries)}")
        print(f"Size: {out.stat().st_size} bytes")
        return 0
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
