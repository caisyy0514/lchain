"""Behaviour tests for the independently packaged Word skill.

Test context explicitly loads this CLI by path; no application or external
database configuration is needed to exercise its document contract.
"""

import importlib.util
import io
import json
import os
import posixpath
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "skills/word-report/scripts/generate_word.py"
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/package/2006/relationships",
}


@pytest.fixture(scope="module")
def generator():
    module_spec = importlib.util.spec_from_file_location("word_report_test_cli", SCRIPT)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def png(path, width=120, height=60):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress((b"\x00" + b"\x20\x80\x90" * width) * height)) + chunk(b"IEND", b"")
    path.write_bytes(data)
    return path


def chart():
    return {"type": "chart", "chart_type": "line", "categories": ["基线", "随访一", "随访二"], "series": [{"name": "样例", "values": [8, None, 5]}], "unit": "测试单位", "source": "合成测试数据", "caption": "图 1 测试", "alt": '趋势 "样例"'}


def run_report(tmp_path, spec):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False))
    result = subprocess.run([sys.executable, str(SCRIPT), "--spec", str(spec_path), "--out-dir", str(tmp_path / "out")], cwd=tmp_path, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True)
    return result, sorted((tmp_path / "out").glob("*.docx"))


def verify_package(archive):
    """Every internal relationship resolves, including embedded workbook parts."""
    names = set(archive.namelist())
    for name in names:
        if name.endswith((".xml", ".rels")):
            xml = ET.fromstring(archive.read(name))
            if name.endswith(".rels"):
                base = "" if name == "_rels/.rels" else name.rsplit("/_rels/", 1)[0]
                ids = []
                for rel in xml:
                    ids.append(rel.attrib["Id"])
                    if rel.attrib.get("TargetMode") != "External":
                        assert posixpath.normpath(posixpath.join(base, rel.attrib["Target"])) in names
                assert len(ids) == len(set(ids))


def test_cli_embeds_mixed_media_editable_charts_and_deduplicated_references(tmp_path):
    png(tmp_path / "sample.png")
    image = {"type": "image", "path": "sample.png", "caption": "图 2 原图", "alt": '含有 "引号" & 符号', "source": "本地附件", "citations": ["alias"]}
    spec = {
        "title": "图文报告",
        "references": [{"id": "r1", "title": "已核验条目", "url": "https://example.org/a?x=1&y=2"}, {"id": "alias", "title": "已核验条目", "url": "https://example.org/a?x=1&y=2"}],
        "blocks": [{"type": "text", "text": "**解释**\n第二行", "citations": ["r1", "alias"]}, {"type": "columns", "left": [image], "right": [chart()], "ratio": [1, 1]}, image, {"type": "table", "headers": ["项目", "说明"], "rows": [["一", "说明"]], "column_widths": [1, 3], "citations": ["r1"]}],
    }
    result, outputs = run_report(tmp_path, spec)
    assert result.returncode == 0, result.stderr
    assert len(outputs) == 1
    with zipfile.ZipFile(outputs[0]) as archive:
        verify_package(archive)
        assert len([p for p in archive.namelist() if p.startswith("word/media/")]) == 1
        doc = ET.fromstring(archive.read("word/document.xml"))
        ids = [n.attrib["id"] for n in doc.findall(".//wp:docPr", NS)]
        assert len(ids) == len(set(ids)) == 3
        assert doc.find(".//w:br", NS) is not None
        assert len(doc.findall('.//w:pPr/w:pStyle[@w:val="Bibliography"]', NS)) == 1
        all_text = "".join(doc.itertext())
        assert "第二行[1]" in all_text
        assert doc.find('.//w:r/w:rPr/w:vertAlign[@w:val="superscript"]', NS) is not None
        assert "[1][1]" not in all_text
        assert doc.find('.//wp:docPr[@descr=\'含有 "引号" & 符号\']', NS) is not None
        chart_xml = ET.fromstring(archive.read("word/charts/chart1.xml"))
        assert chart_xml.find(".//c:numCache/c:ptCount", NS).attrib["val"] == "3"
        assert chart_xml.find('.//c:numCache/c:pt[@idx="1"]', NS) is None
        assert chart_xml.find(".//c:dispBlanksAs", NS).attrib["val"] == "gap"
        with zipfile.ZipFile(io.BytesIO(archive.read("word/embeddings/chart1.xlsx"))) as workbook:
            verify_package(workbook)
            sheet = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
            assert sheet.find('.//s:c[@r="B3"]', NS) is None
            assert sheet.find('.//s:c[@r="B4"]/s:v', NS).text == "5"


