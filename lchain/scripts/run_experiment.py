"""基于 Langfuse Dataset 的离线提示词评估：对同一批问题跑不同提示词版本，结果在 Langfuse「Datasets → Runs」中对比。

用法：
  python -m scripts.run_experiment                       # 用 production 版本
  python -m scripts.run_experiment --version 2           # 指定版本
  python -m scripts.run_experiment --label staging
"""
from __future__ import annotations

import argparse

from dotenv import load_dotenv

load_dotenv()

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from app.core.langfuse_client import get_langfuse  # noqa: E402
from app.core.llm import build_llm  # noqa: E402
from app.prompts.manager import resolve_prompt  # noqa: E402

DATASET = "agent-smoke-test"
SAMPLES = [
    {"input": "你好，请介绍一下你自己", "expected_output": "包含自我介绍"},
    {"input": "用一句话解释什么是递归", "expected_output": "简洁准确地解释递归"},
    {"input": "给我三个提升专注力的建议", "expected_output": "列出三条建议"},
]


def ensure_dataset(lf) -> None:
    try:
        lf.get_dataset(DATASET)
        return
    except Exception:  # noqa: BLE001
        pass
    lf.create_dataset(name=DATASET, description="Agent 冒烟测试集")
    for s in SAMPLES:
        lf.create_dataset_item(dataset_name=DATASET, input=s["input"], expected_output=s["expected_output"])
    print(f"已创建数据集 {DATASET}（{len(SAMPLES)} 条）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default="agent-system")
    ap.add_argument("--label", default=None)
    ap.add_argument("--version", type=int, default=None)
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    lf = get_langfuse()
    if lf is None:
        raise SystemExit("Langfuse 未配置")
    ensure_dataset(lf)
    prompt = resolve_prompt(args.prompt, label=args.label, version=args.version)
    llm = build_llm(model=args.model, temperature=prompt.config.get("temperature"))

    def task(*, item, **_):
        from langfuse.langchain import CallbackHandler

        q = item.input if hasattr(item, "input") else item["input"]
        meta = {"langfuse_prompt": prompt.client} if prompt.client else {}
        resp = llm.invoke([SystemMessage(prompt.text), HumanMessage(q)],
                          config={"callbacks": [CallbackHandler()], "metadata": meta})
        return resp.content

    def length_eval(*, output, **_):
        from langfuse import Evaluation
        return Evaluation(name="length", value=len(output or ""), comment="回答字符数")

    run_name = f"{args.prompt}-v{prompt.version or 'local'}-{llm.model_name}"
    dataset = lf.get_dataset(DATASET)
    result = dataset.run_experiment(name=run_name, task=task, evaluators=[length_eval],
                                    description=f"prompt={prompt.meta()}")
    print(result.format())
    lf.flush()


if __name__ == "__main__":
    main()
