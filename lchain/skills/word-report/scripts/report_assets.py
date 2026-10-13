"""Self-contained references, image validation and editable OOXML chart parts."""

from __future__ import annotations

import io
import math
import re
import struct
import zipfile
import zlib
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape


def xml_attr(value: object) -> str:
    return escape(str(value), {'"': "&quot;"})


def positive_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是正数")
    if value <= 0 or value > 1e12 or not math.isfinite(value):
        raise ValueError(f"{label} 必须是有限正数")
    return float(value)


def web_url(value: object) -> str:
    value = str(value or "").strip()
    if value and (urlsplit(value).scheme not in ("http", "https") or not urlsplit(value).netloc):
        raise ValueError(f"文献 URL 必须是完整的 http(s) 地址: {value}")
    return value


class ReferenceList:
    """Stable spec-order numbering; duplicate IDs/DOIs/URLs share one number."""

    def __init__(self, items: list[dict]) -> None:
        if not isinstance(items, list):
            raise ValueError("references 必须是数组")
        self.entries: list[dict] = []
        self.numbers: dict[str, int] = {}
        identities: dict[str, int] = {}
        for raw in items:
            if not isinstance(raw, dict):
                raise ValueError("references 中每一项必须是对象")
            item = dict(raw)
            ref_id = str(item.get("id", "")).strip()
            title = str(item.get("title", "")).strip()
            if not ref_id or not title:
                raise ValueError("每条文献必须包含非空 id 和 title")
            doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", str(item.get("doi", "")).strip(), flags=re.I)
            if doi and not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
                raise ValueError(f"文献 {ref_id} DOI 格式错误: {doi}")
            url = web_url(item.get("url"))
            item.update(id=ref_id, title=title, doi=doi, url=url)
            keys = ["id:" + ref_id]
            if doi:
                keys.append("doi:" + doi.lower())
            if url:
                keys.append("url:" + url.rstrip("/"))
            matches = {identities[k] for k in keys if k in identities}
            if len(matches) > 1:
                raise ValueError(f"文献 {ref_id} 的 id/DOI/URL 对应多个不同条目")
            if matches:
                number = matches.pop()
                old = self.entries[number - 1]
                # A DOI may legitimately have a publisher URL and a PubMed URL.
                # Reject conflicting reuse of an ID; aliases retain first data.
                for field in (("title", "doi", "url", "authors", "year", "source") if "id:" + ref_id in identities else ("doi",)):
                    if old.get(field) and item.get(field) and str(old[field]).casefold().rstrip("/") != str(item[field]).casefold().rstrip("/"):
                        raise ValueError(f"重复文献 {ref_id} 的 {field} 冲突，请合并核验后再生成")
                for field, value in item.items():
                    if field != "id" and not old.get(field):
                        old[field] = value
            else:
                self.entries.append(item)
                number = len(self.entries)
            self.numbers[ref_id] = number
            for key in keys:
                identities[key] = number

    def marker(self, ids: object) -> str:
        if ids is None:
            return ""
        if not isinstance(ids, list) or any(not isinstance(v, str) for v in ids):
            raise ValueError("citations 必须是文献 id 字符串数组")
        missing = [v for v in ids if v not in self.numbers]
        if missing:
            raise ValueError("未知 citations id: " + ", ".join(missing))
        return "".join(f"[{n}]" for n in sorted({self.numbers[v] for v in ids}))

    @staticmethod
    def text(item: dict) -> str:
        authors = item.get("authors") or ""
        if isinstance(authors, list):
            authors = ", ".join(str(a) for a in authors)
        parts = [str(v).strip().rstrip(".") for v in (authors, item["title"], item.get("source"), item.get("year"), item.get("volume"), item.get("pages")) if v]
        return ". ".join(parts) + "."


