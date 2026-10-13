"""联网搜索工具：web_search（搜索）+ fetch_webpage（读取网页正文）。

搜索服务可切换（SEARCH_PROVIDER）：
- ddgs    免 Key，聚合 bing / brave / yahoo 等（默认）
- tavily  专为 LLM 设计的搜索 API，结果质量高，需要 TAVILY_API_KEY（https://tavily.com 每月有免费额度）
- searxng 自建元搜索引擎，需要 SEARXNG_URL

所有服务都统一返回 [{title, url, snippet, date?, source?}]，工具输出统一为带编号的文本，方便模型引用 [1] [2]。
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from langchain_core.tools import tool

from app.core.config import get_settings

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TimeRange = Literal["day", "week", "month", "year"]


# --------------------------------------------------------------------------- providers
def _search_ddgs(query: str, n: int, topic: str, time_range: str | None) -> list[dict[str, Any]]:
    from ddgs import DDGS

    s = get_settings()
    kw: dict[str, Any] = {"max_results": n, "region": s.search_region}

    if time_range:
        kw["timelimit"] = time_range[0]  # d / w / m / y
    ddgs = DDGS(timeout=15)
    if topic == "news":
        try:
            rows = ddgs.news(query, **kw)
            return [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("body"),
                     "date": r.get("date"), "source": r.get("source")} for r in rows]
        except Exception as e:  # noqa: BLE001  新闻接口失败时退回普通搜索
            logger.info("ddgs news 失败，回退 text 搜索: %s", e)
    # ddgs 默认 auto 会随机选引擎，其中部分（如 mojeek）在国内网络下不可用；
    # 配置了 SEARCH_DDGS_BACKEND 时多个引擎并行搜索，取最先返回非空结果的那个（单个引擎偶发空结果/超时很常见）
    backends = [b.strip() for b in s.search_ddgs_backend.split(",") if b.strip()] or ["auto"]

    def one(be: str) -> list[dict[str, Any]]:
        rows = DDGS(timeout=10).text(query, backend=be, **kw)
        return [{"title": r.get("title"), "url": r.get("href"), "snippet": r.get("body")} for r in rows]

    if len(backends) == 1:
        return one(backends[0])
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    pool = ThreadPoolExecutor(max_workers=len(backends))
    futures = {pool.submit(one, be): be for be in backends}
    last_err: Exception | None = None
    try:
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=15, return_when=FIRST_COMPLETED)
            if not done:
                break
            for f in done:
                try:
                    rows = f.result()
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    logger.info("ddgs[%s] 搜索失败: %s", futures[f], e)
                    continue
                if rows:
                    return rows
    finally:
        pool.shutdown(wait=False, cancel_futures=True)  # 不等待慢的引擎
    if last_err:
        raise last_err
    return []


def _search_tavily(query: str, n: int, topic: str, time_range: str | None) -> list[dict[str, Any]]:
    s = get_settings()
    body: dict[str, Any] = {"query": query, "max_results": n, "topic": "news" if topic == "news" else "general",
                            "search_depth": "basic"}
    if time_range:
        body["time_range"] = time_range
    r = httpx.post("https://api.tavily.com/search", json=body, timeout=20,
                   headers={"Authorization": f"Bearer {s.tavily_api_key}"})
    r.raise_for_status()
    return [{"title": x.get("title"), "url": x.get("url"), "snippet": x.get("content"),
             "date": x.get("published_date")} for x in r.json().get("results", [])]


def _search_searxng(query: str, n: int, topic: str, time_range: str | None) -> list[dict[str, Any]]:
    s = get_settings()
    params: dict[str, Any] = {"q": query, "format": "json", "categories": "news" if topic == "news" else "general"}
    if time_range:
        params["time_range"] = time_range
    r = httpx.get(f"{s.searxng_url.rstrip('/')}/search", params=params, timeout=20)
    r.raise_for_status()
    return [{"title": x.get("title"), "url": x.get("url"), "snippet": x.get("content"),
             "date": x.get("publishedDate")} for x in r.json().get("results", [])[:n]]


PROVIDERS = {"ddgs": _search_ddgs, "tavily": _search_tavily, "searxng": _search_searxng}


def search(query: str, *, max_results: int | None = None, topic: str = "general",
           time_range: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    """执行搜索，返回 (实际使用的 provider, 结果列表)。首选服务失败时自动回退到 ddgs。"""
    s = get_settings()
    n = max(1, min(max_results or s.search_max_results, 10))
    provider = s.resolved_search_provider
    try:
        return provider, PROVIDERS[provider](query, n, topic, time_range)
    except Exception as e:  # noqa: BLE001
        if provider == "ddgs":
            raise
        logger.warning("搜索服务 %s 失败，回退 ddgs: %s", provider, e)
        return "ddgs", _search_ddgs(query, n, topic, time_range)


def format_results(results: list[dict[str, Any]]) -> str:
    lines = []
    for i, r in enumerate(results, 1):
        meta = " · ".join(x for x in (r.get("source"), (r.get("date") or "")[:10]) if x)
        lines.append(f"[{i}] {r.get('title') or '(无标题)'}\nURL: {r.get('url')}"
                     + (f"\n{meta}" if meta else "") + f"\n{(r.get('snippet') or '').strip()[:500]}")
    return "\n\n".join(lines)


# --------------------------------------------------------------------------- 网页抓取
# Clash / Surge 等代理的 Fake-IP 模式会把所有外网域名解析到 198.18.0.0/15（RFC 2544 基准测试段），
# 这个网段不会出现真实的内网服务，放行它，否则开着代理时所有网页都会被误判为内网。
_FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")


def _is_public_url(url: str) -> bool:
    """防 SSRF：只允许 http(s)，且不能解析到 127.x / 10.x / 192.168.x / 169.254.x（云元数据）等内网地址。"""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    if get_settings().fetch_allow_private:
        return True
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.version == 4 and ip in _FAKE_IP_NET:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast \
                or ip.is_unspecified:
            return False
    return True


def fetch_text(url: str, max_chars: int | None = None) -> str:
    import trafilatura

    max_chars = max_chars or get_settings().fetch_max_chars
    if not _is_public_url(url):
        return f"拒绝访问：{url} 不是公网 http(s) 地址"
    with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": UA}) as c:
        r = c.get(url)
        if not _is_public_url(str(r.url)):
            return "拒绝访问：重定向到了内网地址"
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "xml" not in ctype:
            if ctype.startswith("text/") or "json" in ctype:
                return r.text[:max_chars]
            return f"不支持的内容类型: {ctype}"
        html = r.text
    text = trafilatura.extract(html, output_format="markdown", include_links=False,
                               include_tables=True, favor_recall=True) or ""
    meta = trafilatura.extract_metadata(html)
    title = meta.title if meta and meta.title else ""
    if not text.strip():
        return f"未能从 {url} 提取到正文（可能是动态渲染页面）"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n…（正文过长已截断，共 {len(text)} 字符）"
    return f"# {title}\nURL: {url}\n\n{text}" if title else f"URL: {url}\n\n{text}"


# --------------------------------------------------------------------------- tools
@tool
async def web_search(query: str, topic: Literal["general", "news"] = "general",
                     time_range: TimeRange | None = None, max_results: int = 5) -> str:
    """联网搜索，获取实时信息、最新新闻、事实数据或你不确定的知识。

    Args:
        query: 搜索关键词。要具体，必要时包含年份/地点/专有名词；中文问题可同时尝试英文关键词。
        topic: general=通用网页；news=新闻资讯（问到"最新/近期/今天发生"时使用）。
        time_range: 限定时间范围 day/week/month/year，不限则留空。
        max_results: 返回条数，1-10。
    """
    try:
        provider, results = await asyncio.to_thread(
            search, query, max_results=max_results, topic=topic, time_range=time_range)
    except Exception as e:  # noqa: BLE001
        return f"搜索失败：{type(e).__name__}: {e}"
    if not results:
        return f"没有找到与「{query}」相关的结果，可以换个关键词再试。"
    return f"搜索服务: {provider} | 关键词: {query}\n\n{format_results(results)}"


@tool
async def fetch_webpage(url: str) -> str:
    """打开网页并提取正文（Markdown）。当搜索摘要信息不够详细、需要阅读原文时使用，URL 通常来自 web_search 的结果。"""
    try:
        return await asyncio.to_thread(fetch_text, url)
    except httpx.HTTPStatusError as e:
        return f"抓取失败：HTTP {e.response.status_code}"
    except Exception as e:  # noqa: BLE001
        return f"抓取失败：{type(e).__name__}: {e}"


SEARCH_TOOLS = [web_search, fetch_webpage]


def search_status() -> dict[str, Any]:
    s = get_settings()
    return {"provider": s.resolved_search_provider, "max_results": s.search_max_results,
            "tavily_configured": bool(s.tavily_api_key), "searxng_configured": bool(s.searxng_url)}


if __name__ == "__main__":  # python -m app.agent.search "关键词"
    import sys

    from dotenv import load_dotenv

    load_dotenv(".env")
    get_settings.cache_clear()
    q = " ".join(sys.argv[1:]) or "LangChain 最新版本"
    p, res = search(q)
    print(json.dumps({"provider": p, "results": res}, ensure_ascii=False, indent=2))
