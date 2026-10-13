"""AI 生图工具（app/agent/imagegen.py）测试。

默认用假的 CLI（替换 SCRIPT）验证参数、落盘、错误处理；设置 IMAGEGEN_LIVE_TEST=1 时才真正调用生图服务。
PYTHONPATH=. .venv/bin/python -m pytest tests/agent/test_imagegen.py -q
"""
from __future__ import annotations

import asyncio
import os
import textwrap
from types import SimpleNamespace

import pytest

from app.agent import imagegen


def rt(session_id="imgsession"):
    return SimpleNamespace(state={"messages": []}, config={"configurable": {"thread_id": session_id}})


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    from app.core import config

    config.get_settings.cache_clear()
    monkeypatch.setenv("REPORT_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("IMAGEGEN_API_KEY", os.environ.get("IMAGEGEN_API_KEY") or "test-key")
    yield tmp_path / "ws"
    config.get_settings.cache_clear()


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    """假的 image_gen.py：记录参数和环境变量，写一张小 PNG；prompt 含 FAIL 时模拟 429。"""
    script = tmp_path / "fake_image_gen.py"
    log = tmp_path / "args.txt"
    script.write_text(textwrap.dedent(f'''
        import os, sys, struct, zlib
        from pathlib import Path
        args = sys.argv[1:]
        Path({str(log)!r}).write_text(repr(args) + "\\n" + os.environ.get("OPENAI_API_KEY", ""), encoding="utf-8")
        prompt = args[args.index("--prompt") + 1]
        if "FAIL" in prompt:
            print("Error code: 429 - EngineOverloaded", file=sys.stderr); sys.exit(1)
        def png():
            c = lambda k, d: struct.pack(">I", len(d)) + k + d + struct.pack(">I", zlib.crc32(k + d))
            return (b"\\x89PNG\\r\\n\\x1a\\n" + c(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
                    + c(b"IDAT", zlib.compress(b"\\x00" + b"\\xff\\x00\\x00" * 4) * 1) + c(b"IEND", b""))
        if "--out-dir" in args:
            d = Path(args[args.index("--out-dir") + 1]); d.mkdir(parents=True, exist_ok=True)
            n = int(args[args.index("--n") + 1])
            outs = [d / f"image_{{i}}.png" for i in range(1, n + 1)]
        else:
            outs = [Path(args[args.index("--out") + 1])]
        for o in outs:
            o.parent.mkdir(parents=True, exist_ok=True); o.write_bytes(png()); print(f"Wrote {{o}}")
    '''), encoding="utf-8")
    monkeypatch.setattr(imagegen, "SCRIPT", script)
    return log


def call(tool, **kw):
    return asyncio.run(tool.coroutine(**kw, runtime=rt()))


def test_generate_saves_and_returns_markdown(fake_cli, workspace):
    out = call(imagegen.generate_image, prompt="a red apple", name="苹果", size="square", quality="low")
    assert out.startswith("✅") and "![" in out and "/api/workspace/files/" in out
    args = fake_cli.read_text(encoding="utf-8")
    assert "'--size', '1024x1024'" in args and "'--no-augment'" in args and "'--quality', 'low'" in args
    files = list((workspace / "threads").rglob("ai_苹果.png"))
    assert len(files) == 1
    # 再生成同名图片不覆盖
    call(imagegen.generate_image, prompt="a red apple", name="苹果")
    assert len(list((workspace / "threads").rglob("ai_苹果*.png"))) == 2


def test_generate_variants_and_validation(fake_cli):
    out = call(imagegen.generate_image, prompt="logo", name="logo", n=3)
    assert out.count("/api/workspace/files/") == 3
    assert "❌" in call(imagegen.generate_image, prompt="x", name="x", size="huge")
    assert "❌" in call(imagegen.generate_image, prompt="x", name="x", n=9)
    assert "❌" in call(imagegen.generate_image, prompt="  ", name="x")


def test_generate_error_is_friendly(fake_cli):
    out = call(imagegen.generate_image, prompt="FAIL please", name="x")
    assert out.startswith("❌") and "繁忙" in out


def test_edit_requires_workspace_image(fake_cli, workspace):
    assert "找不到" in call(imagegen.edit_image, image_path="/repo/threads/none.png", prompt="x")
    out = call(imagegen.generate_image, prompt="a red apple", name="base")
    src = out.split("- ", 1)[1].split("\n", 1)[0]
    out = call(imagegen.edit_image, image_path=src, prompt="change only background to blue")
    assert out.startswith("✅") and "base-edited" in out


def test_workspace_file_route(fake_cli, workspace):
    from fastapi.testclient import TestClient

    from app.main import app

    out = call(imagegen.generate_image, prompt="a red apple", name="route")
    url = out.split("](", 1)[1].split(")", 1)[0]
    with TestClient(app) as c:
        r = c.get(url)
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert c.get("/api/workspace/files/../../etc/passwd").status_code == 404
        assert c.get("/api/workspace/files/styles/x.json").status_code == 404


@pytest.mark.skipif(os.environ.get("IMAGEGEN_LIVE_TEST") != "1", reason="设置 IMAGEGEN_LIVE_TEST=1 才调用真实生图服务")
def test_live_generate(workspace):
    from dotenv import load_dotenv

    load_dotenv(".env", override=True)
    from app.core import config

    config.get_settings.cache_clear()
    out = call(imagegen.generate_image, prompt="flat icon of a blue heart, white background", name="live", quality="low",
               size="square")
    assert out.startswith("✅"), out