def read_image(path: Path) -> tuple[bytes, int, int]:
    """Reject missing/mislabeled/truncated images before writing an Office file.

    PNG integrity is checked including chunk CRC and compressed scanlines. Other
    baseline formats get container checks; Pillow, when already installed, also
    performs a full decode. No runtime download is needed.
    """
    data = path.read_bytes()
    if len(data) > 100_000_000:
        raise ValueError(f"图片超过 100 MB: {path}")
    ext = path.suffix.lower()
    width = height = 0
    try:
        if ext == ".png":
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("PNG 签名不匹配")
            pos, idat, ended, ihdr = 8, bytearray(), False, None
            while pos + 12 <= len(data):
                count = struct.unpack_from(">I", data, pos)[0]
                kind = data[pos + 4:pos + 8]
                chunk = data[pos + 8:pos + 8 + count]
                if pos + count + 12 > len(data):
                    raise ValueError("PNG 数据截断")
                expected = struct.unpack_from(">I", data, pos + count + 8)[0]
                if zlib.crc32(kind + chunk) & 0xFFFFFFFF != expected:
                    raise ValueError("PNG CRC 校验失败")
                if kind == b"IHDR":
                    if ihdr is not None or pos != 8 or count != 13:
                        raise ValueError("PNG IHDR 非法")
                    ihdr = struct.unpack(">IIBBBBB", chunk)
                    width, height = ihdr[:2]
                    if width * height > 100_000_000:
                        raise ValueError("图片像素过多")
                elif kind == b"IDAT":
                    idat.extend(chunk)
                elif kind == b"IEND":
                    ended = count == 0
                    break
                pos += count + 12
            if not ended or not ihdr or not idat:
                raise ValueError("PNG 缺少必需数据块")
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ihdr[3])
            if not channels or ihdr[2] not in (1, 2, 4, 8, 16):
                raise ValueError("PNG 颜色或位深不受支持")
            # Cap decompression to bound malformed inputs, including zip bombs.
            decoder = zlib.decompressobj()
            decoded = decoder.decompress(bytes(idat), min(800_000_001, (width * channels * 2 + 8) * height + 1024))
            if not decoder.eof:
                raise ValueError("PNG 压缩数据损坏或过长")
            if ihdr[6] == 0:
                stride = (width * channels * ihdr[2] + 7) // 8 + 1
                if len(decoded) != stride * height or any(decoded[p] > 4 for p in range(0, len(decoded), stride)):
                    raise ValueError("PNG 像素行损坏")
        elif ext in (".jpg", ".jpeg"):
            if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
                raise ValueError("JPEG 签名错误或缺少结束标记")
            pos = 2
            while pos + 4 <= len(data):
                if data[pos] != 0xFF:
                    raise ValueError("JPEG 段损坏")
                while pos < len(data) and data[pos] == 0xFF:
                    pos += 1
                marker = data[pos]
                pos += 1
                if marker == 0xDA:
                    break
                count = int.from_bytes(data[pos:pos + 2], "big")
                if count < 2 or pos + count > len(data):
                    raise ValueError("JPEG 段截断")
                if marker in (0xC0, 0xC1, 0xC2):
                    height, width = struct.unpack_from(">HH", data, pos + 3)
                pos += count
        elif ext == ".gif":
            if data[:6] not in (b"GIF87a", b"GIF89a") or len(data) < 14 or data[-1:] != b";" or b"," not in data[13:]:
                raise ValueError("GIF 签名错误或数据截断")
            width, height = struct.unpack_from("<HH", data, 6)
        elif ext == ".bmp":
            if len(data) < 54 or data[:2] != b"BM" or int.from_bytes(data[2:6], "little") != len(data):
                raise ValueError("BMP 签名错误或数据截断")
            width, height = struct.unpack_from("<ii", data, 18)
            height = abs(height)
            offset = int.from_bytes(data[10:14], "little")
            if offset >= len(data):
                raise ValueError("BMP 缺少像素数据")
        else:
            raise ValueError("仅支持 PNG、JPEG、GIF、BMP；请先转换为 PNG")
        if width <= 0 or height <= 0 or width * height > 100_000_000:
            raise ValueError("图片尺寸无效或像素超过 1 亿")
        try:
            from PIL import Image
        except ImportError:
            pass
        else:
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                if image.size != (width, height):
                    raise ValueError("图片尺寸与文件头不匹配")
    except (IndexError, struct.error, zlib.error, OSError, ValueError) as exc:
        raise ValueError(f"图片不可用 {path}: {exc}") from exc
    return data, width, height


def chart_workbook(categories: list[str], series: list[dict]) -> bytes:
    """A minimal embedded XLSX keeps Edit Data usable in Word/WPS."""
    def string_cell(ref: str, value: object) -> str:
        return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'

    rows = ['<row r="1">' + string_cell("A1", "类别") + "".join(string_cell(f"{chr(66 + i)}1", s["name"]) for i, s in enumerate(series)) + "</row>"]
    for j, category in enumerate(categories, 2):
        cells = string_cell(f"A{j}", category)
        for i, item in enumerate(series):
            value = item["values"][j - 2]
            if value is not None:
                cells += f'<c r="{chr(66 + i)}{j}"><v>{value}</v></c>'
        rows.append(f'<row r="{j}">{cells}</row>')
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + "".join(rows) + '</sheetData></worksheet>',
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + content)
    return out.getvalue()


