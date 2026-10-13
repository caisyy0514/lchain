"""Behavioral tests for the standalone HTML skill (no service configuration).

The generator is a script, so this test explicitly loads its module by path.
"""

import importlib.util
import json
import re
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import zlib
from html.parser import HTMLParser
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "skills/html-report/scripts/generate_html.py"
MODULE_SPEC = importlib.util.spec_from_file_location("html_report_generator", SCRIPT)
html_report = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(html_report)


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.ids, self.links, self.images, self.references = [], [], [], []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "a":
            self.links.append(attrs.get("href"))
        if tag == "img":
            self.images.append(attrs)
        if tag == "li" and attrs.get("id", "").startswith("ref-"):
            self.references.append(attrs["id"])


def write_png(path):
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0\0\xff\0")) + chunk(b"IEND", b""))


def chart(**overrides):
    return {"type": "chart", "chart_type": "line", "categories": ["基线", "随访"],
            "series": [{"name": "指标", "values": [1, 2]}], "unit": "mmol/L",
            "source": "测试合成数据，非临床证据", **overrides}


def svg_from(html):
    return ET.fromstring(re.search(r"<svg\b.*?</svg>", html, re.S).group())


def test_mixed_report_embeds_images_data_and_resolvable_references(tmp_path):
    write_png(tmp_path / "figure.png")
    spec = {
        "title": "混排验证",
        "references": [{"id": "r1", "title": "演示资料", "url": "https://example.org/evidence"}],
        "blocks": [{"type": "heading", "text": "同名章节"}, {"type": "heading", "text": "同名章节"},
                   {"type": "columns", "left": [{"type": "paragraph", "text": "结论", "citations": ["r1"]}],
                    "right": [{"type": "image", "path": "figure.png", "alt": "红绿测试图", "caption": "图 1", "citations": ["r1"]}]},
                   chart(citations=["r1"]),
                   {"type": "table", "headers": ["A", "B"], "rows": [[1, 2]], "caption": "结果表", "citations": ["r1"]}],
    }
    html, context = html_report.render_report(spec, base_dir=tmp_path, force_toc=True)
    doc = Document(html)
    assert len(doc.images) == context.figure_count == 1
    assert doc.images[0]["src"].startswith("data:image/png;base64,")
    assert doc.images[0]["alt"] == "红绿测试图"
    assert context.chart_count == 1
    assert len(doc.references) == 1
    assert len(doc.ids) == len(set(doc.ids))
    assert all(href[1:] in doc.ids for href in doc.links if href.startswith("#"))
    assert not re.search(r'<(?:script|link|iframe)\b|src="https?://', html)


def test_reference_aliases_deduplicate_by_both_doi_and_url():
    context = html_report.RenderContext([
        {"id": "doi", "title": "演示资料", "doi": "10.1234/example", "url": "https://example.org/evidence"},
        {"id": "url", "title": "演示资料", "url": "https://example.org/evidence"},
        {"id": "alternate", "title": "演示资料", "doi": "https://doi.org/10.1234/EXAMPLE", "url": "https://example.org/other"},
    ])
    assert len(context.references) == 1
    assert context.cite(["doi", "url", "alternate"]).count('role="doc-biblioref"') == 1
    assert Document(context.bibliography()).references == ["ref-1"]


def test_reference_identity_conflicts_and_unknown_ids_fail():
    with pytest.raises(ValueError):
        html_report.RenderContext([
            {"id": "a", "title": "资料 A", "doi": "10.1234/a"},
            {"id": "b", "title": "资料 B", "url": "https://example.org/b"},
            {"id": "bridge", "title": "矛盾资料", "doi": "10.1234/a", "url": "https://example.org/b"},
        ])
    with pytest.raises(ValueError):
        html_report.RenderContext([{"id": "same", "title": "A"}, {"id": "same", "title": "B"}])
    with pytest.raises(ValueError):
        html_report.render_report({"blocks": [{"type": "paragraph", "text": "结论", "citations": ["absent"]}]})


def test_line_gaps_are_not_interpolated_and_full_data_remains_visible():
    html, _ = html_report.render_report({"blocks": [chart(categories=["A", "B", "C", "D", "E"],
        series=[{"name": "合成值", "values": [1, 2, None, 4, 5]}])]})
    svg = svg_from(html)
    ns = {"s": "http://www.w3.org/2000/svg"}
    paths = svg.findall("s:polyline", ns)
    assert len(paths) == 2
    assert all(len(path.attrib["points"].split()) == 2 for path in paths)
    assert len(svg.findall("s:circle", ns)) == 4
    assert svg.find("s:title", ns) is not None and svg.find("s:desc", ns) is not None
    table = ET.fromstring(re.search(r"<table>.*?</table>", html, re.S).group())
    cells = ["".join(cell.itertext()) for cell in table.findall("tbody/tr/td")]
    assert "缺测" in cells and "5" in cells
    # Caption stays in the keep-together figure; the data table can paginate independently.
    figure = re.search(r'<figure class="chart">.*?</figure>', html, re.S).group()
    assert "<figcaption>" in figure and "<table>" not in figure


