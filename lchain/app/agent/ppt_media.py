"""PPT 图文素材与成品验证工具。

- ppt_search_images   联网搜索图片（ddgs 图片搜索），返回候选图的地址、尺寸、来源页
- ppt_download_image  下载候选图到本会话目录：SSRF 检查、大小/尺寸校验、统一转成 PNG/JPEG、记录来源
- ppt_make_diagram    用 Pillow 绘制与 PPT 同色系的示意图（流程 / 时间线 / 循环 / 对比 / 层级 / 要点卡片），
                      没有合适的真实图片时，保证「图文混排」也有可靠的视觉内容
- ppt_verify          成品验证：解析 PPTX 结构（每页标题、图片、文字），用 LibreOffice 渲染成 PDF → 逐页 PNG，
                      检查中文字体回退、页数、空白页、文字越界、与已确认大纲的一致性、图文密度；
                      可选再把缩略图交给多模态模型做视觉评审。生成预览图（contact sheet）供用户查看

渲染时用自动生成的 fontconfig 配置把「微软雅黑」映射到本机中文字体，否则 LibreOffice 会把中文渲染成方框，
视觉验收就没有意义。
"""
from __future__ import annotations

import asyncio
import base64
import glob
import io
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import httpx
from langchain.tools import ToolRuntime, tool

from app.agent import ppt_workflow as wf
from app.core.config import get_settings

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MIN_IMAGE_SIDE = 300
MAX_IMAGE_SIDE = 2400
PRIMARY, ACCENT, INK, MUTED, LIGHT, BORDER = "#19364D", "#007F7A", "#1F2A33", "#5B6770", "#EEF4F6", "#D5DEE3"
SERIES = [ACCENT, "#3869A8", "#B87924", "#8461A0", "#19364D", "#4E8F5A"]
NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}


# ------------------------------------------------------------------ 字体
_FONT_CANDIDATES = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
    "C:/Windows/Fonts/msyh.ttc",
]


def cjk_font_path() -> str | None:
    s = get_settings()
    if s.ppt_cjk_font and Path(s.ppt_cjk_font).is_file():
        return s.ppt_cjk_font
    return next((p for p in _FONT_CANDIDATES if Path(p).is_file()), None)


_BOLD_STYLES = ("bold", "w6", "w7", "w8", "medium", "semibold", "heavy", "black")
_face_cache: dict[tuple[str, bool], int] = {}


def _face_index(path: str, bold: bool) -> int:
    """在 .ttc 字体集合里找常规 / 粗体对应的 face（如冬青黑体 0=W3、2=W6），找不到就用 0。"""
    from PIL import ImageFont

    key = (path, bold)
    if key not in _face_cache:
        found = 0
        for i in range(12):
            try:
                family, style = ImageFont.truetype(path, 12, index=i).getname()
            except OSError:
                break
            if family.startswith("."):
                continue  # 系统界面专用字体
            if any(b in (style or "").lower() for b in _BOLD_STYLES) == bold:
                found = i
                break
        _face_cache[key] = found
    return _face_cache[key]


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    path = cjk_font_path()
    if not path:
        return ImageFont.load_default(size=size)
    try:
        return ImageFont.truetype(path, size=size, index=_face_index(path, bold))
    except OSError:
        return ImageFont.truetype(path, size=size)


# ------------------------------------------------------------------ 联网图片
def _search_images_sync(query: str, n: int) -> list[dict[str, Any]]:
    from ddgs import DDGS

    s = get_settings()
    backends = [b.strip() for b in s.search_ddgs_backend.split(",") if b.strip()] or ["bing"]
    backends = [b for b in backends if b in ("bing", "yahoo", "duckduckgo")] or ["bing"]
    last: Exception | None = None
    for be in backends:
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            fut = pool.submit(lambda b=be: list(DDGS(timeout=10).images(query, max_results=n * 2, region=s.search_region,
                                                                        backend=b)))
            rows = fut.result(timeout=25)
        except (Exception, FutureTimeout) as e:  # noqa: BLE001
            last = e
            logger.info("图片搜索 %s 失败：%s", be, e)
            continue
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        out = []
        for r in rows:
            try:
                w, h = int(r.get("width") or 0), int(r.get("height") or 0)
            except ValueError:
                w = h = 0
            if w and h and min(w, h) < MIN_IMAGE_SIDE:
                continue
            out.append({"title": (r.get("title") or "")[:80], "image_url": r.get("image"),
                        "source_page": r.get("url"), "width": w, "height": h, "site": r.get("source") or ""})
        if out:
            return out[:n]
    if last:
        raise last
    return []


def _safe_name(name: str) -> str:
    n = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", name or "").strip("_")[:40]
    return n or f"img_{int(time.time())}"


def _normalize_image(data: bytes) -> tuple[bytes, str, int, int]:
    """校验并转换为 PPT 生成器支持的 PNG/JPEG；应用 EXIF 方向，过大的缩小。返回 (数据, 扩展名, 宽, 高)。"""
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"不是可识别的图片：{e}") from e
    img = ImageOps.exif_transpose(img)
    if min(img.size) < MIN_IMAGE_SIDE:
        raise ValueError(f"图片太小（{img.size[0]}×{img.size[1]}），投影后不清晰，请换一张（短边至少 {MIN_IMAGE_SIDE}px）")
    if max(img.size) > MAX_IMAGE_SIDE:
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    has_alpha = img.mode in ("RGBA", "LA", "P") and ("transparency" in img.info or img.mode != "P")
    buf = io.BytesIO()
    if has_alpha:
        img.convert("RGBA").save(buf, "PNG", optimize=True)
        ext = "png"
    else:
        img.convert("RGB").save(buf, "JPEG", quality=90)
        ext = "jpg"
    return buf.getvalue(), ext, img.size[0], img.size[1]


