"""Behavioral tests for the standalone PPT skill.

Private helpers are explicitly imported in this test context to exercise layout
preflight and strict publication failures without needing a production sandbox.
"""
import importlib.util
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "skills/ppt-report/scripts/generate_ppt.py"
BUNDLED_SOFFICE = (Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/soffice")
NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}


@pytest.fixture
def generator(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("ppt_report_test_generator", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "_OUTPUT_ROOT", tmp_path / "o_outcome")
    return module


def png(path, width=300, height=200):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    payload = b"\x89PNG\r\n\x1a\n"
    payload += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    payload += chunk(b"IDAT", zlib.compress((b"\x00" + b"\x2c\x78\x73" * width) * height))
    payload += chunk(b"IEND", b"")
    path.write_bytes(payload)
    return str(path)


def reference(rid="ref-1", **kwargs):
    return {"id": rid, "title": "Test source for layout only", "authors": ["Test Author"],
            "year": 2026, "source": "Synthetic test fixture", "url": "https://example.org/source", **kwargs}


def chart(**kwargs):
    return {"type": "chart", "chart_type": "line", "title": "指标趋势",
            "categories": ["基线", "第1周", "第2周", "第4周"],
            "series": [{"name": "指标甲", "values": [8.2, None, 6.5, 5.4]}],
            "unit": "mmol/L", "source": "合成数据，仅用于测试", **kwargs}


def slide_roots(path):
    with zipfile.ZipFile(path) as package:
        names = sorted((name for name in package.namelist()
                        if name.startswith("ppt/slides/slide") and name.endswith(".xml")),
                       key=lambda name: int(name.split("slide")[-1].split(".")[0]))
        return [ET.fromstring(package.read(name)) for name in names]


def all_text(root):
    return "".join(node.text or "" for node in root.findall(".//a:t", NS))


def named_shapes(root, prefix):
    return [shape for shape in root.findall(".//p:sp", NS)
            if shape.find("p:nvSpPr/p:cNvPr", NS).get("name", "").startswith(prefix)]


def page_reference_text(root):
    return "".join(all_text(shape) for shape in named_shapes(root, "PageReference"))


def test_mixed_media_is_embedded_cited_and_xsd_valid(generator, tmp_path):
    picture = png(tmp_path / "image.png")
    data = {"title": "图文报告测试", "references": [reference(), reference("alias")],
            "slides": [{"type": "columns", "title": "图文对照", "ratio": [1, 1],
                        "left": [{"type": "paragraph", "text": "说明与来源", "citations": ["alias"]}],
                        "right": [{"type": "image", "path": picture, "alt": '比例 "正确"',
                                   "caption": "图 1 资料", "source": "原始资料", "citations": ["ref-1"]}]},
                       chart(citations=["alias"])]}
    out = tmp_path / "mixed.pptx"
    n, counts, images = generator.build_package(data, out)
    assert n == 3 and images == 1 and counts["references"] == 1
    roots = slide_roots(out)
    assert "说明与来源" in all_text(roots[0]) and "[1]" in all_text(roots[0])
    picture_node = roots[0].find(".//p:nvPicPr/p:cNvPr", NS)
    assert picture_node.get("descr") == '比例 "正确"'
    assert "8.2" in all_text(roots[1]) and "缺测" in all_text(roots[1])
    assert "[2]" not in all_text(roots[2])
    assert "https://example.org/source" in all_text(roots[2])
    with zipfile.ZipFile(out) as package:
        assert package.read("ppt/media/image1.png") == Path(picture).read_bytes()
    generator.validate_package(out)
    if Path("/usr/bin/xmllint").exists():
        assert generator.validate_package_xsd(out) > 0


def test_line_chart_does_not_connect_missing_observations(generator, tmp_path):
    out = tmp_path / "gaps.pptx"
    generator.build_package({"slides": [chart()]}, out)
    names = {node.get("name") for node in slide_roots(out)[0].findall(".//p:cNvPr", NS)}
    assert "Point0_1" not in names
    assert "Line0_1" not in names and "Line0_2" not in names
    assert "Line0_3" in names


def test_duplicate_picture_on_one_slide_reuses_relationship(generator, tmp_path):
    image = {"type": "image", "path": png(tmp_path / "image.png")}
    out = tmp_path / "duplicate.pptx"
    _, _, count = generator.build_package({"slides": [{"type": "columns", "left": [image], "right": [image]}]}, out)
    assert count == 1
    generator.validate_package(out)
    with zipfile.ZipFile(out) as package:
        rel = ET.fromstring(package.read("ppt/slides/_rels/slide1.xml.rels"))
        assert len([node for node in rel if node.get("Type", "").endswith("/image")]) == 1


def test_images_charts_and_tables_can_share_a_slide(generator, tmp_path):
    image = {"type": "image", "path": png(tmp_path / "mixed.png"), "caption": "原图说明"}
    comparison = {"type": "table", "headers": ["项目", "所见"], "rows": [["A", "原始数值"], ["B", "需要复核"]]}
    trend = chart(title="", categories=["A", "B", "C"], series=[{"name": "数值", "values": [1, 2, 3]}])
    out = tmp_path / "image-chart-table.pptx"
    generator.build_package({"slides": [{"type": "columns", "title": "图表混排",
                                          "left": [image, comparison], "right": [trend]}]}, out)
    root = slide_roots(out)[0]
    assert root.find(".//p:pic", NS) is not None
    assert root.find(".//a:tbl", NS) is not None
    assert root.find('.//p:cNvPr[@name="Line0_1"]', NS) is not None
    generator.validate_package(out)


def test_half_width_chart_fits_with_two_long_readable_page_references(generator, tmp_path):
    """Measure the actual chart furniture instead of rejecting a fixed 3.2-inch box."""
    refs = [reference("method", title="医学报告图文表达：教学结构与排版方法说明（示例材料）",
                      authors=["演示作者甲", "演示作者乙", "演示作者丙", "演示作者丁"],
                      source="本次样式验证材料", url="https://example.org/layout-method"),
            reference("data", title="双系列随访指标的合成数据表（非临床证据）",
                      authors=["排版验证组"], source="本次生成器测试材料", url="https://example.org/layout-data")]
    comparison = chart(title="", chart_type="bar", categories=["基线", "随访"],
                       series=[{"name": "系列 A", "values": [8.2, 5.4]},
                               {"name": "系列 B", "values": [8.1, 6.8]}],
                       caption="图 3 基线与随访的分组比较", citations=["data"])
    out = tmp_path / "chart-with-long-references.pptx"
    generator.build_package({"references": refs, "slides": [{"type": "columns", "ratio": [1.25, 1],
        "title": "图表与解释并排，让比较更清楚", "left": [comparison],
        "right": [{"type": "paragraph", "text": "**先读图，再核对数值。**\n同一指标使用相同单位，系列配色在全篇保持一致。\n\n柱形从零基线开始，避免夸大微小差异。",
                   "citations": ["data", "method"]}]}]}, out)
    roots = slide_roots(out)
    root = roots[0]
    footer = page_reference_text(root)
    assert all(ref["title"] in footer for ref in refs)
    assert "演示作者丁" not in footer and "等" in footer
    assert "演示作者丁" in "".join(all_text(page) for page in roots[1:])
    reference_top = int(named_shapes(root, "PageReferenceRule")[0].find("p:spPr/a:xfrm/a:off", NS).get("y"))
    grid_y = [int(shape.find("p:spPr/a:xfrm/a:off", NS).get("y")) for shape in named_shapes(root, "Grid")]
    assert max(grid_y) - min(grid_y) >= round(1.2 * generator.EMU_IN)
    for prefix in ("Category", "Value", "Legend", "Tick", "CaptionSource"):
        for shape in named_shapes(root, prefix):
            box = shape.find("p:spPr/a:xfrm", NS)
            assert int(box.find("a:off", NS).get("y")) + int(box.find("a:ext", NS).get("cy")) <= reference_top
    assert all(value in all_text(root) for value in ("8.2", "5.4", "8.1", "6.8"))
    generator.validate_package(out)
    if Path("/usr/bin/xmllint").exists():
        generator.validate_package_xsd(out)


def test_chinese_wrapping_keeps_punctuation_with_its_phrase(generator, tmp_path):
    text = "缺测处保留空档，不把未检查数据当作零。此页数值仅供排版测试。"
    width = (generator._BODY_W - 457200) // 2
    lines = generator._wrap_text(text, width, 2000)
    assert "".join(lines) == text
    assert all(not line.startswith(("。", "，", "）", "】")) for line in lines)
    for example in ("甲乙丙丁。后续说明", "甲乙丙丁，后续说明", "甲乙丙（丁戊）后续", "甲乙【丙丁】后续"):
        wrapped = generator._wrap_text(example, round(4 * 2000 * 127 * 1.08), 2000)
        assert "".join(wrapped) == example
        assert all(not line.startswith(("。", "，", "）", "】")) for line in wrapped)
        assert all(not line.endswith(("（", "【")) for line in wrapped)
    out = tmp_path / "chinese-wrap.pptx"
    generator.build_package({"slides": [{"type": "columns", "left": [{"type": "text", "text": "背景"}],
                                          "right": [{"type": "paragraph", "text": text}]}]}, out)
    assert text in all_text(slide_roots(out)[0])
    generator.validate_package(out)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "12", 10**1000])