# [LChain] 图表配色/字体，可由 generate_word.apply_style 覆盖；默认值即 medex 原版
CHART_THEME = {"palette": ["007F7A", "3869A8", "B87924", "8461A0"], "ink": "4D5D68", "axis": "97A6B0",
               "grid": "E3E9ED", "font_en": "Calibri", "font_cn": "等线"}


def build_chart(block: dict) -> tuple[str, bytes]:
    kind = block.get("chart_type", "bar")
    if kind not in ("bar", "line"):
        raise ValueError("chart_type 仅支持 bar 或 line")
    categories, series = block.get("categories"), block.get("series")
    if not isinstance(categories, list) or not 1 <= len(categories) <= 12 or any(isinstance(v, bool) or not isinstance(v, (str, int, float)) or not str(v).strip() for v in categories):
        raise ValueError("chart categories 需要 1–12 个非空标签；更多数据请拆图或用表格")
    if not isinstance(series, list) or not 1 <= len(series) <= 4:
        raise ValueError("chart series 需要 1–4 个系列")
    if kind == "line" and len(categories) < 2:
        raise ValueError("line chart 至少需要两个类别")
    if len({str(v) for v in categories}) != len(categories):
        raise ValueError("chart categories 不得重复")
    if not isinstance(block.get("unit"), str) or not 1 <= len(block["unit"].strip()) <= 32:
        raise ValueError("chart 必须包含 unit；无量纲数据请显式写明")
    if not block.get("source") and not block.get("citations"):
        raise ValueError("chart 必须包含 source 或 citations，不能生成无来源数据图")
    if any(len(str(v)) > 24 for v in categories):
        raise ValueError("chart 类别标签最长 24 字，请缩短并在图注解释")
    values, names = [], set()
    for item in series:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not 1 <= len(item["name"].strip()) <= 40:
            raise ValueError("每个图表系列需要 1–40 字的 name")
        if item["name"] in names:
            raise ValueError("chart series name 不得重复")
        names.add(item["name"])
        data = item.get("values")
        if not isinstance(data, list) or len(data) != len(categories):
            raise ValueError("chart 每个系列 values 的数量必须与 categories 一致")
        for value in data:
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or abs(value) > 1e12 or not math.isfinite(value)):
                raise ValueError("chart values 只能是绝对值不超过 1e12 的有限数字或 null（缺测）；请按单位换算缩放极值，不得把缺测写成 0")
        if not any(value is not None for value in data):
            raise ValueError("chart 每个系列至少需要一个有效数据点")
        values.extend(value for value in data if value is not None)
    # Restrained teal, blue, ochre and purple; marker and dash differences
    # keep multi-series lines distinguishable in grayscale and colour deficits.
    palette = list(CHART_THEME["palette"])[:4]
    ink, fen, fcn = CHART_THEME["ink"], escape(CHART_THEME["font_en"]), escape(CHART_THEME["font_cn"])
    symbols = ["circle", "square", "triangle", "diamond"]
    dashes = ["solid", "dash", "dot", "dashDot"]

    def text_props(size: int = 1000) -> str:
        return f'<c:txPr><a:bodyPr/><a:lstStyle/><a:p><a:pPr><a:defRPr sz="{size}"><a:solidFill><a:srgbClr val="{ink}"/></a:solidFill><a:latin typeface="{fen}"/><a:ea typeface="{fcn}"/></a:defRPr></a:pPr><a:endParaRPr lang="zh-CN"/></a:p></c:txPr>'

    def title(text: object) -> str:
        return '<c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="zh-CN" sz="1000"><a:solidFill><a:srgbClr val="' + ink + '"/></a:solidFill><a:latin typeface="' + fen + '"/><a:ea typeface="' + fcn + '"/></a:rPr><a:t>' + escape(str(text)) + '</a:t></a:r></a:p></c:rich></c:tx><c:overlay val="0"/></c:title>'

    series_xml = []
    for i, item in enumerate(series):
        column = chr(66 + i)
        data = item["values"]
        name = escape(str(item["name"]))
        color = palette[i]
        stroke = f'<a:ln w="20320"><a:solidFill><a:srgbClr val="{color}"/></a:solidFill><a:prstDash val="{dashes[i]}"/></a:ln>' if kind == "line" else '<a:ln><a:noFill/></a:ln>'
        style = f'<c:spPr><a:solidFill><a:srgbClr val="{color}"/></a:solidFill>{stroke}</c:spPr>'
        marker = f'<c:marker><c:symbol val="{symbols[i]}"/><c:size val="5"/><c:spPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill><a:ln w="16510"><a:solidFill><a:srgbClr val="{color}"/></a:solidFill></a:ln></c:spPr></c:marker>' if kind == "line" else ""
        cat = '<c:cat><c:strRef><c:f>Data!$A$2:$A$' + str(len(categories) + 1) + '</c:f><c:strCache><c:ptCount val="' + str(len(categories)) + '"/>' + ''.join(f'<c:pt idx="{j}"><c:v>{escape(str(v))}</c:v></c:pt>' for j, v in enumerate(categories)) + '</c:strCache></c:strRef></c:cat>'
        vals = f'<c:val><c:numRef><c:f>Data!${column}$2:${column}${len(categories) + 1}</c:f><c:numCache><c:formatCode>General</c:formatCode><c:ptCount val="{len(categories)}"/>' + ''.join(f'<c:pt idx="{j}"><c:v>{v}</c:v></c:pt>' for j, v in enumerate(data) if v is not None) + '</c:numCache></c:numRef></c:val>'
        series_xml.append(f'<c:ser><c:idx val="{i}"/><c:order val="{i}"/><c:tx><c:strRef><c:f>Data!${column}$1</c:f><c:strCache><c:ptCount val="1"/><c:pt idx="0"><c:v>{name}</c:v></c:pt></c:strCache></c:strRef></c:tx>{style}{marker}{cat}{vals}' + ('<c:smooth val="0"/>' if kind == "line" else '') + '</c:ser>')
    plot = f'<c:{kind}Chart>' + ('<c:barDir val="col"/><c:grouping val="clustered"/>' if kind == "bar" else '<c:grouping val="standard"/>') + '<c:varyColors val="0"/>' + ''.join(series_xml) + ('<c:gapWidth val="120"/><c:overlap val="0"/>' if kind == "bar" else '') + f'<c:axId val="100"/><c:axId val="200"/></c:{kind}Chart>'
    baseline = '<c:min val="0"/>' if kind == "bar" and min(values) >= 0 else '<c:max val="0"/>' if kind == "bar" and max(values) <= 0 else ''
    axis_stroke = '<c:spPr><a:ln w="9525"><a:solidFill><a:srgbClr val="' + CHART_THEME["axis"] + '"/></a:solidFill></a:ln></c:spPr>'
    cat_axis = '<c:catAx><c:axId val="100"/><c:scaling><c:orientation val="minMax"/></c:scaling><c:delete val="0"/><c:axPos val="b"/><c:majorTickMark val="none"/><c:minorTickMark val="none"/><c:tickLblPos val="nextTo"/>' + axis_stroke + text_props() + '<c:crossAx val="200"/><c:crosses val="autoZero"/><c:auto val="1"/><c:lblAlgn val="ctr"/><c:lblOffset val="100"/></c:catAx>'
    val_axis = '<c:valAx><c:axId val="200"/><c:scaling><c:orientation val="minMax"/>' + baseline + '</c:scaling><c:delete val="0"/><c:axPos val="l"/><c:majorGridlines><c:spPr><a:ln w="6350"><a:solidFill><a:srgbClr val="' + CHART_THEME["grid"] + '"/></a:solidFill></a:ln></c:spPr></c:majorGridlines>' + (title(block["unit"]) if block.get("unit") else '') + '<c:numFmt formatCode="General" sourceLinked="0"/><c:majorTickMark val="out"/><c:minorTickMark val="none"/><c:tickLblPos val="nextTo"/>' + axis_stroke + text_props() + '<c:crossAx val="100"/><c:crosses val="autoZero"/><c:crossBetween val="between"/></c:valAx>'
    xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><c:date1904 val="0"/><c:lang val="zh-CN"/><c:roundedCorners val="0"/><c:chart><c:autoTitleDeleted val="1"/><c:plotArea><c:layout/>' + plot + cat_axis + val_axis + '</c:plotArea><c:legend><c:legendPos val="b"/><c:layout/><c:overlay val="0"/>' + text_props(950) + '</c:legend><c:plotVisOnly val="1"/><c:dispBlanksAs val="gap"/><c:showDLblsOverMax val="0"/></c:chart><c:spPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill><a:ln><a:noFill/></a:ln></c:spPr>' + text_props() + '<c:externalData r:id="rId1"><c:autoUpdate val="0"/></c:externalData></c:chartSpace>'
    return xml, chart_workbook(categories, series)
