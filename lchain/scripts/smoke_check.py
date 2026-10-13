"""服务器部署自检：Web / MySQL / 家庭医生配置 /（可选）LLM 四项检查。

用法（在项目根目录）：
    PYTHONPATH=. .venv/bin/python scripts/smoke_check.py            # 只查 HTTP + MySQL + 配置
    PYTHONPATH=. .venv/bin/python scripts/smoke_check.py --llm      # 额外真实调用一次模型（消耗 token）
    ... --url http://127.0.0.1:8000                                 # 指定服务地址
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request


def _get(url: str, timeout: float = 8.0) -> tuple[int | None, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--llm", action="store_true", help="真实调用一次模型（消耗少量 token）")
    a = ap.parse_args()
    ok = True

    # 1) web + Langfuse 状态
    st, body = _get(f"{a.url}/api/health")
    if st == 200:
        try:
            lf = json.loads(body).get("langfuse")
        except Exception:  # noqa: BLE001
            lf = None
        lf_txt = "未配置（使用代码内置提示词，功能正常）" if lf is None else ("已连接" if lf else "配置了但连接失败")
        print(f"✅ web /api/health：200（Langfuse：{lf_txt}）")
    else:
        print(f"❌ web /api/health：{st or body}")
        ok = False

    # 2) 家庭医生配置
    st, body = _get(f"{a.url}/api/wecom/fd/config")
    if st == 200:
        d = json.loads(body)
        print(f"✅ 家庭医生：放行={d.get('release_stage')} 红旗规则={d.get('red_flag_rules')} 条 "
              f"低风险路径={d.get('pathways')} 条 急救电话={d.get('emergency_number')}")
    else:
        print(f"❌ /api/wecom/fd/config：{st or body}")
        ok = False

    # 3) MySQL（群列表接口依赖数据库）
    st, body = _get(f"{a.url}/api/wecom/groups?limit=1")
    if st == 200:
        total = json.loads(body).get("total", "?")
        print(f"✅ MySQL：已连接（当前 {total} 个群）")
    else:
        print(f"❌ MySQL：{st or body}")
        ok = False

    # 4) 页面
    for path in ("/wecom", "/docs"):
        st, _ = _get(f"{a.url}{path}")
        print(("✅" if st == 200 else "❌") + f" 页面 {path}：{st}")
        ok &= st == 200

    # 5) （可选）真实调用模型
    if a.llm:
        try:
            from dotenv import load_dotenv

            load_dotenv()
            from app.core.config import get_settings
            from app.core.llm import build_llm

            s = get_settings()
            llm = build_llm(model=s.llm_model, temperature=0)
            r = asyncio.run(llm.ainvoke("只回复两个字：正常"))
            text = getattr(r, "text", None) or (r.content if isinstance(r.content, str) else str(r.content))
            print(f"✅ LLM（{s.llm_model}）：{text[:40]}")
        except Exception as e:  # noqa: BLE001
            print(f"❌ LLM 调用失败：{type(e).__name__}: {e}")
            ok = False

    print("\n" + ("全部通过 ✅" if ok else "存在失败项，请按上面提示排查 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