def test_chart_rejects_non_numeric_or_unbounded_values(generator, tmp_path, value):
    invalid = chart(series=[{"name": "指标", "values": [value, 1, 2, 3]}])
    with pytest.raises(ValueError, match="有限数值"):
        generator.build_package({"slides": [invalid]}, tmp_path / "invalid.pptx")


@pytest.mark.parametrize("updates, message", [
    ({"series": [{"name": "指标", "values": [1]}]}, "等长"),
    ({"series": [{"name": "指标", "values": [None] * 4}]}, "有限数值"),
    ({"y_min": 5}, "截轴"),
    ({"unit": ""}, "单位"),
    ({"source": ""}, "数据来源"),
])
def test_chart_rejects_ambiguous_data(generator, tmp_path, updates, message):
    with pytest.raises(ValueError, match=message):
        generator.build_package({"slides": [chart(**updates)]}, tmp_path / "invalid.pptx")


def test_negative_values_have_zero_axis_and_exact_value_grid(generator, tmp_path):
    out = tmp_path / "negative.pptx"
    generator.build_package({"slides": [chart(chart_type="bar", series=[{"name": "变化", "values": [-2, 0, None, 3.25]}])]}, out)
    root = slide_roots(out)[0]
    assert all(value in all_text(root) for value in ("-2", "0", "缺测", "3.25"))
    assert root.find('.//p:cNvPr[@name="ZeroAxis"]', NS) is not None
    assert root.find('.//p:cNvPr[@name="Bar0_2"]', NS) is None
    generator.validate_package(out)


def test_nice_chart_ticks_cover_data_and_keep_zero(generator, tmp_path):
    assert generator._nice_ticks([8.2, 5.4]) == [0, 2.5, 5, 7.5, 10]
    assert generator._nice_ticks([-2, 3.25]) == [-2, 0, 2, 4]
    for values in ([0], [-0.00003, 0.00002], [1e12], [5e-324]):
        ticks = generator._nice_ticks(values)
        assert ticks[0] <= min(values) and ticks[-1] >= max(values)
        assert 0 in ticks and len(ticks) >= 2
    out = tmp_path / "nice-axis.pptx"
    generator.build_package({"slides": [chart(series=[{"name": "Observed", "values": [8.2, 7.6, None, 5.4]}])]}, out)
    root = slide_roots(out)[0]
    shapes = {shape.find("p:nvSpPr/p:cNvPr", NS).get("name"): shape for shape in root.findall(".//p:sp", NS)}
    assert all_text(shapes["Tick4"]) == "10"
    assert all_text(shapes["Value0_0"]) == "8.2"
    assert all_text(shapes["ValueSeries0"]) == "Observed"