def _download(url: str) -> bytes:
    from app.agent.search import _is_public_url

    if not _is_public_url(url):
        raise ValueError(f"拒绝访问：{url} 不是公网 http(s) 地址")
    with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": UA, "Referer": url}) as c:
        with c.stream("GET", url) as r:
            if not _is_public_url(str(r.url)):
                raise ValueError("拒绝访问：重定向到了内网地址")
            r.raise_for_status()
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_IMAGE_BYTES:
                    raise ValueError("图片超过 15MB")
    return bytes(buf)


def _register_asset(session_id: str, entry: dict[str, Any]) -> None:
    f = wf.session_dir(session_id) / "assets.json"
    items = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else []
    items = [x for x in items if x.get("path") != entry["path"]] + [entry]
    f.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ 示意图
def _wrap(draw, text: str, font, width: int) -> list[str]:
    lines: list[str] = []
    for para in str(text).split("\n"):
        cur = ""
        for ch in para:
            if draw.textlength(cur + ch, font=font) <= width:
                cur += ch
            else:
                # 英文单词尽量不断开
                m = re.search(r"[A-Za-z0-9]+$", cur)
                if m and ch.isalnum() and m.start() > 0:
                    lines.append(cur[:m.start()])
                    cur = cur[m.start():] + ch
                else:
                    lines.append(cur)
                    cur = ch
        lines.append(cur)
    return [line for line in lines if line.strip()] or [""]


def _text_block(draw, xy, text, font, width, fill, line_gap=1.3, align="center") -> int:
    """在 (x, y) 起画自动换行文字，返回占用高度。"""
    x, y = xy
    lines = _wrap(draw, text, font, width)
    lh = int(font.size * line_gap)
    for i, line in enumerate(lines):
        tw = draw.textlength(line, font=font)
        tx = x + (width - tw) / 2 if align == "center" else x
        draw.text((tx, y + i * lh), line, font=font, fill=fill)
    return lh * len(lines)


def _measure(draw, text, font, width, line_gap=1.3) -> int:
    return int(font.size * line_gap) * len(_wrap(draw, text, font, width)) if text else 0


def _arrow(draw, p1, p2, color, width=6, head=18):
    draw.line([p1, p2], fill=color, width=width)
    ang = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
    pts = [p2, (p2[0] - head * math.cos(ang - 0.45), p2[1] - head * math.sin(ang - 0.45)),
           (p2[0] - head * math.cos(ang + 0.45), p2[1] - head * math.sin(ang + 0.45))]
    draw.polygon(pts, fill=color)