def test_tall_images_contain_and_reserve_space_for_notes(generator, tmp_path):
    image_path = png(tmp_path / "tall.png", 80, 2400)
    block = {"type": "image", "path": str(image_path), "caption": "图 1 长图", "source": "来源说明"}
    doc, _, _, _ = generator.build_document({"blocks": [block, {"type": "columns", "left": [block], "right": [{"type": "text", "text": "解释"}]}]}, generator.ImageMedia())
    root = ET.fromstring(doc)
    for extent in root.findall(".//wp:extent", NS):
        width, height = int(extent.attrib["cx"]), int(extent.attrib["cy"])
        assert width / height == pytest.approx(80 / 2400, abs=1e-6)
        assert height < (generator.CONTENT_H - 800) * 635
    drawing_paragraph = next(p for p in root.findall(".//w:p", NS) if p.find(".//w:drawing", NS) is not None)
    assert drawing_paragraph.find("w:pPr/w:keepNext", NS) is not None
    assert root.find(".//wp:effectLst", NS) is None


def test_long_caption_reserves_cjk_lines_and_heading(generator, tmp_path):
    image_path = png(tmp_path / "portrait.png", 500, 1600)
    block = {"type": "image", "path": str(image_path), "caption": "图 1 长图。" + "图注必须与图片一起呈现。" * 8, "source": "本地合成测试图"}
    doc, _, _, _ = generator.build_document({"blocks": [{"type": "heading", "text": "长图"}, block]}, generator.ImageMedia())
    root = ET.fromstring(doc)
    extent = root.find(".//wp:extent", NS)
    # Leaves more than one inch for a multi-line CJK caption and its heading.
    assert int(extent.attrib["cy"]) < 7.7 * generator.EMU_PER_INCH


def test_long_table_repeats_header_but_does_not_lock_whole_table(generator):
    doc, _, _, _ = generator.build_document({"blocks": [{"type": "table", "headers": ["序号", "解释"], "rows": [[str(i), "解释内容"] for i in range(100)], "column_widths": [1, 4]}]}, generator.ImageMedia())
    root = ET.fromstring(doc)
    rows = root.findall(".//w:tbl/w:tr", NS)
    assert len(rows) == 101
    assert rows[0].find("w:trPr/w:tblHeader", NS) is not None
    assert all(row.find("w:trPr/w:cantSplit", NS) is not None for row in rows)
    assert not root.findall(".//w:tbl//w:keepNext", NS)
    widths = [int(n.attrib[f'{{{NS["w"]}}}w']) for n in root.findall(".//w:tblGrid/w:gridCol", NS)]
    assert sum(widths) == generator.CONTENT_W
    assert widths[1] / widths[0] == pytest.approx(4, abs=.01)
    with pytest.raises(ValueError, match="单行超过"):
        generator.table(["说明"], [["过长说明" * 5000]])


def test_columns_can_paginate_and_nested_table_has_cell_terminator(generator):
    spec = {"blocks": [{"type": "columns", "left": [{"type": "paragraph", "text": "长正文" * 2000}], "right": [{"type": "table", "headers": ["项目"], "rows": [["结果"]]}]}]}
    doc, _, _, _ = generator.build_document(spec, generator.ImageMedia())
    root = ET.fromstring(doc)
    outer = root.find(".//w:body/w:tbl", NS)
    assert outer.find("w:tr/w:trPr/w:cantSplit", NS) is None
    for cell in outer.findall("w:tr/w:tc", NS):
        assert cell[-1].tag == f'{{{NS["w"]}}}p'


def test_table_preserves_zero_and_aligns_values_without_squeezing_explanations(generator):
    def parse_table(*args, **kwargs):
        return ET.fromstring(f'<root xmlns:w="{NS["w"]}">{generator.table(*args, **kwargs)}</root>')[0]

    root = parse_table(
        ["结果", "解释"],
        [[0, "原始数据中的零是实际测量结果，不是缺测"], [-1.25, "根据检测条件与来源解释"], [None, "此项缺测，请保留为空"]],
    )
    rows = root.findall("w:tr", NS)
    assert "".join(rows[1].find("w:tc", NS).itertext()) == "0"
    assert "".join(rows[3].find("w:tc", NS).itertext()) == ""
    for row in rows:
        assert [n.attrib[f'{{{NS["w"]}}}val'] for n in row.findall("w:tc/w:p/w:pPr/w:jc", NS)] == ["right", "left"]
    widths = [int(n.attrib[f'{{{NS["w"]}}}w']) for n in root.findall("w:tblGrid/w:gridCol", NS)]
    assert widths[1] > widths[0]
    # Header emphasis survives the inline parser even without Markdown tokens.
    assert all(cell.find("w:p/w:r/w:rPr/w:b", NS) is not None for cell in rows[0].findall("w:tc", NS))
    assert all(cell.find("w:p/w:r/w:rPr/w:b", NS) is None for cell in rows[1].findall("w:tc", NS))
    custom = parse_table(["结果", "解释"], [[0, "说明"]], align=["center", "right"], widths=[2, 3])
    assert [n.attrib[f'{{{NS["w"]}}}val'] for n in custom.findall("w:tr[2]/w:tc/w:p/w:pPr/w:jc", NS)] == ["center", "right"]


