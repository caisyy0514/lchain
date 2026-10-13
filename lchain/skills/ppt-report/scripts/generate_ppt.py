#!/usr/bin/env python3
"""Generate a valid .pptx (Office Open XML Presentation) from a JSON spec.

Builds OOXML Transitional parts with the Python standard library, then runs
offline ECMA-376 XSD and Microsoft Open XML SDK validation before publishing
the file. No python-pptx dependency. 16:9 slides. Targets PowerPoint 2016
through Microsoft 365. Supports mixed text/images/tables, editable shape charts,
stable numbered references, automatic continuation slides, and speaker notes.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import posixpath
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
import uuid
import zipfile
import zlib
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

# EMU: 914400 per inch. 16:9 slide = 13.333in x 7.5in.
SLIDE_W = 12192000
SLIDE_H = 6858000
EMU_IN = 914400
PIXEL_EMU = EMU_IN // 96  # 9525 EMU per pixel at 96 DPI

# Explicit East Asian and Latin fonts; Office performs system fallback if unavailable.
CN_FONT = "微软雅黑"
EN_FONT = "Calibri"
SUPPORTED_IMAGE_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
}

# Academic / medical palette: deep navy ink, teal accent, muted gold.
_TEXT_COLOR = "253746"   # body text
_INK = "162D40"          # slide titles (deep navy ink)
_MUTED_COLOR = "617180"  # secondary / captions / footer
_PRIMARY = "19364D"      # deep academic navy: section band, footer, table header
_ACCENT = "007F7A"       # teal: header rules, bullet markers, emphasis
_GOLD = "B58948"         # muted academic gold: title rule, section accent
_BORDER = "DAE3E8"       # subtle horizontal rules
_ALT = "F5F8FA"          # zebra rows, chart value bands

# [LChain] 自定义样式：默认值即上面的 medex 原版配色；spec._style（app/agent/report_styles.py 解析结果）
#   存在时由 apply_style() 覆盖这些模块级颜色/字体。版式尺寸、字号与排版测量不变。
_BACKGROUND = "FFFFFF"
_ON_PRIMARY = "FFFFFF"
_COVER_BAND = False
_TABLE_STYLE = "header_fill"
_BRAND_LABEL = ""
_PRIMARY_TEXT = _PRIMARY  # 主色用作文字时的颜色（深色背景下自动换成标题色）
_DEFAULT_STYLE_STATE = None
_PPT_FONT_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff \-]{0,39}$")


def apply_style(style: dict | None) -> None:
    """设置本次生成的配色与字体；None 恢复 medex 原版默认。"""
    global _TEXT_COLOR, _INK, _MUTED_COLOR, _PRIMARY, _ACCENT, _GOLD, _BORDER, _ALT, _CHART_COLORS
    global CN_FONT, EN_FONT, _BACKGROUND, _ON_PRIMARY, _COVER_BAND, _TABLE_STYLE, _BRAND_LABEL, _DEFAULT_STYLE_STATE
    global _PRIMARY_TEXT
    names = ("_TEXT_COLOR", "_INK", "_MUTED_COLOR", "_PRIMARY", "_ACCENT", "_GOLD", "_BORDER", "_ALT",
             "_CHART_COLORS", "CN_FONT", "EN_FONT", "_BACKGROUND", "_ON_PRIMARY", "_COVER_BAND", "_TABLE_STYLE",
             "_BRAND_LABEL", "_PRIMARY_TEXT")
    g = globals()
    if _DEFAULT_STYLE_STATE is None:
        _DEFAULT_STYLE_STATE = {n: g[n] for n in names}
    g.update(_DEFAULT_STYLE_STATE)
    if not style:
        return
    colors = style.get("colors") or {}
    for key in ("primary", "accent", "text", "heading", "muted", "border", "alt", "highlight", "background",
                "on_primary"):
        if not re.fullmatch(r"[0-9A-Fa-f]{6}", str(colors.get(key, ""))):
            raise ValueError(f"style.colors.{key} 无效: {colors.get(key)!r}")
    c = {k: str(v).upper() for k, v in colors.items() if k != "chart"}
    fonts = style.get("fonts") or {}
    for key in ("cn", "en"):
        if fonts.get(key) and not _PPT_FONT_RE.match(str(fonts[key])):
            raise ValueError(f"style.fonts.{key} 无效: {fonts[key]!r}")
    chart = tuple(str(x).upper() for x in colors.get("chart", []) if re.fullmatch(r"[0-9A-Fa-f]{6}", str(x)))
    _TEXT_COLOR, _INK, _MUTED_COLOR, _PRIMARY = c["text"], c["heading"], c["muted"], c["primary"]
    _ACCENT, _GOLD, _BORDER, _ALT = c["accent"], c["highlight"], c["border"], c["alt"]
    _BACKGROUND, _ON_PRIMARY = c["background"], c["on_primary"]
    _PRIMARY_TEXT = c.get("primary_text") or _PRIMARY
    _CHART_COLORS = (chart + _DEFAULT_STYLE_STATE["_CHART_COLORS"])[:4] if len(chart) < 4 else chart[:4]
    CN_FONT = str(fonts.get("cn") or CN_FONT)
    EN_FONT = str(fonts.get("en") or EN_FONT)
    _COVER_BAND = style.get("cover") == "band"
    _TABLE_STYLE = style.get("table") if style.get("table") in ("three_line", "grid") else "header_fill"
    _BRAND_LABEL = str((style.get("brand") or {}).get("label") or "")


def theme_xml(name: str = "Medex") -> str:
    """按当前配色与字体生成主题部件（默认样式下与原版 THEME 等价）。"""
    en, cn = xml_attr(EN_FONT), xml_attr(CN_FONT)
    c3, c4, c5 = (list(_CHART_COLORS) + ["3869A8", "B87924", "8461A0"])[1:4]
    return (THEME.replace('name="Medex"', f'name="{name}"')
            .replace('<a:accent1><a:srgbClr val="19364D"/>', f'<a:accent1><a:srgbClr val="{_PRIMARY}"/>')
            .replace('<a:accent2><a:srgbClr val="007F7A"/>', f'<a:accent2><a:srgbClr val="{_ACCENT}"/>')
            .replace('<a:accent3><a:srgbClr val="3869A8"/>', f'<a:accent3><a:srgbClr val="{c3}"/>')
            .replace('<a:accent4><a:srgbClr val="B87924"/>', f'<a:accent4><a:srgbClr val="{c4}"/>')
            .replace('<a:accent5><a:srgbClr val="8461A0"/>', f'<a:accent5><a:srgbClr val="{c5}"/>')
            .replace('<a:accent6><a:srgbClr val="617180"/>', f'<a:accent6><a:srgbClr val="{_MUTED_COLOR}"/>')
            .replace('<a:hlink><a:srgbClr val="007F7A"/>', f'<a:hlink><a:srgbClr val="{_ACCENT}"/>')
            .replace('<a:latin typeface="Calibri"/>', f'<a:latin typeface="{en}"/>')
            .replace('<a:cs typeface="Calibri"/>', f'<a:cs typeface="{en}"/>')
            .replace('<a:ea typeface="微软雅黑"/>', f'<a:ea typeface="{cn}"/>')
            .replace('<a:bgFillStyleLst><a:solidFill><a:srgbClr val="FFFFFF"/>',
                     f'<a:bgFillStyleLst><a:solidFill><a:srgbClr val="{_BACKGROUND}"/>'))


def _slide_background() -> str:
    """非白色背景时为每页写显式背景（主题 bg1 是系统窗口色，不能跟随样式）。"""
    if _BACKGROUND == "FFFFFF":
        return ""
    return f'<p:bg><p:bgPr><a:solidFill><a:srgbClr val="{_BACKGROUND}"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>'


# ECMA-376 ST_TextAnchoringType uses "ctr" for vertical centering (not "m").
_VALID_TEXT_ANCHORS = frozenset({"t", "ctr", "b", "just", "dist"})

_XSD_DIR = Path(__file__).resolve().parent.parent / "schemas" / "ecma-376-5th"
_XSD_VALIDATION_TIMEOUT_SECONDS = 120
_XSD_VALIDATION_BATCH_SIZE = 200
_XMLLINT_VALIDATOR = Path(os.environ.get("PPT_REPORT_XMLLINT") or "/usr/bin/xmllint")
_OPENXML_SDK_VALIDATION_TIMEOUT_SECONDS = 120
# AGSX images must install a self-contained, root-managed validator at this
# exact path.  It must invoke DocumentFormat.OpenXml.Validation.OpenXmlValidator
# and accept: --target Office2016 --file <absolute-pptx-path>.  Deliberately do
# not resolve this executable via PATH.
# [LChain] 可用环境变量 PPT_REPORT_OPENXML_SDK_VALIDATOR 指定路径（仍要求 root 所有、不可被他人写入）；
#   PPT_REPORT_SDK_VALIDATION=optional 时，校验器未安装则跳过该项（输出中会如实注明）。
_OPENXML_SDK_VALIDATOR = Path(os.environ.get("PPT_REPORT_OPENXML_SDK_VALIDATOR")
                              or "/usr/local/bin/medex-openxml-sdk-validator")
_SDK_VALIDATION_MODE = os.environ.get("PPT_REPORT_SDK_VALIDATION", "required").strip().lower()
# [LChain] root（默认，与 medex 沙箱一致）：校验器及所有父目录必须 root 所有；
#   user：本地开发机没有 root 权限时，允许当前用户所有、不可被他人写入的校验器。
_SDK_VALIDATOR_OWNER = os.environ.get("PPT_REPORT_SDK_VALIDATOR_OWNER", "root").strip().lower()
_VALIDATOR_ENV = {
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
}
_OUTPUT_RESERVATION_ATTEMPTS = 1000
# [LChain] 工作区根目录 / 输出目录；默认保持 medex 沙箱的 /repo 与 /repo/o_outcome，
#   可用 REPORT_REPO_ROOT / REPORT_OUTPUT_DIR 覆盖（输出目录必须在工作区内）。
_REPO_ROOT = Path(os.environ.get("REPORT_REPO_ROOT") or "/repo")
_OUTPUT_ROOT = Path(os.environ.get("REPORT_OUTPUT_DIR") or (_REPO_ROOT / "o_outcome"))
_MAX_SPEC_BYTES = 4 * 1024 * 1024
_MAX_IMAGE_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_MEDIA_BYTES = 256 * 1024 * 1024
_MAX_SLIDES = 200
_BODY_X, _BODY_Y = 685800, 1280160
_BODY_W, _BODY_H = 10820400, 4819840
_BLOCK_GAP = 137160
_PAGE_REFERENCE_SIZE = 1300
_PAGE_REFERENCE_MAX_H = 1828800
_CITE_START, _CITE_END = "\ue000", "\ue001"
_CITE_RE = re.compile(f"({_CITE_START}[^{_CITE_END}]*{_CITE_END})")
_CHART_COLORS = (_ACCENT, "3869A8", "B87924", "8461A0")
_CHART_MARKERS = ("ellipse", "diamond", "rect", "triangle")
_CHART_DASHES = ("solid", "dash", "sysDot", "lgDashDot")
_PML_PART_RE = re.compile(
    r"ppt/(?:presentation\.xml|presProps\.xml|viewProps\.xml|"
    r"slideMasters/slideMaster\d+\.xml|"
    r"slideLayouts/slideLayout\d+\.xml|"
    r"slides/slide\d+\.xml|"
    r"notesMasters/notesMaster\d+\.xml|"
    r"notesSlides/notesSlide\d+\.xml)"
)


_XML_INVALID_RE = re.compile(
    "[^\\x09\\x0A\\x0D\\x20-\\uD7FF\\uE000-\\uFFFD"
    "\\U00010000-\\U0010FFFF]"
)


def xml_text(value) -> str:
    """Escape text after removing characters forbidden by XML 1.0."""
    return escape(_XML_INVALID_RE.sub("", str(value)))


def xml_attr(value) -> str:
    return escape(_XML_INVALID_RE.sub("", str(value)), {'"': "&quot;"})


# --- image embedding helpers ----------------------------------------------

def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Parse JPEG SOF marker for native pixel size; fall back to (800, 600)."""
    i = 2
    n = len(data)
    while i + 8 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        while i + 1 < n and data[i + 1] == 0xFF:
            i += 1
        marker = data[i + 1] if i + 1 < n else 0
        i += 2
        if marker in (0xD9, 0xDA):
            break
        if 0xD0 <= marker <= 0xD7:
            continue
        if i + 4 > n:
            break
        seg = int.from_bytes(data[i:i + 2], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = int.from_bytes(data[i + 3:i + 5], "big")
            w = int.from_bytes(data[i + 5:i + 7], "big")
            return w or 800, h or 600
        i += seg
    return 800, 600


def _valid_png(data: bytes) -> bool:
    if len(data) < 45 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    position = 8
    saw_ihdr = False
    saw_idat = False
    saw_plte = False
    finished = False
    width = height = bit_depth = color_type = interlace = 0
    idat_chunks: list[bytes] = []
    while position + 12 <= len(data):
        length = int.from_bytes(data[position:position + 4], "big")
        chunk_end = position + 12 + length
        if chunk_end > len(data):
            return False
        chunk_type = data[position + 4:position + 8]
        payload = data[position + 8:position + 8 + length]
        stored_crc = int.from_bytes(data[position + 8 + length:chunk_end], "big")
        calculated_crc = zlib.crc32(chunk_type)
        calculated_crc = zlib.crc32(payload, calculated_crc) & 0xFFFFFFFF
        if stored_crc != calculated_crc:
            return False
        if not saw_ihdr:
            if chunk_type != b"IHDR" or length != 13:
                return False
            width = int.from_bytes(payload[0:4], "big")
            height = int.from_bytes(payload[4:8], "big")
            bit_depth, color_type = payload[8], payload[9]
            allowed_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if width <= 0 or height <= 0 or bit_depth not in allowed_depths.get(
                color_type, set()
            ):
                return False
            interlace = payload[12]
            if payload[10] != 0 or payload[11] != 0 or interlace not in (0, 1):
                return False
            saw_ihdr = True
        elif chunk_type == b"IHDR":
            return False
        if chunk_type == b"PLTE":
            if saw_idat or length == 0 or length % 3 or length > 768:
                return False
            saw_plte = True
        if chunk_type == b"IDAT":
            saw_idat = True
            idat_chunks.append(payload)
        if chunk_type == b"IEND":
            finished = length == 0 and chunk_end == len(data)
            break
        position = chunk_end
    if not (finished and saw_ihdr and saw_idat) or (color_type == 3 and not saw_plte):
        return False

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    passes = (
        ((0, 0, 1, 1),)
        if interlace == 0 else
        (
            (0, 0, 8, 8),
            (4, 0, 8, 8),
            (0, 4, 4, 8),
            (2, 0, 4, 4),
            (0, 2, 2, 4),
            (1, 0, 2, 2),
            (0, 1, 1, 2),
        )
    )
    pass_sizes: list[tuple[int, int]] = []
    expected_size = 0
    for start_x, start_y, step_x, step_y in passes:
        pass_width = 0 if width <= start_x else (width - start_x + step_x - 1) // step_x
        pass_height = 0 if height <= start_y else (height - start_y + step_y - 1) // step_y
        if pass_width == 0 or pass_height == 0:
            continue
        row_size = (pass_width * channels * bit_depth + 7) // 8
        expected_size += pass_height * (row_size + 1)
        pass_sizes.append((pass_height, row_size))
    if expected_size <= 0 or expected_size > 128 * 1024 * 1024:
        return False
    try:
        decompressor = zlib.decompressobj()
        pixels = decompressor.decompress(b"".join(idat_chunks), expected_size + 1)
    except zlib.error:
        return False
    if len(pixels) != expected_size or not decompressor.eof \
            or decompressor.unused_data or decompressor.unconsumed_tail:
        return False
    offset = 0
    for pass_height, row_size in pass_sizes:
        for _ in range(pass_height):
            if pixels[offset] > 4:
                return False
            offset += row_size + 1
    return offset == len(pixels)


def _valid_jpeg(data: bytes) -> bool:
    if len(data) < 16 or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        return False
    # Limit embedded JPEGs to the Huffman-coded sequential/progressive formats
    # consistently supported by Office and browser preview engines.
    sof_markers = {0xC0, 0xC1, 0xC2}
    position = 2
    saw_sof = False
    saw_sos = False
    in_scan = False
    scan_has_data = False
    frame_marker = 0
    frame_precision = 0
    frame_components: dict[int, int] = {}
    quantization_tables: set[int] = set()
    huffman_tables: dict[tuple[int, int], bytes] = {}
    scanned_components: set[int] = set()
    while position < len(data):
        if in_scan:
            while position < len(data) and data[position] != 0xFF:
                scan_has_data = True
                position += 1
            if position >= len(data):
                return False
        if data[position] != 0xFF:
            return False
        marker_start = position
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            return False
        marker = data[position]
        position += 1
        if in_scan:
            if marker == 0x00 or 0xD0 <= marker <= 0xD7:
                scan_has_data = True
                continue
            if not scan_has_data:
                return False
        in_scan = False
        if marker == 0xD9:
            return (
                saw_sof
                and saw_sos
                and scanned_components == set(frame_components)
                and position == len(data)
            )
        if marker in (0x00, 0xD8):
            return False
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if position + 2 > len(data):
            return False
        segment_length = int.from_bytes(data[position:position + 2], "big")
        if segment_length < 2 or position + segment_length > len(data):
            return False
        segment_end = position + segment_length
        if marker == 0xDB:
            cursor = position + 2
            while cursor < segment_end:
                table_info = data[cursor]
                cursor += 1
                precision, table_id = table_info >> 4, table_info & 0x0F
                if precision not in (0, 1) or table_id > 3:
                    return False
                table_size = 64 * (precision + 1)
                if cursor + table_size > segment_end:
                    return False
                quantization_tables.add(table_id)
                cursor += table_size
            if cursor != segment_end:
                return False
        if marker == 0xC4:
            cursor = position + 2
            while cursor < segment_end:
                table_info = data[cursor]
                cursor += 1
                table_class, table_id = table_info >> 4, table_info & 0x0F
                if table_class not in (0, 1) or table_id > 3 or cursor + 16 > segment_end:
                    return False
                code_counts = data[cursor:cursor + 16]
                cursor += 16
                symbol_count = sum(code_counts)
                if symbol_count == 0 or symbol_count > 256 \
                        or cursor + symbol_count > segment_end:
                    return False
                available_codes = 1
                for count in code_counts:
                    available_codes = available_codes * 2 - count
                    if available_codes < 0:
                        return False
                symbols = data[cursor:cursor + symbol_count]
                if table_class == 0 and any(symbol > 15 for symbol in symbols):
                    return False
                if table_class == 1 and any(
                    (symbol & 0x0F) > 14
                    or ((symbol & 0x0F) == 0 and symbol not in (0x00, 0xF0))
                    for symbol in symbols
                ):
                    return False
                huffman_tables[(table_class, table_id)] = symbols
                cursor += symbol_count
            if cursor != segment_end:
                return False
        if marker in sof_markers:
            if saw_sof or segment_length < 11:
                return False
            height = int.from_bytes(data[position + 3:position + 5], "big")
            width = int.from_bytes(data[position + 5:position + 7], "big")
            component_count = data[position + 7]
            if width <= 0 or height <= 0 or not 1 <= component_count <= 4 \
                    or segment_length != 8 + 3 * component_count:
                return False
            frame_precision = data[position + 2]
            if frame_precision not in (8, 12):
                return False
            components: dict[int, int] = {}
            for component_index in range(component_count):
                component_offset = position + 8 + component_index * 3
                component_id = data[component_offset]
                sampling = data[component_offset + 1]
                quantization_id = data[component_offset + 2]
                horizontal, vertical = sampling >> 4, sampling & 0x0F
                if component_id in components or not 1 <= horizontal <= 4 \
                        or not 1 <= vertical <= 4 or quantization_id > 3:
                    return False
                components[component_id] = quantization_id
            frame_marker = marker
            frame_components = components
            saw_sof = True
        if marker == 0xDA:
            scan_components = data[position + 2] if segment_length >= 3 else 0
            if not saw_sof or not 1 <= scan_components <= len(frame_components) \
                    or segment_length != 6 + 2 * scan_components:
                return False
            selectors: set[int] = set()
            for component_index in range(scan_components):
                component_offset = position + 3 + component_index * 2
                component_id = data[component_offset]
                table_selector = data[component_offset + 1]
                dc_table, ac_table = table_selector >> 4, table_selector & 0x0F
                if component_id not in frame_components or component_id in selectors \
                        or dc_table > 3 or ac_table > 3:
                    return False
                selectors.add(component_id)
            spectral_start = data[position + 3 + 2 * scan_components]
            spectral_end = data[position + 4 + 2 * scan_components]
            approximation = data[position + 5 + 2 * scan_components]
            successive_high, successive_low = approximation >> 4, approximation & 0x0F
            if spectral_start > spectral_end or spectral_end > 63 \
                    or successive_high > 13 or successive_low > 13:
                return False
            if frame_marker in (0xC0, 0xC1) and (
                spectral_start != 0 or spectral_end != 63 or approximation != 0
            ):
                return False
            if frame_marker == 0xC2 and spectral_start > 0 and scan_components != 1:
                return False
            for component_index in range(scan_components):
                table_selector = data[position + 4 + component_index * 2]
                dc_table, ac_table = table_selector >> 4, table_selector & 0x0F
                if spectral_start == 0 and (0, dc_table) not in huffman_tables:
                    return False
                if spectral_end > 0 and (1, ac_table) not in huffman_tables:
                    return False
                dc_symbols = huffman_tables.get((0, dc_table), b"")
                ac_symbols = huffman_tables.get((1, ac_table), b"")
                max_dc_category = 11 if frame_precision == 8 else 15
                max_ac_size = 10 if frame_precision == 8 else 14
                if spectral_start == 0 and any(
                    symbol > max_dc_category for symbol in dc_symbols
                ):
                    return False
                if spectral_end > 0 and any(
                    (symbol & 0x0F) > max_ac_size for symbol in ac_symbols
                ):
                    return False
            if any(
                frame_components[component_id] not in quantization_tables
                for component_id in selectors
            ):
                return False
            scanned_components.update(selectors)
            saw_sos = True
            in_scan = True
            scan_has_data = False
        position = segment_end
        if position <= marker_start:
            return False
    return False


def _gif_sub_blocks(data: bytes, position: int) -> tuple[int, bytes] | None:
    payload = bytearray()
    while position < len(data):
        size = data[position]
        position += 1
        if size == 0:
            return position, bytes(payload)
        if position + size > len(data):
            return None
        payload.extend(data[position:position + size])
        position += size
    return None


def _valid_gif_lzw(minimum_code_size: int, data: bytes, pixel_count: int) -> bool:
    clear_code = 1 << minimum_code_size
    end_code = clear_code + 1
    table: list[bytes] = []
    code_size = minimum_code_size + 1
    next_code = end_code + 1
    bit_position = 0
    previous: bytes | None = None
    output_count = 0

    def reset_table() -> None:
        nonlocal table, code_size, next_code, previous
        table = [bytes([value]) for value in range(clear_code)] + [b"", b""]
        code_size = minimum_code_size + 1
        next_code = end_code + 1
        previous = None

    def read_code() -> int | None:
        nonlocal bit_position
        if bit_position + code_size > len(data) * 8:
            return None
        code = 0
        for bit in range(code_size):
            byte_index = (bit_position + bit) // 8
            bit_index = (bit_position + bit) % 8
            code |= ((data[byte_index] >> bit_index) & 1) << bit
        bit_position += code_size
        return code

    reset_table()
    # Every code consumes at least ``minimum_code_size + 1`` bits. Bounding
    # iterations by the payload size prevents malformed GIF data from keeping
    # validation alive indefinitely while still allowing every possible code.
    max_codes = len(data) * 8 // (minimum_code_size + 1)
    for _ in range(max_codes):
        code = read_code()
        if code is None:
            return False
        if code == clear_code:
            reset_table()
            continue
        if code == end_code:
            return output_count == pixel_count
        if code < len(table) and table[code]:
            entry = table[code]
        elif code == next_code and previous is not None:
            entry = previous + previous[:1]
        else:
            return False
        output_count += len(entry)
        if output_count > pixel_count:
            return False
        if previous is not None and next_code < 4096:
            table.append(previous + entry[:1])
            next_code += 1
            if next_code == 1 << code_size and code_size < 12:
                code_size += 1
        previous = entry
    return False


def _valid_gif(data: bytes) -> bool:
    if len(data) < 14 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return False
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    if width <= 0 or height <= 0:
        return False
    packed = data[10]
    position = 13
    if packed & 0x80:
        position += 3 * (2 ** ((packed & 0x07) + 1))
    saw_image = False
    while position < len(data):
        introducer = data[position]
        if introducer == 0x3B:
            return saw_image and position + 1 == len(data)
        if introducer == 0x21:
            if position + 2 > len(data):
                return False
            blocks = _gif_sub_blocks(data, position + 2)
            position = blocks[0] if blocks is not None else -1
        elif introducer == 0x2C:
            if position + 10 > len(data):
                return False
            image_width = int.from_bytes(data[position + 5:position + 7], "little")
            image_height = int.from_bytes(data[position + 7:position + 9], "little")
            if image_width <= 0 or image_height <= 0:
                return False
            local_packed = data[position + 9]
            position += 10
            if local_packed & 0x80:
                position += 3 * (2 ** ((local_packed & 0x07) + 1))
            if position >= len(data) or not 2 <= data[position] <= 8:
                return False
            minimum_code_size = data[position]
            blocks = _gif_sub_blocks(data, position + 1)
            if blocks is None or not _valid_gif_lzw(
                minimum_code_size, blocks[1], image_width * image_height
            ):
                return False
            position = blocks[0]
            saw_image = True
        else:
            return False
        if position < 0 or position > len(data):
            return False
    return False


def _valid_bmp_rle(
    data: bytes,
    start: int,
    end: int,
    width: int,
    height: int,
    bits_per_pixel: int,
) -> bool:
    position = start
    x = y = 0
    wrote_pixels = False
    while position + 2 <= end:
        count, command = data[position], data[position + 1]
        position += 2
        if count:
            if y >= height or x + count > width:
                return False
            x += count
            wrote_pixels = True
            continue
        if command == 0:  # end of line
            x = 0
            y += 1
            if y > height:
                return False
            continue
        if command == 1:  # end of bitmap
            return wrote_pixels and y <= height
        if command == 2:  # delta
            if position + 2 > end:
                return False
            x += data[position]
            y += data[position + 1]
            position += 2
            if x > width or y >= height:
                return False
            continue

        pixel_count = command
        packed_size = (
            pixel_count if bits_per_pixel == 8 else (pixel_count + 1) // 2
        )
        padded_size = packed_size + (packed_size & 1)
        if position + padded_size > end or y >= height or x + pixel_count > width:
            return False
        x += pixel_count
        position += padded_size
        wrote_pixels = True
    return False


def _valid_bmp(data: bytes) -> bool:
    if len(data) < 26 or not data.startswith(b"BM"):
        return False
    file_size = int.from_bytes(data[2:6], "little")
    pixel_offset = int.from_bytes(data[10:14], "little")
    dib_size = int.from_bytes(data[14:18], "little")
    if file_size < pixel_offset or file_size > len(data) or pixel_offset >= len(data):
        return False
    if dib_size == 12:
        if len(data) < 26:
            return False
        width = int.from_bytes(data[18:20], "little")
        height = int.from_bytes(data[20:22], "little")
        planes = int.from_bytes(data[22:24], "little")
        bits_per_pixel = int.from_bytes(data[24:26], "little")
        compression = 0
    elif dib_size >= 40:
        if len(data) < 14 + dib_size:
            return False
        width = int.from_bytes(data[18:22], "little", signed=True)
        height = int.from_bytes(data[22:26], "little", signed=True)
        planes = int.from_bytes(data[26:28], "little")
        bits_per_pixel = int.from_bytes(data[28:30], "little")
        compression = int.from_bytes(data[30:34], "little")
    else:
        return False
    if width <= 0 or height == 0 or planes != 1 or pixel_offset < 14 + dib_size:
        return False
    if bits_per_pixel not in {1, 4, 8, 16, 24, 32} \
            or compression not in {0, 1, 2, 3, 6}:
        return False
    if compression == 1 and bits_per_pixel != 8:
        return False
    if compression == 2 and bits_per_pixel != 4:
        return False
    if compression in {3, 6} and bits_per_pixel not in {16, 32}:
        return False
    if compression in {1, 2}:
        if dib_size < 40 or height < 0:
            return False
        image_size = int.from_bytes(data[34:38], "little")
        pixel_end = file_size if image_size == 0 else pixel_offset + image_size
        if pixel_end > file_size:
            return False
        return _valid_bmp_rle(
            data,
            pixel_offset,
            pixel_end,
            width,
            height,
            bits_per_pixel,
        )
    row_size = ((bits_per_pixel * width + 31) // 32) * 4
    required_pixels = row_size * abs(height)
    return pixel_offset + required_pixels <= file_size


def _valid_image_payload(ext: str, data: bytes) -> bool:
    if ext == "png":
        return _valid_png(data)
    if ext in ("jpg", "jpeg"):
        return _valid_jpeg(data)
    if ext == "gif":
        return _valid_gif(data)
    if ext == "bmp":
        return _valid_bmp(data)
    return False


def _image_dimensions(path) -> tuple[int, int]:
    """Best-effort native pixel size for PNG/JPEG/GIF/BMP; else (800, 600)."""
    try:
        p = _resolve_allowed_input(Path(path), label="image path")
        if p.stat().st_size > _MAX_IMAGE_BYTES:
            return 800, 600
        data = p.read_bytes()
    except (OSError, ValueError):
        return 800, 600
    ext = p.suffix.lower()
    try:
        if ext == ".png" and data[:8] == b"\x89PNG\r\n\x1a\n":
            w = int.from_bytes(data[16:20], "big")
            h = int.from_bytes(data[20:24], "big")
            return w or 800, h or 600
        if ext in (".jpg", ".jpeg") and data[:2] == b"\xff\xd8":
            return _jpeg_dimensions(data)
        if ext == ".gif" and data[:6] in (b"GIF87a", b"GIF89a"):
            w = int.from_bytes(data[6:8], "little")
            h = int.from_bytes(data[8:10], "little")
            return w or 800, h or 600
        if ext == ".bmp" and data[:2] == b"BM":
            dib_size = int.from_bytes(data[14:18], "little")
            if dib_size == 12:
                w = int.from_bytes(data[18:20], "little")
                h = int.from_bytes(data[20:22], "little")
            elif dib_size >= 40:
                w = int.from_bytes(data[18:22], "little", signed=True)
                h = abs(int.from_bytes(data[22:26], "little", signed=True))
            else:
                return 800, 600
            return (w if w > 0 else 800), (h if h > 0 else 600)
    except (ValueError, IndexError):
        pass
    return 800, 600


class ImageMedia:
    """Deduplicates images by path across all slides; assigns unique media
    targets so the .pptx is fully self-contained (downloadable, all referenced
    materials embedded)."""

    def __init__(self) -> None:
        self._items: list[dict] = []
        self._by_path: dict[str, dict] = {}
        self._next_num = 1
        self._total_bytes = 0

    def add(self, path) -> dict | None:
        key = str(path)
        if key in self._by_path:
            return self._by_path[key]
        try:
            p = _resolve_allowed_input(Path(path), label="image path")
        except (OSError, ValueError):
            return None
        if not p.is_file():
            return None
        try:
            if p.stat().st_size > _MAX_IMAGE_BYTES:
                return None
            data = p.read_bytes()
        except OSError:
            return None
        if len(data) > _MAX_IMAGE_BYTES \
                or self._total_bytes + len(data) > _MAX_TOTAL_MEDIA_BYTES:
            return None
        ext = p.suffix.lower().lstrip(".")
        if ext not in SUPPORTED_IMAGE_TYPES:
            return None
        if not _valid_image_payload(ext, data):
            return None
        entry = {
            "path": key,
            "bytes": data,
            "ext": ext,
            "num": self._next_num,
            # Relationship targets resolve from ppt/slides/slideN.xml.
            "target": f"../media/image{self._next_num}.{ext}",
        }
        self._next_num += 1
        self._items.append(entry)
        self._by_path[key] = entry
        self._total_bytes += len(data)
        return entry

    def items(self) -> list[dict]:
        return list(self._items)

    def extensions(self) -> set[str]:
        return {e["ext"] for e in self._items}


def pic_shape(name: str, rid: str, x: int, y: int, cx: int, cy: int,
              alt: str = "") -> str:
    """A picture shape referencing an embedded image via r:embed=rid."""
    return (
        "<p:pic>"
        f'<p:nvPicPr><p:cNvPr id="0" name="{xml_text(name)}" '
        f'descr="{xml_attr(alt)}"/>'
        '<p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr>'
        '<p:nvPr/></p:nvPicPr>'
        f'<p:blipFill><a:blip r:embed="{rid}"/>'
        '<a:stretch><a:fillRect/></a:stretch></p:blipFill>'
        '<p:spPr>'
        f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        '</p:spPr>'
        '</p:pic>'
    )


# --- inline markdown -> DrawingML runs -------------------------------------

_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"\*([^*]+)\*")


def parse_inline(text: str) -> list[tuple[str, dict]]:
    """Return (segment_text, fmt) with bold/italic flags."""
    segs: list[tuple[str, dict]] = []
    cursor = 0
    for m in _BOLD_RE.finditer(text):
        if m.start() > cursor:
            segs.extend(_split_italic(text[cursor:m.start()], bold=False))
        segs.extend(_split_italic(m.group(1), bold=True))
        cursor = m.end()
    if cursor < len(text):
        segs.extend(_split_italic(text[cursor:], bold=False))
    return segs or [("", {})]


def _split_italic(text: str, *, bold: bool) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    cursor = 0
    for m in _ITALIC_RE.finditer(text):
        if m.start() > cursor:
            out.append((text[cursor:m.start()], {"bold": bold}))
        out.append((m.group(1), {"bold": bold, "italic": True}))
        cursor = m.end()
    if cursor < len(text):
        out.append((text[cursor:], {"bold": bold}))
    return out or [("", {"bold": bold})]


def _wrap_text(text, width: int, size: int) -> list[str]:
    """Conservatively wrap CJK, Latin text and long URLs at a fixed font size.

    Explicit breaks make the result independent of Office's word-break locale;
    this is a preflight estimate, so actual client rendering still needs QA.
    """
    capacity = width / (size * 127) / 1.08
    if capacity < 1:
        raise ValueError("文本栏过窄；请调整列宽或拆页")
    prohibited_start = "，。！？；：、）》」』】〕〉”’…％,.;:!?%)]}" + _CITE_START
    prohibited_end = "（《「『【〔〈“‘([{"

    def char_width(ch):
        if ch.startswith(_CITE_START) and ch.endswith(_CITE_END):
            # Office clients scale superscripts when applying the baseline.
            # Measure the declared size conservatively; do not shrink twice.
            return sum(char_width(c) for c in ch[1:-1]) * max(size, 1800) / size
        if ch in ("*", _CITE_START, _CITE_END):
            return 0.0  # inline Markdown formatting delimiters
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            return 1.0
        if ch in "MW@%&":
            return 0.95
        if ch in "ilI.,:;'!| ":
            return 0.35
        return 0.64

    lines, current, used = [], "", 0.0
    tokens = re.findall(f"{_CITE_START}[^{_CITE_END}]*{_CITE_END}|.", str(text), re.S)
    for ch in tokens:
        if ch == "\n":
            lines.append(current)
            current, used = "", 0.0
            continue
        advance = char_width(ch)
        if current and used + advance > capacity:
            # Prefer a word boundary when it uses at least 60% of the line.
            boundary = current.rfind(" ")
            cut = boundary if boundary >= len(current) * 0.6 else len(current)
            combined = current + ch
            citation_spans = [(match.start(), match.end()) for match in _CITE_RE.finditer(combined)]
            # Move an earlier character/phrase to the next line instead of
            # hanging punctuation outside the box or starting a line with 。/）.
            while cut > 0:
                protected_start = next((start for start, end in citation_spans if start < cut < end), None)
                if protected_start is not None:
                    cut = protected_start
                    continue
                head = combined[:cut].rstrip(" *")
                tail = combined[cut:].lstrip(" *")
                if head and tail and head[-1] not in prohibited_end and tail[0] not in prohibited_start:
                    break
                cut -= 1
            if not cut:
                raise ValueError("中文标点组合无法在当前列宽换行；请增大列宽或调整内容")
            lines.append(combined[:cut].rstrip(" "))
            current = combined[cut:].lstrip(" ")
            used = sum(char_width(c) for c in re.findall(
                f"{_CITE_START}[^{_CITE_END}]*{_CITE_END}|.", current, re.S))
            if used > capacity:
                raise ValueError("中文标点组合超出列宽；请增大列宽或调整内容")
        else:
            current += ch
            used += advance
    lines.append(current)
    return lines


def _text_height(text, width: int, size: int) -> int:
    return max(1, len(_wrap_text(text, width, size))) * round(size * 1.4) * 127 + 40000


def _checked_text(text, width: int, height: int, size: int, label: str) -> str:
    lines = _wrap_text(text, width, size)
    needed = _text_height(text, width, size)
    if needed > height:
        raise ValueError(f"{label} 文字超出可读区域（需要 {needed / EMU_IN:.2f} 英寸，"
                         f"仅有 {height / EMU_IN:.2f} 英寸）；请精简或拆页，禁止缩小字号截断")
    return "\n".join(lines)


def _citation_markers(node: dict, reference_numbers: dict[str, int]) -> str:
    citations = node.get("citations", [])
    if not isinstance(citations, list) or any(not isinstance(c, str) for c in citations):
        raise ValueError("citations 必须是文献 id 字符串数组")
    unknown = [c for c in citations if c not in reference_numbers]
    if unknown:
        raise ValueError("引用了未知文献 id: " + ", ".join(unknown))
    markers = "".join(f"[{n}]" for n in sorted({reference_numbers[c] for c in citations}))
    return _CITE_START + markers + _CITE_END if markers else ""


def _collect_citation_numbers(node, reference_numbers: dict[str, int]) -> list[int]:
    """Collect only structured citations, never bracketed text in user content."""
    if isinstance(node, list):
        return sorted({number for child in node for number in _collect_citation_numbers(child, reference_numbers)})
    if not isinstance(node, dict):
        return []
    _citation_markers(node, reference_numbers)  # shared type/unknown-id validation
    found = {reference_numbers[rid] for rid in node.get("citations", [])}
    for field in ("bullets", "left", "right"):
        found.update(_collect_citation_numbers(node.get(field, []), reference_numbers))
    return sorted(found)


def _inline_text(node: dict, reference_numbers: dict[str, int]) -> str:
    text = str(node.get("text", ""))
    if not (node.get("caption") or node.get("source")):
        text += _citation_markers(node, reference_numbers)
    return text


def _meta_text(node: dict, reference_numbers: dict[str, int]) -> str:
    caption = str(node.get("caption") or "").strip()
    source = str(node.get("source") or "").strip()
    markers = _citation_markers(node, reference_numbers)
    if caption:
        caption += markers
    elif source:
        source += markers
    elif markers and node.get("type") in ("image", "table", "chart", "columns", "two_column"):
        kind = node["type"]
        if kind not in ("columns", "two_column") or not node.get("title"):
            caption = {"image": "图片依据", "table": "表格依据", "chart": "数据依据",
                       "columns": "本页依据", "two_column": "本页依据"}[kind] + markers
    if node.get("caption") and "**" not in caption:
        caption = f"**{caption}**"
    return "\n".join(part for part in (caption, f"来源：{source}" if source else "") if part)


def _reference_catalog(spec: dict) -> tuple[dict[str, int], list[str]]:
    raw = spec.get("references", [])
    if not isinstance(raw, list):
        raise ValueError("references 必须是数组")
    numbers, keys, definitions, entries = {}, {}, {}, []
    for index, ref in enumerate(raw, 1):
        if not isinstance(ref, dict) or not isinstance(ref.get("id"), str) or not ref["id"].strip():
            raise ValueError(f"references[{index}] 必须有非空 id")
        if not isinstance(ref.get("title"), str) or not ref["title"].strip():
            raise ValueError(f"references[{index}] 必须有真实文献 title")
        rid = ref["id"]
        for field in ("source", "doi", "url"):
            if ref.get(field) is not None and not isinstance(ref[field], str):
                raise ValueError(f"references[{index}].{field} 必须是字符串")
        doi = str(ref.get("doi") or "").strip()
        doi_key = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi, flags=re.I).lower()
        url = str(ref.get("url") or "").strip()
        if url and not re.match(r"^https?://[^\s/]+(?:/[^\s]*)?$", url):
            raise ValueError(f"references[{index}].url 必须是可核验的 http(s) URL")
        if doi_key and not re.match(r"^10\.\d{4,9}/\S+$", doi_key):
            raise ValueError(f"references[{index}].doi 格式无效")
        if not (doi or url or ref.get("source")):
            raise ValueError(f"references[{index}] 需要 source、doi 或 url 以便核验")
        authors = ref.get("authors", [])
        if isinstance(authors, list):
            if any(not isinstance(a, str) for a in authors):
                raise ValueError("文献 authors 必须是字符串或字符串数组")
            authors = ", ".join(authors)
        elif not isinstance(authors, str):
            raise ValueError("文献 authors 必须是字符串或字符串数组")
        canonical = (ref["title"].strip(), authors, str(ref.get("year") or ""),
                     str(ref.get("source") or ""), doi_key, url)
        if rid in definitions and definitions[rid] != canonical:
            raise ValueError(f"重复文献 id 定义冲突: {rid}")
        definitions[rid] = canonical
        identities = (["doi:" + doi_key] if doi_key else []) + (["url:" + url.rstrip("/")] if url else [])
        if not identities:
            identities = ["work:" + "|".join(canonical[:4]).casefold()]
        existing = {keys[key] for key in identities if key in keys}
        if len(existing) > 1:
            raise ValueError(f"文献 DOI 与 URL 指向不同的已有条目: {rid}；请核对来源")
        number = next(iter(existing)) if existing else len(entries) + 1
        numbers[rid] = number
        for key in identities:
            keys[key] = number
        if not existing:
            body = ". ".join(part for part in (authors, ref["title"].strip(),
                              str(ref.get("source") or ""), str(ref.get("year") or "")) if part)
            if doi_key:
                body += f". DOI: {doi_key}"
            if url and not (doi_key and url.rstrip("/").lower().endswith(doi_key)):
                body += f". {url}"
            entries.append(f"[{number}] {body}")
    return numbers, entries