def _card(draw, box, text, detail, fill, fg, *, title_font, body_font, radius=22, outline=None):
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=3 if outline else 0)
    inner = x2 - x1 - 40
    th = _measure(draw, text, title_font, inner)
    dh = _measure(draw, detail, body_font, inner) if detail else 0
    total = th + (16 + dh if detail else 0)
    y = y1 + max(20, (y2 - y1 - total) // 2)
    _text_block(draw, (x1 + 20, y), text, title_font, inner, fg)
    if detail:
        _text_block(draw, (x1 + 20, y + th + 16), detail, body_font, inner, fg if fill != "white" else MUTED)


def draw_diagram(kind: str, items: list[dict[str, str]], title: str = "", width: int = 1600, height: int = 900):
    """返回 PIL Image。items：[{"label": "...", "detail": "..."}]。"""
    from PIL import Image, ImageDraw

    n = len(items)
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    top = 40
    if title:
        f = _font(46, bold=True)
        top += _text_block(d, (60, 40), title, f, width - 120, PRIMARY) + 30
    tf, bf = _font(38 if n <= 4 else 32, bold=True), _font(28 if n <= 4 else 25)
    area = (60, top, width - 60, height - 50)
    aw, ah = area[2] - area[0], area[3] - area[1]

    if kind == "flow":
        per_row = (n if n <= 5 else math.ceil(n / 2)) if width >= 1400 else 1  # 半栏画布改为竖排
        rows = math.ceil(n / per_row)
        gap = 70
        bw = (aw - gap * (per_row - 1)) // per_row
        need = max(_measure(d, f"{i + 1}. {it['label']}", tf, bw - 40)
                   + (16 + _measure(d, it.get("detail", ""), bf, bw - 40) if it.get("detail") else 0)
                   for i, it in enumerate(items)) + 80
        bh = min(max(need, 200 if per_row > 1 else 110), (ah - 60 * (rows - 1)) // rows)
        for i, it in enumerate(items):
            r, c = divmod(i, per_row)
            x = area[0] + c * (bw + gap)
            y = area[1] + r * (bh + 60) + (ah - rows * bh - (rows - 1) * 60) // 2
            color = PRIMARY if i % 2 == 0 else ACCENT
            _card(d, (x, y, x + bw, y + bh), f"{i + 1}. {it['label']}", it.get("detail", ""), color, "white",
                  title_font=tf, body_font=bf)
            if i < n - 1:
                if per_row == 1:
                    _arrow(d, (x + bw // 2, y + bh + 6), (x + bw // 2, y + bh + 54), MUTED)
                elif c < per_row - 1:
                    _arrow(d, (x + bw + 8, y + bh // 2), (x + bw + gap - 8, y + bh // 2), MUTED)
                else:
                    nx = area[0] + (bw // 2)
                    _arrow(d, (x + bw // 2, y + bh + 6), (x + bw // 2, y + bh + 30), MUTED)
                    d.line([(x + bw // 2, y + bh + 30), (nx, y + bh + 30)], fill=MUTED, width=6)
                    _arrow(d, (nx, y + bh + 30), (nx, y + bh + 54), MUTED)
    elif kind == "timeline":
        y = area[1] + ah // 2
        d.line([(area[0], y), (area[2], y)], fill=BORDER, width=8)
        step = aw / n
        for i, it in enumerate(items):
            cx = int(area[0] + step * (i + 0.5))
            color = SERIES[i % len(SERIES)]
            d.ellipse((cx - 20, y - 20, cx + 20, y + 20), fill=color, outline="white", width=5)
            w = int(step - 24)
            lf = _font(34 if n <= 5 else 28, bold=True)
            hlabel = _measure(d, it["label"], lf, w)
            # 节点名统一在线上方、说明统一在线下方，便于横向对照阅读
            _text_block(d, (cx - w // 2, y - 50 - hlabel), it["label"], lf, w, color)
            if it.get("detail"):
                d.line([(cx, y + 24), (cx, y + 44)], fill=BORDER, width=3)
                _text_block(d, (cx - w // 2, y + 56), it["detail"], bf, w, INK)
    elif kind == "cycle":
        cx, cy = area[0] + aw // 2, area[1] + ah // 2
        rad = min(aw * 0.36, ah * 0.36)
        bw, bh = int(min(380, aw / max(3, n) * 1.25)), int(min(200, ah * 0.3))
        d.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), outline=BORDER, width=8)
        for i, it in enumerate(items):
            ang = -math.pi / 2 + 2 * math.pi * i / n
            x, y = cx + rad * math.cos(ang), cy + rad * math.sin(ang) * 0.95
            color = SERIES[i % len(SERIES)]
            _card(d, (int(x - bw / 2), int(y - bh / 2), int(x + bw / 2), int(y + bh / 2)), it["label"],
                  it.get("detail", ""), color, "white", title_font=tf, body_font=bf)
            a2 = ang + math.pi / n
            ax, ay = cx + rad * math.cos(a2), cy + rad * math.sin(a2) * 0.95
            _arrow(d, (ax - 14 * math.sin(a2) * -1, ay - 14 * math.cos(a2)), (ax, ay), MUTED, width=4, head=22)
    elif kind == "compare":
        gap = 40
        bw = (aw - gap * (n - 1)) // n
        bf = _font(32 if n <= 3 else 28)

        def lines_of(it):
            return [x.strip() for x in re.split(r"[\n；;]", it.get("detail", "")) if x.strip()]
        need = max(sum(_measure(d, ln, bf, bw - 90) + 18 for ln in lines_of(it)) for it in items) + 190
        bottom = min(area[3], area[1] + max(need, 320))
        for i, it in enumerate(items):
            x = area[0] + i * (bw + gap)
            color = SERIES[i % len(SERIES)]
            d.rounded_rectangle((x, area[1], x + bw, bottom), radius=24, fill=LIGHT)
            d.rounded_rectangle((x, area[1], x + bw, area[1] + 120), radius=24, fill=color)
            d.rectangle((x, area[1] + 90, x + bw, area[1] + 120), fill=color)
            _text_block(d, (x + 20, area[1] + 30), it["label"], _font(40, bold=True), bw - 40, "white")
            y = area[1] + 150
            for line in lines_of(it):
                d.ellipse((x + 28, y + 14, x + 42, y + 28), fill=color)
                y += _text_block(d, (x + 60, y), line, bf, bw - 90, INK, align="left") + 18
    elif kind == "hierarchy":
        root, kids = items[0], items[1:] or [{"label": ""}]
        rw, rh = min(700, aw // 2), 150
        rx = area[0] + (aw - rw) // 2
        _card(d, (rx, area[1], rx + rw, area[1] + rh), root["label"], root.get("detail", ""), PRIMARY, "white",
              title_font=_font(42, bold=True), body_font=bf)
        k = len(kids)
        gap = 36
        kw = (aw - gap * (k - 1)) // k
        ky = area[1] + rh + 110
        bus = area[1] + rh + 55
        d.line([(rx + rw // 2, area[1] + rh), (rx + rw // 2, bus)], fill=MUTED, width=5)
        centers = [area[0] + i * (kw + gap) + kw // 2 for i in range(k)]
        kneed = max(_measure(d, it["label"], tf, kw - 40)
                    + (16 + _measure(d, it.get("detail", ""), bf, kw - 40) if it.get("detail") else 0)
                    for it in kids) + 90
        kbottom = min(area[3], ky + max(kneed, 180))
        d.line([(centers[0], bus), (centers[-1], bus)], fill=MUTED, width=5)
        for i, it in enumerate(kids):
            x = area[0] + i * (kw + gap)
            _arrow(d, (centers[i], bus), (centers[i], ky - 4), MUTED, width=5, head=16)
            _card(d, (x, ky, x + kw, kbottom), it["label"], it.get("detail", ""), LIGHT, INK,
                  title_font=tf, body_font=bf, outline=SERIES[i % len(SERIES)])
    else:  # cards：要点卡片网格
        cols = (n if n <= 3 else (2 if n == 4 else 3)) if width >= 1400 else (1 if n <= 3 else 2)
        rows = math.ceil(n / cols)
        gap = 36
        bw = (aw - gap * (cols - 1)) // cols
        need = max(104 + _measure(d, it["label"], _font(36, bold=True), bw - 70) + 12
                   + _measure(d, it.get("detail", ""), bf, bw - 70) for it in items) + 40
        bh = min(max(need, 220), (ah - gap * (rows - 1)) // rows)
        oy = area[1] + (ah - rows * bh - (rows - 1) * gap) // 2
        for i, it in enumerate(items):
            r, c = divmod(i, cols)
            x, y = area[0] + c * (bw + gap), oy + r * (bh + gap)
            color = SERIES[i % len(SERIES)]
            d.rounded_rectangle((x, y, x + bw, y + bh), radius=22, fill=LIGHT)
            d.rectangle((x, y + 22, x + 12, y + bh - 22), fill=color)
            num_f = _font(56, bold=True)
            d.text((x + 40, y + 28), f"{i + 1:02d}", font=num_f, fill=color)
            ty = y + 28 + 76
            ty += _text_block(d, (x + 40, ty), it["label"], _font(36, bold=True), bw - 70, INK, align="left") + 12
            if it.get("detail"):
                _text_block(d, (x + 40, ty), it["detail"], bf, bw - 70, MUTED, align="left")
    return _trim_vertical(img, min_height=0)


def _trim_vertical(img, margin: int = 40, min_height: int = 560):
    """裁掉四周多余空白，宽高比控制在 1:2.6 以内（生成器会拒绝宽高比 >3 且缩放后过小的长图）。"""
    from PIL import ImageChops, Image

    bg = Image.new(img.mode, img.size, "white")
    box = ImageChops.difference(img, bg).getbbox()
    if not box:
        return img
    left, right = max(0, box[0] - margin), min(img.width, box[2] + margin)
    top, bottom = max(0, box[1] - margin), min(img.height, box[3] + margin)
    min_h = max(min(min_height, img.height), int((right - left) / 2.6))
    if bottom - top < min_h:
        pad = (min_h - (bottom - top)) // 2
        top, bottom = max(0, top - pad), min(img.height, bottom + pad)
    return img.crop((left, top, right, bottom))


DIAGRAM_KINDS = {"flow": "流程", "timeline": "时间线", "cycle": "循环", "compare": "对比", "hierarchy": "层级",
                 "cards": "要点卡片"}


# ------------------------------------------------------------------ 渲染
def find_soffice() -> str | None:
    s = get_settings()
    cands = [s.ppt_soffice_path] if s.ppt_soffice_path else []
    cands += [shutil.which("soffice") or "", shutil.which("libreoffice") or "",
              "/Applications/LibreOffice.app/Contents/MacOS/soffice", "/usr/bin/soffice", "/opt/libreoffice/program/soffice"]
    cands += sorted(glob.glob(os.path.expanduser("~/.cache/codex-runtimes/*/dependencies/bin/override/soffice")))
    return next((c for c in cands if c and Path(c).is_file() and os.access(c, os.X_OK)), None)


def _fontconfig_file(render_root: Path) -> Path | None:
    """生成 fontconfig 配置：把 PPT 里的「微软雅黑 / Calibri」映射到本机可用的中文字体。"""
    font = cjk_font_path()
    if not font:
        return None
    try:
        family = _font(20).getname()[0]
    except Exception:  # noqa: BLE001
        family = "Hiragino Sans GB"
    dirs = {str(Path(font).parent), "/System/Library/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts",
            "/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/Library/Fonts")}
    alias = "".join(
        f"<alias binding=\"same\"><family>{name}</family><prefer><family>{family}</family></prefer></alias>"
        for name in ("微软雅黑", "Microsoft YaHei", "SimHei", "SimSun", "宋体", "黑体", "sans-serif"))
    conf = (
        '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>'
        '<include ignore_missing="yes">/etc/fonts/fonts.conf</include>'
        + "".join(f"<dir>{d}</dir>" for d in sorted(dirs) if Path(d).is_dir())
        + f"<cachedir>{render_root / 'fccache'}</cachedir>" + alias
        + f"<alias><family>Calibri</family><prefer><family>Carlito</family><family>{family}</family></prefer></alias>"
        "</fontconfig>")
    f = render_root / "fonts.conf"
    if not f.is_file() or f.read_text(encoding="utf-8") != conf:
        f.write_text(conf, encoding="utf-8")
    return f


def render_pdf(pptx: Path, out_dir: Path, timeout: int = 180) -> Path:
    soffice = find_soffice()
    if not soffice:
        raise FileNotFoundError("未找到 LibreOffice（soffice），无法渲染验证；可设置 PPT_SOFFICE_PATH")
    from app.agent.report_tools import workspace

    render_root = workspace() / ".render"
    render_root.mkdir(exist_ok=True)
    env = {**os.environ}
    fc = _fontconfig_file(render_root)
    if fc:
        env["FONTCONFIG_FILE"] = str(fc)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lo-", dir=render_root) as profile:
        # 独立的用户配置目录：并发渲染互不干扰，也不会被已打开的 LibreOffice 实例接管
        r = subprocess.run([soffice, "--headless", "--norestore", f"-env:UserInstallation=file://{profile}",
                            "--convert-to", "pdf", "--outdir", str(out_dir), str(pptx)],
                           capture_output=True, text=True, timeout=timeout, env=env)
    pdf = out_dir / (pptx.stem + ".pdf")
    if not pdf.is_file():
        raise RuntimeError(f"LibreOffice 渲染失败：{(r.stderr or r.stdout)[-500:]}")
    return pdf


# ------------------------------------------------------------------ PPTX 结构解析
def inspect_pptx(path: Path) -> list[dict[str, Any]]:
    """逐页返回 {index, type, title, texts, pictures, chars}。"""
    slides = []
    with zipfile.ZipFile(path) as z:
        names = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                       key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
        for i, n in enumerate(names, 1):
            root = ET.fromstring(z.read(n))
            csld = root.find("p:cSld", NS)
            stype = (csld.get("name") or "").split(":", 1)[-1] if csld is not None else ""
            title, texts, pics, pic_areas = "", [], 0, []
            for sp in root.iter():
                tag = sp.tag.rsplit("}", 1)[-1]
                if tag == "pic":
                    pics += 1
                    ext = sp.find(".//a:xfrm/a:ext", NS)
                    if ext is not None:
                        pic_areas.append(int(ext.get("cx", 0)) * int(ext.get("cy", 0)) / (12192000 * 6858000))
                elif tag == "sp":
                    nv = sp.find("p:nvSpPr/p:cNvPr", NS)
                    name = nv.get("name") if nv is not None else ""
                    t = "".join(x.text or "" for x in sp.iter(f"{{{NS['a']}}}t")).strip()
                    if not t:
                        continue
                    if name in ("Title", "Section") and not title:
                        title = t
                    elif name not in ("FooterL", "FooterR", "TitleFoot"):
                        texts.append(t)
            tables = len(root.findall(".//a:tbl", NS))
            charts = sum(1 for sp in root.iter(f"{{{NS['p']}}}cNvPr") if (sp.get("name") or "").startswith("ChartUnit"))
            slides.append({"index": i, "type": stype, "title": re.sub("[\ue000\ue001]", "", title), "texts": texts,
                           "pictures": pics, "pic_areas": pic_areas, "tables": tables, "charts": charts,
                           "chars": sum(len(t) for t in texts)})
    return slides


# ------------------------------------------------------------------ 渲染结果检查
_CJK_FONT_HINTS = ("hiragino", "pingfang", "heiti", "songti", "noto", "cjk", "wqy", "yahei", "simhei", "simsun",
                   "sourcehan", "source han", "droid", "arialunicode", "fangsong", "kaiti", "stsong")


def check_pdf(pdf: Path, preview_dir: Path, slides: list[dict[str, Any]]) -> tuple[list[str], list[str], list[Path]]:
    """返回 (问题, 提示, 每页 PNG)。"""
    import pymupdf

    issues, notes, pngs = [], [], []
    preview_dir.mkdir(parents=True, exist_ok=True)
    for old in preview_dir.glob("slide-*.png"):
        old.unlink()
    with pymupdf.open(pdf) as doc:
        if doc.page_count != len(slides):
            issues.append(f"渲染页数 {doc.page_count} 与 PPTX 页数 {len(slides)} 不一致")
        fonts = {f[3].split("+", 1)[-1].lower() for p in doc for f in p.get_fonts()}
        has_cjk_text = any(re.search(r"[\u4e00-\u9fff]", s["title"] + "".join(s["texts"])) for s in slides)
        if has_cjk_text and not any(h in f.replace(" ", "") for f in fonts for h in _CJK_FONT_HINTS):
            issues.append("渲染时没有找到中文字体（中文可能显示为方框），视觉验收无效：请安装中文字体或设置 PPT_CJK_FONT")
        for i, page in enumerate(doc, 1):
            pix = page.get_pixmap(dpi=72)
            f = preview_dir / f"slide-{i:02d}.png"
            pix.save(str(f))
            pngs.append(f)
            rect = page.rect
            over = []
            for b in page.get_text("dict").get("blocks", []):
                if b.get("type") != 0:
                    continue
                x0, y0, x1, y1 = b["bbox"]
                if x1 > rect.x1 + 2 or y1 > rect.y1 + 2 or x0 < rect.x0 - 2 or y0 < rect.y0 - 2:
                    txt = "".join(s["text"] for ln in b["lines"] for s in ln["spans"])[:20]
                    over.append(txt)
            if over:
                issues.append(f"第 {i} 页文字超出页面：{'、'.join(over[:3])}")
            # 空白页：几乎全白
            from PIL import Image, ImageStat

            im = Image.open(f).convert("L")
            if ImageStat.Stat(im).stddev[0] < 3:
                issues.append(f"第 {i} 页渲染后几乎空白")
    return issues, notes, pngs


def contact_sheet(pngs: list[Path], out: Path, cols: int = 3) -> Path:
    from PIL import Image, ImageDraw

    if not pngs:
        raise ValueError("没有可拼接的页面")
    thumbs = [Image.open(p).convert("RGB") for p in pngs]
    tw = 480
    th = int(thumbs[0].height * tw / thumbs[0].width)
    rows = math.ceil(len(thumbs) / cols)
    pad, label = 16, 30
    sheet = Image.new("RGB", (cols * (tw + pad) + pad, rows * (th + pad + label) + pad), "#E9EDF0")
    d = ImageDraw.Draw(sheet)
    f = _font(20, bold=True)
    for i, im in enumerate(thumbs):
        r, c = divmod(i, cols)
        x, y = pad + c * (tw + pad), pad + r * (th + pad + label)
        d.text((x, y), f"第 {i + 1} 页", font=f, fill=PRIMARY)
        sheet.paste(im.resize((tw, th)), (x, y + label))
        d.rectangle((x - 1, y + label - 1, x + tw, y + label + th), outline="#C5CED4")
    sheet.save(out, optimize=True)
    return out


def compare_outline(slides: list[dict[str, Any]], session_id: str) -> tuple[list[str], list[str]]:
    state = wf.load_state(session_id) or {}
    ver = state.get("approved_version")
    if not ver:
        return [], ["本会话没有已确认的大纲，跳过大纲一致性检查"]
    try:
        outline = json.loads((wf.session_dir(session_id) / f"outline_v{ver}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], ["读取已确认大纲失败，跳过一致性检查"]
    issues, notes = [], []
    rendered = {}
    for s in slides:
        rendered.setdefault(wf.normalize_title(s["title"]), []).append(s)
    for i, o in enumerate(outline.get("slides", []), 1):
        got = rendered.get(wf.normalize_title(o.get("title", "")))
        if not got:
            issues.append(f"大纲第 {i} 页「{o.get('title')}」在成品中缺失")
            continue
        kind = wf._visual(o).get("kind", "none")
        if kind != "none" and o.get("layout") not in ("title", "section"):
            if not any(s["pictures"] or s["tables"] or s["charts"] for s in got):
                issues.append(f"「{o.get('title')}」大纲计划配{kind}，成品页没有图片/图表/表格")
    content = [s for s in slides if s["type"] not in ("title", "section", "references")]
    visual = [s for s in content if s["pictures"] or s["tables"] or s["charts"]]
    need = wf.VISUAL_DENSITY.get((state.get("requirements") or {}).get("visual_density", "rich"), 0.5)
    if content and len(visual) / len(content) < need:
        issues.append(f"图文密度 {len(visual)}/{len(content)} 低于要求 {need:.0%}")
    for s in content:
        small = [a for a in s.get("pic_areas", []) if a < 0.08]
        if small:
            issues.append(f"第 {s['index']} 页「{s['title']}」图片只占页面 {min(small):.0%}，投影时看不清："
                          "半栏请用 aspect=column 的示意图或近方形图片，宽幅图改放独立 image 页")
        if s["chars"] > 420 and not (s["pictures"] or s["charts"] or s["tables"]):
            notes.append(f"第 {s['index']} 页「{s['title']}」纯文字约 {s['chars']} 字，偏密，建议拆页或配图")
    return issues, notes


async def vision_review(sheet: Path, outline_titles: list[str]) -> tuple[list[str], str]:
    """把缩略图交给多模态模型做版面评审；失败时返回空（不阻塞交付，但会在结果里注明）。

    缩略图分辨率低，模型对小字的判断（如繁简体、细小文字重叠）容易误报，所以视觉评审的结论只作为
    「建议」返回给模型自行复核，不计入必须修复的问题。"""
    s = get_settings()
    if not s.ppt_verify_vision_model:
        return [], "未配置视觉评审模型（PPT_VERIFY_VISION_MODEL），跳过"
    from langchain_core.messages import HumanMessage

    from app.core.llm import build_llm

    b64 = base64.b64encode(sheet.read_bytes()).decode()
    prompt = (
        "你是演示文稿版面审核员。下图是一份 PPT 所有页面的缩略图（按“第 N 页”标注）。逐页检查：文字重叠或被截断、"
        "中文显示为方框/乱码、图片缺失/变形/与标题明显无关、图注与图片分离、页面过于拥挤或大面积空白、配色不统一。"
        "只报告确实能看出的问题，没有就返回空数组。输出 JSON："
        '{"issues": [{"page": 页码, "problem": "问题", "suggestion": "修改建议"}], "overall": "一句话总体评价"}'
        + (f"\n大纲页标题：{'；'.join(outline_titles)[:800]}" if outline_titles else "")
    )
    try:
        llm = build_llm(model=s.ppt_verify_vision_model, temperature=0).bind(
            response_format={"type": "json_object"})
        resp = await asyncio.wait_for(llm.ainvoke([HumanMessage(content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}])]), timeout=120)
        m = re.search(r"\{.*\}", resp.text, re.S)
        data = json.loads(m.group(0) if m else resp.text)
    except Exception as e:  # noqa: BLE001
        logger.warning("视觉评审失败：%s", e)
        return [], f"视觉评审调用失败（{type(e).__name__}），仅完成自动检查"
    issues = [f"第 {x.get('page')} 页：{x.get('problem')}（建议：{x.get('suggestion', '')}）"
              for x in data.get("issues", []) if isinstance(x, dict) and x.get("problem")]
    return issues, str(data.get("overall") or "")


# ------------------------------------------------------------------ 工具
def _sid(runtime: ToolRuntime | None) -> tuple[str | None, str | None]:
    return wf._session_or_error(runtime)


@tool
async def ppt_search_images(query: str, max_results: int = 6, runtime: ToolRuntime = None) -> str:
    """联网搜索可用于 PPT 的图片（照片、结构图、场景图等）。query 建议用具体的中文或英文描述，
    如「心脏 解剖 结构 示意图」「hospital outpatient clinic photo」。返回候选图片的 image_url、尺寸和来源页；
    选中后用 ppt_download_image 下载。医学证据类图片需来自可核验的来源，装饰性图片不能当证据。"""
    try:
        rows = await asyncio.to_thread(_search_images_sync, query, max(1, min(int(max_results), 10)))
    except Exception as e:  # noqa: BLE001
        return f"图片搜索失败：{type(e).__name__}: {e}。可换关键词重试，或改用 ppt_make_diagram 绘制示意图。"
    if not rows:
        return "没有找到合适尺寸的图片，请换关键词，或改用 ppt_make_diagram 绘制示意图。"
    return "\n".join(f"[{i}] {r['title']}  {r['width']}×{r['height']}  站点：{r['site']}\n"
                     f"    image_url: {r['image_url']}\n    来源页: {r['source_page']}" for i, r in enumerate(rows, 1))


@tool
async def ppt_download_image(image_url: str, name: str, source_page: str = "", description: str = "",
                             runtime: ToolRuntime = None) -> str:
    """下载图片到本会话目录，校验后统一转为 PNG/JPEG，返回可直接写进 spec 的 /repo 路径。
    name：简短英文/中文文件名；source_page：图片所在网页（写进 spec 的 source）；description：图片内容（写进 alt）。
    下载失败或图片太小时换下一张候选。"""
    sid, err = _sid(runtime)
    if err:
        return err
    try:
        data = await asyncio.to_thread(_download, image_url)
        out, ext, w, h = await asyncio.to_thread(_normalize_image, data)
    except Exception as e:  # noqa: BLE001
        return f"❌ 下载失败：{type(e).__name__}: {e}。请换一张候选图。"
    d = wf.session_dir(sid) / "images"
    d.mkdir(exist_ok=True)
    f = d / f"{_safe_name(name)}.{ext}"
    f.write_bytes(out)
    rp = wf.repo_path(f)
    host = re.sub(r"^https?://([^/]+).*$", r"\1", source_page or image_url)
    _register_asset(sid, {"path": rp, "kind": "web_image", "image_url": image_url, "source_page": source_page,
                          "description": description, "size": [w, h]})
    ratio = w / h
    tip = "横图，适合 columns 右栏或独立 image 页" if ratio >= 1.2 else ("竖图，适合 columns 并设 ratio [1.4,1]" if ratio <= 0.8 else "近方形，适合 columns")
    return (f"✅ 已保存 {rp}（{w}×{h}，{tip}）。\n"
            f"spec 写法：{{\"type\": \"image\", \"path\": \"{rp}\", \"alt\": \"{description or name}\", "
            f"\"caption\": \"图 N：…\", \"source\": \"网络图片：{host}\"}}")


@tool
async def ppt_make_diagram(kind: str, items: list[dict], name: str, title: str = "", aspect: str = "column",
                           runtime: ToolRuntime = None) -> str:
    """绘制与 PPT 配色一致的示意图 PNG，用于图文混排。kind：
    flow 流程（2-8 步）/ timeline 时间线（2-8 个节点）/ cycle 循环（3-6 个环节）/ compare 对比（2-4 列，detail 用「；」分隔多条）/
    hierarchy 层级（第 1 项为总述，其余为分支，2-6 个）/ cards 要点卡片（2-6 个）。
    items：[{"label": "短标题（≤10 字）", "detail": "补充说明（可选，≤30 字）"}]；title 可选（放在 PPT 页标题里时留空）。
    aspect：column（默认，约 1000×860，放在 columns 半栏里；流程改为竖排）/ wide（1600×900，放在独立 image 整页）。
    返回 /repo 路径；spec 中 caption 需注明「示意图」。"""
    sid, err = _sid(runtime)
    if err:
        return err
    if kind not in DIAGRAM_KINDS:
        return f"❌ kind 必须是 {list(DIAGRAM_KINDS)} 之一"
    norm = [{"label": str(it.get("label") or "").strip(), "detail": str(it.get("detail") or "").strip()}
            for it in items or [] if isinstance(it, dict)]
    norm = [x for x in norm if x["label"]]
    lo, hi = {"cycle": (3, 6), "compare": (2, 4), "hierarchy": (2, 7), "cards": (2, 6)}.get(kind, (2, 8))
    if not lo <= len(norm) <= hi:
        return f"❌ {DIAGRAM_KINDS[kind]}图需要 {lo}-{hi} 项（收到 {len(norm)} 项），请拆分或合并"
    if any(len(x["label"]) > 16 or len(x["detail"]) > 60 for x in norm):
        return "❌ label 请 ≤16 字、detail ≤60 字，太长会导致图中字号过小；把长说明放到 PPT 正文里"
    if not cjk_font_path():
        return "❌ 本机没有可用的中文字体，无法绘制示意图（可设置 PPT_CJK_FONT）"
    # 半栏画布更小：字号（像素）不变，缩放进栏后文字相对更大，投影时看得清
    size = (1600, 900) if aspect == "wide" else (1000, 860)
    try:
        img = await asyncio.to_thread(draw_diagram, kind, norm, title, *size)
    except Exception as e:  # noqa: BLE001
        logger.exception("绘制示意图失败")
        return f"❌ 绘制失败：{type(e).__name__}: {e}"
    d = wf.session_dir(sid) / "images"
    d.mkdir(exist_ok=True)
    f = d / f"diagram_{_safe_name(name)}.png"
    await asyncio.to_thread(img.save, f, "PNG", optimize=True)
    rp = wf.repo_path(f)
    _register_asset(sid, {"path": rp, "kind": f"diagram:{kind}", "items": norm})
    placement = "独立 image 页" if aspect == "wide" else "columns 半栏（ratio 建议 [1,1.2]）"
    return (f"✅ 已生成{DIAGRAM_KINDS[kind]}示意图 {rp}（{img.width}×{img.height}，适合放在{placement}）。\n"
            f"spec 写法：{{\"type\": \"image\", \"path\": \"{rp}\", \"alt\": \"{DIAGRAM_KINDS[kind]}示意图：{'、'.join(x['label'] for x in norm)[:60]}\", "
            f"\"caption\": \"图 N：…（示意图）\", \"source\": \"根据本报告内容绘制的示意图\"}}")


def _resolve_output(session_id: str, file: str) -> Path | None:
    from app.agent.report_tools import workspace

    out_dir = workspace() / "o_outcome"
    if file:
        p = (out_dir / Path(file).name).resolve()
        return p if p.is_file() and p.is_relative_to(out_dir) else None
    state = wf.load_state(session_id) or {}
    if state.get("outputs"):
        p = out_dir / Path(state["outputs"][-1]["file"]).name
        return p if p.is_file() else None
    return None


async def verify_pptx(session_id: str, pptx: Path) -> dict[str, Any]:
    slides = await asyncio.to_thread(inspect_pptx, pptx)
    issues, notes = compare_outline(slides, session_id)
    for s in slides:
        if s["type"] not in ("title", "section") and not s["texts"] and not s["pictures"]:
            issues.append(f"第 {s['index']} 页「{s['title']}」只有标题，没有正文或图片")
    d = wf.session_dir(session_id) / "preview" / pptx.stem
    rendered, sheet_url, vision_note = False, "", ""
    try:
        pdf = await asyncio.to_thread(render_pdf, pptx, d)
        r_issues, r_notes, pngs = await asyncio.to_thread(check_pdf, pdf, d, slides)
        issues += r_issues
        notes += r_notes
        sheet = await asyncio.to_thread(contact_sheet, pngs, d / "contact_sheet.png")
        rendered = True
        sheet_url = f"/api/ppt/preview/{wf.session_dir_name(session_id)}/{pptx.stem}/contact_sheet.png"
        state = wf.load_state(session_id) or {}
        titles = [s["title"] for s in slides]
        v_issues, vision_note = await vision_review(sheet, titles if state else [])
        notes += [f"[视觉评审建议，请对照 spec 复核后决定是否修改] {x}" for x in v_issues]
    except (FileNotFoundError, RuntimeError, subprocess.TimeoutExpired) as e:
        notes.append(f"未完成渲染验证：{e}")
    stats = {
        "slides": len(slides),
        "pictures": sum(s["pictures"] for s in slides),
        "visual_slides": sum(1 for s in slides if s["pictures"] or s["tables"] or s["charts"]),
        "content_slides": sum(1 for s in slides if s["type"] not in ("title", "section", "references")),
    }
    passed = rendered and not issues
    summary = (f"{stats['slides']} 页，内容页 {stats['content_slides']}，含图/图表/表格 {stats['visual_slides']} 页，"
               f"嵌入图片 {stats['pictures']} 张；问题 {len(issues)} 个")
    return {"file": pptx.name, "passed": passed, "rendered": rendered, "issues": issues, "notes": notes,
            "stats": stats, "summary": summary, "preview": sheet_url, "vision": vision_note,
            "slides": [{k: s[k] for k in ("index", "type", "title", "pictures", "tables", "charts", "chars")}
                       for s in slides]}


@tool
async def ppt_verify(file: str = "", runtime: ToolRuntime = None) -> str:
    """验证生成的 PPT：解析每页结构、用 LibreOffice 渲染逐页检查（中文字体、越界、空白页）、对照已确认大纲检查缺页和配图、
    检查图文密度，并做视觉评审。file 为 o_outcome 下的文件名，留空则验证本会话最近一次生成的 PPT。
    有问题时按提示修正 spec 并重新生成、再次验证；通过后把预览图和下载地址给用户。"""
    sid, err = _sid(runtime)
    if err:
        return err
    pptx = _resolve_output(sid, file)
    if not pptx or pptx.suffix.lower() != ".pptx":
        return "❌ 找不到要验证的 PPTX；请先用 report_run_generator 生成，或传入 o_outcome 下的文件名"
    res = await verify_pptx(sid, pptx)
    wf.record_verify(sid, res, wf.latest_user_turn(runtime))
    pages = "\n".join(f"  {s['index']:>2}. [{s['type']}] {s['title'] or '(无标题)'}"
                      f"{'  🖼️×' + str(s['pictures']) if s['pictures'] else ''}{'  📋' if s['tables'] else ''}"
                      f"{'  📊' if s['charts'] else ''}" for s in res["slides"])
    lines = [f"{'✅ 验证通过' if res['passed'] else '❌ 验证未通过'}：{res['file']}", res["summary"], "逐页：", pages]
    if res["issues"]:
        lines.append("问题（需修正 spec 后重新生成并再次验证）：\n- " + "\n- ".join(res["issues"]))
    if res["notes"]:
        lines.append("提示：\n- " + "\n- ".join(res["notes"]))
    if res["vision"]:
        lines.append(f"视觉评审：{res['vision']}")
    if res["preview"]:
        lines.append(f"预览图（可在回复中用 Markdown 图片展示给用户）：![PPT 预览]({res['preview']})")
    lines.append(f"下载：/api/reports/{res['file']}")
    if res["passed"]:
        lines.append("👉 验证已通过：现在就把下载地址、预览图和结论交付给用户，本轮不要再重新生成；"
                     "上面的建议（如有）列给用户，由用户决定是否调整。")
    return "\n".join(lines)


MEDIA_TOOLS = [ppt_search_images, ppt_download_image, ppt_make_diagram, ppt_verify]