def test_table_column_widths_numeric_alignment_and_pagination_agree(generator, tmp_path):
    rows = [[f"Item {i:02d}", str(i + 0.25), "解释内容需要按照真实列宽进行换行。" * 2] for i in range(20)]
    out = tmp_path / "weighted-table.pptx"
    generator.build_package({"slides": [{"type": "table", "title": "结果与解释",
        "headers": ["项目", "数值", "解释"], "rows": rows, "column_widths": [1, 1, 3]}]}, out)
    roots = slide_roots(out)
    assert len(roots) > 1
    for root in roots:
        table = root.find(".//a:tbl", NS)
        widths = [int(node.get("w")) for node in table.findall("a:tblGrid/a:gridCol", NS)]
        assert sum(widths) == generator._BODY_W and abs(widths[2] - 3 * widths[0]) <= 2
        for row in table.findall("a:tr", NS):
            cells = row.findall("a:tc", NS)
            assert cells[1].find("a:txBody/a:p/a:pPr", NS).get("algn") == "r"
            assert cells[2].find("a:txBody/a:p/a:pPr", NS).get("algn") == "l"
            assert all(cell.find("a:tcPr/a:lnL/a:noFill", NS) is not None for cell in cells)
        height = sum(int(row.get("h")) for row in table.findall("a:tr", NS))
        assert height <= generator._BODY_H
    text = "".join(all_text(root) for root in roots)
    assert all(text.count(f"Item {i:02d}") == 1 for i in range(20))
    generator.validate_package(out)


def test_figure_caption_stays_with_image_in_spacious_column(generator, tmp_path):
    out = tmp_path / "figure-caption.pptx"
    generator.build_package({"slides": [{"type": "columns",
        "left": [{"type": "paragraph", "text": "图片说明"}],
        "right": [{"type": "image", "path": png(tmp_path / "figure.png", 600, 300),
                   "caption": "图 1 比例与说明", "source": "合成测试图片"}]}]}, out)
    root = slide_roots(out)[0]
    image_box = root.find(".//p:pic/p:spPr/a:xfrm", NS)
    image_bottom = int(image_box.find("a:off", NS).get("y")) + int(image_box.find("a:ext", NS).get("cy"))
    caption = next(shape for shape in root.findall(".//p:sp", NS)
                   if shape.find("p:nvSpPr/p:cNvPr", NS).get("name") == "CaptionSource")
    caption_top = int(caption.find("p:spPr/a:xfrm/a:off", NS).get("y"))
    assert caption_top - image_bottom == generator._BLOCK_GAP


def test_unknown_nested_reference_is_not_silently_dropped(generator, tmp_path):
    data = {"references": [reference()], "slides": [{"type": "columns",
            "left": [{"type": "text", "text": "结论", "citations": ["unknown"]}],
            "right": [{"type": "text", "text": "背景"}]}]}
    with pytest.raises(ValueError, match="未知文献"):
        generator.build_package(data, tmp_path / "invalid.pptx")


def test_reference_ids_conflict_and_doi_aliases_are_stable(generator):
    data = {"references": [reference(doi="10.1234/test"),
                           reference("alias", doi="https://doi.org/10.1234/TEST"),
                           reference("second", title="Other source", url="https://example.org/other")]}
    numbers, entries = generator._reference_catalog(data)
    assert numbers == {"ref-1": 1, "alias": 1, "second": 2} and len(entries) == 2
    with pytest.raises(ValueError, match="定义冲突"):
        generator._reference_catalog({"references": [reference(), reference(title="Conflicting")]})


def test_page_references_collect_only_local_nested_and_slide_citations(generator, tmp_path):
    refs = [reference(title="Evidence Alpha", doi="10.1234/alpha"),
            reference("alias", title="Evidence Alpha", doi="https://doi.org/10.1234/ALPHA"),
            reference("ref-2", title="Evidence Beta", url="https://example.org/beta"),
            reference("ref-3", title="Evidence Gamma", url="https://example.org/gamma"),
            reference("unused", title="Unused Delta", url="https://example.org/delta")]
    out = tmp_path / "local-references.pptx"
    generator.build_package({"references": refs, "slides": [
        {"type": "title", "title": "Cover claim", "citations": ["ref-2"]},
        {"type": "columns", "title": "Nested claims", "citations": ["ref-2"],
         "left": [{"type": "text", "text": "First claim", "citations": ["alias", "ref-1"]}],
         "right": [{"type": "bullets", "bullets": [{"text": "Third claim", "citations": ["ref-3"]}]}]},
        {"type": "text", "text": "Second claim", "citations": ["ref-2"]},
        {"type": "two_column", "citations": ["alias"],
         "left": [{"text": "Legacy third claim", "citations": ["ref-3"]}], "right": ["Context"]}
    ]}, out)
    roots = slide_roots(out)
    for root, expected in zip(roots[:4], ([2], [1, 2, 3], [2], [1, 3])):
        footer = page_reference_text(root)
        numbers = [all_text(shape) for shape in named_shapes(root, "PageReferenceNumber")]
        assert numbers == [f"[{number}]" for number in expected]
        for number, title in enumerate(("Evidence Alpha", "Evidence Beta", "Evidence Gamma", "Unused Delta"), 1):
            assert footer.count(title) == (1 if number in expected else 0)
    assert "DOI: 10.1234/alpha" in page_reference_text(roots[1])
    assert "https://example.org/beta" in page_reference_text(roots[2])
    assert all(not named_shapes(root, "PageReference") for root in roots[4:])
    bibliography = "".join(all_text(root) for root in roots[4:])
    assert all(bibliography.count(title) == 1 for title in ("Evidence Alpha", "Evidence Beta", "Evidence Gamma", "Unused Delta"))
    generator.validate_package(out)
    if Path("/usr/bin/xmllint").exists():
        generator.validate_package_xsd(out)


def test_uncited_slide_keeps_full_body_without_empty_reference_region(generator, tmp_path):
    out = tmp_path / "uncited.pptx"
    data = {"references": [reference()], "slides": [{"type": "text", "text": "Original observation"}]}
    slides, _ = generator._prepare_slides(data)
    assert not slides[0].get("_page_references")
    assert generator._page_reference_height([]) == 0
    generator.build_package(data, out)
    root = slide_roots(out)[0]
    assert not named_shapes(root, "PageReference")
    text_box = named_shapes(root, "Text")[0]
    box = text_box.find("p:spPr/a:xfrm", NS)
    assert int(box.find("a:off", NS).get("y")) == generator._BODY_Y
    assert int(box.find("a:ext", NS).get("cy")) == generator._BODY_H


