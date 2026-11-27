"""可重复的 Retrieval Golden Dataset 评测 CLI。

用法（backend/ 目录下）：
    python -m scripts.knowledge_eval --kb-id 1 --dataset golden.json --mode hybrid
    python -m scripts.knowledge_eval --kb-id 1 --dataset golden.json --rerank --output result.json

Golden Dataset 格式：
{
  "name": "设备手册基线",
  "cases": [
    {"question": "设备多久检查一次？", "expected_document_ids": [12],
     "expected_pages": [3], "relevant_chunk_ids": [], "no_answer": false},
    {"question": "完全不相关的问题", "no_answer": true}
  ]
}
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.core.database import AsyncSessionLocal, init_db
from app.services.knowledge.evaluation import EvaluationCase, evaluate


async def _run(kb_id: int, dataset: dict, mode: str, top_k: int,
               rerank: bool | None, output: str | None) -> dict:
    await init_db()
    cases = [EvaluationCase.from_dict(item) for item in dataset.get("cases") or []]
    if not cases:
        raise SystemExit("数据集缺少 cases")
    async with AsyncSessionLocal() as db:
        result = await evaluate(
            db, knowledge_base_ids=[kb_id], cases=cases,
            mode=mode, top_k=top_k, rerank=rerank,
        )
    result["dataset_name"] = dataset.get("name") or ""
    result["knowledge_base_id"] = kb_id
    if output:
        Path(output).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="知识库检索质量评测（Golden Dataset）")
    parser.add_argument("--kb-id", type=int, required=True)
    parser.add_argument("--dataset", required=True, help="Golden Dataset JSON 文件")
    parser.add_argument("--mode", default="hybrid", choices=["hybrid", "dense", "bm25"])
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--rerank", action="store_true")
    parser.add_argument("--output", default=None, help="把完整结果写入 JSON 文件")
    args = parser.parse_args()
    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    result = asyncio.run(_run(
        args.kb_id, dataset, args.mode, args.top_k,
        True if args.rerank else None, args.output,
    ))
    summary = {key: value for key, value in result.items() if key != "details"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