def _reference_layout(entry: str, number_width: int = 457200) -> tuple[str, str, str, int, int]:
    number, body = entry.split(" ", 1)
    parts = re.split(r"\. (?=(?:DOI:|https?://))", body, maxsplit=1)
    description, locator = parts[0], parts[1] if len(parts) > 1 else ""
    width = _BODY_W - number_width - 91440
    body_h = _text_height(description, width, 1600)
    locator_h = _text_height(locator, width, 1400) + 45720 if locator else 0
    return number, description, locator, body_h, locator_h


def _page_reference_catalog(spec: dict, reference_numbers: dict[str, int]) -> dict[int, str]:
    """Shorten authors explicitly, while retaining full titles and a locator."""
    result = {}
    for original in spec.get("references", []):
        number = reference_numbers[original["id"]]
        if number in result:
            continue
        compact = dict(original)
        authors = compact.get("authors", [])
        if isinstance(authors, list) and len(authors) > 3:
            compact["authors"] = authors[:3] + ["等"]
        if compact.get("doi"):
            compact.pop("url", None)  # DOI is sufficient here; appendix keeps both.
        _, entries = _reference_catalog({"references": [compact]})
        result[number] = f"[{number}] " + entries[0].split(" ", 1)[1]
    return result


def _page_reference_layout(entries: list[str]) -> tuple[int, list[tuple[str, str, int]]]:
    number_width = math.ceil(max(355600, max((len(entry.split(" ", 1)[0]) * _PAGE_REFERENCE_SIZE * 127 * 0.7
                                             for entry in entries), default=0)))
    width = _BODY_W - number_width - 91440
    rows = []
    for entry in entries:
        number, body = entry.split(" ", 1)
        rows.append((number, body, _text_height(body, width, _PAGE_REFERENCE_SIZE)))
    return number_width, rows