def test_line_series_keep_noncolor_cues_and_underlying_measurements(generator):
    block = chart()
    block["series"].append({"name": "比较", "values": [8, 7, 6]})
    media = generator.ImageMedia()
    generator.build_document({"blocks": [block]}, media)
    root = ET.fromstring(media.charts[0]["xml"])
    series = root.findall(".//c:lineChart/c:ser", NS)
    markers = [item.find("c:marker/c:symbol", NS).attrib["val"] for item in series]
    dashes = [item.find("c:spPr/a:ln/a:prstDash", NS).attrib["val"] for item in series]
    assert len(set(markers)) == len(series)
    assert len(set(dashes)) == len(series)
    assert [point.text for point in series[0].findall("c:val/c:numRef/c:numCache/c:pt/c:v", NS)] == ["8", "5"]
    assert root.find(".//c:legend/c:overlay", NS).attrib["val"] == "0"


def test_reference_aliases_share_number_and_unknown_or_conflicting_ids_fail(generator):
    refs = generator.ReferenceList([
        {"id": "first", "title": "Study", "doi": "10.1234/example", "url": "https://publisher.example/study"},
        {"id": "pubmed", "title": "Study alternate entry", "doi": "https://doi.org/10.1234/example", "url": "https://pubmed.example/123"},
        {"id": "second", "title": "Another", "url": "https://example.org/second"},
    ])
    assert refs.marker(["second", "pubmed", "first"]) == "[1][2]"
    assert len(refs.entries) == 2
    with pytest.raises(ValueError, match="未知 citations"):
        refs.marker(["missing"])
    with pytest.raises(ValueError, match="冲突"):
        generator.ReferenceList([{"id": "x", "title": "One"}, {"id": "x", "title": "Two"}])


@pytest.mark.parametrize("change", [
    {"unit": ""}, {"source": ""}, {"chart_type": "pie"}, {"categories": ["a", "a", "b"]},
    {"categories": ["a"]}, {"series": [{"name": "x", "values": [1]}]},
    {"series": [{"name": "x", "values": [1, float("nan"), 3]}]},
    {"series": [{"name": "x", "values": [1, 1e100, 3]}]},
    {"series": [{"name": "x", "values": [None, None, None]}]},
    {"series": [{"name": "x", "values": [1, 2, 3]}, {"name": "x", "values": [1, 2, 3]}]},
    {"series": [None]}, {"series": [{"name": ["x"], "values": [1, 2, 3]}]},
    {"categories": ["类别标签太长会互相遮挡一", "类别标签太长会互相遮挡二", "类别标签太长会互相遮挡三"]},
])
def test_untraceable_or_malformed_chart_is_rejected(generator, change):
    with pytest.raises(ValueError):
        generator.build_document({"blocks": [{**chart(), **change}]}, generator.ImageMedia())


@pytest.mark.parametrize("mode", ["missing", "truncated", "bad_crc", "wrong_extension"])
def test_invalid_images_fail_before_creating_output(tmp_path, mode):
    image_path = tmp_path / "image.png"
    if mode != "missing":
        png(image_path)
        data = image_path.read_bytes()
        if mode == "truncated":
            image_path.write_bytes(data[:-8])
        elif mode == "bad_crc":
            image_path.write_bytes(data[:29] + bytes([data[29] ^ 1]) + data[30:])
        else:
            image_path = image_path.rename(tmp_path / "image.jpg")
    result, outputs = run_report(tmp_path, {"blocks": [{"type": "image", "path": str(image_path)}]})
    assert result.returncode != 0
    assert "ERROR:" in result.stderr
    assert not outputs


def test_unknown_block_and_nested_unknown_citation_are_not_silently_dropped(tmp_path):
    for block in ({"type": "unsupported", "text": "Important"}, {"type": "columns", "left": [{"type": "text", "text": "Important", "citations": ["missing"]}], "right": []}):
        result, outputs = run_report(tmp_path, {"blocks": [block]})
        assert result.returncode != 0
        assert not outputs


def test_legacy_text_report_versions_remain_editable_and_never_overwrite(tmp_path):
    spec = {"title": "Old report", "blocks": [{"type": "heading", "text": "Heading"}, {"type": "paragraph", "text": "**bold** and *italic*"}, {"type": "bullets", "items": ["one", "two"]}, {"type": "numbered", "items": ["first", "second"]}, {"type": "quote", "text": "quoted"}, {"type": "code", "text": "print('hello')"}, {"type": "page_break"}]}
    result, outputs = run_report(tmp_path, spec)
    assert result.returncode == 0, result.stderr
    first = outputs[0].read_bytes()
    result, outputs = run_report(tmp_path, spec)
    assert result.returncode == 0, result.stderr
    assert len(outputs) == 2
    assert outputs[0].read_bytes() == first
    with zipfile.ZipFile(outputs[1]) as archive:
        verify_package(archive)
        root = ET.fromstring(archive.read("word/document.xml"))
        assert "quoted" in "".join(root.itertext())
        assert root.find(".//w:b", NS) is not None
