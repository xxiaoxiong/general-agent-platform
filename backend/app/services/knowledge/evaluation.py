"""检索质量评测（任务书 §13）。

确定性指标：Recall@5/10、Precision@K、MRR@10、nDCG@10、No-answer Accuracy、
Citation Precision、P50/P95 延迟。不把检索分数伪装成"回答准确率"。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .retrieval import KnowledgeRetrievalService, RetrievalOptions


@dataclass
class EvaluationCase:
    question: str
    expected_document_ids: List[int] = field(default_factory=list)
    expected_pages: List[int] = field(default_factory=list)
    relevant_chunk_ids: List[str] = field(default_factory=list)
    expected_answer_points: List[str] = field(default_factory=list)
    no_answer: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EvaluationCase":
        return cls(
            question=str(data.get("question") or ""),
            expected_document_ids=[int(x) for x in data.get("expected_document_ids") or []],
            expected_pages=[int(x) for x in data.get("expected_pages") or []],
            relevant_chunk_ids=[str(x) for x in data.get("relevant_chunk_ids") or []],
            expected_answer_points=[str(x) for x in data.get("expected_answer_points") or []],
            no_answer=bool(data.get("no_answer")),
        )


def _is_relevant(item, case: EvaluationCase) -> bool:
    citation = item.citation or {}
    if case.relevant_chunk_ids:
        return str(citation.get("chunk_id") or "") in set(case.relevant_chunk_ids)
    document_id = int(citation.get("document_id") or 0)
    if case.expected_document_ids and document_id not in case.expected_document_ids:
        return False
    if case.expected_pages:
        page_start = citation.get("page_start")
        page_end = citation.get("page_end") or page_start
        if page_start is None:
            # 非分页来源（Sheet/Slide）以来源存在为准
            return bool(citation.get("sheet_name") or citation.get("slide_number"))
        return any(
            int(page_start) <= page <= int(page_end or page_start)
            for page in case.expected_pages
        )
    return bool(case.expected_document_ids)


def _ndcg(relevances: Sequence[int], k: int = 10) -> float:
    dcg = 0.0
    for index, rel in enumerate(relevances[:k]):
        if rel:
            dcg += 1.0 / math.log2(index + 2)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(sum(1 for r in relevances if r), k)))
    return dcg / ideal if ideal else 0.0


def _percentile(values: List[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((percent / 100.0) * (len(ordered) - 1)))))
    return round(ordered[index], 2)


async def evaluate(
    db,
    *,
    knowledge_base_ids: Sequence[int],
    cases: Sequence[EvaluationCase],
    mode: str = "hybrid",
    top_k: int = 5,
    rerank: Optional[bool] = None,
) -> Dict[str, Any]:
    """逐条检索并汇总指标（每条用例使用独立 Retrieval，避免缓存假象）。"""
    service = KnowledgeRetrievalService(db)
    details: List[Dict[str, Any]] = []
    latencies: List[float] = []
    recall5_hits = 0
    recall10_hits = 0
    mrr_values: List[float] = []
    ndcg_values: List[float] = []
    precision_values: List[float] = []
    citation_hits = 0
    citation_total = 0
    no_answer_total = 0
    no_answer_correct = 0
    answered_total = 0

    for case in cases:
        started = time.monotonic()
        result = await service.search(
            knowledge_base_ids=knowledge_base_ids,
            query=case.question,
            options=RetrievalOptions(top_k=top_k, mode=mode, rerank=rerank),
        )
        latency = (time.monotonic() - started) * 1000
        latencies.append(latency)
        relevances = [1 if _is_relevant(item, case) else 0 for item in result.items]
        top5 = 1 if any(relevances[:5]) else 0
        top10 = 1 if any(relevances[:10]) else 0
        recall5_hits += top5
        recall10_hits += top10
        first_rank = next((i + 1 for i, rel in enumerate(relevances) if rel), 0)
        mrr_values.append(1.0 / first_rank if first_rank else 0.0)
        ndcg_values.append(_ndcg(relevances, 10))
        if result.items:
            answered_total += 1
            precision_values.append(sum(relevances[:top_k]) / max(1, min(top_k, len(result.items))))
            for item, rel in zip(result.items[:top_k], relevances[:top_k]):
                citation_total += 1
                citation = item.citation or {}
                if rel and citation.get("document_id") and citation.get("filename"):
                    citation_hits += 1
        if case.no_answer:
            no_answer_total += 1
            # 无答案：返回空或没有任何相关命中都视为正确拒答
            if not result.items or not any(relevances):
                no_answer_correct += 1
        details.append({
            "question": case.question,
            "expected_document_ids": case.expected_document_ids,
            "expected_pages": case.expected_pages,
            "no_answer": case.no_answer,
            "results": [
                {
                    "chunk_id": item.chunk_id,
                    "document_id": (item.citation or {}).get("document_id"),
                    "filename": (item.citation or {}).get("filename"),
                    "page_start": (item.citation or {}).get("page_start"),
                    "score": round(item.score, 6),
                    "rrf": round(item.rrf_score, 6),
                    "dense": None if item.dense_score is None else round(item.dense_score, 6),
                    "bm25": None if item.bm25_score is None else round(item.bm25_score, 6),
                    "relevant": bool(relevances[index]),
                }
                for index, item in enumerate(result.items)
            ],
            "latency_ms": round(latency, 2),
            "retrieval_id": result.retrieval_id,
        })

    total = max(1, len(cases))
    return {
        "case_count": len(cases),
        "retrieval_mode": mode,
        "top_k": top_k,
        "recall_at_5": round(recall5_hits / total, 4),
        "recall_at_10": round(recall10_hits / total, 4),
        "precision_at_k": round(
            sum(precision_values) / max(1, len(precision_values)), 4
        ),
        "mrr_at_10": round(sum(mrr_values) / total, 4),
        "ndcg_at_10": round(sum(ndcg_values) / total, 4),
        "citation_precision": round(citation_hits / max(1, citation_total), 4),
        "no_answer_accuracy": (
            round(no_answer_correct / no_answer_total, 4) if no_answer_total else None
        ),
        "answered_cases": answered_total,
        "latency_p50_ms": _percentile(latencies, 50),
        "latency_p95_ms": _percentile(latencies, 95),
        "details": details,
    }