def _page_reference_height(entries: list[str]) -> int:
    if not entries:
        return 0
    _, rows = _page_reference_layout(entries)
    return 68580 + _text_height("本页参考文献", _BODY_W, 1200) + 45720 \
        + sum(height for _, _, height in rows) + 38100 * (len(rows) - 1)


def _page_reference_shapes(entries: list[str]) -> list[str]:
    height = _page_reference_height(entries)
    if not height:
        return []
    top = _BODY_Y + _BODY_H - height
    shapes = [rect_shape("PageReferenceRule", _BODY_X, top, _BODY_W, 4000, _BORDER)]
    top += 68580
    label_h = _text_height("本页参考文献", _BODY_W, 1200)
    shapes.append(textbox("PageReferenceLabel", "本页参考文献", x=_BODY_X, y=top,
                          cx=_BODY_W, cy=label_h, size=1200, color=_MUTED_COLOR, bold=True))
    top += label_h + 45720
    number_width, rows = _page_reference_layout(entries)
    for number, body, row_h in rows:
        shapes.append(textbox("PageReferenceNumber", number, x=_BODY_X, y=top,
                              cx=number_width, cy=row_h, size=_PAGE_REFERENCE_SIZE,
                              color=_ACCENT, bold=True))
        shapes.append(textbox("PageReferenceEntry", body, x=_BODY_X + number_width + 91440,
                              y=top, cx=_BODY_W - number_width - 91440, cy=row_h,
                              size=_PAGE_REFERENCE_SIZE, color=_MUTED_COLOR))
        top += row_h + 38100
    return shapes


def _validate_chart(block: dict) -> None:
    if block.get("chart_type") not in ("bar", "line"):
        raise ValueError("chart_type 仅支持 bar 或 line")
    categories, series = block.get("categories"), block.get("series")
    if not isinstance(categories, list) or not 1 <= len(categories) <= 8 \
            or any(not isinstance(c, str) or not c.strip() for c in categories):
        raise ValueError("chart.categories 需要 1–8 个非空标签；更多数据请拆页或用表格")
    if len(set(categories)) != len(categories):
        raise ValueError("chart.categories 不能重复")
    if block["chart_type"] == "line" and len(categories) < 2:
        raise ValueError("折线图至少需要两个时间点/有序类别")
    if not isinstance(series, list) or not 1 <= len(series) <= 4:
        raise ValueError("chart.series 需要 1–4 个数据系列")
    names = set()
    for item in series:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
            raise ValueError("chart.series 每项需要 name")
        if item["name"] in names:
            raise ValueError("chart.series.name 不能重复")
        names.add(item["name"])
        values = item.get("values")
        if not isinstance(values, list) or len(values) != len(categories):
            raise ValueError("chart.series.values 必须与 categories 等长")
        present = [v for v in values if v is not None]
        if not present or any(isinstance(v, bool) or not isinstance(v, (int, float))
                              or abs(v) > 1e12 or not math.isfinite(v) for v in present):
            raise ValueError("chart.values 必须有至少一个有限数值（绝对值 ≤ 1e12）；缺测用 null，不得造数")
    if not isinstance(block.get("unit"), str) or not block["unit"].strip():
        raise ValueError("chart.unit 必须明确单位（无量纲可写‘比值’）")
    if not (block.get("source") or block.get("citations")):
        raise ValueError("chart 需要 source 或 citations 标明数据来源；不得虚构数值")
    if "y_min" in block or "y_max" in block:
        raise ValueError("图表使用包含零点的自动坐标轴；不接受 y_min/y_max 截轴")


def _bullet_items(items: list, reference_numbers: dict[str, int]) -> list[dict]:
    if not isinstance(items, list):
        raise ValueError("bullets/旧 two_column 内容必须是数组")
    result = []
    for item in items:
        if isinstance(item, dict) and "text" not in item:
            raise ValueError("要点对象需要 text；图片/图表/表格混排请使用 columns 内容块")
        item = dict(item) if isinstance(item, dict) else {"text": str(item)}
        level = item.get("level", 0)
        if isinstance(level, bool) or level not in (0, 1):
            raise ValueError("bullet level 必须是 0 或 1")
        markers = _citation_markers(item, reference_numbers)
        item["text"] = str(item.get("text", "")) + (f" {markers}" if markers else "")
        if item.get("source"):
            item["text"] += f"（来源：{item['source']}）"
        result.append(item)
    return result


def _bullet_height(items: list, width: int) -> int:
    return 91440 + sum(_text_height(item.get("text", ""), width - 411480 - 228600 * item.get("level", 0),
                                   2000 if item.get("level", 0) == 0 else 1800) + 76200
                       for item in items)


def _block_bullets(block: dict, reference_numbers: dict[str, int], *, prepared: bool = False) -> list[dict]:
    items = [dict(item) for item in block.get("bullets", [])] if prepared \
        else _bullet_items(block.get("bullets", []), reference_numbers)
    markers = _citation_markers(block, reference_numbers)
    if markers and not (block.get("caption") or block.get("source")):
        if items:
            items[-1]["text"] += markers
        else:
            items = [{"text": "参考依据" + markers}]
    return items


def _table_layout(headers: list, rows: list, width: int,
                  column_widths: list | None = None) -> tuple[list[int], int, list[int]]:
    if not isinstance(headers, list) or not isinstance(rows, list) \
            or any(not isinstance(row, list) for row in rows):
        raise ValueError("table.headers/rows 必须为数组，rows 每项必须为数组")
    cols = max(len(headers), max((len(row) for row in rows), default=0))
    if not 1 <= cols <= 6:
        raise ValueError("table 需要 1–6 列；更多列请按主题拆表")
    if headers and any(len(row) > len(headers) for row in rows):
        raise ValueError("表格数据列超过表头列数；请修正数据，禁止丢列")
    if column_widths is None:
        column_widths = [1] * cols
    if not isinstance(column_widths, list) or len(column_widths) != cols \
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or v <= 0 or v > 1e6 or not math.isfinite(v) for v in column_widths):
        raise ValueError("table.column_widths 必须与列数等长且为有限正数")
    widths = [round(width * value / sum(column_widths)) for value in column_widths]
    widths[-1] += width - sum(widths)
    size = 1600 if width >= 8000000 and cols <= 4 else 1400
    heights = []
    for row, header in ([(headers, True)] if headers else []) + [(row, False) for row in rows]:
        row_size = size + 200 if header else size
        heights.append(max(457200, max((_text_height(c, widths[i] - 274320, row_size)
                                       for i, c in enumerate(row)), default=0) + 137160))
    return widths, size, heights


def _has_visible_text(value) -> bool:
    """Ignore whitespace, formatting-only text and invisible Unicode controls."""
    if not isinstance(value, str):
        return False
    plain = "".join(text for text, _ in parse_inline(value))
    return any(not char.isspace() and not unicodedata.category(char).startswith("C")
               for char in plain)


def _has_cell_value(value) -> bool:
    # Zero (and an explicit boolean) is data; None and empty cells are not.
    return (_has_visible_text(value) or isinstance(value, int)
            or isinstance(value, float) and math.isfinite(value))


def _has_payload(value) -> bool:
    if isinstance(value, dict):
        return any(_has_payload(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_payload(item) for item in value)
    return _has_cell_value(value)


def _check_body_fields(node: dict, kind: str, location: str) -> None:
    """Catch likely lost content without rejecting unrelated metadata."""
    fields = {
        "title": {"subtitle"}, "section": {"subtitle"},
        "text": {"text"}, "paragraph": {"text"}, "bullets": {"bullets"},
        "table": {"headers", "rows"}, "image": {"path"},
        "chart": {"chart_type", "categories", "series"},
        "columns": {"left", "right"}, "two_column": {"left", "right"},
    }
    hints = {
        "title": "标题页使用 title/subtitle；有正文时请改用相应的内容页型",
        "section": "章节过渡页使用 title/subtitle；有正文时请改用相应的内容页型",
        "text": "正文请写入 text 字符串",
        "paragraph": "正文请写入 text 字符串",
        "bullets": "要点请写入 bullets 数组；连续文字请使用 type=text/paragraph 和 text",
        "table": "表格请使用 headers 和 rows，其中 rows 必须含数据行",
        "image": "图片请使用 path，解释可写入 caption",
        "chart": "图表请使用 chart_type、categories 和 series，解释可写入 caption",
        "columns": "混排请使用 left/right 内容块数组，并在子块中填写对应正文",
        "two_column": "左右要点请使用 left/right 要点数组",
    }
    body_keys = ("content", "body", "items", "blocks", "text", "bullets", "subtitle", "headers",
                 "rows", "left", "right", "path", "chart_type", "categories", "series")
    for field in body_keys:
        if field not in fields[kind] and _has_payload(node.get(field)):
            raise ValueError(f"{location}.{field} 与 type={kind} 不匹配，正文会被忽略；{hints[kind]}")


def _validate_bullet_content(items, location: str) -> None:
    if not isinstance(items, list) or not items:
        raise ValueError(f"{location} 必须是非空要点数组，不能生成无正文的内容页")
    for index, item in enumerate(items):
        item_location = f"{location}[{index}]"
        if isinstance(item, dict):
            _check_body_fields(item, "text", item_location)
            text = item.get("text")
            item_location += ".text"
        else:
            text = item
        if not _has_visible_text(text):
            raise ValueError(f"{item_location} 需要有效非空正文 text；标题、备注、来源或引用不能代替要点")


def _validate_body_node(node, location: str, *, is_slide: bool) -> None:
    if not isinstance(node, dict):
        raise ValueError(f"{location} 必须是内容块对象")
    kind = node.get("type", "bullets" if is_slide else "text")
    allowed = {"text", "paragraph", "bullets", "table", "image", "chart"}
    if is_slide:
        allowed.update({"title", "section", "columns", "two_column"})
    if not isinstance(kind, str) or kind not in allowed:
        raise ValueError(f"{location}.type 不支持: {kind}")
    _check_body_fields(node, kind, location)
    if kind in ("title", "section"):
        if not _has_visible_text(node.get("title")):
            raise ValueError(f"{location}.title 需要有效非空标题；仅显式 title/section 页可不含正文")
    elif kind in ("text", "paragraph"):
        if not _has_visible_text(node.get("text")):
            raise ValueError(f"{location}.text 需要有效非空正文；标题、备注、图注、来源或引用不能代替正文")
    elif kind == "bullets":
        _validate_bullet_content(node.get("bullets"), f"{location}.bullets")
    elif kind == "table":
        rows = node.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"{location}.rows 至少需要一行实质数据，只有表头或空 rows 不算正文")
        for index, row in enumerate(rows):
            if not isinstance(row, list) or not any(_has_cell_value(cell) for cell in row):
                raise ValueError(f"{location}.rows[{index}] 必须是非空数据行，不能生成空续表页")
    elif kind in ("columns", "two_column"):
        for side in ("left", "right"):
            children = node.get(side)
            child_location = f"{location}.{side}"
            if kind == "two_column":
                _validate_bullet_content(children, child_location)
            else:
                if not isinstance(children, list) or not children:
                    raise ValueError(f"{child_location} 必须是非空内容块数组，空栏不能用标题或来源代替")
                for index, child in enumerate(children):
                    _validate_body_node(child, f"{child_location}[{index}]", is_slide=False)
    elif kind == "image":
        if not isinstance(node.get("path"), str) or not node["path"].strip():
            raise ValueError(f"{location}.path 必须是图片绝对路径")
    elif kind == "chart":
        try:
            _validate_chart(node)
        except ValueError as exc:
            raise ValueError(f"{location}: {exc}") from exc


def _prepare_slides(spec: dict) -> tuple[list[dict], dict[str, int]]:
    raw = spec.get("slides", [])
    if not isinstance(raw, list) or not raw:
        raise ValueError("spec.slides 为空或不是数组，无法生成演示文稿")
    if len(raw) > _MAX_SLIDES or any(not isinstance(s, dict) for s in raw):
        raise ValueError(f"spec.slides 必须是对象数组，最多 {_MAX_SLIDES} 页")
    for index, slide in enumerate(raw):
        title = slide.get("title") if isinstance(slide.get("title"), str) else "无标题"
        location = f"第 {index + 1} 页「{title or '无标题'}」（slides[{index}]）"
        _validate_body_node(slide, location, is_slide=True)
    numbers, entries = _reference_catalog(spec)
    page_catalog = _page_reference_catalog(spec, numbers)

    def measure_page(node):
        page_entries = [page_catalog[number] for number in _collect_citation_numbers(node, numbers)]
        ref_h = _page_reference_height(page_entries)
        body_h = _BODY_H - ref_h - (_BLOCK_GAP if ref_h else 0)
        meta = _meta_text(node, numbers)
        available = body_h - (_text_height(meta, _BODY_W, 1400) + _BLOCK_GAP if meta else 0)
        return page_entries, ref_h, body_h, available

    def finish_page(node):
        page_entries, ref_h, body_h, _ = measure_page(node)
        if ref_h > _PAGE_REFERENCE_MAX_H:
            raise ValueError("本页参考文献过密；请拆页并按各页实际论断分配引用，不能截断文献或缩小字号")
        return {**node, "_page_references": page_entries, "_body_height": body_h}

    slides = []
    for original in raw:
        slide = dict(original)
        stype = slide.get("type", "bullets")
        if stype not in {"title", "section", "bullets", "text", "paragraph", "table", "image", "two_column", "columns", "chart"}:
            raise ValueError(f"不支持的 slide.type: {stype}")
        chunks = []
        if stype == "bullets":
            items = _bullet_items(slide.get("bullets", []), numbers)
            chunk = []
            for item in items:
                single = {**slide, "bullets": [item]}
                _, ref_h, _, available = measure_page(single)
                if ref_h > _PAGE_REFERENCE_MAX_H:
                    finish_page(single)  # raise the explicit density error
                if _bullet_height(_block_bullets(single, numbers, prepared=True), _BODY_W) > available:
                    raise ValueError("单条要点与本页参考文献超出可读区域；请拆分论断或拆页")
                candidate = {**slide, "bullets": chunk + [item]}
                _, ref_h, _, available = measure_page(candidate)
                if chunk and (len(chunk) >= 6 or ref_h > _PAGE_REFERENCE_MAX_H
                              or _bullet_height(_block_bullets(candidate, numbers, prepared=True), _BODY_W) > available):
                    chunks.append({**slide, "bullets": chunk})
                    chunk = []
                chunk.append(item)
            chunks.append({**slide, "bullets": chunk})
        elif stype == "table":
            checked = finish_page(slide)
            _, _, _, available = measure_page(checked)
            headers, rows = slide.get("headers", []), slide.get("rows", [])
            _, _, heights = _table_layout(headers, rows, _BODY_W, slide.get("column_widths"))
            header_h = heights[0] if headers else 0
            data_heights = heights[1:] if headers else heights
            if header_h > available:
                raise ValueError("表头与本页参考文献超出可读区域；请精简或拆表")
            chunk, used = [], header_h
            for row, height in zip(rows, data_heights):
                if header_h + height > available:
                    raise ValueError("单行表格与本页参考文献超出可读区域；请拆成多个可读行，禁止截断单元格")
                if chunk and (used + height > available or len(chunk) >= 10):
                    chunks.append({**slide, "rows": chunk})
                    chunk, used = [], header_h
                chunk.append(row)
                used += height
            chunks.append({**slide, "rows": chunk})
        else:
            chunks = [slide]
        for index, chunk in enumerate(chunks, 1):
            if len(chunks) > 1:
                chunk["title"] = f"{slide.get('title', '')}（{index}/{len(chunks)}）"
            slides.append(finish_page(chunk))
    ref_chunks, current, used = [], [], 0
    number_width = max(457200, max((len(entry.split(" ", 1)[0]) * 1600 * 127 * 0.7
                                   for entry in entries), default=0))
    number_width = math.ceil(number_width)
    for entry in entries:
        _, _, _, body_h, locator_h = _reference_layout(entry, number_width)
        height = body_h + locator_h + 228600
        if height > _BODY_H:
            raise ValueError("单条参考文献过长；请使用 DOI 或规范短链接，保留可核验信息")
        if current and used + height > _BODY_H:
            ref_chunks.append(current)
            current, used = [], 0
        current.append(entry)
        used += height
    if current:
        ref_chunks.append(current)
    for index, refs in enumerate(ref_chunks, 1):
        suffix = f"（{index}/{len(ref_chunks)}）" if len(ref_chunks) > 1 else ""
        slides.append({"type": "references", "title": "参考文献" + suffix,
                       "entries": refs, "number_width": number_width,
                       "_page_references": [], "_body_height": _BODY_H})
    if len(slides) > _MAX_SLIDES:
        raise ValueError(f"自动拆页及参考文献后超过 {_MAX_SLIDES} 页；请拆分报告")
    return slides, numbers