def test_bullet_continuations_cite_only_the_items_visible_on_that_page(generator, tmp_path):
    refs = [reference(title="Evidence Alpha"),
            reference("ref-2", title="Evidence Beta", url="https://example.org/beta")]
    items = [{"text": f"POINT-{index:02d}", "citations": ["ref-1" if index < 7 else "ref-2"]}
             for index in range(14)]
    out = tmp_path / "bullet-references.pptx"
    _, counts, _ = generator.build_package({"references": refs, "slides": [
        {"type": "bullets", "title": "Evidence per claim", "bullets": items}]}, out)
    roots = slide_roots(out)[:counts["bullets"]]
    assert len(roots) >= 3
    observed = []
    for root in roots:
        indices = [int(index) for index in re.findall(r"POINT-(\d{2})", all_text(root))]
        assert indices
        observed.extend(indices)
        expected = {1 if index < 7 else 2 for index in indices}
        footer = page_reference_text(root)
        assert ("Evidence Alpha" in footer) == (1 in expected)
        assert ("Evidence Beta" in footer) == (2 in expected)
        assert [all_text(shape) for shape in named_shapes(root, "PageReferenceNumber")] == [f"[{n}]" for n in sorted(expected)]
        body = named_shapes(root, "Content")[0].find("p:spPr/a:xfrm", NS)
        bottom = int(body.find("a:off", NS).get("y")) + int(body.find("a:ext", NS).get("cy"))
        reference_top = int(named_shapes(root, "PageReferenceRule")[0].find("p:spPr/a:xfrm/a:off", NS).get("y"))
        assert bottom <= reference_top
    assert observed == list(range(14))
    generator.validate_package(out)


def test_table_continuations_reserve_reference_space_without_losing_rows(generator, tmp_path):
    rows = [[f"ROW-{index:02d}", str(index)] for index in range(29)]
    table = {"type": "table", "headers": ["Observation", "Value"], "rows": rows}
    plain, cited = tmp_path / "plain-table.pptx", tmp_path / "cited-table.pptx"
    _, plain_counts, _ = generator.build_package({"slides": [table]}, plain)
    _, counts, _ = generator.build_package({"references": [reference(title="Table evidence")],
                                          "slides": [{**table, "citations": ["ref-1"]}]}, cited)
    assert counts["table"] > plain_counts["table"]
    roots = slide_roots(cited)[:counts["table"]]
    observed = []
    for root in roots:
        assert page_reference_text(root).count("Table evidence") == 1
        frame = root.find(".//p:graphicFrame", NS)
        assert "ObservationValue" in all_text(frame)
        observed.extend(int(index) for index in re.findall(r"ROW-(\d{2})", all_text(frame)))
        top = int(frame.find("p:xfrm/a:off", NS).get("y"))
        height = sum(int(row.get("h")) for row in frame.findall(".//a:tr", NS))
        reference_top = int(named_shapes(root, "PageReferenceRule")[0].find("p:spPr/a:xfrm/a:off", NS).get("y"))
        assert top + height <= reference_top
    assert observed == list(range(29))
    generator.validate_package(cited)


@pytest.mark.parametrize("many", [False, True], ids=["long-entry", "many-entries"])
def test_overfull_page_references_fail_instead_of_truncating_evidence(generator, tmp_path, many):
    count = 15 if many else 1
    refs = [reference(f"r-{index}", title=f"Evidence {index} " + ("完整研究题名" * 80 if not many else ""),
                      url=f"https://example.org/evidence/{index}") for index in range(count)]
    with pytest.raises(ValueError, match="本页参考文献过密.*拆页"):
        generator.build_package({"references": refs, "slides": [
            {"type": "text", "text": "Claim", "citations": [ref["id"] for ref in refs]}]}, tmp_path / "overfull.pptx")


def test_only_real_citation_markers_are_superscript_and_stay_with_their_text(generator, tmp_path):
    markers = generator._citation_markers({"citations": ["second", "first"]}, {"first": 1, "second": 2})
    for phrase in ("甲乙丙丁戊己。" + markers, "甲乙丙丁" + markers + "。后续", "甲乙丙丁" + markers + "，后续"):
        wrapped = generator._wrap_text(phrase, round(6 * 2000 * 127 * 1.08), 2000)
        assert "".join(wrapped) == phrase and len(wrapped) > 1
        assert sum(markers in line for line in wrapped) == 1
        assert all(not line.startswith(markers) for line in wrapped)
    image = png(tmp_path / "citation.png")
    out = tmp_path / "citation-runs.pptx"
    generator.build_package({"references": [reference(), reference("alias"),
                                            reference("ref-2", title="Second source", url="https://example.org/second")],
                            "slides": [{"type": "columns",
        "left": [{"type": "text", "text": "Literal [1].", "citations": ["ref-2", "alias", "ref-1"]}],
        "right": [{"type": "image", "path": image, "caption": "Panel [1].", "citations": ["ref-1"]}]}]}, out)
    root = slide_roots(out)[0]
    for prefix, literal, expected in (("Text", "Literal [1].", [1, 2]), ("CaptionSource", "Panel [1].", [1])):
        shape = named_shapes(root, prefix)[0]
        paragraph = shape.find("p:txBody/a:p", NS)
        runs = paragraph.findall("a:r", NS)
        normal = [run for run in runs if int(run.find("a:rPr", NS).get("baseline", "0")) <= 0]
        elevated = [run for run in runs if int(run.find("a:rPr", NS).get("baseline", "0")) > 0]
        assert literal in "".join(all_text(run) for run in normal)
        assert [int(number) for run in elevated for number in re.findall(r"\d+", all_text(run))] == expected
        body_size = max(int(run.find("a:rPr", NS).get("sz")) for run in normal)
        # Clients already scale DrawingML superscripts; do not shrink twice.
        assert all(int(run.find("a:rPr", NS).get("sz")) >= body_size for run in elevated)
        children = list(paragraph)
        assert all(children[children.index(run) - 1].tag != f"{{{NS['a']}}}br" for run in elevated)
        assert not any(0xE000 <= ord(character) <= 0xF8FF for character in all_text(shape))
    generator.validate_package(out)
    if Path("/usr/bin/xmllint").exists():
        generator.validate_package_xsd(out)


