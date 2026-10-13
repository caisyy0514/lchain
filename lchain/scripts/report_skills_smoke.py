"""报告类 Skill 端到端冒烟测试：通过 app.agent.report_tools（与 Agent 调用同一条路径）生成三种报告，并检查产物。

python -m scripts.report_skills_smoke

每种报告都包含：标题 / 段落 / 列表 / 表格 / 柱状图 / 折线图（含缺测）/ 图片 / 双栏 / 文献引用。
全部数据为合成演示数据。
"""
from __future__ import annotations

import json
import re
import struct
import sys
import zipfile
import zlib
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(".env")

from app.agent.report_tools import (report_list_outputs, report_run_generator,  # noqa: E402
                                    report_write_file, workspace)

THREAD = "threads/smoke-test"
REFS = [{"id": "demo", "title": "合成演示数据说明（非临床证据）", "authors": ["LChain"], "year": 2026,
         "source": "冒烟测试", "url": "https://example.org/demo"}]
BAR = {"type": "chart", "chart_type": "bar", "categories": ["基线", "随访"],
       "series": [{"name": "演示指标", "values": [8.2, 6.5]}], "unit": "mmol/L", "caption": "图 1 指标比较",
       "source": "合成演示数据", "citations": ["demo"]}
LINE = {"type": "chart", "chart_type": "line", "categories": ["第1周", "第2周", "第3周", "第4周"],
        "series": [{"name": "系列 A", "values": [8.2, 7.6, None, 6.5]}, {"name": "系列 B", "values": [8.1, 7.9, 7.2, 6.8]}],
        "unit": "mmol/L", "caption": "图 2 缺测处断开", "alt": "系列 A 第3周缺测", "source": "合成演示数据"}
TABLE = {"type": "table", "headers": ["项目", "数值", "说明"], "rows": [["基线", "8.20 mmol/L", "合成值"], ["随访", "6.50 mmol/L", "合成值"]],
         "caption": "表 1 原始数值", "source": "合成演示数据", "citations": ["demo"]}


def png(path: Path, w=640, h=360) -> None:
    """生成一张渐变色 PNG（只用标准库）。"""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    row = b"\x00" + b"".join(bytes((40 + x * 180 // w, 110, 200 - x * 120 // w)) for x in range(w))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(row * h)) + chunk(b"IEND", b""))


def image_block(p: str) -> dict:
    return {"type": "image", "path": p, "alt": "合成渐变示意图", "caption": "图 3 示意图（合成）", "source": "冒烟测试自动生成"}


def main() -> int:
    ws = workspace()
    img = ws / THREAD / "demo.png"
    img.parent.mkdir(parents=True, exist_ok=True)
    png(img)
    img_repo = f"/repo/{THREAD}/demo.png"
    blocks = [
        {"type": "heading", "text": "01 概述"},
        {"type": "paragraph", "text": "本报告用于验证报告类 Skill 在 LChain 项目中可正常生成。", "citations": ["demo"]},
        {"type": "bullets", "items": ["表格与图表", "图片嵌入", "文献引用"]},
        {"type": "heading", "text": "02 图表"},
        {"type": "columns", "ratio": [1, 1], "left": [BAR], "right": [TABLE]},
        LINE,
        {"type": "heading", "text": "03 图片"},
        image_block(img_repo),
    ]
    specs = {
        "html-report": {"title": "报告Skill冒烟测试", "author": "LChain", "references": REFS, "blocks": blocks},
        "word-report": {"title": "报告Skill冒烟测试", "author": "LChain", "references": REFS, "blocks": blocks},
        "ppt-report": {"title": "报告Skill冒烟测试", "author": "LChain", "references": REFS, "slides": [
            {"type": "title", "title": "报告 Skill 冒烟测试", "subtitle": "合成演示数据"},
            {"type": "bullets", "title": "概述", "bullets": [{"text": "验证三种报告 Skill 可在本项目生成", "citations": ["demo"]},
                                                           "表格、图表、图片、文献引用"]},
            {"type": "columns", "title": "图表与原始数值", "left": [BAR], "right": [TABLE]},
            dict(LINE, title="趋势（含缺测）"),
            {"type": "image", "title": "图片嵌入", "path": img_repo, "alt": "合成渐变示意图", "caption": "图 3 示意图（合成）",
             "source": "冒烟测试自动生成"},
        ]},
    }
    ok = True
    for skill, spec in specs.items():
        sp = f"{THREAD}/{skill}.json"
        print(report_write_file.invoke({"path": sp, "content": json.dumps(spec, ensure_ascii=False)}))
        out = report_run_generator.invoke({"skill": skill, "spec_path": sp})
        print(f"--- {skill}\n{out}\n")
        m = re.search(r"Output:\s*(\S+)", out)
        if not out.startswith("成功") or not m:
            ok = False
            continue
        f = ws / m.group(1).replace("/repo/", "")
        ok &= check(skill, f)
    print(report_list_outputs.invoke({}))
    print("\n全部通过 ✅" if ok else "\n存在失败 ❌")
    return 0 if ok else 1


def check(skill: str, f: Path) -> bool:
    if skill == "html-report":
        s = f.read_text(encoding="utf-8")
        res = {"内联图片": "data:image/png;base64" in s, "SVG 图表": s.count("<svg") >= 2, "表格": "<table" in s,
               "文献": "合成演示数据说明" in s, "无外链脚本": "<script src" not in s}
    else:
        z = zipfile.ZipFile(f)
        names = z.namelist()
        media = [n for n in names if "/media/" in n]
        if skill == "word-report":
            doc = z.read("word/document.xml").decode()
            res = {"图片": bool(media), "原生图表": any(n.startswith("word/charts/chart") for n in names),
                   "内嵌工作簿": any(n.endswith(".xlsx") for n in names), "表格": "<w:tbl>" in doc, "文献": "合成演示数据说明" in doc}
        else:
            slides = [n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)]
            allx = "".join(z.read(n).decode() for n in slides)
            res = {"页数≥6": len(slides) >= 6, "图片": bool(media), "表格": "<a:tbl>" in allx, "文献": "合成演示数据说明" in allx}
    bad = [k for k, v in res.items() if not v]
    print(f"    检查 {skill}：{'通过' if not bad else '失败 ' + str(bad)}  {res}")
    return not bad


if __name__ == "__main__":
    sys.exit(main())