def runs_xml(text: str, *, size: int = 1800, color: str | None = None,
             bold: bool = False) -> str:
    """Build <a:r> runs for inline-markdown text at a given point size (x100)."""
    color = color or _TEXT_COLOR
    parts = []
    for seg_text, fmt in parse_inline(str(text)):
        for chunk in _CITE_RE.split(seg_text):
            if not chunk:
                continue
            citation = chunk.startswith(_CITE_START) and chunk.endswith(_CITE_END)
            run_text = chunk[1:-1] if citation else chunk
            # Baseline itself reduces the displayed glyph size in Office/LO.
            # Keep small-caption markers legible instead of applying another
            # reduction that would turn a 14 pt caption marker into ~6 pt.
            run_size = max(size, 1800) if citation else size
            attrs = ['lang="zh-CN"', f'sz="{run_size}"', 'dirty="0"']
            if citation:
                attrs.append('baseline="30000"')
            elif fmt.get("bold") or bold:
                attrs.append('b="1"')
            if fmt.get("italic") and not citation:
                attrs.append('i="1"')
            run_color = _ACCENT if citation else color
            rpr = (
                f'<a:rPr {" ".join(attrs)}>'
                f'<a:solidFill><a:srgbClr val="{run_color}"/></a:solidFill>'
                f'<a:latin typeface="{EN_FONT}"/><a:ea typeface="{CN_FONT}"/>'
                f'<a:cs typeface="{EN_FONT}"/>'
                "</a:rPr>"
            )
            for index, line in enumerate(run_text.split("\n")):
                if index:
                    parts.append(f"<a:br>{rpr}</a:br>")
                parts.append(f"<a:r>{rpr}<a:t>{xml_text(line)}</a:t></a:r>")
    return "".join(parts)


def _paragraph_font(size: int, element: str) -> str:
    # Office/LibreOffice can use inherited paragraph-end font metrics even when
    # every visible run has a size. Set all three script fonts at this level too.
    return (f'<a:{element} lang="zh-CN" sz="{size}">'
            f'<a:latin typeface="{EN_FONT}"/><a:ea typeface="{CN_FONT}"/>'
            f'<a:cs typeface="{EN_FONT}"/></a:{element}>')


def txbody(text: str, *, size: int = 1800, color: str | None = None,
           align: str = "l", anchor: str = "t", wrap: bool = True,
           spc: int | None = None, bullet: bool = False, bold: bool = False) -> str:
    if anchor not in _VALID_TEXT_ANCHORS:
        raise ValueError(f"invalid DrawingML text anchor: {anchor}")
    body_pr = (
        f'<a:bodyPr vert="horz" wrap="{("square" if wrap else "none")}" '
        f'anchor="{anchor}" anchorCtr="0" vertOverflow="clip" rtlCol="0" '
        'lIns="0" tIns="0" rIns="0" bIns="0">'
        '<a:noAutofit/>'
        '</a:bodyPr>'
    )
    lst_style = "<a:lstStyle/>"
    ppr_parts = [f'<a:pPr algn="{align}" indent="0" latinLnBrk="1">',
                 f'<a:lnSpc><a:spcPts val="{round(size * 1.4)}"/></a:lnSpc>']
    if spc is not None:
        ppr_parts.append(f'<a:spcBef><a:spcPts val="{spc}"/></a:spcBef>')
    if bullet:
        ppr_parts.append('<a:buFont typeface="Arial" pitchFamily="34" charset="0"/>')
        ppr_parts.append('<a:buChar char="•"/>')
    else:
        ppr_parts.append("<a:buNone/>")
    ppr_parts.append(_paragraph_font(size, "defRPr"))
    ppr_parts.append("</a:pPr>")
    ppr = "".join(ppr_parts)
    return (f'<p:txBody>{body_pr}{lst_style}<a:p>{ppr}'
            f'{runs_xml(text, size=size, color=color, bold=bold)}'
            f'{_paragraph_font(size, "endParaRPr")}</a:p></p:txBody>')


def textbox(name: str, text: str, *, x: int, y: int, cx: int, cy: int,
            size: int = 1800, color: str | None = None, align: str = "l",
            anchor: str = "t", spc: int | None = None, bullet: bool = False,
            bold: bool = False) -> str:
    text = _checked_text(text, cx, cy, size, name)
    sp = (
        "<p:sp>"
        f'<p:nvSpPr><p:cNvPr id="0" name="{xml_text(name)}"/>'
        '<p:cNvSpPr txBox="1"><a:spLocks noGrp="1"/></p:cNvSpPr>'
        "<p:nvPr/></p:nvSpPr>"
        "<p:spPr>"
        f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        '<a:noFill/><a:ln><a:noFill/></a:ln>'
        "</p:spPr>"
    )
    return sp + txbody(text, size=size, color=color, align=align, anchor=anchor,
                       spc=spc, bullet=bullet, bold=bold) + "</p:sp>"


def rect_shape(name: str, x: int, y: int, cx: int, cy: int, fill: str) -> str:
    """Decorative filled rectangle (no text): accent rules, bands, dividers."""
    return (
        "<p:sp>"
        f'<p:nvSpPr><p:cNvPr id="0" name="{xml_text(name)}"/>'
        "<p:cNvSpPr><a:spLocks noGrp=\"1\"/></p:cNvSpPr><p:nvPr/></p:nvSpPr>"
        "<p:spPr>"
        f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>'
        '<a:ln><a:noFill/></a:ln>'
        "</p:spPr>"
        "</p:sp>"
    )


def header_rule(y: int = 1000000) -> str:
    """Short teal rule under a content-slide title (academic editorial style)."""
    return rect_shape("HeaderRule", 685800, y, 914400, 19050, _ACCENT)


def footer_shapes(ctx: dict) -> list[str]:
    """Bottom hairline + report label (left) and page number (right)."""
    out: list[str] = [rect_shape("FooterRule", 685800, 6346200, 10820400, 9525, _BORDER)]
    label = ctx.get("deck_title") or "Medex 医学报告"
    if _BRAND_LABEL:
        label = _BRAND_LABEL
    author = ctx.get("author")
    left_text = label + (f"  ·  {author}" if author else "")
    out.append(textbox("FooterL", left_text, x=685800, y=6370000, cx=7000000, cy=285750,
                       size=1000, color=_MUTED_COLOR, align="l", anchor="ctr"))
    page = f"{ctx.get('index', 0)} / {ctx.get('total', 0)}"
    out.append(textbox("FooterR", page, x=8000000, y=6370000, cx=3508200, cy=285750,
                       size=1000, color=_MUTED_COLOR, align="r", anchor="ctr"))
    return out


