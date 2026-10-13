"""把本地默认提示词同步到 Langfuse（已存在同名提示词则跳过）。

用法：python -m scripts.init_prompts [--force]
  --force  即使已存在也创建新版本并打 production label
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()

from app.core.langfuse_client import get_langfuse  # noqa: E402
from app.prompts.manager import DEFAULT_PROMPTS  # noqa: E402


def main() -> None:
    force = "--force" in sys.argv
    lf = get_langfuse()
    if lf is None:
        sys.exit("Langfuse 未配置，请检查 .env 中的 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY")
    if not lf.auth_check():
        sys.exit("Langfuse 认证失败")

    for name, spec in DEFAULT_PROMPTS.items():
        exists = True
        try:
            lf.get_prompt(name, cache_ttl_seconds=0, max_retries=0)
        except Exception:  # noqa: BLE001
            exists = False
        if exists and not force:
            print(f"[skip] {name} 已存在")
            continue
        p = lf.create_prompt(
            name=name, prompt=spec["prompt"], type="text",
            labels=spec.get("labels", ["production"]), config=spec.get("config", {}),
            commit_message="init from scripts/init_prompts.py",
        )
        print(f"[ok]   {name} -> v{p.version} labels={p.labels}")
    lf.flush()


if __name__ == "__main__":
    main()