def test_bar_baseline_covers_negative_positive_and_zero_values():
    html, _ = html_report.render_report({"blocks": [chart(chart_type="bar", categories=["负值", "零", "正值"],
        series=[{"name": "变化量", "values": [-4, 0, 6]}])]})
    svg = svg_from(html)
    ns = {"s": "http://www.w3.org/2000/svg"}
    baseline = svg.find('s:line[@class="zero-baseline"]', ns)
    zero = float(baseline.attrib["y1"])
    bars = [node for node in svg.findall("s:rect", ns) if node.find("s:title", ns) is not None]
    assert len(bars) == 3
    assert float(bars[0].attrib["y"]) == zero
    assert float(bars[1].attrib["height"]) == 0
    assert float(bars[2].attrib["y"]) < zero


@pytest.mark.parametrize("overrides", [
    {"series": [{"name": "x", "values": [1]}]},
    {"series": [{"name": "x", "values": [1, float("nan")]}]},
    {"series": [{"name": "x", "values": [1, float("inf")]}]},
    {"series": [{"name": "x", "values": [1, 10**1000]}]},
    {"series": [{"name": "x", "values": [True, 2]}]},
    {"series": [{"name": "x", "values": [None, None]}]},
    {"series": [{"name": "x", "values": [1, 2]}, {"name": "x", "values": [3, 4]}]},
    {"categories": ["same", "same"]},
    {"categories": ["one"], "series": [{"name": "x", "values": [1]}]},
    {"chart_type": "pie"}, {"unit": ""}, {"source": ""},
])
def test_invalid_chart_inputs_fail_before_rendering(overrides):
    with pytest.raises(ValueError):
        html_report.render_report({"blocks": [chart(**overrides)]})


def test_markdown_relative_images_citations_and_pipe_paragraph_finish(tmp_path):
    write_png(tmp_path / "figure.png")
    context = html_report.RenderContext([{"id": "r", "title": "演示资料"}], base_dir=tmp_path)
    body, headings = html_report.render_markdown(
        "# 标题\n\n![示意图](figure.png) [@r]\n\n| 单独管线不是表格 |\n\n"
        "正文 **结论 [@r]**。\n\n| 项目 | 值 |\n| --- | --- |\n| A | 1 |\n", context)
    doc = Document(body)
    assert headings[0][1] == "标题"
    assert len(doc.images) == 1 and doc.images[0]["src"].startswith("data:image/png;")
    assert doc.links.count("#ref-1") == 2
    assert "单独管线不是表格" in body
    assert body.count("<table>") == 1


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,evil", "file:///tmp/secret", "//example.org"])
def test_unsafe_links_fail(url):
    with pytest.raises(ValueError):
        html_report.render_inline(f"[link]({url})")


def test_text_is_escaped_and_query_link_is_encoded_once():
    output = html_report.render_inline('<script>alert(1)</script> [link](https://example.org/?a=1&b=2)')
    assert "<script>" not in output and "&lt;script&gt;" in output
    assert 'href="https://example.org/?a=1&amp;b=2"' in output


def test_missing_malformed_and_external_svg_assets_fail(tmp_path):
    (tmp_path / "bad.png").write_bytes(b"not an image")
    (tmp_path / "external.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.org/x.png"/></svg>')
    for name in ("absent.png", "bad.png", "external.svg"):
        with pytest.raises(ValueError):
            html_report.render_report({"blocks": [{"type": "image", "path": name}]}, base_dir=tmp_path)


def test_cli_failure_does_not_leave_a_report_and_versions_preserve_history(tmp_path):
    source, output = tmp_path / "spec.json", tmp_path / "output"
    source.write_text(json.dumps({"title": "test", "blocks": [{"type": "image", "path": "missing.png"}]}))
    command = [sys.executable, str(SCRIPT), "--spec", str(source), "--out-dir", str(output), "--date", "2026-09-08"]
    failed = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert failed.returncode == 1 and "Output:" not in failed.stdout
    assert not list(output.glob("*.html"))
    output.mkdir(exist_ok=True)
    history = output / "test_2026_09_08_v3.html"
    history.write_text("keep historical report")
    source.write_text(json.dumps({"title": "test", "blocks": [{"type": "paragraph", "text": "legacy content"}]}))
    generated = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert generated.returncode == 0
    assert (output / "test_2026_09_08_v4.html").is_file()
    assert history.read_text() == "keep historical report"


def test_invalid_table_and_columns_are_not_silently_dropped():
    for block in [{"type": "table", "headers": ["A", "B"], "rows": [[1]]},
                  {"type": "columns", "ratio": [1, 0], "left": [], "right": []},
                  {"type": "unknown"}]:
        with pytest.raises(ValueError):
            html_report.render_report({"blocks": [block]})


def test_numeric_columns_align_without_changing_precision_or_missing_labels():
    html, _ = html_report.render_report({"blocks": [{"type": "table", "headers": ["随访", "数值", "说明"],
        "rows": [["第1周", "8.20 mmol/L", "升高"], ["第2周", "缺测", "暂不可判断"], ["第3周", "6.50 mmol/L", "降低"]]}]})
    table = ET.fromstring(re.search(r"<table>.*?</table>", html, re.S).group())
    headers = table.findall("thead/tr/th")
    assert [header.get("class") for header in headers] == [None, "num", None]
    rows = table.findall("tbody/tr")
    assert [row.findall("td")[1].text for row in rows] == ["8.20 mmol/L", "缺测", "6.50 mmol/L"]
    assert all(row.findall("td")[1].get("class") == "num" for row in rows)