def bullets_box(name: str, bullets: list, *, x: int, y: int, cx: int, cy: int) -> str:
    """Build a text box with multiple paragraphs, one per bullet (with levels)."""
    normalized = [item if isinstance(item, dict) else {"text": str(item)} for item in bullets]
    if _bullet_height(normalized, cx) > cy:
        raise ValueError(f"{name} 要点超出可读区域；请减少内容或拆页")
    body_pr = (
        '<a:bodyPr vert="horz" wrap="square" anchor="t" anchorCtr="0" '
        'vertOverflow="clip" rtlCol="0" lIns="91440" tIns="45720" '
        'rIns="91440" bIns="45720">'
        '<a:noAutofit/>'
        '</a:bodyPr>'
    )
    paragraphs = []
    for item in bullets:
        if isinstance(item, dict):
            text = str(item.get("text", ""))
            try:
                level = int(item.get("level", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("bullet level 必须是 0 或 1") from exc
        else:
            text = str(item)
            level = 0
        if level not in (0, 1):
            raise ValueError("bullet level 必须是 0 或 1")
        size = 2000 if level == 0 else 1800
        text = "\n".join(_wrap_text(text, cx - 411480 - 228600 * level, size))
        color = _INK if level == 0 else _MUTED_COLOR
        indent = 228600 * level  # 0.25in per level
        ppr = (
            f'<a:pPr algn="l" marL="{indent + 228600}" indent="-228600" latinLnBrk="1">'
            f'<a:lnSpc><a:spcPts val="{round(size * 1.4)}"/></a:lnSpc>'
            f'<a:spcBef><a:spcPts val="600"/></a:spcBef>'
        )
        if level == 0:
            ppr += f'<a:buClr><a:srgbClr val="{_ACCENT}"/></a:buClr>'
            ppr += '<a:buFont typeface="Arial" pitchFamily="34" charset="0"/>'
            ppr += '<a:buChar char="•"/>'
        else:
            ppr += f'<a:buClr><a:srgbClr val="{_MUTED_COLOR}"/></a:buClr>'
            ppr += '<a:buFont typeface="Arial" pitchFamily="34" charset="0"/>'
            ppr += '<a:buChar char="–"/>'
        ppr += _paragraph_font(size, "defRPr") + "</a:pPr>"
        paragraphs.append(f'<a:p>{ppr}{runs_xml(text, size=size, color=color)}'
                          f'{_paragraph_font(size, "endParaRPr")}</a:p>')
    if not paragraphs:
        paragraphs.append("<a:p><a:pPr><a:buNone/></a:pPr></a:p>")
    return (
        "<p:sp>"
        f'<p:nvSpPr><p:cNvPr id="0" name="{xml_text(name)}"/>'
        '<p:cNvSpPr txBox="1"><a:spLocks noGrp="1"/></p:cNvSpPr>'
        "<p:nvPr/></p:nvSpPr>"
        "<p:spPr>"
        f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        '<a:noFill/><a:ln><a:noFill/></a:ln>'
        "</p:spPr>"
        f'<p:txBody>{body_pr}<a:lstStyle/>{"".join(paragraphs)}</p:txBody>'
        "</p:sp>"
    )


def table_frame(name: str, headers: list[str], rows: list[list[str]],
                *, x: int, y: int, cx: int, column_widths: list | None = None,
                align: list | None = None) -> str:
    widths, body_size, heights = _table_layout(headers, rows, cx, column_widths)
    cols = len(widths)
    grid = "".join(f'<a:gridCol w="{width}"/>' for width in widths)
    header_size = body_size + 200
    numeric_pattern = re.compile(r"^[+−-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+−-]?\d+)?(?:\s*%)?$")
    missing = {"", "—", "-", "缺测", "未测", "NA", "N/A"}
    if align is None:
        alignment = []
        for col in range(cols):
            values = [str(row[col]).replace("*", "").strip() for row in rows if len(row) > col]
            observed = [value for value in values if value not in missing]
            alignment.append("r" if observed and all(numeric_pattern.fullmatch(v) for v in observed) else "l")
    else:
        if not isinstance(align, list) or len(align) != cols \
                or any(value not in ("left", "center", "right") for value in align):
            raise ValueError("table.align 必须与列数等长，取 left/center/right")
        alignment = [{"left": "l", "center": "ctr", "right": "r"}[value] for value in align]

    def cell(text: str, width: int, col: int, *, header: bool = False, zebra: bool = False) -> str:
        if header:
            color = _ON_PRIMARY
            size = header_size
            fill = _PRIMARY
            if _TABLE_STYLE != "header_fill":
                color, fill = _INK, (_ALT if _TABLE_STYLE == "grid" else _BACKGROUND)
        else:
            color = _TEXT_COLOR
            size = body_size
            fill = _ALT if zebra and _TABLE_STYLE == "header_fill" else _BACKGROUND
        ppr = (
            f'<a:pPr algn="{alignment[col]}" latinLnBrk="1">'
            f'<a:lnSpc><a:spcPts val="{round(size * 1.4)}"/></a:lnSpc>'
            "<a:buNone/></a:pPr>"
        )
        if _TABLE_STYLE == "grid":
            edge = f'<a:solidFill><a:srgbClr val="{_BORDER}"/></a:solidFill>'
            lines = (f'<a:lnL w="9525">{edge}</a:lnL><a:lnR w="9525">{edge}</a:lnR>'
                     f'<a:lnT w="9525">{edge}</a:lnT><a:lnB w="9525">{edge}</a:lnB>')
        elif _TABLE_STYLE == "three_line":
            ink = f'<a:solidFill><a:srgbClr val="{_INK}"/></a:solidFill>'
            top = f'<a:lnT w="19050">{ink}</a:lnT>' if header else '<a:lnT><a:noFill/></a:lnT>'
            bottom = (f'<a:lnB w="9525">{ink}</a:lnB>' if header else
                      f'<a:lnB w="4000"><a:solidFill><a:srgbClr val="{_BORDER}"/></a:solidFill></a:lnB>')
            lines = '<a:lnL><a:noFill/></a:lnL><a:lnR><a:noFill/></a:lnR>' + top + bottom
        else:
            lines = ('<a:lnL><a:noFill/></a:lnL><a:lnR><a:noFill/></a:lnR><a:lnT><a:noFill/></a:lnT>'
                     f'<a:lnB w="{12700 if header else 4000}"><a:solidFill>'
                     f'<a:srgbClr val="{_ACCENT if header else _BORDER}"/></a:solidFill></a:lnB>')
        tc_pr = (
            f'<a:tcPr marL="137160" marR="137160" marT="45720" marB="45720" anchor="ctr">'
            + lines +
            f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>'
            f'</a:tcPr>'
        )
        run = runs_xml("\n".join(_wrap_text(text, width - 274320, size)),
                       size=size, color=color, bold=header)
        ppr = ppr.replace("</a:pPr>", _paragraph_font(size, "defRPr") + "</a:pPr>")
        body = (f'<a:txBody><a:bodyPr/><a:lstStyle/><a:p>{ppr}{run}'
                f'{_paragraph_font(size, "endParaRPr")}</a:p></a:txBody>')
        return f"<a:tc>{body}{tc_pr}</a:tc>"

    def row(cells, *, header: bool, h: int, zebra: bool = False) -> str:
        padded = list(cells) + [""] * (cols - len(cells))
        return f'<a:tr h="{h}">{"".join(cell(c, widths[i], i, header=header, zebra=zebra) for i, c in enumerate(padded))}</a:tr>'

    rows_xml = ""
    if headers:
        rows_xml += row(headers, header=True, h=heights[0])
    for idx, r in enumerate(rows):
        rows_xml += row(r, header=False, h=heights[idx + bool(headers)], zebra=(idx % 2 == 1))
    cy = max(411480, sum(heights))
    tbl = (
        '<a:tbl>'
        f'<a:tblPr firstRow="{1 if headers else 0}" bandRow="0"/>'
        f'<a:tblGrid>{grid}</a:tblGrid>'
        f'{rows_xml}'
        '</a:tbl>'
    )
    return (
        "<p:graphicFrame>"
        f'<p:nvGraphicFramePr><p:cNvPr id="0" name="{escape(name)}"/>'
        '<p:cNvGraphicFramePr><a:graphicFrameLocks noGrp="1"/></p:cNvGraphicFramePr>'
        "<p:nvPr/></p:nvGraphicFramePr>"
        f'<p:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></p:xfrm>'
        f'<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">{tbl}</a:graphicData></a:graphic>'
        "</p:graphicFrame>"
    )


# --- slide assembly --------------------------------------------------------

def _line_shape(name: str, x1: int, y1: int, x2: int, y2: int, color: str,
                dash: str = "solid") -> str:
    x, y = min(x1, x2), min(y1, y2)
    cx, cy = max(1, abs(x2 - x1)), max(1, abs(y2 - y1))
    flip = ' flipV="1"' if (x2 - x1) * (y2 - y1) < 0 else ""
    return (
        '<p:sp><p:nvSpPr>'
        f'<p:cNvPr id="0" name="{name}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:xfrm{flip}><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="line"><a:avLst/></a:prstGeom>'
        f'<a:ln w="25400"><a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
        f'<a:prstDash val="{dash}"/></a:ln>'
        '</p:spPr></p:sp>'
    )


def _marker_shape(name: str, x: int, y: int, size: int, series_index: int) -> str:
    return rect_shape(name, x, y, size, size, _CHART_COLORS[series_index]).replace(
        'prst="rect"', f'prst="{_CHART_MARKERS[series_index]}"', 1)


def _nice_ticks(values: list[float]) -> list[float]:
    """Zero-inclusive axis with familiar 1/2/2.5/5 × 10ⁿ intervals."""
    low, high = min(0.0, min(values)), max(0.0, max(values))
    if low == high:
        high = 1.0
    raw_step = max((high - low) / 4, math.ulp(0.0))
    magnitude = max(10.0 ** math.floor(math.log10(raw_step)), math.ulp(0.0))
    fraction = raw_step / magnitude
    step = next(value for value in (1, 2, 2.5, 5, 10) if value >= fraction) * magnitude
    first = math.floor(low / step + 1e-12)
    last = math.ceil(high / step - 1e-12)
    return [index * step for index in range(first, last + 1)]


def _chart_metrics(block: dict, cx: int) -> tuple[int, int, int, int, int, int, int]:
    categories, series = block["categories"], block["series"]
    label_size = 1400
    axis_w = min(round(cx * 0.24), max(1005840, round(cx * 0.12)))
    plot_w = cx - axis_w - 91440
    cell_w = plot_w // len(categories)
    category_h = max(_text_height(c, cell_w - 45720, label_size) for c in categories) + 68580
    value_h = max(_text_height(item["name"], axis_w - 137160, label_size) for item in series) + 45720
    unit_w = min(cx // 3, 1600200)
    legend_item_w = min((cx - unit_w - 137160) // len(series), 1828800)
    legend_h = max(_text_height(item["name"], legend_item_w - 274320, label_size)
                   for item in series)
    unit_text = f"单位：{block['unit']}"
    unit_h = _text_height(unit_text, unit_w, label_size)
    top_h = max(legend_h, unit_h)
    return axis_w, cell_w, category_h, value_h, unit_w, legend_item_w, top_h


def _chart_shapes(block: dict, *, x: int, y: int, cx: int, cy: int) -> list[str]:
    """Editable chart shapes and a readable, aligned original-value grid."""
    _validate_chart(block)
    categories, series = block["categories"], block["series"]
    shapes = []
    label_size = 1400
    axis_w, cell_w, category_h, value_h, unit_w, legend_item_w, top_h = _chart_metrics(block, cx)
    plot_x, plot_w = x + axis_w, cx - axis_w - 91440
    unit_text = f"单位：{block['unit']}"
    plot_y = y + top_h + 182880
    plot_h = cy - (plot_y - y) - category_h - value_h * len(series) - 182880
    if plot_h < 1097280:
        raise ValueError("图表区域太小；请缩短类别/图例文字、减少系列或将图表移到独立页")
    shapes.append(textbox("ChartUnit", unit_text, x=x + cx - unit_w, y=y, cx=unit_w,
                          cy=top_h, size=label_size, color=_MUTED_COLOR, align="r"))
    description = block.get("alt") or block.get("caption") or f"{block['chart_type']} chart; data shown below"
    shapes[-1] = shapes[-1].replace('name="ChartUnit"', f'name="ChartUnit" descr="{xml_attr(description)}"', 1)
    for index, item in enumerate(series):
        lx = x + index * legend_item_w
        if block["chart_type"] == "line":
            shapes.append(_line_shape(f"LegendLine{index}", lx, y + 114300, lx + 182880,
                                      y + 114300, _CHART_COLORS[index], _CHART_DASHES[index]))
            shapes.append(_marker_shape(f"LegendColor{index}", lx + 53340, y + 76200, 76200, index))
        else:
            shapes.append(rect_shape(f"LegendColor{index}", lx, y + 76200,
                                     152400, 101600, _CHART_COLORS[index]))
        shapes.append(textbox(f"Legend{index}", item["name"], x=lx + 228600, y=y,
                              cx=legend_item_w - 274320, cy=top_h, size=label_size))
    values = [float(value) for item in series for value in item["values"] if value is not None]
    ticks = _nice_ticks(values)
    low, high = ticks[0], ticks[-1]

    def point_y(value):
        return plot_y + round((high - value) / (high - low) * plot_h)

    for tick, value in enumerate(ticks):
        yy = point_y(value)
        shapes.append(rect_shape(f"Grid{tick}", plot_x, yy, plot_w, 4000, _BORDER))
        shapes.append(textbox(f"Tick{tick}", f"{value:.10g}", x=x, y=yy - 111760,
                              cx=axis_w - 137160, cy=254000, size=1200,
                              align="r", color=_MUTED_COLOR))
    zero_y = point_y(0)
    shapes.append(rect_shape("ZeroAxis", plot_x, zero_y, plot_w, 9525, _MUTED_COLOR))
    data_top = plot_y + plot_h + category_h + 91440
    shapes.append(rect_shape("ValueBand", x, data_top, cx, value_h * len(series), _ALT))
    for si, item in enumerate(series):
        color = _CHART_COLORS[si]
        points = []
        for ci, value in enumerate(item["values"]):
            center = plot_x + round(cell_w * (ci + 0.5))
            yy = point_y(value) if value is not None else None
            if value is not None and block["chart_type"] == "bar":
                group_w = cell_w * 0.64
                bar_w = max(1, int(group_w / len(series) * 0.8))
                bx = int(center - group_w / 2 + group_w * (si + 0.1) / len(series))
                shapes.append(rect_shape(f"Bar{si}_{ci}", bx, min(yy, zero_y), bar_w,
                                         max(6350, abs(yy - zero_y)), color))
            elif value is not None:
                points.append((center, yy))
                shapes.append(_marker_shape(f"Point{si}_{ci}", center - 45720, yy - 45720,
                                            91440, si))
            else:
                points.append(None)
            # Preserve exact input values; only the coordinate ticks are rounded.
            shapes.append(textbox(f"Value{si}_{ci}", "缺测" if value is None else str(value),
                                  x=plot_x + ci * cell_w, y=data_top + si * value_h,
                                  cx=cell_w, cy=value_h, size=label_size,
                                  color=_MUTED_COLOR if value is None else color, align="ctr", anchor="ctr"))
        for index in range(1, len(points)):
            if points[index - 1] is not None and points[index] is not None:
                shapes.append(_line_shape(f"Line{si}_{index}", *points[index - 1], *points[index],
                                          color, _CHART_DASHES[si]))
        shapes.append(textbox(f"ValueSeries{si}", item["name"], x=x + 68580,
                              y=data_top + si * value_h, cx=axis_w - 137160,
                              cy=value_h, size=label_size, color=color, anchor="ctr"))
    for index, category in enumerate(categories):
        shapes.append(textbox(f"Category{index}", category, x=plot_x + index * cell_w,
                              y=plot_y + plot_h + 91440, cx=cell_w - 45720,
                              cy=category_h, size=label_size, align="ctr"))
    return shapes


def _block_min_height(block: dict, width: int, reference_numbers: dict[str, int]) -> tuple[int, bool]:
    if not isinstance(block, dict):
        raise ValueError("columns 中每项必须是 text/image/chart/table 对象")
    kind = block.get("type", "text")
    meta = _meta_text(block, reference_numbers)
    overhead = _text_height(meta, width, 1400) + _BLOCK_GAP if meta else 0
    if block.get("title"):
        overhead += _text_height(block["title"], width, 1800) + _BLOCK_GAP
    if kind in ("text", "paragraph"):
        return _text_height(_inline_text(block, reference_numbers), width, 2000) + overhead, False
    if kind == "bullets":
        return _bullet_height(_block_bullets(block, reference_numbers), width) + overhead, False
    if kind == "table":
        return sum(_table_layout(block.get("headers", []), block.get("rows", []), width,
                                 block.get("column_widths"))[2]) + overhead, False
    if kind == "image":
        return 1371600 + overhead, True
    if kind == "chart":
        _validate_chart(block)
        _, _, category_h, value_h, _, _, top_h = _chart_metrics(block, width)
        minimum = top_h + 182880 + 1097280 + category_h + value_h * len(block["series"]) + 182880
        return minimum + overhead, True
    raise ValueError(f"columns 中不支持的块类型: {kind}")


def _render_block(block: dict, *, x: int, y: int, cx: int, cy: int,
                  media: ImageMedia, image_refs: list, first_rid: int,
                  reference_numbers: dict[str, int]) -> list[str]:
    shapes = []
    kind = block.get("type", "text")
    if block.get("title"):
        height = _text_height(block["title"], cx, 1800)
        shapes.append(textbox("BlockTitle", block["title"], x=x, y=y, cx=cx, cy=height,
                              size=1800, color=_PRIMARY_TEXT, bold=True))
        y += height + _BLOCK_GAP
        cy -= height + _BLOCK_GAP
    meta = _meta_text(block, reference_numbers)
    if kind == "table":
        table_h = sum(_table_layout(block.get("headers", []), block.get("rows", []), cx,
                                    block.get("column_widths"))[2])
        meta_h = _text_height(meta, cx, 1400) + _BLOCK_GAP if meta else 0
        # Keep short-table provenance next to the table, not at the page footer.
        cy = min(cy, table_h + meta_h)
    if meta and kind != "image":
        height = _text_height(meta, cx, 1400)
        shapes.append(textbox("CaptionSource", meta, x=x, y=y + cy - height,
                              cx=cx, cy=height, size=1400, color=_MUTED_COLOR))
        cy -= height + _BLOCK_GAP
    if cy <= 0:
        raise ValueError("图注/来源文字占满内容区域；请精简或拆页")
    if kind in ("text", "paragraph"):
        shapes.append(textbox("Text", _inline_text(block, reference_numbers), x=x, y=y, cx=cx, cy=cy, size=2000))
    elif kind == "bullets":
        items = _block_bullets(block, reference_numbers, prepared=bool(block.get("_prepared_bullets")))
        shapes.append(bullets_box("Content", items, x=x, y=y, cx=cx, cy=cy))
    elif kind == "table":
        headers, rows = block.get("headers", []), block.get("rows", [])
        if sum(_table_layout(headers, rows, cx, block.get("column_widths"))[2]) > cy:
            raise ValueError("表格超过当前栏高度；请将其移到独立 table 页自动拆分续表")
        shapes.append(table_frame("Table", headers, rows, x=x, y=y, cx=cx,
                                  column_widths=block.get("column_widths"), align=block.get("align")))
    elif kind == "image":
        path = block.get("path", "")
        entry = media.add(path)
        if entry is None:
            raise ValueError(f"图片无法嵌入: {path}；请检查文件是否存在、损坏、超限或非 PNG/JPEG/GIF/BMP")
        width, height = _image_dimensions(path)
        meta_h = _text_height(meta, cx, 1400) if meta else 0
        image_space = cy - meta_h - (_BLOCK_GAP if meta else 0)
        if image_space <= 0:
            raise ValueError("图片图注占满内容区域；请精简或拆页")
        scale = min(cx / width, image_space / height)
        iw, ih = round(width * scale), round(height * scale)
        if (width / height > 3 or height / width > 3) and min(iw, ih) < 1371600:
            raise ValueError(f"长图缩放后细节不可读: {path}；请制作保持上下文的原图页及分段放大图页")
        rid = next((rid for rid, target in image_refs if target == entry["target"]), None)
        if rid is None:
            rid = f"rId{first_rid + len(image_refs)}"
            image_refs.append((rid, entry["target"]))
        shapes.append(pic_shape(f"Picture {entry['num']}", rid,
                                x + (cx - iw) // 2, y, iw, ih,
                                str(block.get("alt") or block.get("caption") or Path(path).name)))
        if meta:
            shapes.append(textbox("CaptionSource", meta, x=x, y=y + ih + _BLOCK_GAP,
                                  cx=cx, cy=meta_h, size=1400, color=_MUTED_COLOR))
    elif kind == "chart":
        shapes.extend(_chart_shapes(block, x=x, y=y, cx=cx, cy=cy))
    else:
        raise ValueError(f"不支持的内容块: {kind}")
    return shapes


def _columns_shapes(slide: dict, *, y: int, cy: int, media: ImageMedia,
                    image_refs: list, first_rid: int, reference_numbers: dict[str, int]) -> list[str]:
    ratio = slide.get("ratio", [1, 1])
    if not isinstance(ratio, list) or len(ratio) != 2 \
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in ratio) \
            or not 0.5 <= ratio[0] / ratio[1] <= 2:
        raise ValueError("columns.ratio 必须为两个正数，比例介于 1:2 与 2:1")
    gap = 457200
    left_w = round((_BODY_W - gap) * ratio[0] / sum(ratio))
    widths = [left_w, _BODY_W - gap - left_w]
    shapes = []
    if slide.get("type") == "two_column":
        shapes.append(rect_shape("ColDivider", _BODY_X + left_w + gap // 2, y, 6350, cy, _BORDER))
    for side, x, width in zip(("left", "right"), (_BODY_X, _BODY_X + left_w + gap), widths):
        blocks = slide.get(side, [])
        if not isinstance(blocks, list) or not blocks:
            raise ValueError(f"columns.{side} 必须是非空内容块数组")
        measures = [_block_min_height(block, width, reference_numbers) for block in blocks]
        used = sum(h for h, _ in measures) + _BLOCK_GAP * (len(blocks) - 1)
        if used > cy:
            raise ValueError(f"columns.{side} 内容超出可读区域；请减少块数或移至独立页")
        flex_count = sum(flex for _, flex in measures)
        extra = (cy - used) // flex_count if flex_count else 0
        top = y
        for block, (height, flexible) in zip(blocks, measures):
            height += extra if flexible else 0
            shapes.extend(_render_block(block, x=x, y=top, cx=width, cy=height,
                                        media=media, image_refs=image_refs, first_rid=first_rid,
                                        reference_numbers=reference_numbers))
            top += height + _BLOCK_GAP
    return shapes

def build_slide(slide: dict, media: ImageMedia, ctx: dict):
    """Return (slide_xml, has_notes, image_refs).

    image_refs is a list of (rid, media_target) pairs that must be written into
    this slide's relationship part so embedded images resolve. ctx carries the
    page index/total and deck title/author for the academic footer.
    """
    stype = slide.get("type", "bullets")
    page_references = slide.get("_page_references", [])
    body_height = slide.get("_body_height", _BODY_H)
    reference_numbers = ctx.get("reference_numbers", {})
    # Build shape tree; IDs must be unique within a slide. We re-number after.
    shapes: list[str] = []
    image_refs: list[tuple[str, str]] = []

    has_notes = bool(slide.get("notes"))
    # rId layout: rId1=slideLayout, rId2=notes (if any), then images.
    next_rid = 3 if has_notes else 2

    if stype == "title":
        title = slide.get("title", "")
        if not (slide.get("caption") or slide.get("source")):
            markers = _citation_markers(slide, reference_numbers)
            title = (title or ctx.get("deck_title", "报告")) + markers if markers else title
        sub = slide.get("subtitle", "")
        title_color = _INK
        if _COVER_BAND:
            # 主色色带封面：标题区铺主色，标题反白
            shapes.append(rect_shape("TitleBand", 0, 1700000, SLIDE_W, 2950000, _PRIMARY))
            title_color = _ON_PRIMARY
        shapes.append(textbox("Title", title, x=914400, y=2150000, cx=10363200, cy=950000,
                              size=5000, color=title_color, align="ctr", anchor="ctr", bold=True))
        shapes.append(rect_shape("TitleRule", (SLIDE_W - 1600000) // 2, 3200000, 1600000, 28575, _GOLD))
        if sub:
            shapes.append(textbox("Subtitle", sub, x=1371600, y=3420000, cx=9448800, cy=650000,
                                  size=2400, color=_ON_PRIMARY if _COVER_BAND else _MUTED_COLOR,
                                  align="ctr", anchor="t"))
        shapes.append(rect_shape("TitleBottomRule", 685800, 6300000, 10820400, 9525, _BORDER))
        foot = []
        if ctx.get("author"):
            foot.append(str(ctx["author"]))
        if ctx.get("date_tag"):
            foot.append(str(ctx["date_tag"]))
        if foot:
            shapes.append(textbox("TitleFoot", "  ·  ".join(foot), x=685800, y=6340000,
                                  cx=10820400, cy=285750, size=1100, color=_MUTED_COLOR,
                                  align="ctr", anchor="ctr"))
    elif stype == "section":
        title = slide.get("title", "")
        if not (slide.get("caption") or slide.get("source")):
            markers = _citation_markers(slide, reference_numbers)
            title = (title or "参考依据") + markers if markers else title
        sub = slide.get("subtitle")
        shapes.append(rect_shape("SectionBand", 0, 0, 228600, SLIDE_H, _PRIMARY))
        shapes.append(textbox("Section", title, x=685800, y=2450000, cx=10500000, cy=770000,
                              size=4000, color=_PRIMARY_TEXT, align="l", anchor="ctr", bold=True))
        shapes.append(rect_shape("SectionRule", 685800, 3250000, 914400, 28575, _GOLD))
        if sub:
            shapes.append(textbox("SectionSub", sub, x=685800, y=3460000, cx=9448800, cy=650000,
                                  size=2400, color=_MUTED_COLOR, align="l", anchor="t"))
        shapes.extend(footer_shapes(ctx))
    else:
        title = slide.get("title", "")
        if title and stype in ("columns", "two_column") and not (slide.get("caption") or slide.get("source")):
            title += _citation_markers(slide, reference_numbers)
        if title:
            shapes.append(textbox("Title", title, x=685800, y=228600, cx=10820400, cy=743260,
                                  size=3500, color=_INK, align="l", anchor="t", bold=True))
            shapes.append(header_rule())
        numbers = ctx.get("reference_numbers", {})
        if stype == "references":
            top = _BODY_Y
            number_width = slide["number_width"]
            indent = number_width + 91440
            for entry in slide["entries"]:
                number, description, locator, body_h, locator_h = _reference_layout(entry, number_width)
                shapes.append(textbox("ReferenceNumber", number, x=_BODY_X, y=top,
                                      cx=number_width, cy=body_h, size=1600, color=_ACCENT, bold=True))
                shapes.append(textbox("Reference", description, x=_BODY_X + indent, y=top,
                                      cx=_BODY_W - indent, cy=body_h, size=1600, color=_INK))
                if locator:
                    shapes.append(textbox("ReferenceLocator", locator, x=_BODY_X + indent,
                                          y=top + body_h + 45720, cx=_BODY_W - indent,
                                          cy=locator_h - 45720, size=1400, color=_MUTED_COLOR))
                top += body_h + locator_h + 228600
        elif stype in ("columns", "two_column"):
            meta = _meta_text(slide, numbers)
            body_h = body_height
            if meta:
                height = _text_height(meta, _BODY_W, 1400)
                shapes.append(textbox("SlideSource", meta, x=_BODY_X, y=_BODY_Y + body_h - height,
                                      cx=_BODY_W, cy=height, size=1400, color=_MUTED_COLOR))
                body_h -= height + _BLOCK_GAP
            columns = slide
            if stype == "two_column":
                columns = {**slide, "left": [{"type": "bullets", "bullets": slide.get("left", [])}],
                           "right": [{"type": "bullets", "bullets": slide.get("right", [])}]}
            shapes.extend(_columns_shapes(columns, y=_BODY_Y, cy=body_h, media=media,
                                          image_refs=image_refs, first_rid=next_rid,
                                          reference_numbers=numbers))
        else:
            block = {key: value for key, value in slide.items() if key != "title"}
            block["type"] = stype
            block["_prepared_bullets"] = stype == "bullets"
            shapes.extend(_render_block(block, x=_BODY_X, y=_BODY_Y, cx=_BODY_W, cy=body_height,
                                        media=media, image_refs=image_refs, first_rid=next_rid,
                                        reference_numbers=numbers))
        shapes.extend(footer_shapes(ctx))

    if stype in ("title", "section"):
        meta = _meta_text(slide, ctx.get("reference_numbers", {}))
        if meta:
            height = _text_height(meta, _BODY_W, 1400)
            if height > 1371600:
                raise ValueError("封面/章节页的来源过长；请移到内容页或参考文献列表")
            meta_y = min(6000000 - height, _BODY_Y + body_height - height)
            if meta_y < 4114800:
                raise ValueError("封面/章节页来源与本页参考文献区域过密；请拆页")
            shapes.append(textbox("SlideSource", meta, x=_BODY_X, y=meta_y,
                                  cx=_BODY_W, cy=height, size=1400, color=_MUTED_COLOR))

    shapes.extend(_page_reference_shapes(page_references))

    # Renumber ids deterministically.
    renumbered = renumber_shape_ids(shapes)
    sld = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        f'<p:cSld name="medex:{stype}">'
        f'{_slide_background()}'
        '<p:spTree>'
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/>'
        '<p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        '{shapes}'
        '</p:spTree>'
        '</p:cSld>'
        '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>'
        '</p:sld>'
    ).format(shapes="".join(renumbered))
    return sld, has_notes, image_refs


def renumber_shape_ids(shapes: list[str]) -> list[str]:
    """Assign increasing cNvPr id values starting from 2 within a slide."""
    out = []
    next_id = 2
    for s in shapes:
        s2 = s.replace('<p:cNvPr id="0"', f'<p:cNvPr id="{next_id}"', 1)
        out.append(s2)
        next_id += 1
    return out


def build_notes(text: str) -> str:
    body_pr = '<a:bodyPr vert="horz" wrap="square" anchor="t"/>'
    ppr = '<a:pPr algn="l"><a:buNone/></a:pPr>'
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<p:notes xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:cSld>'
        '<p:spTree>'
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/>'
        '<p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        '<p:sp>'
        '<p:nvSpPr><p:cNvPr id="2" name="Notes Placeholder"/><p:cNvSpPr>'
        '<a:spLocks noGrp="1"/></p:cNvSpPr>'
        '<p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>'
        '<p:spPr>'
        '<a:xfrm><a:off x="91440" y="91440"/><a:ext cx="5297460" cy="3931920"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
        f'<p:txBody>{body_pr}<a:lstStyle/><a:p>{ppr}{runs_xml(text, size=1400, color=_TEXT_COLOR)}</a:p></p:txBody>'
        '</p:sp>'
        '</p:spTree>'
        '</p:cSld>'
        '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>'
        '</p:notes>'
    )


# --- package parts ---------------------------------------------------------

def content_types_xml(slide_overrides: str, media_exts: set[str]) -> str:
    defaults = (
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
    )
    for ext in sorted(media_exts):
        ctype = SUPPORTED_IMAGE_TYPES[ext]
        defaults += f'<Default Extension="{ext}" ContentType="{ctype}"/>'
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        f'{defaults}'
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
        '<Override PartName="/ppt/presProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presProps+xml"/>'
        '<Override PartName="/ppt/viewProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.viewProps+xml"/>'
        '<Override PartName="/ppt/tableStyles.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.tableStyles+xml"/>'
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        f'{slide_overrides}'
        '</Types>'
    )

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

THEME = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Medex">
<a:themeElements>
<a:clrScheme name="Medex"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>
<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>
<a:dk2><a:srgbClr val="1F2328"/></a:dk2><a:lt2><a:srgbClr val="FFFFFF"/></a:lt2>
<a:accent1><a:srgbClr val="19364D"/></a:accent1><a:accent2><a:srgbClr val="007F7A"/></a:accent2>
<a:accent3><a:srgbClr val="3869A8"/></a:accent3><a:accent4><a:srgbClr val="B87924"/></a:accent4>
<a:accent5><a:srgbClr val="8461A0"/></a:accent5><a:accent6><a:srgbClr val="617180"/></a:accent6>
<a:hlink><a:srgbClr val="007F7A"/></a:hlink><a:folHlink><a:srgbClr val="617180"/></a:folHlink></a:clrScheme>
<a:fontScheme name="Medex"><a:majorFont><a:latin typeface="Calibri"/><a:ea typeface="微软雅黑"/>
<a:cs typeface="Calibri"/></a:majorFont><a:minorFont><a:latin typeface="Calibri"/><a:ea typeface="微软雅黑"/>
<a:cs typeface="Calibri"/></a:minorFont></a:fontScheme>
<a:fmtScheme name="Medex"><a:fillStyleLst><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>
<a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></a:fillStyleLst>
<a:lnStyleLst><a:ln w="6350" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:srgbClr val="D8DEE4"/></a:solidFill>
<a:prstDash val="solid"/></a:ln><a:ln w="12700" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:srgbClr val="D8DEE4"/></a:solidFill>
<a:prstDash val="solid"/></a:ln><a:ln w="19050" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:srgbClr val="D8DEE4"/></a:solidFill>
<a:prstDash val="solid"/></a:ln></a:lnStyleLst>
<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle>
<a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>
<a:bgFillStyleLst><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>
<a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></a:bgFillStyleLst></a:fmtScheme></a:themeElements>
<a:objectDefaults/><a:extraClrSchemeLst/></a:theme>"""

# Notes use their own theme part, as produced by PowerPoint/LibreOffice.  Keeping
# the part separate avoids web viewers treating notes-page dimensions and font
# defaults as an extension of the slide theme.
NOTES_THEME = THEME.replace('name="Medex"', 'name="Medex Notes"')

PRESENTATION_PROPERTIES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentationPr xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>"""

VIEW_PROPERTIES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:viewPr xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 lastView="sldView" showComments="1"/>"""

TABLE_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:tblStyleLst xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 def="{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}"/>"""

_SLIDE_MASTER_TEXT_STYLES = """
<p:txStyles>
<p:titleStyle><a:lvl1pPr marL="0" indent="0" algn="l"><a:buNone/>
<a:defRPr lang="zh-CN" sz="3500" b="1"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill>
<a:latin typeface="+mj-lt"/><a:ea typeface="+mj-ea"/><a:cs typeface="+mj-cs"/></a:defRPr>
</a:lvl1pPr></p:titleStyle>
<p:bodyStyle>
<a:lvl1pPr marL="342900" indent="-285750" algn="l"><a:buChar char="•"/>
<a:defRPr lang="zh-CN" sz="2000"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill>
<a:latin typeface="+mn-lt"/><a:ea typeface="+mn-ea"/><a:cs typeface="+mn-cs"/></a:defRPr></a:lvl1pPr>
<a:lvl2pPr marL="742950" indent="-285750" algn="l"><a:buChar char="–"/>
<a:defRPr lang="zh-CN" sz="1800"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill>
<a:latin typeface="+mn-lt"/><a:ea typeface="+mn-ea"/><a:cs typeface="+mn-cs"/></a:defRPr></a:lvl2pPr>
</p:bodyStyle>
<p:otherStyle><a:lvl1pPr marL="0" indent="0" algn="l"><a:buNone/>
<a:defRPr lang="zh-CN" sz="1800"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill>
<a:latin typeface="+mn-lt"/><a:ea typeface="+mn-ea"/><a:cs typeface="+mn-cs"/></a:defRPr>
</a:lvl1pPr></p:otherStyle>
</p:txStyles>"""

SLIDE_MASTER = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>
<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld>
<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2"
 accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>
""" + _SLIDE_MASTER_TEXT_STYLES + """
</p:sldMaster>"""

SLIDE_MASTER_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>
</Relationships>"""

SLIDE_LAYOUT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1">
<p:cSld name="Blank"><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>
<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld>
</p:sldLayout>"""

SLIDE_LAYOUT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>"""


def presentation_xml(n_slides: int, notes_slides: list[int]) -> str:
    sld_ids = "".join(
        f'<p:sldId id="{256 + i}" r:id="rId{i + 2}"/>' for i in range(n_slides)
    )
    notes_master_rid = f"rId{n_slides + 2}"
    notes_master = (
        f'<p:notesMasterIdLst><p:notesMasterId r:id="{notes_master_rid}"/>'
        '</p:notesMasterIdLst>'
        if notes_slides else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" saveSubsetFonts="1">'
        f'<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        f'{notes_master}'
        f'<p:sldIdLst>{sld_ids}</p:sldIdLst>'
        f'<p:sldSz cx="{SLIDE_W}" cy="{SLIDE_H}" type="screen16x9"/>'
        '<p:notesSz cx="6858000" cy="9144000"/>'
        '</p:presentation>'
    )


def presentation_rels(n_slides: int, has_notes_master: bool) -> str:
    rels = ['<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>']
    for i in range(n_slides):
        rels.append(
            f'<Relationship Id="rId{i + 2}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i + 1}.xml"/>'
        )
    if has_notes_master:
        rels.append(
            f'<Relationship Id="rId{n_slides + 2}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesMaster" Target="notesMasters/notesMaster1.xml"/>'
        )
    next_id = n_slides + 3 if has_notes_master else n_slides + 2
    rels.extend((
        f'<Relationship Id="rId{next_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>',
        f'<Relationship Id="rId{next_id + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/presProps" Target="presProps.xml"/>',
        f'<Relationship Id="rId{next_id + 2}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/viewProps" Target="viewProps.xml"/>',
        f'<Relationship Id="rId{next_id + 3}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/tableStyles" Target="tableStyles.xml"/>',
    ))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(rels) + '</Relationships>'
    )


def slide_rels(slide_num: int, has_notes: bool, image_refs: list[tuple[str, str]]) -> str:
    rels = [
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
    ]
    if has_notes:
        rels.append(
            f'<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" Target="../notesSlides/notesSlide{slide_num}.xml"/>'
        )
    for rid, target in image_refs:
        rels.append(
            f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="{target}"/>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(rels) + '</Relationships>'
    )


def notes_rels(slide_num: int) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
        f'Target="../slides/slide{slide_num}.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesMaster" '
        'Target="../notesMasters/notesMaster1.xml"/>'
        '</Relationships>'
    )


NOTES_MASTER = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:notesMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>
<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
<p:sp>
<p:nvSpPr><p:cNvPr id="2" name="Notes Placeholder 2"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>
<p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>
<p:spPr><a:xfrm><a:off x="91440" y="91440"/><a:ext cx="5297460" cy="3931920"/></a:xfrm>
<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
<p:txBody><a:bodyPr vert="horz" wrap="square" anchor="t"/>
<a:lstStyle><a:lvl1pPr marL="0" indent="0"><a:buNone/></a:lvl1pPr></a:lstStyle>
<a:p><a:endParaRPr lang="zh-CN"/></a:p></p:txBody>
</p:sp>
</p:spTree></p:cSld>
<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2"
 accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
<p:notesStyle><a:lvl1pPr marL="0" indent="0" algn="l"><a:buNone/>
<a:defRPr lang="zh-CN" sz="1400"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill>
<a:latin typeface="+mn-lt"/><a:ea typeface="+mn-ea"/><a:cs typeface="+mn-cs"/></a:defRPr>
</a:lvl1pPr></p:notesStyle>
</p:notesMaster>"""

NOTES_MASTER_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme2.xml"/>
</Relationships>"""


def core_xml(title: str, author: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:dcterms="http://purl.org/dc/terms/"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{xml_text(title)}</dc:title>"
        f"<dc:creator>{xml_text(author)}</dc:creator>"
        "<cp:lastModifiedBy>Medex</cp:lastModifiedBy>"
        "</cp:coreProperties>"
    )


def app_xml(slide_count: int, titles: list[str]) -> str:
    vt_ns = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
    part_titles = ["Medex"] + titles
    lpstr = "".join(f"<vt:lpstr>{xml_text(t)}</vt:lpstr>" for t in part_titles)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        f'xmlns:vt="{vt_ns}">'
        "<PresentationFormat>On-screen Show (16:9)</PresentationFormat>"
        "<Application>Medex PPT Report Generator</Application>"
        f"<Slides>{slide_count}</Slides>"
        "<HeadingPairs>"
        '<vt:vector size="4" baseType="variant">'
        "<vt:variant><vt:lpstr>Theme</vt:lpstr></vt:variant>"
        "<vt:variant><vt:i4>1</vt:i4></vt:variant>"
        "<vt:variant><vt:lpstr>Slide Titles</vt:lpstr></vt:variant>"
        f"<vt:variant><vt:i4>{slide_count}</vt:i4></vt:variant>"
        "</vt:vector>"
        "</HeadingPairs>"
        "<TitlesOfParts>"
        f'<vt:vector size="{len(part_titles)}" baseType="lpstr">{lpstr}</vt:vector>'
        "</TitlesOfParts>"
        "<Company>Medex</Company>"
        "<AppVersion>16.0000</AppVersion>"
        "</Properties>"
    )


def build_package(spec: dict, out: Path) -> tuple[int, dict, int]:
    apply_style(spec.get("_style") or None)
    _validate_spec_asset_paths(spec)
    slides, reference_numbers = _prepare_slides(spec)

    title = spec.get("title") or "报告"
    author = spec.get("author") or ""

    # Build slides + notes.
    slide_xmls: list[str] = []
    slide_meta: list[dict] = []  # per-slide {has_notes, image_refs}
    notes_data: list[tuple[int, str]] = []  # (slide_num, notes_text)
    titles: list[str] = []
    counts = {"title": 0, "section": 0, "bullets": 0, "table": 0,
              "two_column": 0, "image": 0, "text": 0, "columns": 0,
              "chart": 0, "references": 0}
    media = ImageMedia()

    for i, slide in enumerate(slides, start=1):
        stype = slide.get("type", "bullets")
        counts[stype] = counts.get(stype, 0) + 1
        ctx = {"index": i, "total": len(slides), "deck_title": title,
               "author": author, "date_tag": datetime.now().strftime("%Y-%m-%d"),
               "reference_numbers": reference_numbers}
        sxml, has_notes, image_refs = build_slide(slide, media, ctx)
        slide_xmls.append(sxml)
        slide_meta.append({"has_notes": has_notes, "image_refs": image_refs})
        notes = slide.get("notes")
        if notes:
            notes_data.append((i, str(notes)))
        t = slide.get("title") or slide.get("subtitle") or f"Slide {i}"
        titles.append(str(t))

    n = len(slide_xmls)
    slide_overrides = "".join(
        f'<Override PartName="/ppt/slides/slide{i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for i in range(n)
    )
    notes_overrides = "".join(
        f'<Override PartName="/ppt/notesSlides/notesSlide{sn}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"/>'
        for sn, _ in notes_data
    )
    notes_master_override = (
        '<Override PartName="/ppt/notesMasters/notesMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.notesMaster+xml"/>'
        '<Override PartName="/ppt/theme/theme2.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
        if notes_data else ""
    )
    content_types = content_types_xml(
        slide_overrides + notes_overrides + notes_master_override,
        media.extensions(),
    )

    has_notes_master = bool(notes_data)

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", RELS)
        z.writestr("ppt/presentation.xml", presentation_xml(n, [sn for sn, _ in notes_data]))
        z.writestr("ppt/_rels/presentation.xml.rels", presentation_rels(n, has_notes_master))
        z.writestr("ppt/presProps.xml", PRESENTATION_PROPERTIES)
        z.writestr("ppt/viewProps.xml", VIEW_PROPERTIES)
        z.writestr("ppt/tableStyles.xml", TABLE_STYLES)
        z.writestr("ppt/theme/theme1.xml", theme_xml())
        z.writestr("ppt/slideMasters/slideMaster1.xml", SLIDE_MASTER)
        z.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", SLIDE_MASTER_RELS)
        z.writestr("ppt/slideLayouts/slideLayout1.xml", SLIDE_LAYOUT)
        z.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", SLIDE_LAYOUT_RELS)
        for i, sxml in enumerate(slide_xmls, start=1):
            z.writestr(f"ppt/slides/slide{i}.xml", sxml)
            meta_i = slide_meta[i - 1]
            z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels",
                       slide_rels(i, meta_i["has_notes"], meta_i["image_refs"]))
        # Embedded images (self-contained, downloadable).
        for e in media.items():
            z.writestr(f"ppt/media/image{e['num']}.{e['ext']}", e["bytes"])
        if has_notes_master:
            z.writestr("ppt/theme/theme2.xml", theme_xml("Medex Notes"))
            z.writestr("ppt/notesMasters/notesMaster1.xml", NOTES_MASTER)
            z.writestr("ppt/notesMasters/_rels/notesMaster1.xml.rels", NOTES_MASTER_RELS)
            for sn, ntext in notes_data:
                z.writestr(f"ppt/notesSlides/notesSlide{sn}.xml", build_notes(ntext))
                z.writestr(f"ppt/notesSlides/_rels/notesSlide{sn}.xml.rels", notes_rels(sn))
        z.writestr("docProps/core.xml", core_xml(title, author))
        z.writestr("docProps/app.xml", app_xml(n, titles))

    return n, counts, len(media.items())


