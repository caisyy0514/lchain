"""报告自定义样式（app/agent/report_styles.py + 三个生成脚本的 _style 支持）测试。

PYTHONPATH=. .venv/bin/python -m pytest tests/agent/test_report_styles.py -q
"""
from __future__ import annotations

import json
import re
import zipfile

import pytest

from app.agent import report_styles as rs


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    from app.core import config

    config.get_settings.cache_clear()
    monkeypatch.setenv("REPORT_WORKSPACE", str(tmp_path / "ws"))
    yield tmp_path / "ws"
    config.get_settings.cache_clear()


# ------------------------------------------------------------------ 解析与校验
def test_all_presets_resolve_and_are_readable():
    for name in rs.PRESETS:
        for skill in rs.SKILLS:
            if name == "dark" and skill == "word-report":
                with pytest.raises(ValueError, match="深色"):
                    rs.resolve_style(name, skill)
                continue
            style, _ = rs.resolve_style(name, skill)
            c = style["colors"]
            assert rs.contrast(c["text"], c["background"]) >= 4.5
            assert rs.contrast(c["on_primary"], c["primary"]) >= 4.5
            assert len(c["chart"]) == 6


def test_override_merges_and_links_colors():
    style, _ = rs.resolve_style({"preset": "fresh", "colors": {"primary": "#6A1B9A", "accent": "00897b"},
                                 "fonts": {"cn": "宋体"}, "table": "grid", "brand": {"label": "XX 医院"}}, "ppt-report")
    c = style["colors"]
    assert c["primary"] == "6A1B9A" and c["accent"] == "00897B"
    assert c["chart"][:2] == ["00897B", "6A1B9A"]  # 只改主色/强调色时图表色联动
    assert c["heading"] != rs.PRESETS["fresh"]["colors"]["heading"]
    assert style["fonts"]["cn"] == "宋体" and style["fonts"]["heading_cn"] == "宋体"
    assert style["table"] == "grid" and style["brand"]["label"] == "XX 医院"


@pytest.mark.parametrize("bad,msg", [
    ({"colors": {"text": "#EEEEEE"}}, "对比度"),
    ({"colors": {"primary": "red"}}, "十六进制"),
    ({"table": "fancy"}, "style.table"),
    ({"fonts": {"cn": "x\"/><evil"}}, "字体名"),
    ({"font_scale": 3}, "0.85"),
    ({"unknown": 1}, "不支持的字段"),
    ("no-such-style", "未知样式"),
])
def test_invalid_styles_rejected(bad, msg):
    with pytest.raises(ValueError, match=msg):
        rs.resolve_style(bad, "html-report")


def test_ppt_ignores_font_scale():
    style, notes = rs.resolve_style({"preset": "academic", "font_scale": 1.2}, "ppt-report")
    assert style["font_scale"] == 1.0 and any("font_scale" in n for n in notes)


def test_save_and_reuse_named_style():
    rs.save_style("我的医院", {"preset": "hospital", "colors": {"primary": "#123456"}})
    style, _ = rs.resolve_style("我的医院", "word-report")
    assert style["colors"]["primary"] == "123456"
    assert "我的医院" in rs.list_styles_text()
    with pytest.raises(ValueError, match="内置预设"):
        rs.save_style("business", "academic")


def test_preview_tool(tmp_path):
    out = rs.report_preview_style.func(style={"preset": "warm", "cover": "band"}, runtime=None)
    assert out.startswith("✅") and "warm" in out
    assert "❌" in rs.report_preview_style.func(style={"colors": {"text": "#FFFFFF"}}, runtime=None)