def test_table_and_bullets_paginate_without_losing_content(generator, tmp_path):
    rows = [[f"Row-{i:02d}", "解释内容" * 8] for i in range(23)]
    bullets = [{"text": f"要点 {i}", "citations": ["ref-1"]} for i in range(14)]
    out = tmp_path / "pages.pptx"
    n, counts, _ = generator.build_package({"references": [reference()], "slides": [
        {"type": "table", "title": "表格", "headers": ["项目", "说明"], "rows": rows},
        {"type": "bullets", "title": "要点", "bullets": bullets}]}, out)
    roots = slide_roots(out)
    assert counts["table"] >= 3 and counts["bullets"] == 3
    joined = "\n".join(all_text(root) for root in roots)
    for i in range(23):
        assert joined.count(f"Row-{i:02d}") == 1
    for root in roots[:counts["table"]]:
        assert "项目说明" in all_text(root)
    assert joined.count("[1]") == 15 + counts["bullets"]  # 14 inline uses, per-page entries, one bibliography entry
    generator.validate_package(out)


def test_long_reference_list_has_readable_continuation_slides(generator, tmp_path):
    refs = [reference(str(i), title=f"Research item {i:02d} " + "Full publication title " * 3,
                      url=f"https://example.org/paper/{i}") for i in range(25)]
    out = tmp_path / "references.pptx"
    _, counts, _ = generator.build_package({"references": refs,
                                          "slides": [{"type": "bullets", "bullets": ["概要"]}]}, out)
    assert counts["references"] > 1
    joined = "".join(all_text(root) for root in slide_roots(out))
    for i in range(25):
        assert f"Research item {i:02d}" in joined
    for root in slide_roots(out):
        for shape in root.findall(".//p:sp", NS):
            if shape.find("p:nvSpPr/p:cNvPr", NS).get("name") == "ReferenceNumber":
                assert not shape.findall(".//a:br", NS)
    generator.validate_package(out)


def test_long_text_and_unreadable_long_image_fail_clearly(generator, tmp_path):
    with pytest.raises(ValueError, match="文字超出"):
        generator.build_package({"slides": [{"type": "text", "text": "长文字" * 2000}]}, tmp_path / "text.pptx")
    long_picture = png(tmp_path / "long.png", width=120, height=1600)
    with pytest.raises(ValueError, match="长图缩放后细节不可读"):
        generator.build_package({"slides": [{"type": "image", "path": long_picture}]}, tmp_path / "long.pptx")


def test_missing_corrupt_and_escaped_nested_images_fail(generator, tmp_path):
    broken = tmp_path / "corrupt.png"
    broken.write_bytes(b"not png")
    with pytest.raises(ValueError, match="图片无法嵌入"):
        generator.build_package({"slides": [{"type": "image", "path": str(broken)}]}, tmp_path / "invalid.pptx")
    escaped = {"type": "columns", "left": [{"type": "image", "path": str(SCRIPT)}],
               "right": [{"type": "text", "text": "说明"}]}
    with pytest.raises(ValueError, match="must stay under"):
        generator.build_package({"slides": [escaped]}, tmp_path / "escape.pptx")


@pytest.mark.parametrize("slide", [
    pytest.param({"type": "text"}, id="missing-text"),
    pytest.param({"type": "paragraph", "text": " \n\t "}, id="whitespace-text"),
    pytest.param({"type": "paragraph", "text": None}, id="null-text"),
    pytest.param({"type": "text", "text": "** **"}, id="formatting-only-text"),
    pytest.param({"type": "text", "text": "\u200b\u2060"}, id="invisible-text"),
    pytest.param({"type": "bullets"}, id="missing-bullets"),
    pytest.param({"type": "bullets", "bullets": []}, id="empty-bullets"),
    pytest.param({"type": "bullets", "bullets": [" ", {"text": "\n"}]}, id="blank-bullets"),
    pytest.param({"type": "bullets", "bullets": [], "caption": "Caption only", "source": "Source only",
                  "notes": "Speaker notes only", "citations": ["ref-1"]}, id="metadata-is-not-body"),
])
def test_content_slides_require_visible_body(generator, tmp_path, slide):
    out = tmp_path / "empty-body.pptx"
    with pytest.raises(ValueError) as error:
        generator.build_package({"references": [reference()],
                                 "slides": [{"title": "Content heading", **slide}]}, out)
    assert any(term in str(error.value) for term in ("正文", "text", "bullets"))
    assert "slides[0]" in str(error.value)
    assert not out.exists()


@pytest.mark.parametrize("slide, field", [
    pytest.param({"content": ["Must not disappear"]}, "content", id="default-content-alias"),
    pytest.param({"text": "Must not disappear"}, "text", id="default-type-text"),
    pytest.param({"type": "bullets", "items": ["Must not disappear"]}, "items", id="wrong-items-key"),
    pytest.param({"type": "paragraph", "body": "Must not disappear"}, "body", id="wrong-body-key"),
    pytest.param({"type": "text", "text": "Visible claim", "content": "Must not disappear"},
                 "content", id="extra-body-is-not-discarded"),
    pytest.param({"type": "bullets", "bullets": ["Visible claim"], "subtitle": "Must not disappear"},
                 "subtitle", id="bullets-cannot-discard-subtitle"),
    pytest.param({"type": "text", "text": "Visible claim", "subtitle": "Must not disappear"},
                 "subtitle", id="text-cannot-discard-subtitle"),
])
def test_misspelled_or_misplaced_body_fields_fail_with_the_field_name(generator, tmp_path, slide, field):
    out = tmp_path / "wrong-field.pptx"
    with pytest.raises(ValueError, match=field):
        generator.build_package({"slides": [{"title": "Content heading", **slide}]}, out)
    assert not out.exists()