def _unique_package_names(package: zipfile.ZipFile) -> set[str]:
    names: set[str] = set()
    duplicates: set[str] = set()
    for name in package.namelist():
        if name in names:
            duplicates.add(name)
        names.add(name)
    if duplicates:
        raise ValueError(
            "PPTX 包含重复部件名: " + ", ".join(sorted(duplicates))
        )
    return names


def _relationship_source_name(relationship_part: str) -> str:
    if relationship_part == "_rels/.rels":
        return ""
    relationship_dir = posixpath.dirname(relationship_part)
    if posixpath.basename(relationship_dir) != "_rels":
        raise ValueError(f"OOXML 关系部件路径无效: {relationship_part}")
    filename = posixpath.basename(relationship_part)
    if not filename.endswith(".rels") or filename == ".rels":
        raise ValueError(f"OOXML 关系部件路径无效: {relationship_part}")
    source_dir = posixpath.dirname(relationship_dir)
    return posixpath.join(source_dir, filename[:-5])


def _expected_content_type_for_part(name: str) -> str | None:
    if name.endswith(".rels"):
        return "application/vnd.openxmlformats-package.relationships+xml"
    fixed = {
        "docProps/app.xml": (
            "application/vnd.openxmlformats-officedocument.extended-properties+xml"
        ),
        "docProps/core.xml": "application/vnd.openxmlformats-package.core-properties+xml",
        "ppt/presentation.xml": (
            "application/vnd.openxmlformats-officedocument.presentationml."
            "presentation.main+xml"
        ),
        "ppt/presProps.xml": (
            "application/vnd.openxmlformats-officedocument.presentationml.presProps+xml"
        ),
        "ppt/viewProps.xml": (
            "application/vnd.openxmlformats-officedocument.presentationml.viewProps+xml"
        ),
        "ppt/tableStyles.xml": (
            "application/vnd.openxmlformats-officedocument.presentationml.tableStyles+xml"
        ),
    }
    if name in fixed:
        return fixed[name]
    for pattern, content_type in (
        (
            r"ppt/slideMasters/slideMaster\d+\.xml",
            "application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml",
        ),
        (
            r"ppt/slideLayouts/slideLayout\d+\.xml",
            "application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml",
        ),
        (
            r"ppt/slides/slide\d+\.xml",
            "application/vnd.openxmlformats-officedocument.presentationml.slide+xml",
        ),
        (
            r"ppt/notesMasters/notesMaster\d+\.xml",
            "application/vnd.openxmlformats-officedocument.presentationml.notesMaster+xml",
        ),
        (
            r"ppt/notesSlides/notesSlide\d+\.xml",
            "application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml",
        ),
        (
            r"ppt/theme/theme\d+\.xml",
            "application/vnd.openxmlformats-officedocument.theme+xml",
        ),
    ):
        if re.fullmatch(pattern, name):
            return content_type
    if name.startswith("ppt/media/"):
        return SUPPORTED_IMAGE_TYPES.get(Path(name).suffix.lower().lstrip("."))
    return None


def _validate_content_type_bindings(
    content_types: ET.Element,
    package_names: set[str],
) -> None:
    namespace = "{http://schemas.openxmlformats.org/package/2006/content-types}"
    defaults: dict[str, str] = {}
    overrides: dict[str, str] = {}
    for node in content_types.findall(f"{namespace}Default"):
        extension = node.get("Extension", "").lower()
        if not extension or extension in defaults:
            raise ValueError(f"OOXML Content Types 默认扩展名缺失或重复: {extension}")
        defaults[extension] = node.get("ContentType", "")
    for node in content_types.findall(f"{namespace}Override"):
        part_name = node.get("PartName", "")
        if not part_name.startswith("/") or part_name in overrides:
            raise ValueError(f"OOXML Content Types Override 路径无效或重复: {part_name}")
        overrides[part_name] = node.get("ContentType", "")

    missing_parts = sorted(
        part_name for part_name in overrides if part_name[1:] not in package_names
    )
    if missing_parts:
        raise ValueError(
            "OOXML Content Types 引用了不存在的部件: " + ", ".join(missing_parts)
        )

    for name in sorted(package_names - {"[Content_Types].xml"}):
        if name.endswith("/"):
            continue
        expected = _expected_content_type_for_part(name)
        if expected is None:
            raise ValueError(f"OOXML Content Types 没有受支持的部件映射: {name}")
        extension = "rels" if name.endswith(".rels") else Path(name).suffix.lower().lstrip(".")
        actual = overrides.get(f"/{name}", defaults.get(extension, ""))
        if actual != expected:
            raise ValueError(
                f"OOXML Content Type 与部件不匹配: {name}: "
                f"expected {expected}, got {actual or '<missing>'}"
            )


def _validate_relationship_topology(
    package_names: set[str],
    roots: dict[str, ET.Element],
    relationships: dict[str, dict[str, tuple[str, str, bool]]],
) -> None:
    office_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    package_rel = "http://schemas.openxmlformats.org/package/2006/relationships/"
    presentation = "ppt/presentation.xml"
    presentation_properties = "ppt/presProps.xml"
    view_properties = "ppt/viewProps.xml"
    table_styles = "ppt/tableStyles.xml"
    slide_masters = sorted(
        name for name in package_names
        if re.fullmatch(r"ppt/slideMasters/slideMaster\d+\.xml", name)
    )
    slide_layouts = sorted(
        name for name in package_names
        if re.fullmatch(r"ppt/slideLayouts/slideLayout\d+\.xml", name)
    )
    slides = sorted(
        name for name in package_names
        if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
    )
    notes_masters = sorted(
        name for name in package_names
        if re.fullmatch(r"ppt/notesMasters/notesMaster\d+\.xml", name)
    )
    notes_slides = sorted(
        name for name in package_names
        if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", name)
    )
    themes = sorted(
        name for name in package_names
        if re.fullmatch(r"ppt/theme/theme\d+\.xml", name)
    )
    if len(slide_masters) != 1 or len(slide_layouts) != 1:
        raise ValueError(
            "OOXML 关系拓扑不符合生成器约束: 必须各有一个 slideMaster 和 "
            "slideLayout"
        )
    if len(notes_masters) > 1:
        raise ValueError("OOXML 关系拓扑不符合生成器约束: notesMaster 最多一个")
    expected_theme_names = {"ppt/theme/theme1.xml"}
    if notes_masters:
        expected_theme_names.add("ppt/theme/theme2.xml")
    if set(themes) != expected_theme_names:
        raise ValueError(
            "OOXML 关系拓扑不符合生成器约束: theme 部件不匹配: "
            f"expected={sorted(expected_theme_names)}, got={themes}"
        )

    expected: dict[str, set[tuple[str, str]]] = {
        "": {
            (f"{office_rel}officeDocument", presentation),
            (f"{package_rel}metadata/core-properties", "docProps/core.xml"),
            (f"{office_rel}extended-properties", "docProps/app.xml"),
        },
        presentation: {
            *((f"{office_rel}slideMaster", target) for target in slide_masters),
            *((f"{office_rel}slide", target) for target in slides),
            *((f"{office_rel}notesMaster", target) for target in notes_masters),
            (f"{office_rel}theme", "ppt/theme/theme1.xml"),
            (f"{office_rel}presProps", presentation_properties),
            (f"{office_rel}viewProps", view_properties),
            (f"{office_rel}tableStyles", table_styles),
        },
    }
    for master in slide_masters:
        expected[master] = {
            *((f"{office_rel}slideLayout", target) for target in slide_layouts),
            (f"{office_rel}theme", "ppt/theme/theme1.xml"),
        }
    for layout in slide_layouts:
        expected[layout] = {
            (f"{office_rel}slideMaster", slide_masters[0]),
        }
    for notes_master in notes_masters:
        expected[notes_master] = {
            (f"{office_rel}theme", "ppt/theme/theme2.xml"),
        }

    relationship_ns = (
        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    )
    presentation_ns = (
        "{http://schemas.openxmlformats.org/presentationml/2006/main}"
    )
    relationship_id_attr = f"{relationship_ns}id"

    def validate_bound_targets(
        source: str,
        element_path: str,
        relationship_type: str,
        expected_targets: set[str],
    ) -> None:
        actual_targets: list[str] = []
        for element in roots[source].findall(element_path):
            relationship_id = element.get(relationship_id_attr, "")
            record = relationships.get(source, {}).get(relationship_id)
            if record is None:
                raise ValueError(
                    f"OOXML 元素引用了不存在的关系: {source}: {relationship_id}"
                )
            rel_type, target, external = record
            if external or rel_type != relationship_type or target not in expected_targets:
                raise ValueError(
                    f"OOXML 元素与关系类型/目标不匹配: {source}: "
                    f"{relationship_id} -> ({rel_type}, {target})"
                )
            actual_targets.append(target)
        if len(actual_targets) != len(set(actual_targets)) \
                or set(actual_targets) != expected_targets:
            raise ValueError(
                f"OOXML 元素关系绑定不完整或重复: {source}: "
                f"expected={sorted(expected_targets)}, got={sorted(actual_targets)}"
            )

    validate_bound_targets(
        presentation,
        f"{presentation_ns}sldMasterIdLst/{presentation_ns}sldMasterId",
        f"{office_rel}slideMaster",
        set(slide_masters),
    )
    validate_bound_targets(
        presentation,
        f"{presentation_ns}sldIdLst/{presentation_ns}sldId",
        f"{office_rel}slide",
        set(slides),
    )
    validate_bound_targets(
        presentation,
        f"{presentation_ns}notesMasterIdLst/{presentation_ns}notesMasterId",
        f"{office_rel}notesMaster",
        set(notes_masters),
    )
    for master in slide_masters:
        validate_bound_targets(
            master,
            f"{presentation_ns}sldLayoutIdLst/{presentation_ns}sldLayoutId",
            f"{office_rel}slideLayout",
            set(slide_layouts),
        )

    image_type = f"{office_rel}image"
    media_parts = {name for name in package_names if name.startswith("ppt/media/")}
    notes_by_number = {
        re.search(r"(\d+)\.xml$", name).group(1): name
        for name in notes_slides
    }
    slides_by_number = {
        re.search(r"(\d+)\.xml$", name).group(1): name
        for name in slides
    }
    for slide in slides:
        slide_number = re.search(r"(\d+)\.xml$", slide).group(1)
        required = {(f"{office_rel}slideLayout", slide_layouts[0])}
        if slide_number in notes_by_number:
            required.add((f"{office_rel}notesSlide", notes_by_number[slide_number]))
        for element in roots[slide].iter():
            embedded_id = element.get(f"{relationship_ns}embed")
            if embedded_id is None:
                continue
            record = relationships.get(slide, {}).get(embedded_id)
            if record is None:
                continue
            rel_type, target, external = record
            if external or rel_type != image_type or target not in media_parts:
                raise ValueError(
                    f"OOXML 图片关系类型或目标无效: {slide}: {embedded_id}"
                )
            required.add((image_type, target))
        expected[slide] = required

    for notes_slide in notes_slides:
        slide_number = re.search(r"(\d+)\.xml$", notes_slide).group(1)
        if slide_number not in slides_by_number or not notes_masters:
            raise ValueError(f"OOXML 备注页没有对应 slide/notesMaster: {notes_slide}")
        expected[notes_slide] = {
            (f"{office_rel}slide", slides_by_number[slide_number]),
            (f"{office_rel}notesMaster", notes_masters[0]),
        }

    extra_sources = sorted(set(relationships) - set(expected))
    if extra_sources:
        raise ValueError(
            "OOXML 存在不受支持的关系源: " + ", ".join(extra_sources)
        )
    for source, required in expected.items():
        source_relationships = relationships.get(source)
        source_label = source or "/"
        if source_relationships is None:
            raise ValueError(f"OOXML 缺少必需关系部件: {source_label}")
        actual: set[tuple[str, str]] = set()
        for rel_type, target, external in source_relationships.values():
            if external:
                raise ValueError(f"OOXML 生成器不支持外部关系: {source_label}")
            pair = (rel_type, target)
            if pair in actual:
                raise ValueError(f"OOXML 关系类型/目标重复: {source_label}: {pair}")
            actual.add(pair)
        if actual != required:
            missing = sorted(required - actual)
            unexpected = sorted(actual - required)
            raise ValueError(
                f"OOXML 关系拓扑不匹配: {source_label}: "
                f"missing={missing}, unexpected={unexpected}"
            )


