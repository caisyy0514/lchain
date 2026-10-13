"""LLM 工厂（`app/core/llm.py`）的回归测试。

锁住两件事：

1. **缓存键必须能无损还原额外参数**。老实现用 `repr` 把参数拼成 `reasoning_effort='low'` 当缓存键，
   再按 `=` / `|` 切回来，字符串就带上了引号 —— 发给模型的是 `"'low'"`，
   deepseek v4 网关对 `reasoning_effort` 是严格枚举校验，直接 422，整轮执行失败。
2. `reasoning_effort` 的清洗：.env / Langfuse 里写脏（带引号、大写、拼错）不能把整轮执行带崩。

    PYTHONPATH=. .venv/bin/python -m pytest tests/agent/test_llm_factory.py -q
"""
from __future__ import annotations

import json

import pytest

from app.core import llm as llm_mod
from app.core.llm import REASONING_EFFORTS, build_llm, clean_reasoning_effort


def _payload(**kwargs: object) -> dict:
    llm = build_llm(model="test-model", temperature=0, **kwargs)
    return llm._get_request_payload([{"role": "user", "content": "hi"}])


def test_extra_kwargs_survive_cache_key_round_trip() -> None:
    """额外参数经缓存键往返后不能变形（曾经的 bug：字符串被 repr 成 "'low'"）。"""
    payload = _payload(reasoning_effort="low", top_p=0.5)
    assert payload["reasoning_effort"] == "low"
    assert payload["top_p"] == 0.5


def test_int_extra_kwargs_survive_cache_key_round_trip() -> None:
    """数字参数同样不能变（langchain 会把 max_tokens 映射成 max_completion_tokens）。"""
    payload = _payload(max_tokens=16)
    assert payload["max_completion_tokens"] == 16


def test_cache_key_is_lossless_json() -> None:
    """缓存键就是参数的 JSON，不带 repr 的引号。"""
    key = llm_mod._cache_key(reasoning_effort="low", max_tokens=16)
    assert json.loads(key) == {"reasoning_effort": "low", "max_tokens": 16}
    built = llm_mod._build("test-model", 0.0, key)
    assert built.max_tokens == 16


def test_same_params_share_one_client() -> None:
    """同参数复用同一个客户端（共享 httpx 连接池）；不同参数是不同的实例。"""
    a = build_llm(model="test-model", temperature=0, reasoning_effort="low")
    b = build_llm(model="test-model", temperature=0, reasoning_effort="low")
    c = build_llm(model="test-model", temperature=0, reasoning_effort="high")
    assert a is b
    assert a is not c


@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        ("low", "low"),
        (" 'low' ", "low"),
        ('"HIGH"', "high"),
        ("low\n", "low"),
        ("medium", "medium"),
        ("bogus", ""),
        ("", ""),
        ("   ", ""),
        (None, ""),
        (1, ""),
    ],
)
def test_clean_reasoning_effort(raw: object, expect: str) -> None:
    assert clean_reasoning_effort(raw) == expect


def test_all_expected_variants_are_kept() -> None:
    """允许的取值一个都不能少（deepseek v4 网关的枚举）。"""
    for v in REASONING_EFFORTS:
        assert clean_reasoning_effort(v) == v
    assert set(REASONING_EFFORTS) == {"none", "minimal", "low", "medium", "high", "xhigh", "ultra", "max"}


def test_dirty_effort_in_env_is_not_sent_as_quoted_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """即使配置里写着 'low'（带引号），发出去的也必须是清理过的 low。"""
    monkeypatch.setattr(llm_mod.get_settings(), "wecom_fast_reasoning_effort", "'low'")
    from app.wecom.llm import model_for

    _, extra = model_for("wecom-intent")
    assert extra.get("reasoning_effort") == "low"
    assert _payload(**extra)["reasoning_effort"] == "low"