@pytest.mark.parametrize("updates", [
    pytest.param({}, id="missing-rows"),
    pytest.param({"rows": []}, id="headers-only"),
    pytest.param({"rows": [[]]}, id="empty-row"),
    pytest.param({"rows": [["  "]]}, id="blank-row"),
    pytest.param({"rows": [[f"Value {index}"] for index in range(9)] + [[""]]}, id="blank-continuation-row"),
])
def test_tables_require_data_rows_and_reject_empty_continuations(generator, tmp_path, updates):
    out = tmp_path / "empty-table.pptx"
    with pytest.raises(ValueError) as error:
        generator.build_package({"slides": [{"type": "table", "title": "Table heading",
                                             "headers": ["Observation"], **updates}]}, out)
    assert any(term in str(error.value) for term in ("rows", "表格", "数据行", "正文"))
    assert not out.exists()


@pytest.mark.parametrize("slide", [
    pytest.param({"type": "columns", "left": [], "right": [{"type": "text", "text": "Context"}]}, id="empty-column"),
    pytest.param({"type": "columns", "left": [{}], "right": [{"type": "text", "text": "Context"}]}, id="missing-block-body"),
    pytest.param({"type": "columns", "left": [{"type": "text", "title": "Small heading only"}],
                  "right": [{"type": "text", "text": "Context"}]}, id="block-heading-is-not-body"),
    pytest.param({"type": "two_column", "left": [], "right": ["Context"]}, id="empty-legacy-column"),
    pytest.param({"type": "two_column", "left": ["Context"], "right": [" \n "]}, id="blank-legacy-column"),
])
def test_both_column_layouts_reject_empty_columns_or_blocks(generator, tmp_path, slide):
    out = tmp_path / "empty-columns.pptx"
    with pytest.raises(ValueError) as error:
        generator.build_package({"slides": [{"title": "Columns heading", **slide}]}, out)
    assert any(term in str(error.value) for term in ("left", "right", "text", "正文", "栏"))
    assert not out.exists()


@pytest.mark.parametrize("slide", [
    pytest.param({"type": "title", "title": " "}, id="blank-cover-title"),
    pytest.param({"type": "section"}, id="missing-section-title"),
    pytest.param({"type": "title", "title": "Cover", "text": "Must not disappear"}, id="cover-with-body"),
    pytest.param({"type": "section", "title": "Section", "bullets": ["Must not disappear"]}, id="section-with-body"),
])
def test_title_only_layouts_need_a_title_and_cannot_discard_body_fields(generator, tmp_path, slide):
    with pytest.raises(ValueError) as error:
        generator.build_package({"slides": [slide]}, tmp_path / "invalid-title.pptx")
    assert any(term in str(error.value) for term in ("title", "text", "bullets", "标题", "正文"))


def test_explicit_title_pages_media_pages_and_zero_values_remain_valid(generator, tmp_path):
    out = tmp_path / "valid-content-types.pptx"
    image = png(tmp_path / "figure.png")
    count, _, _ = generator.build_package({"references": [reference()], "slides": [
        {"type": "title", "title": "参考依据", "citations": ["ref-1"]},
        {"type": "section", "title": "参考依据", "citations": ["ref-1"]},
        {"type": "image", "title": "Image is the body", "path": image},
        chart(title="Chart is the body", series=[{"name": "Observed", "values": [0, 0, 0, 0]}], citations=["ref-1"]),
        {"type": "table", "title": "Zero is data", "headers": ["Value"], "rows": [[0]]},
        {"title": "Legacy default bullets", "bullets": ["Body remains visible"]},
    ]}, out)
    assert count == 7
    roots = slide_roots(out)
    assert roots[2].find(".//p:pic", NS) is not None
    assert all_text(named_shapes(roots[3], "Value0_0")[0]) == "0"
    assert "Value0" in all_text(roots[4].find(".//a:tbl", NS))
    assert "Body remains visible" in all_text(roots[5])
    generator.validate_package(out)
    if Path("/usr/bin/xmllint").exists():
        generator.validate_package_xsd(out)
    # Earlier generated decks have the same visible shapes without page-kind metadata.
    legacy = tmp_path / "valid-legacy-content.pptx"
    with zipfile.ZipFile(out) as source, zipfile.ZipFile(legacy, "w", zipfile.ZIP_DEFLATED) as destination:
        for item in source.infolist():
            data = source.read(item.filename)
            if re.fullmatch(r"ppt/slides/slide\d+\.xml", item.filename):
                root = ET.fromstring(data)
                root.find("p:cSld", NS).attrib.pop("name", None)
                data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            destination.writestr(item, data)
    generator.validate_package(legacy)


def test_trailing_empty_bullet_cannot_create_a_title_only_continuation(generator, tmp_path):
    out = tmp_path / "blank-bullet-continuation.pptx"
    with pytest.raises(ValueError):
        generator.build_package({"slides": [{"type": "bullets", "title": "Claims",
                                             "bullets": [f"Claim {index}" for index in range(6)] + [""]}]}, out)
    assert not out.exists()


@pytest.mark.parametrize("mode", ["removed", "blanked", "legacy-blanked"])
def test_package_validation_detects_missing_body_despite_metadata(generator, tmp_path, mode):
    out = tmp_path / "body-integrity.pptx"
    generator.build_package({"references": [reference()], "slides": [
        {"type": "bullets", "title": "Main title survives", "bullets": ["BODY_SENTINEL"],
         "caption": "Caption survives", "source": "Source survives", "citations": ["ref-1"],
         "notes": "Speaker notes survive"}]}, out)
    generator.validate_package(out)
    with zipfile.ZipFile(out) as package:
        members = [(item, package.read(item.filename)) for item in package.infolist()]
    root = ET.fromstring(next(data for item, data in members if item.filename == "ppt/slides/slide1.xml"))
    if mode == "legacy-blanked":
        root.find("p:cSld", NS).attrib.pop("name", None)
    body = named_shapes(root, "Content")
    assert body and any("BODY_SENTINEL" in all_text(shape) for shape in body)
    for shape in body:
        if mode == "removed":
            root.find("p:cSld/p:spTree", NS).remove(shape)
        else:
            for text in shape.findall(".//a:t", NS):
                text.text = ""
    assert "Main title survives" in all_text(root) and "Caption survives" in all_text(root)
    assert "Source survives" in all_text(root) and "Test source for layout only" in all_text(root)
    assert named_shapes(root, "PageReferenceNumber")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as package:
        for item, data in members:
            if item.filename == "ppt/slides/slide1.xml":
                data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            package.writestr(item, data)
    with zipfile.ZipFile(out) as package:
        assert "ppt/notesSlides/notesSlide1.xml" in package.namelist()
    with pytest.raises(ValueError) as error:
        generator.validate_package(out)
    assert any(term in str(error.value) for term in ("正文", "内容为空", "body"))