def _validate_office_compatibility_parts(
    package_names: set[str],
    roots: dict[str, ET.Element],
) -> None:
    """Check conventional Office package metadata beyond XSD validity."""
    presentation_ns = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
    drawing_ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    extended_ns = (
        "{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}"
    )
    variant_ns = (
        "{http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes}"
    )

    group_transform_parts = sorted(
        name for name in package_names
        if re.fullmatch(
            r"ppt/(?:slideMasters/slideMaster\d+|slideLayouts/slideLayout\d+|"
            r"slides/slide\d+|notesMasters/notesMaster\d+|"
            r"notesSlides/notesSlide\d+)\.xml",
            name,
        )
    )
    expected_transform_children = {
        f"{drawing_ns}off": ("x", "y"),
        f"{drawing_ns}ext": ("cx", "cy"),
        f"{drawing_ns}chOff": ("x", "y"),
        f"{drawing_ns}chExt": ("cx", "cy"),
    }
    for name in group_transform_parts:
        transform = roots[name].find(
            f"{presentation_ns}cSld/{presentation_ns}spTree/"
            f"{presentation_ns}grpSpPr/{drawing_ns}xfrm"
        )
        if transform is None:
            raise ValueError(f"OOXML 顶层分组缺少变换: {name}")
        for child_tag, attributes in expected_transform_children.items():
            child = transform.find(child_tag)
            if child is None or any(child.get(attribute) != "0" for attribute in attributes):
                local_name = child_tag.split("}", 1)[1]
                raise ValueError(
                    f"OOXML 顶层分组变换必须归零: {name}: {local_name}"
                )

    slide_master = roots["ppt/slideMasters/slideMaster1.xml"]
    text_styles = slide_master.find(f"{presentation_ns}txStyles")
    if text_styles is None or any(
        text_styles.find(f"{presentation_ns}{style_name}") is None
        for style_name in ("titleStyle", "bodyStyle", "otherStyle")
    ):
        raise ValueError("OOXML slideMaster 缺少完整 txStyles")
    notes_masters = sorted(
        name for name in package_names
        if re.fullmatch(r"ppt/notesMasters/notesMaster\d+\.xml", name)
    )
    for name in notes_masters:
        if roots[name].find(f"{presentation_ns}notesStyle") is None:
            raise ValueError(f"OOXML notesMaster 缺少 notesStyle: {name}")

    app = roots["docProps/app.xml"]
    slides = sorted(
        name for name in package_names
        if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
    )
    slide_count = len(slides)
    slide_count_node = app.find(f"{extended_ns}Slides")
    if slide_count_node is None or slide_count_node.text != str(slide_count):
        raise ValueError("OOXML app.xml 的 Slides 计数与实际页数不一致")

    heading_vector = app.find(f"{extended_ns}HeadingPairs/{variant_ns}vector")
    if heading_vector is None \
            or heading_vector.get("size") != "4" \
            or heading_vector.get("baseType") != "variant":
        raise ValueError("OOXML app.xml 的 HeadingPairs 结构无效")
    heading_values: list[tuple[str, str]] = []
    for variant in heading_vector.findall(f"{variant_ns}variant"):
        children = list(variant)
        if len(children) != 1:
            raise ValueError("OOXML app.xml 的 HeadingPairs variant 无效")
        heading_values.append((children[0].tag, children[0].text or ""))
    expected_headings = [
        (f"{variant_ns}lpstr", "Theme"),
        (f"{variant_ns}i4", "1"),
        (f"{variant_ns}lpstr", "Slide Titles"),
        (f"{variant_ns}i4", str(slide_count)),
    ]
    if heading_values != expected_headings:
        raise ValueError("OOXML app.xml 的 HeadingPairs 与演示内容不一致")

    titles_vector = app.find(f"{extended_ns}TitlesOfParts/{variant_ns}vector")
    title_nodes = [] if titles_vector is None else titles_vector.findall(
        f"{variant_ns}lpstr"
    )
    if titles_vector is None \
            or titles_vector.get("baseType") != "lpstr" \
            or titles_vector.get("size") != str(slide_count + 1) \
            or len(title_nodes) != slide_count + 1 \
            or (title_nodes[0].text or "") != "Medex":
        raise ValueError("OOXML app.xml 的 TitlesOfParts 与 HeadingPairs 不一致")


def _validate_visible_slide_content(root: ET.Element, part_name: str) -> None:
    """Reject empty generated slides, including older packages without a spec.

    Only this generator's named content shapes and layout signatures participate;
    a title, caption, reference footer or decorative rule is not body content.
    """
    p = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
    a = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    r = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    common = root.find(f"{p}cSld")
    if common is None:
        return
    tree = common.find(f"{p}spTree")
    if tree is None:
        return
    named: dict[str, list[ET.Element]] = defaultdict(list)
    for shape in tree:
        properties = shape.find(f".//{p}cNvPr")
        if properties is not None:
            named[properties.get("name", "")].append(shape)
    names = set(named)
    marker = common.get("name", "")
    kind = marker.removeprefix("medex:") if marker.startswith("medex:") else None
    if kind is None:
        if {"Title", "TitleRule", "TitleBottomRule"} <= names:
            kind = "title"
        elif {"Section", "SectionBand", "SectionRule"} <= names:
            kind = "section"
        elif {"FooterRule", "FooterL", "FooterR"} <= names and (
                {"Title", "HeaderRule"} <= names or names.intersection(
                    {"Content", "Text", "Table", "ChartUnit", "Reference"})
                or any(name.startswith("Picture ") for name in names)):
            kind = "references" if {"Reference", "ReferenceNumber"} <= names else "content"
        else:
            return  # Do not classify arbitrary external shape names as empty.

    def plain_run_text(shape: ET.Element) -> tuple[str, bool]:
        pieces, has_citation = [], False
        for run in shape.iter(f"{a}r"):
            properties = run.find(f"{a}rPr")
            elevated = properties is not None and int(properties.get("baseline", "0")) > 0
            if elevated:
                has_citation = True
            else:
                pieces.extend(text.text or "" for text in run.findall(f"{a}t"))
        return "".join(pieces), has_citation

    def has_body_text(shape: ET.Element, *, ignore_reference_label: bool = False) -> bool:
        text, citation = plain_run_text(shape)
        # An older citations-only bullet page used this fallback label.
        return _has_visible_text(text) and not (
            ignore_reference_label and citation and text.strip() == "参考依据")

    title_shapes = named.get("Section", []) if kind == "section" else named.get("Title", [])
    title = next((plain_run_text(shape)[0].strip() for shape in title_shapes
                  if _has_visible_text(plain_run_text(shape)[0])), "无标题")
    page_number = re.search(r"slide(\d+)\.xml$", part_name).group(1)
    location = f"第 {page_number} 页「{title}」（{part_name}）"
    if kind in ("title", "section"):
        if not any(has_body_text(shape) for shape in title_shapes):
            raise ValueError(f"{location} 标题页缺少有效非空标题")
        return
    if kind == "references":
        if any(has_body_text(shape) for shape in named.get("Reference", [])):
            return
        raise ValueError(f"{location} 参考文献正文为空，编号或链接不能代替完整条目")
    if any(has_body_text(shape, ignore_reference_label=name == "Content")
           for name in ("Content", "Text") for shape in named.get(name, [])):
        return
    for name, shapes in named.items():
        if re.fullmatch(r"Picture \d+", name):
            for shape in shapes:
                blip = shape.find(f"{p}blipFill/{a}blip")
                if blip is not None and blip.get(f"{r}embed"):
                    return  # Relationship and media validity are checked separately.
    for shape in named.get("Table", []):
        for table in shape.iter(f"{a}tbl"):
            props = table.find(f"{a}tblPr")
            first_data = int(props is not None and props.get("firstRow") in ("1", "true"))
            for row in table.findall(f"{a}tr")[first_data:]:
                if any(_has_visible_text(text.text or "") for text in row.iter(f"{a}t")):
                    return
    if "ChartUnit" in names:
        for name, shapes in named.items():
            match = re.fullmatch(r"Value(\d+_\d+)", name)
            if match and ({"Bar" + match[1], "Point" + match[1]} & names):
                for shape in shapes:
                    try:
                        value = float(plain_run_text(shape)[0].strip())
                    except ValueError:
                        continue
                    if math.isfinite(value):
                        return
    raise ValueError(f"{location} 可见正文为空；标题、图注、来源、备注、引用和页码不能代替主内容")


def validate_package(path: Path) -> None:
    """Reject malformed OOXML packages before they can be delivered."""
    required = {
        "[Content_Types].xml", "_rels/.rels", "ppt/presentation.xml",
        "ppt/_rels/presentation.xml.rels", "ppt/presProps.xml",
        "ppt/viewProps.xml", "ppt/tableStyles.xml", "ppt/theme/theme1.xml",
        "ppt/slideMasters/slideMaster1.xml", "ppt/slideLayouts/slideLayout1.xml",
        "docProps/core.xml", "docProps/app.xml",
    }
    with zipfile.ZipFile(path) as z:
        names = _unique_package_names(z)
        bad = z.testzip()
        if bad:
            raise ValueError(f"PPTX ZIP 部件损坏: {bad}")
        missing = required - names
        if missing:
            raise ValueError(f"PPTX 缺少必要部件: {', '.join(sorted(missing))}")

        roots: dict[str, ET.Element] = {}
        for name in names:
            if name.endswith((".xml", ".rels")) or name == "[Content_Types].xml":
                try:
                    roots[name] = ET.fromstring(z.read(name))
                except ET.ParseError as exc:
                    raise ValueError(f"OOXML 无法解析: {name}: {exc}") from exc
                expected_root = _expected_root_for_part(name)
                if expected_root is not None and roots[name].tag != expected_root:
                    raise ValueError(
                        f"OOXML 根元素与路径不匹配: {name}: "
                        f"expected {expected_root}, got {roots[name].tag}"
                    )

        _validate_content_type_bindings(roots["[Content_Types].xml"], names)

        rel_tag = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
        relationship_ids_by_source: dict[str, set[str]] = {}
        relationship_records: dict[str, dict[str, tuple[str, str, bool]]] = {}
        for name, root in roots.items():
            if not name.endswith(".rels"):
                continue
            source = _relationship_source_name(name)
            if source and source not in names:
                raise ValueError(f"OOXML 关系源部件不存在: {name} -> {source}")
            folder = posixpath.dirname(source)
            relationship_ids: set[str] = set()
            records: dict[str, tuple[str, str, bool]] = {}
            for rel in root.findall(rel_tag):
                relationship_id = rel.get("Id", "")
                if not relationship_id or relationship_id in relationship_ids:
                    raise ValueError(
                        f"OOXML 关系 ID 缺失或重复: {name}: {relationship_id}"
                    )
                relationship_ids.add(relationship_id)
                target = rel.get("Target", "")
                external = rel.get("TargetMode") == "External"
                if external:
                    records[relationship_id] = (rel.get("Type", ""), target, True)
                    continue
                resolved = target.lstrip("/") if target.startswith("/") else posixpath.normpath(
                    posixpath.join(folder, target)
                )
                if resolved not in names:
                    raise ValueError(f"OOXML 关系目标不存在: {name} -> {target}")
                records[relationship_id] = (rel.get("Type", ""), resolved, False)
            relationship_ids_by_source[source] = relationship_ids
            relationship_records[source] = records

        relationship_attr_ns = (
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
        )
        for name, root in roots.items():
            if name.endswith(".rels") or name == "[Content_Types].xml":
                continue
            referenced_ids = {
                value
                for element in root.iter()
                for attribute, value in element.attrib.items()
                if attribute.startswith(relationship_attr_ns)
            }
            missing_ids = sorted(
                referenced_ids - relationship_ids_by_source.get(name, set())
            )
            if missing_ids:
                raise ValueError(
                    f"OOXML 部件引用了不存在的关系 ID: {name}: "
                    + ", ".join(missing_ids)
                )

        _validate_relationship_topology(names, roots, relationship_records)
        _validate_office_compatibility_parts(names, roots)

        p_ns = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
        presentation = roots["ppt/presentation.xml"]
        order = [node.tag for node in presentation]
        expected = [f"{p_ns}sldMasterIdLst"]
        if f"{p_ns}notesMasterIdLst" in order:
            expected.append(f"{p_ns}notesMasterIdLst")
        expected.extend([f"{p_ns}sldIdLst", f"{p_ns}sldSz", f"{p_ns}notesSz"])
        positions = [order.index(tag) for tag in expected]
        if positions != sorted(positions):
            raise ValueError("presentation.xml 子节点顺序不符合 Office 2016 OOXML 规范")

        forbidden = (b"<a:b/>", b"<a:i/>", b"cmpen=", b"<a:lnTbl")
        xml_blob = b"\n".join(z.read(name) for name in roots)
        hit = next((token for token in forbidden if token in xml_blob), None)
        if hit:
            raise ValueError(f"发现 Office 2016 不兼容的 DrawingML: {hit.decode()}")

        a_ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
        for name, root in roots.items():
            for body_pr in root.iter(f"{a_ns}bodyPr"):
                anchor = body_pr.get("anchor")
                if anchor is not None and anchor not in _VALID_TEXT_ANCHORS:
                    raise ValueError(
                        f"DrawingML 文本垂直对齐值无效: {name}: anchor={anchor}"
                    )
            for text_body_namespace in (p_ns, a_ns):
                for text_body in root.iter(f"{text_body_namespace}txBody"):
                    if text_body.find(f"{a_ns}p") is None:
                        raise ValueError(
                            f"DrawingML 文本框缺少必需段落: {name}: txBody/a:p"
                        )
            for properties_name in ("rPr", "defRPr", "endParaRPr"):
                for properties in root.iter(f"{a_ns}{properties_name}"):
                    child_tags = [child.tag for child in properties]
                    fill_positions = [
                        child_tags.index(f"{a_ns}{fill_name}")
                        for fill_name in (
                            "noFill", "solidFill", "gradFill", "blipFill",
                            "pattFill", "grpFill",
                        )
                        if f"{a_ns}{fill_name}" in child_tags
                    ]
                    font_positions = [
                        child_tags.index(f"{a_ns}{font_name}")
                        for font_name in ("latin", "ea", "cs", "sym")
                        if f"{a_ns}{font_name}" in child_tags
                    ]
                    if fill_positions and font_positions and min(fill_positions) > min(font_positions):
                        raise ValueError(
                            f"DrawingML 文字样式子节点顺序无效: {name}: "
                            f"{properties_name} fill must precede font"
                        )

            if not re.fullmatch(r"ppt/slides/slide\d+\.xml", name):
                continue
            shape_bounds: dict[str, tuple[int, int, int, int]] = {}
            shape_paths = (
                ("sp", f"{p_ns}spPr/{a_ns}xfrm", f"{p_ns}nvSpPr/{p_ns}cNvPr"),
                ("pic", f"{p_ns}spPr/{a_ns}xfrm", f"{p_ns}nvPicPr/{p_ns}cNvPr"),
                ("graphicFrame", f"{p_ns}xfrm", f"{p_ns}nvGraphicFramePr/{p_ns}cNvPr"),
            )
            for element_name, transform_path, properties_path in shape_paths:
                for element in root.iter(f"{p_ns}{element_name}"):
                    transform = element.find(transform_path)
                    if transform is None:
                        raise ValueError(f"DrawingML 图形缺少坐标: {name}: {element_name}")
                    offset = transform.find(f"{a_ns}off")
                    extent = transform.find(f"{a_ns}ext")
                    if offset is None or extent is None:
                        raise ValueError(f"DrawingML 图形缺少坐标: {name}: {element_name}")
                    x = int(offset.get("x", "-1"))
                    y = int(offset.get("y", "-1"))
                    cx = int(extent.get("cx", "0"))
                    cy = int(extent.get("cy", "0"))
                    if x < 0 or y < 0 or cx <= 0 or cy <= 0 \
                            or x + cx > SLIDE_W or y + cy > SLIDE_H:
                        raise ValueError(
                            f"DrawingML 图形超出画布: {name}: "
                            f"x={x}, y={y}, cx={cx}, cy={cy}"
                        )
                    properties = element.find(properties_path)
                    if properties is not None and properties.get("name"):
                        shape_bounds[properties.get("name")] = (x, y, x + cx, y + cy)

            for title_name, rule_name, subtitle_name in (
                ("Title", "TitleRule", "Subtitle"),
                ("Section", "SectionRule", "SectionSub"),
            ):
                title_box = shape_bounds.get(title_name)
                rule_box = shape_bounds.get(rule_name)
                subtitle_box = shape_bounds.get(subtitle_name)
                if title_box and rule_box and title_box[3] > rule_box[1]:
                    raise ValueError(
                        f"DrawingML 版式重叠: {name}: {title_name}/{rule_name}"
                    )
                if rule_box and subtitle_box and rule_box[3] > subtitle_box[1]:
                    raise ValueError(
                        f"DrawingML 版式重叠: {name}: {rule_name}/{subtitle_name}"
                    )

        slide_parts = sorted((name for name in roots if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                             key=lambda name: int(re.search(r"slide(\d+)\.xml$", name).group(1)))
        for name in slide_parts:
            _validate_visible_slide_content(roots[name], name)


def _xsd_for_part(name: str) -> str | None:
    """Return the ECMA-376 Transitional XSD for a generated XML part."""
    if name == "[Content_Types].xml":
        return "opc-contentTypes.xsd"
    if name.endswith(".rels"):
        return "opc-relationships.xsd"
    if _PML_PART_RE.fullmatch(name):
        return "pml.xsd"
    if re.fullmatch(r"ppt/theme/theme\d+\.xml", name) or name == "ppt/tableStyles.xml":
        return "dml-main.xsd"
    if name == "docProps/app.xml":
        return "shared-documentPropertiesExtended.xsd"
    if name == "docProps/core.xml":
        return "opc-coreProperties.xsd"
    return None


def _expected_root_for_part(name: str) -> str | None:
    """Return the root QName required at a specific package path."""
    content_types_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    relationships_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    presentation_ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
    drawing_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    extended_ns = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
    core_ns = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"

    if name == "[Content_Types].xml":
        return f"{{{content_types_ns}}}Types"
    if name.endswith(".rels"):
        return f"{{{relationships_ns}}}Relationships"
    if name == "ppt/presentation.xml":
        return f"{{{presentation_ns}}}presentation"
    if name == "ppt/presProps.xml":
        return f"{{{presentation_ns}}}presentationPr"
    if name == "ppt/viewProps.xml":
        return f"{{{presentation_ns}}}viewPr"
    for pattern, local_name in (
        (r"ppt/slideMasters/slideMaster\d+\.xml", "sldMaster"),
        (r"ppt/slideLayouts/slideLayout\d+\.xml", "sldLayout"),
        (r"ppt/slides/slide\d+\.xml", "sld"),
        (r"ppt/notesMasters/notesMaster\d+\.xml", "notesMaster"),
        (r"ppt/notesSlides/notesSlide\d+\.xml", "notes"),
    ):
        if re.fullmatch(pattern, name):
            return f"{{{presentation_ns}}}{local_name}"
    if re.fullmatch(r"ppt/theme/theme\d+\.xml", name):
        return f"{{{drawing_ns}}}theme"
    if name == "ppt/tableStyles.xml":
        return f"{{{drawing_ns}}}tblStyleLst"
    if name == "docProps/app.xml":
        return f"{{{extended_ns}}}Properties"
    if name == "docProps/core.xml":
        return f"{{{core_ns}}}coreProperties"
    return None


def _resolve_trusted_executable(path: Path, *, label: str) -> Path:
    """Accept only a root-owned, fixed-path executable that is not writable by peers."""
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ValueError(f"{label}不可用: 找不到受信任的可执行文件 {path}") from exc
    unsafe_mode = stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or not os.access(path, os.X_OK)
        or metadata.st_uid != 0
        or metadata.st_mode & unsafe_mode
    ):
        raise ValueError(f"{label}不可用: 可执行文件不受信任 {path}")
    for parent in path.parents:
        try:
            parent_metadata = parent.stat()
        except OSError as exc:
            raise ValueError(f"{label}不可用: 无法检查父目录 {parent}") from exc
        if (
            parent.is_symlink()
            or parent_metadata.st_uid != 0
            or parent_metadata.st_mode & unsafe_mode
        ):
            raise ValueError(f"{label}不可用: 父目录不受信任 {parent}")
    return path


