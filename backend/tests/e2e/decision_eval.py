"""Decision Architecture 离线评测：200+ 标注样例的意图/路线/连续性准确率。

容器内运行（真实模型）：
  docker cp backend/tests/evaluation/decision_cases.jsonl ai_plat_backend:/tmp/decision_cases.jsonl
  docker cp backend/tests/e2e/decision_eval.py ai_plat_backend:/tmp/decision_eval.py
  docker exec -e PYTHONPATH=/app -w /app ai_plat_backend python /tmp/decision_eval.py \
      --cases /tmp/decision_cases.jsonl --model-config-id 0 --limit 50 --report /tmp/decision_eval.json

说明：
- 逐条调用与线上一致的决策模型（非流式、温度 0），再走同一套 Validator；
- 记录 intent / route / continuity / deliverable_format / clarify 准确率与混淆矩阵；
- 任何单条失败（超时/非法输出）计入 fallback，不伪造成功。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

from app.services.agent_runtime.decision.context import build_decision_context
from app.services.agent_runtime.decision.service import decide
from app.services.agent_runtime.decision.validator import validate_decision
from app.services.agent_runtime.task_router import detect_deliverable_format


def _load_cases(path: str, limit: int, offset: int = 0) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(json.loads(line))
    sliced = cases[offset:]
    return sliced[:limit] if limit > 0 else sliced


def _rate(hit: int, total: int) -> float:
    return round(hit / total, 4) if total else 0.0


def _precision_recall(
    pairs: list[tuple[str, str]], target: str,
) -> dict[str, float]:
    tp = sum(1 for expected, predicted in pairs if expected == target and predicted == target)
    fp = sum(1 for expected, predicted in pairs if expected != target and predicted == target)
    fn = sum(1 for expected, predicted in pairs if expected == target and predicted != target)
    return {
        "precision": _rate(tp, tp + fp),
        "recall": _rate(tp, tp + fn),
        "support": sum(1 for expected, _ in pairs if expected == target),
    }


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [item for item in records if not item["fallback"]]
    intent_checked = [item for item in valid if not item["expected"].get("intent_any")]
    intent_pairs = [
        (item["expected"]["intent"], item["predicted"]["intent"]) for item in intent_checked
    ]
    route_pairs = [(item["expected"]["route"], item["predicted"]["route"]) for item in valid]
    continuity_pairs = [
        (item["expected"].get("continuity", "new_task"), item["predicted"]["continuity"])
        for item in valid
    ]
    format_items = [
        item for item in valid if item["expected"].get("deliverable_format") not in (None, "none")
    ]
    clarify_count = sum(1 for item in valid if item["expected"].get("route") == "clarify")

    def accuracy(pairs: list[tuple[str, str]]) -> float:
        return _rate(sum(1 for expected, predicted in pairs if expected == predicted), len(pairs))

    confusion: dict[str, dict[str, int]] = {}
    for expected, predicted in intent_pairs:
        confusion.setdefault(expected, {})
        confusion[expected][predicted] = confusion[expected].get(predicted, 0) + 1

    categories = sorted({item["category"] for item in valid})
    per_category = {}
    for category in categories:
        items = [item for item in valid if item["category"] == category]
        checked = [item for item in items if not item["expected"].get("intent_any")]
        per_category[category] = {
            "count": len(items),
            "intent_accuracy": accuracy([
                (item["expected"]["intent"], item["predicted"]["intent"]) for item in checked
            ]) if checked else None,
            "route_accuracy": accuracy([
                (item["expected"]["route"], item["predicted"]["route"]) for item in items
            ]),
        }

    latencies = [item["latency_ms"] for item in valid] or [0]
    latencies.sort()

    def percentile(value: float) -> int:
        index = min(len(latencies) - 1, max(0, int(round((len(latencies) - 1) * value))))
        return latencies[index]

    return {
        "total": len(records),
        "valid": len(valid),
        "fallback": len(records) - len(valid),
        "intent_accuracy": accuracy(intent_pairs),
        "route_accuracy": accuracy(route_pairs),
        "continuity_accuracy": accuracy(continuity_pairs),
        "deliverable_format_accuracy": accuracy([
            (item["expected"].get("deliverable_format", "none"),
             item["predicted"].get("deliverable_format", "none"))
            for item in format_items
        ]) if format_items else None,
        "clarify_recall": (
            _precision_recall(route_pairs, "clarify")["recall"] if clarify_count else None
        ),
        "document_graph_route": _precision_recall(route_pairs, "document_graph"),
        "team_project_route": _precision_recall(route_pairs, "team_project_graph"),
        "confusion_intent": confusion,
        "per_category": per_category,
        "latency_ms": {
            "p50": statistics.median(latencies),
            "p95": percentile(0.95),
            "max": max(latencies),
        },
        "tokens": {
            "input": sum(int(item.get("input_tokens") or 0) for item in records),
            "output": sum(int(item.get("output_tokens") or 0) for item in records),
            "avg_input": round(
                sum(int(item.get("input_tokens") or 0) for item in records)
                / max(1, len(records)), 1
            ),
            "avg_output": round(
                sum(int(item.get("output_tokens") or 0) for item in records)
                / max(1, len(records)), 1
            ),
        },
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Decision Architecture 离线评测")
    parser.add_argument("--cases", required=True)
    parser.add_argument("--model-config-id", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--report", default="/tmp/decision_eval.json")
    args = parser.parse_args()

    from app.services.agent_runtime import get_runtime_service

    provider, model_config = await get_runtime_service()._load_model(
        args.model_config_id or None
    )
    cases = _load_cases(args.cases, args.limit, args.offset)
    records: list[dict[str, Any]] = []
    for index, case in enumerate(cases, 1):
        context = build_decision_context(
            case["message"],
            history=case.get("history") or [],
            artifacts=case.get("artifacts") or [],
            recent_runs=case.get("recent_runs") or [],
            preview_document=case.get("preview_document"),
            attachments=case.get("attachments") or [],
            capabilities=case.get("capabilities") or [],
            tools=case.get("tools") or [],
            explicit=case.get("explicit") or {},
        )
        started = time.monotonic()
        outcome = await decide(
            context, provider=provider, model_config=model_config,
            run_id=f"eval-{case['id']}", timeout=args.timeout,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        if outcome.decision is None:
            record = {
                "id": case["id"], "category": case["category"],
                "message": case["message"], "expected": case["expected"],
                "predicted": None, "fallback": True, "error": outcome.error,
                "latency_ms": latency_ms,
                "input_tokens": outcome.input_tokens,
                "output_tokens": outcome.output_tokens,
            }
        else:
            validated = validate_decision(
                outcome.decision, context=context, auto_team=False,
                # 与线上一致：用户点名的格式是确定性事实
                user_format=detect_deliverable_format(case["message"]),
            )
            record = {
                "id": case["id"], "category": case["category"],
                "message": case["message"], "expected": case["expected"],
                "fallback": False,
                "latency_ms": latency_ms,
                "input_tokens": outcome.input_tokens,
                "output_tokens": outcome.output_tokens,
                "predicted": {
                    "intent": validated.intent,
                    "route": validated.route,
                    "continuity": validated.decision.continuity,
                    "deliverable_format": validated.decision.deliverable_format,
                    "clarification_question": validated.clarification_question,
                },
                "notes": validated.notes,
            }
        records.append(record)
        expected = case["expected"]
        predicted = record["predicted"] or {}
        print(
            f"[{index}/{len(cases)}] {case['id']} "
            f"{'OK' if predicted.get('intent') == expected['intent'] and predicted.get('route') == expected['route'] else 'DIFF'} "
            f"expected={expected['intent']}/{expected['route']} "
            f"predicted={predicted.get('intent')}/{predicted.get('route')}",
            flush=True,
        )

    report = {
        "model": model_config.get("name"),
        "cases_file": args.cases,
        "summary": _summary(records),
        "records": records,
    }
    Path(args.report).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2), flush=True)
    print(f"报告已写入 {args.report}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