def test_legacy_slide_types_and_speaker_notes_remain_valid(generator, tmp_path):
    out = tmp_path / "legacy.pptx"
    data = {"slides": [{"type": "title", "title": "报告", "subtitle": "摘要"},
                       {"type": "section", "title": "事实"},
                       {"type": "bullets", "title": "要点", "bullets": ["**重点**", {"text": "子点", "level": 1}], "notes": "补充说明"},
                       {"type": "two_column", "left": ["左"], "right": ["右"]},
                       {"type": "table", "headers": ["列 A", "列 B"], "rows": [["A", "B"]]},
                       {"type": "image", "path": png(tmp_path / "legacy.png"), "caption": "原图"}]}
    assert generator.build_package(data, out)[0] == 6
    generator.validate_package(out)
    if Path("/usr/bin/xmllint").exists():
        generator.validate_package_xsd(out)
    with zipfile.ZipFile(out) as package:
        assert "ppt/notesSlides/notesSlide3.xml" in package.namelist()


def test_missing_sdk_never_publishes_or_changes_history(generator, tmp_path, monkeypatch, capsys):
    import json
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({"title": "Protected", "slides": [{"type": "bullets", "bullets": ["正文"]}]}))
    destination = tmp_path / "o_outcome"
    destination.mkdir()
    historical = destination / "Protected_2026_09_08_v1.pptx"
    historical.write_bytes(b"historical content")
    monkeypatch.setattr(generator, "_OPENXML_SDK_VALIDATOR", tmp_path / "missing-sdk")
    # This test isolates the SDK publication gate; XSD is exercised separately.
    monkeypatch.setattr(generator, "validate_package_xsd", lambda path: 10)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--spec", str(spec_path), "--date", "2026-09-08"])
    assert generator.main() == 1
    assert historical.read_bytes() == b"historical content"
    assert list(destination.iterdir()) == [historical]
    assert "Output:" not in capsys.readouterr().out


def test_empty_body_cli_failure_leaves_no_candidate_and_preserves_history(generator, tmp_path, monkeypatch, capsys):
    import json
    spec_path = tmp_path / "empty-spec.json"
    spec_path.write_text(json.dumps({"title": "Protected", "slides": [
        {"type": "bullets", "title": "Only a heading", "notes": "Notes cannot replace slide content"}]}))
    destination = tmp_path / "o_outcome"
    destination.mkdir()
    historical = destination / "Protected_2026_09_08_v1.pptx"
    historical.write_bytes(b"historical content")

    def unexpected_external_validation(_):
        pytest.fail("An empty content slide reached external publication validators")

    monkeypatch.setattr(generator, "validate_package_xsd", unexpected_external_validation)
    monkeypatch.setattr(generator, "validate_package_openxml_sdk", unexpected_external_validation)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--spec", str(spec_path), "--date", "2026-09-08"])
    assert generator.main() == 1
    captured = capsys.readouterr()
    assert "Output:" not in captured.out
    assert "正文" in captured.err or "bullets" in captured.err
    assert historical.read_bytes() == b"historical content"
    assert list(destination.iterdir()) == [historical]


def test_cli_checks_generated_body_before_publication_and_cleans_failed_candidate(generator, tmp_path, monkeypatch, capsys):
    import json
    spec_path = tmp_path / "valid-spec.json"
    spec_path.write_text(json.dumps({"title": "Protected", "references": [reference()], "slides": [
        {"type": "bullets", "title": "Main title survives", "bullets": ["VALID_BODY_SENTINEL"],
         "caption": "Caption survives", "source": "Source survives", "citations": ["ref-1"],
         "notes": "Speaker notes survive"}]}))
    destination = tmp_path / "o_outcome"
    destination.mkdir()
    historical = destination / "Protected_2026_09_08_v1.pptx"
    historical.write_bytes(b"historical content")
    real_build = generator.build_package
    candidates = []

    def build_then_remove_body(spec, candidate):
        result = real_build(spec, candidate)
        generator.validate_package(candidate)  # The valid spec produced a complete package first.
        candidates.append(candidate)
        with zipfile.ZipFile(candidate) as package:
            members = [(item, package.read(item.filename)) for item in package.infolist()]
        root = ET.fromstring(next(data for item, data in members if item.filename == "ppt/slides/slide1.xml"))
        body = named_shapes(root, "Content")
        assert body and any("VALID_BODY_SENTINEL" in all_text(shape) for shape in body)
        for shape in body:
            root.find("p:cSld/p:spTree", NS).remove(shape)
        assert "VALID_BODY_SENTINEL" not in all_text(root)
        assert all(text in all_text(root) for text in (
            "Main title survives", "Caption survives", "Source survives", "Test source for layout only"))
        assert named_shapes(root, "PageReferenceNumber")
        assert any(item.filename == "ppt/notesSlides/notesSlide1.xml" for item, _ in members)
        with zipfile.ZipFile(candidate, "w", zipfile.ZIP_DEFLATED) as package:
            for item, data in members:
                if item.filename == "ppt/slides/slide1.xml":
                    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
                package.writestr(item, data)
        return result

    def unexpected_external_validation(_):
        pytest.fail("A generated slide with missing body reached external publication validators")

    monkeypatch.setattr(generator, "build_package", build_then_remove_body)
    monkeypatch.setattr(generator, "validate_package_xsd", unexpected_external_validation)
    monkeypatch.setattr(generator, "validate_package_openxml_sdk", unexpected_external_validation)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--spec", str(spec_path), "--date", "2026-09-08"])
    assert generator.main() == 1
    captured = capsys.readouterr()
    assert len(candidates) == 1 and not candidates[0].exists()
    assert "正文" in captured.err and "ppt/slides/slide1.xml" in captured.err
    assert not any(marker in captured.out for marker in (
        "Output:", "Content validation:", "Compatibility:", "Strict validation:", "passed", "通过"))
    assert historical.read_bytes() == b"historical content"
    assert list(destination.iterdir()) == [historical]