# ------------------------------------------------------------------ 生成结果
def _spec(skill: str, style) -> dict:
    table = {"type": "table", "headers": ["指标", "数值"], "rows": [["A", "1"], ["B", "2"]], "caption": "表 1", "source": "合成"}
    chart = {"type": "chart", "chart_type": "bar", "categories": ["一", "二"], "series": [{"name": "s", "values": [1, 2]}],
             "unit": "个", "source": "合成"}
    if skill == "ppt-report":
        return {"title": "样式测试", "style": style, "slides": [
            {"type": "title", "title": "样式测试", "subtitle": "副标题"},
            {"type": "table", "title": "表格页", **{k: v for k, v in table.items() if k != "type"}},
            {"type": "chart", "title": "图表页", **{k: v for k, v in chart.items() if k != "type"}}]}
    return {"title": "样式测试", "subtitle": "副标题", "style": style,
            "blocks": [{"type": "heading", "text": "一、章节", "level": 1}, {"type": "paragraph", "text": "正文"}, table, chart]}


def _generate(workspace, skill: str, style):
    from app.agent.report_tools import run_generator

    d = workspace / "threads" / "t"
    d.mkdir(parents=True, exist_ok=True)
    spec = _spec(skill, style)
    if style is None:
        spec.pop("style")  # 不写 style：输出与 medex 原版一致
    (d / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    code, out = run_generator(skill, "threads/t/spec.json")
    if code != 0 and skill == "ppt-report" and ("validator" in out.lower() or "校验器" in out):
        pytest.skip("本机缺少 PPT 校验器")
    assert code == 0, out
    path = re.search(r"^Output:\s*/repo/(\S.*)$", out, re.M).group(1)
    return workspace / path, out


def test_word_applies_style(workspace):
    f, out = _generate(workspace, "word-report", {"preset": "hospital", "fonts": {"cn": "宋体", "heading_cn": "黑体"},
                                                  "font_scale": 1.1, "brand": {"label": "XX 医院", "footer": "医务部出品"}})
    assert "样式：" in out
    with zipfile.ZipFile(f) as z:
        styles, doc, chart = (z.read(n).decode() for n in ("word/styles.xml", "word/document.xml", "word/charts/chart1.xml"))
    assert 'w:eastAsia="宋体"' in styles and 'w:eastAsia="黑体"' in styles
    assert "9E1B32" in styles  # 标题主色
    assert 'w:val="24"' in styles  # 正文 22 半磅 × 1.1
    assert 'w:fill="9E1B32"' in doc  # 表头填充 + 色带封面
    assert "C8102E" in chart
    assert "XX 医院" in doc and 'r:id="rIdFooter1"' in doc
    with zipfile.ZipFile(f) as z:
        assert "医务部出品" in z.read("word/footer1.xml").decode()
        assert "footer1.xml" in z.read("[Content_Types].xml").decode()
        assert "rIdFooter1" in z.read("word/_rels/document.xml.rels").decode()


def test_word_default_unchanged(workspace):
    f, out = _generate(workspace, "word-report", None)
    with zipfile.ZipFile(f) as z:
        styles = z.read("word/styles.xml").decode()
    assert 'w:eastAsia="等线"' in styles and "19364D" in styles


def test_html_applies_style(workspace):
    f, _ = _generate(workspace, "html-report", {"preset": "tech", "brand": {"label": "XX 科技", "footer": "内部资料"}})
    html = f.read_text(encoding="utf-8")
    assert "--primary:#0f2447" in html and "XX 科技" in html and "内部资料" in html
    assert "MEDEX" not in html and "Medex 生成" not in html


def test_ppt_applies_style(workspace):
    f, _ = _generate(workspace, "ppt-report", {"preset": "dark", "fonts": {"cn": "黑体"}, "brand": {"label": "品牌页脚"}})
    with zipfile.ZipFile(f) as z:
        theme = z.read("ppt/theme/theme1.xml").decode()
        s1, s2 = z.read("ppt/slides/slide1.xml").decode(), z.read("ppt/slides/slide2.xml").decode()
    assert '<a:ea typeface="黑体"/>' in theme
    assert "0F1C2A" in s1 and "<p:bgPr>" in s1  # 深色背景
    assert "品牌页脚" in s2