def _resolve_user_owned_executable(path: Path, *, label: str) -> Path:
    """[LChain] 本地开发环境（无 root 权限）用：只接受当前用户所有、不可被组/其他用户写入、
    非符号链接的可执行文件，父目录同样不可被他人写入。需显式设置 PPT_REPORT_SDK_VALIDATOR_OWNER=user。"""
    unsafe_mode = stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ValueError(f"{label}不可用: 找不到可执行文件 {path}") from exc
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or not os.access(path, os.X_OK)
        or metadata.st_uid not in (os.geteuid(), 0)
        or metadata.st_mode & unsafe_mode
    ):
        raise ValueError(f"{label}不可用: 可执行文件不受信任 {path}")
    for parent in path.parents:
        try:
            pm = parent.stat()
        except OSError as exc:
            raise ValueError(f"{label}不可用: 无法检查父目录 {parent}") from exc
        if parent.is_symlink() or pm.st_uid not in (os.geteuid(), 0) or pm.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError(f"{label}不可用: 父目录不受信任 {parent}")
    return path


def _resolve_xmllint() -> Path:
    try:
        return _resolve_trusted_executable(
            _XMLLINT_VALIDATOR,
            label="严格 XSD 校验",
        )
    except ValueError as exc:
        raise ValueError(
            "严格 XSD 校验不可用: 请在 AGSX 模板固定路径 "
            f"{_XMLLINT_VALIDATOR} 预装 root 所有且不可被组/其他用户写入的 "
            "libxml2-utils/xmllint"
        ) from exc


def validate_package_xsd(
    path: Path,
    *,
    schema_dir: Path | None = None,
) -> int:
    """Validate every generated XML part against offline Transitional XSDs.

    The validator is deliberately fail-closed: a missing executable, missing
    schema, timeout, unmapped XML part, or any schema error rejects the deck.
    Returns the number of XML parts validated on success.
    """
    validator = _resolve_xmllint()
    schemas = (schema_dir or _XSD_DIR).resolve()
    if not schemas.is_dir():
        raise ValueError(f"严格 XSD 校验不可用: schema 目录不存在: {schemas}")

    groups: dict[str, list[str]] = defaultdict(list)
    part_data: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as package:
        for name in sorted(package.namelist()):
            if not (name == "[Content_Types].xml" or name.endswith((".xml", ".rels"))):
                continue
            schema_name = _xsd_for_part(name)
            expected_root = _expected_root_for_part(name)
            if schema_name is None or expected_root is None:
                raise ValueError(f"严格 XSD 校验没有部件映射: {name}")
            data = package.read(name)
            try:
                actual_root = ET.fromstring(data).tag
            except ET.ParseError as exc:
                raise ValueError(f"严格 XSD 校验无法解析部件: {name}: {exc}") from exc
            if actual_root != expected_root:
                raise ValueError(
                    f"OOXML 部件根元素与路径不匹配: {name}: "
                    f"expected {expected_root}, got {actual_root}"
                )
            groups[schema_name].append(name)
            part_data[name] = data

        if not groups:
            raise ValueError("严格 XSD 校验失败: PPTX 中没有可校验的 XML 部件")

        required_schemas = sorted(groups)
        missing_schemas = [name for name in required_schemas if not (schemas / name).is_file()]
        if missing_schemas:
            raise ValueError(
                "严格 XSD 校验不可用: 缺少 schema: " + ", ".join(missing_schemas)
            )

        with tempfile.TemporaryDirectory(prefix="medex-pptx-xsd-") as temp_name:
            temp_root = Path(temp_name)
            extracted: dict[str, Path] = {}
            part_index = 0
            for part_names in groups.values():
                for name in part_names:
                    part_index += 1
                    destination = temp_root / f"part-{part_index}.xml"
                    destination.write_bytes(part_data[name])
                    extracted[name] = destination

            for schema_name, part_names in sorted(groups.items()):
                for start in range(0, len(part_names), _XSD_VALIDATION_BATCH_SIZE):
                    batch = part_names[start:start + _XSD_VALIDATION_BATCH_SIZE]
                    command = [
                        str(validator),
                        "--nonet",
                        "--noout",
                        "--schema",
                        str(schemas / schema_name),
                        *(str(extracted[name]) for name in batch),
                    ]
                    try:
                        result = subprocess.run(
                            command,
                            capture_output=True,
                            check=False,
                            text=True,
                            timeout=_XSD_VALIDATION_TIMEOUT_SECONDS,
                            env=_VALIDATOR_ENV,
                        )
                    except subprocess.TimeoutExpired as exc:
                        raise ValueError(
                            f"严格 XSD 校验超时: {schema_name} "
                            f"({_XSD_VALIDATION_TIMEOUT_SECONDS}s)"
                        ) from exc
                    except OSError as exc:
                        raise ValueError(f"严格 XSD 校验器执行失败: {exc}") from exc
                    if result.returncode != 0:
                        detail = (result.stderr or result.stdout or "未知 schema 错误").strip()
                        for part_name, temporary_path in extracted.items():
                            detail = detail.replace(str(temporary_path), part_name)
                        if len(detail) > 4000:
                            detail = detail[-4000:]
                        raise ValueError(
                            f"ECMA-376 Transitional XSD 校验失败 ({schema_name}): "
                            f"{detail}"
                        )

    return sum(len(part_names) for part_names in groups.values())


def _resolve_openxml_sdk_validator() -> Path:
    try:
        if _SDK_VALIDATOR_OWNER == "user":
            return _resolve_user_owned_executable(_OPENXML_SDK_VALIDATOR, label="严格 Open XML SDK 校验")
        return _resolve_trusted_executable(
            _OPENXML_SDK_VALIDATOR,
            label="严格 Open XML SDK 校验",
        )
    except ValueError as exc:
        raise ValueError(
            "严格 Open XML SDK 校验不可用: 缺少受信任的可执行文件 "
            f"{_OPENXML_SDK_VALIDATOR}；请在 AGSX 模板中预装 root 所有、"
            "不可被组/其他用户写入且基于 "
            "DocumentFormat.OpenXml.Validation.OpenXmlValidator 的自包含校验器"
        ) from exc


def validate_package_openxml_sdk(path: Path) -> None:
    """Run the fixed, trusted Open XML SDK validator and fail closed."""
    validator = _resolve_openxml_sdk_validator()
    command = [
        str(validator),
        "--target",
        "Office2016",
        "--file",
        str(path.resolve(strict=True)),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=_OPENXML_SDK_VALIDATION_TIMEOUT_SECONDS,
            env={
                **_VALIDATOR_ENV,
                "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
                "DOTNET_NOLOGO": "1",
            },
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            "严格 Open XML SDK 校验超时 "
            f"({_OPENXML_SDK_VALIDATION_TIMEOUT_SECONDS}s)"
        ) from exc
    except OSError as exc:
        raise ValueError(f"严格 Open XML SDK 校验器执行失败: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "未知 Open XML SDK 错误").strip()
        if len(detail) > 4000:
            detail = detail[-4000:]
        raise ValueError(f"Open XML SDK 校验失败 (Office2016): {detail}")
    success_marker = "VALID target=Office2016 errors=0"
    if result.stdout.strip() != success_marker or result.stderr.strip():
        raise ValueError(
            "Open XML SDK 校验器返回了不可信的成功响应；"
            f"expected '{success_marker}'"
        )


def _sanitize_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name)).strip("_")
    return name or "report"


def _resolve_allowed_input(path: Path, *, label: str) -> Path:
    if not path.expanduser().is_absolute():
        raise ValueError(f"{label} must be an absolute path under {_REPO_ROOT}")
    resolved = path.expanduser().resolve(strict=True)
    repo_root = _REPO_ROOT.resolve()
    if not resolved.is_relative_to(repo_root):
        raise ValueError(f"{label} must stay under {repo_root}: {resolved}")
    return resolved


def _validate_spec_asset_paths(spec: dict) -> None:
    slides = spec.get("slides", [])
    if not isinstance(slides, list):
        return
    def inspect(node, location):
        if not isinstance(node, dict):
            return
        if node.get("type") == "image":
            image_path = node.get("path")
            if not isinstance(image_path, str) or not image_path:
                raise ValueError(f"{location}.path 必须是图片绝对路径")
            _resolve_allowed_input(Path(image_path), label=f"{location}.path")
        if node.get("type") == "columns":
            for side in ("left", "right"):
                children = node.get(side, [])
                if isinstance(children, list):
                    for index, child in enumerate(children):
                        inspect(child, f"{location}.{side}[{index}]")
    for slide_index, slide in enumerate(slides):
        inspect(slide, f"slides[{slide_index}]")


def _validate_output_path(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    output_root = _OUTPUT_ROOT.resolve()
    if resolved.parent != output_root:
        raise ValueError(f"{label} must be a direct child of {output_root}: {resolved}")
    return resolved


def resolve_output(args, spec: dict, src_path: Path) -> Path:
    """Resolve output: /repo/o_outcome/{name}_{yyyy}_{mm}_{dd}_v{N}.pptx."""
    if args.out:
        out = _validate_output_path(args.out, label="--out")
        if out.suffix.lower() != ".pptx":
            raise ValueError("--out must use the .pptx extension")
        if out.exists() and not args.force:
            raise FileExistsError(f"output exists (use --force to overwrite): {out}")
        return out
    name = _sanitize_name(args.name or spec.get("title") or src_path.stem)
    raw_date = args.date or datetime.now().strftime("%Y-%m-%d")
    try:
        date_tag = datetime.strptime(raw_date, "%Y-%m-%d").strftime("%Y_%m_%d")
    except ValueError as exc:
        raise ValueError("--date must use a valid YYYY-MM-DD value") from exc
    out_dir = args.out_dir.expanduser().resolve()
    if out_dir != _OUTPUT_ROOT.resolve():
        raise ValueError(f"--out-dir must be {_OUTPUT_ROOT.resolve()}: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.version is not None:
        if args.version < 1:
            raise ValueError("--version must be a positive integer")
        out = _validate_output_path(
            out_dir / f"{name}_{date_tag}_v{args.version}.pptx",
            label="generated output",
        )
        if out.exists() and not args.force:
            raise FileExistsError(f"output exists (use --force to overwrite): {out}")
        return out
    version = _next_version(out_dir, name, date_tag, "pptx")
    return _validate_output_path(
        out_dir / f"{name}_{date_tag}_v{version}.pptx",
        label="generated output",
    )


def _next_version(out_dir: Path, name: str, date_tag: str, ext: str) -> int:
    """Return a version number strictly greater than any existing report with
    the same name/date/extension. Guarantees every run produces a brand-new file
    that never collides with or overwrites a historical report."""
    stem_pattern = (
        rf"{re.escape(name)}_{re.escape(date_tag)}_v(\d+)\."
        rf"{re.escape(ext.lstrip('.'))}"
    )
    output_pattern = re.compile(rf"^{stem_pattern}$")
    reservation_pattern = re.compile(rf"^\.{stem_pattern}\.publish-lock$")
    highest = 0
    try:
        for entry in out_dir.iterdir():
            m = output_pattern.match(entry.name) or reservation_pattern.match(entry.name)
            if m:
                highest = max(highest, int(m.group(1)))
    except OSError:
        pass
    return highest + 1


def _output_reservation_path(output: Path) -> Path:
    return output.with_name(f".{output.name}.publish-lock")


def _reserve_output(output: Path) -> Path:
    """Atomically reserve one output target in its shared COSFS directory."""
    lock_path = _output_reservation_path(output)
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise FileExistsError(
            f"output publication is already in progress: {output}"
        ) from exc
    try:
        reservation = json.dumps(
            {
                "created_at": datetime.now().astimezone().isoformat(),
                "pid": os.getpid(),
                "target": output.name,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        os.write(lock_fd, reservation)
    except BaseException:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(lock_fd)
    return lock_path


def _release_output_reservation(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def _resolve_and_reserve_output(args, spec: dict, src_path: Path) -> tuple[Path, Path]:
    """Resolve and claim an output, retrying auto-version races."""
    auto_version = args.out is None and args.version is None
    output = resolve_output(args, spec, src_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    attempts = _OUTPUT_RESERVATION_ATTEMPTS if auto_version else 1
    for _ in range(attempts):
        try:
            reservation = _reserve_output(output)
        except FileExistsError:
            if not auto_version:
                raise
        else:
            if not output.exists() or (args.force and not auto_version):
                return output, reservation
            _release_output_reservation(reservation)

        match = re.fullmatch(r"(?P<prefix>.+_v)(?P<version>\d+)(?P<suffix>\.pptx)", output.name)
        if not auto_version or match is None:
            raise FileExistsError(
                f"output exists (use --force to overwrite): {output}"
            )
        output = output.with_name(
            f"{match.group('prefix')}{int(match.group('version')) + 1}"
            f"{match.group('suffix')}"
        )
    raise FileExistsError(
        "could not reserve an output version after "
        f"{_OUTPUT_RESERVATION_ATTEMPTS} attempts: {output.parent}"
    )


def _publish_candidate(
    candidate: Path,
    output: Path,
    *,
    force: bool,
    reservation: Path | None = None,
) -> None:
    """Publish a validated candidate behind a shared-directory reservation.

    The output directory is COSFS-backed in AGSX, so a host-local ``flock``
    cannot coordinate separate containers. ``O_EXCL`` creates a per-target
    reservation in that shared directory; an abandoned auto-version
    reservation is treated as an occupied version by ``_next_version``.
    """
    lock_path = reservation or _reserve_output(output)
    if lock_path != _output_reservation_path(output) or not lock_path.is_file():
        raise FileNotFoundError(f"output reservation is missing or mismatched: {output}")
    try:
        if output.exists() and not force:
            raise FileExistsError(
                f"output exists (use --force to overwrite): {output}"
            )
        candidate.replace(output)
    finally:
        _release_output_reservation(lock_path)


def _run_sdk_validation(candidate: Path) -> str:
    """[LChain] 执行 Open XML SDK 校验。默认（required）与 medex 一致：校验器缺失即失败。
    PPT_REPORT_SDK_VALIDATION=optional 时，仅在校验器未安装/不受信任时跳过，并在输出中如实注明；
    校验器存在但报错时仍然失败。"""
    if _SDK_VALIDATION_MODE == "optional":
        try:
            _resolve_openxml_sdk_validator()
        except ValueError:
            return ("Strict validation: Microsoft Open XML SDK SKIPPED "
                    f"(validator not installed at {_OPENXML_SDK_VALIDATOR}; PPT_REPORT_SDK_VALIDATION=optional)")
    validate_package_openxml_sdk(candidate)
    return "Strict validation: Microsoft Open XML SDK passed (Office2016 target)"


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a .pptx deck from a JSON spec.")
    ap.add_argument("--spec", type=Path, required=True, help="JSON spec file path")
    ap.add_argument("--out", type=Path, help="Explicit output .pptx path (overrides auto naming)")
    ap.add_argument("--name", help="Report base name; default: spec title or spec stem")
    ap.add_argument("--version", type=int, help="Version number; default: auto-increment to avoid clash")
    ap.add_argument("--out-dir", type=Path, default=_OUTPUT_ROOT,
                    help="Output directory (default: /repo/o_outcome)")
    ap.add_argument("--date", help="Date string YYYY-MM-DD (default: today)")
    ap.add_argument("--title", help="Override deck title")
    ap.add_argument("--author", help="Override author")
    ap.add_argument("--force", action="store_true", help="Overwrite existing output")
    args = ap.parse_args()

    try:
        spec_path = _resolve_allowed_input(args.spec, label="--spec")
        if spec_path.stat().st_size > _MAX_SPEC_BYTES:
            raise ValueError(
                f"spec 文件不能超过 {_MAX_SPEC_BYTES // (1024 * 1024)} MiB"
            )
        with spec_path.open("r", encoding="utf-8") as f:
            spec = json.load(f)
        if not isinstance(spec, dict):
            raise ValueError("spec 顶层必须是 JSON 对象")
        _validate_spec_asset_paths(spec)
        if args.title:
            spec["title"] = args.title
        if args.author:
            spec["author"] = args.author

        out, reservation = _resolve_and_reserve_output(args, spec, spec_path)
        candidate = out.with_name(
            f".{out.stem}.{uuid.uuid4().hex}.candidate{out.suffix}"
        )
        try:
            n, counts, n_images = build_package(spec, candidate)
            # Reopen the written PPTX and check every page before publication;
            # valid input alone cannot prove that the body survived generation.
            validate_package(candidate)
            validated_parts = validate_package_xsd(candidate)
            sdk_status = _run_sdk_validation(candidate)
            auto_version = args.out is None and args.version is None
            _publish_candidate(
                candidate,
                out,
                force=args.force and not auto_version,
                reservation=reservation,
            )
        finally:
            candidate.unlink(missing_ok=True)
            _release_output_reservation(reservation)

        print(f"Output: {out}")
        print("Content validation: generated PPTX body checks passed "
              f"({n} slides; {counts.get('title', 0) + counts.get('section', 0)} "
              "explicit title/section pages)")
        print("Compatibility: OOXML package checks passed (PowerPoint 2016+ target)")
        print(
            "Strict validation: ECMA-376 5th Transitional XSD passed "
            f"({validated_parts} XML parts)"
        )
        print(sdk_status)
        print(f"Slides: {n} "
              f"(title {counts.get('title', 0)}, section {counts.get('section', 0)}, "
              f"bullets {counts.get('bullets', 0)}, table {counts.get('table', 0)}, "
              f"two_column {counts.get('two_column', 0)}, "
              f"image {counts.get('image', 0)}, text {counts.get('text', 0)}, "
              f"columns {counts.get('columns', 0)}, chart {counts.get('chart', 0)}, "
              f"references {counts.get('references', 0)})")
        print(f"Images: {n_images} embedded")
        print(f"Size: {out.stat().st_size} bytes")
        return 0
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