@pytest.mark.skipif(not BUNDLED_SOFFICE.exists() or not shutil.which("pdftotext"),
                    reason="Optional real-client layout regression needs bundled LibreOffice and pdftotext")
def test_rendered_multiline_text_sources_and_references_remain_visible(generator, tmp_path):
    """Regression: inherited paragraph-end sizes used to clip complete lines."""
    out = tmp_path / "render.pptx"
    image = png(tmp_path / "render.png")
    data = {"title": "Layout regression", "references": [reference(), reference(
        "ref-2", title="Evidence reserved for second page", url="https://example.org/second")], "slides": [
        {"type": "columns", "title": "Content visibility",
         "left": [{"type": "text", "text": "FirstLineVisible\nSecondLineMustRemainVisible", "citations": ["ref-1"]}],
         "right": [{"type": "image", "path": image, "caption": "CaptionLineVisible",
                    "source": "SourceLineMustRemainVisible", "citations": ["ref-1"]}]},
        {"type": "text", "title": "Second evidence page", "text": "Second page claim", "citations": ["ref-2"]}]}
    generator.build_package(data, out)
    result = subprocess.run([str(BUNDLED_SOFFICE), f"-env:UserInstallation={(tmp_path / 'lo-profile').as_uri()}",
                             "--headless", "--convert-to", "pdf", "--outdir", str(tmp_path), str(out)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([shutil.which("pdftotext"), "-layout", str(out.with_suffix(".pdf")), "-"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    for required in ("FirstLineVisible", "SecondLineMustRemainVisible", "CaptionLineVisible",
                     "SourceLineMustRemainVisible", "Test source for layout only", "https://example.org/source", "[1]"):
        assert required in result.stdout
    pages = result.stdout.split("\f")
    assert "Test source for layout only" in pages[0] and "https://example.org/source" in pages[0]
    assert "Evidence reserved for second page" not in pages[0]
    assert "Evidence reserved for second page" in pages[1] and "https://example.org/second" in pages[1]
    assert "Test source for layout only" not in pages[1]
    if shutil.which("pdftohtml"):
        rendered = subprocess.run([shutil.which("pdftohtml"), "-xml", "-i", "-zoom", "1", "-stdout",
                                   str(out.with_suffix(".pdf"))], capture_output=True, text=True, timeout=30)
        assert rendered.returncode == 0, rendered.stderr
        document = ET.fromstring(rendered.stdout)
        fonts = {node.get("id"): float(node.get("size")) for node in document.findall(".//fontspec")}
        visible = document.find("page").findall("text")
        caption = next(node for node in visible if "".join(node.itertext()) == "CaptionLineVisible")
        caption_right = float(caption.get("left")) + float(caption.get("width"))
        marker = next(node for node in visible if "".join(node.itertext()) == "[1]"
                      and abs(float(node.get("left")) - caption_right) < 2)
        assert fonts[marker.get("font")] >= 10  # Rendered points, after the client's superscript transform.
        assert float(marker.get("top")) + float(marker.get("height")) < float(caption.get("top")) + float(caption.get("height"))


@pytest.mark.skipif(not BUNDLED_SOFFICE.exists() or not shutil.which("pdftotext")
                    or not Path("/System/Library/Fonts/Hiragino Sans GB.ttc").exists(),
                    reason="Optional CJK client regression needs bundled LibreOffice, pdftotext, and the test CJK font")
def test_rendered_bold_cjk_paragraphs_keep_final_lines(generator, tmp_path):
    intro = "**让结论可解释，让证据可追溯。**\n先呈现原始事实，再解释变化及其局限。图片与说明放在同一阅读区域，重要判断附上对应来源。"
    explanation = "**先读图，再核对数值。**\n同一指标使用相同单位，系列配色在全篇保持一致。\n\n柱形从零基线开始，避免夸大微小差异。"
    out = tmp_path / "cjk-leading.pptx"
    generator.build_package({"slides": [
        {"type": "columns", "ratio": [1, 1.2], "left": [{"type": "paragraph", "text": intro}],
         "right": [{"type": "paragraph", "text": "图像位置"}]},
        {"type": "columns", "ratio": [1.25, 1], "left": [{"type": "paragraph", "text": "图表位置"}],
         "right": [{"type": "paragraph", "text": explanation}]}]}, out)
    fonts = tmp_path / "fonts.conf"
    fonts.write_text('<fontconfig><dir>/System/Library/Fonts</dir><dir>/Library/Fonts</dir>'
                     f'<cachedir>{tmp_path / "font-cache"}</cachedir>'
                     '<alias><family>微软雅黑</family><prefer><family>Hiragino Sans GB</family></prefer></alias>'
                     '</fontconfig>')
    env = {**os.environ, "FONTCONFIG_FILE": str(fonts)}
    result = subprocess.run([str(BUNDLED_SOFFICE), f"-env:UserInstallation={(tmp_path / 'lo-cjk-profile').as_uri()}",
                             "--headless", "--convert-to", "pdf", "--outdir", str(tmp_path), str(out)],
                            capture_output=True, text=True, timeout=60, env=env)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([shutil.which("pdftotext"), "-layout", str(out.with_suffix(".pdf")), "-"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    actual = "".join(result.stdout.split())
    for paragraph in (intro, explanation):
        for section in paragraph.replace("**", "").split("\n"):
            assert "".join(section.split()) in actual
