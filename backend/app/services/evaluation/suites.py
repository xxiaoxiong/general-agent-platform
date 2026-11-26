"""Eval Suite：多数据集组合与不可变版本快照（任务书 §4.1 / 阶段 2）。

- EvalSuite 维护组成关系（哪些 Dataset、顺序）；
- EvalSuiteVersion 固化一版完整用例内容（含 input/expected/criteria/config 等），
  Campaign 创建时只读版本快照，保证历史测评可复现；
- 用例内容摘要（sha256）用于完整性核对，防止快照被误改。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evaluation import (
    EvalDataset, EvalDatasetItem, EvalSuite, EvalSuiteDataset, EvalSuiteVersion,
)
from .capabilities import resolve_capability


def _item_snapshot(item: EvalDatasetItem) -> Dict[str, Any]:
    payload = {
        "id": item.id,
        "position": int(item.position or 0),
        "input": item.input or "",
        "expected": item.expected or "",
        "criteria": item.criteria or "",
        "scoring_method": item.scoring_method,
        "scoring_config": dict(item.scoring_config or {}),
        "complexity": item.complexity or "L2",
        "task_type": item.task_type or "direct_qa",
        "capability": resolve_capability(item.capability, item.task_type),
        "required_tools": list(item.required_tools or []),
        "expected_min_steps": item.expected_min_steps,
        "turns": list(item.turns or []),
        "metadata": dict(item.item_metadata or {}),
    }
    digest_src = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload["digest"] = hashlib.sha256(digest_src.encode("utf-8")).hexdigest()
    return payload


async def build_suite_snapshot(
    db: AsyncSession, suite: EvalSuite, version_no: int,
) -> Dict[str, Any]:
    links = (await db.execute(
        select(EvalSuiteDataset)
        .where(EvalSuiteDataset.suite_id == suite.id)
        .order_by(EvalSuiteDataset.position, EvalSuiteDataset.id)
    )).scalars().all()
    datasets: List[Dict[str, Any]] = []
    for link in links:
        dataset = (await db.execute(
            select(EvalDataset).where(EvalDataset.id == link.dataset_id)
        )).scalar_one_or_none()
        if dataset is None:
            continue
        items = (await db.execute(
            select(EvalDatasetItem)
            .where(EvalDatasetItem.dataset_id == dataset.id)
            .order_by(EvalDatasetItem.position, EvalDatasetItem.id)
        )).scalars().all()
        datasets.append({
            "dataset_id": dataset.id,
            "name": dataset.name,
            "description": dataset.description or "",
            "category": dataset.category,
            "scoring_method": dataset.scoring_method,
            "item_count": len(items),
            "items": [_item_snapshot(item) for item in items],
        })
    snapshot = {
        "suite_id": suite.id,
        "suite_name": suite.name,
        "version_no": version_no,
        "datasets": datasets,
        "total_items": sum(d["item_count"] for d in datasets),
        "created_at": datetime.utcnow().isoformat(),
    }
    digest_src = json.dumps(
        [{**d, "items": [{k: v for k, v in i.items() if k != "digest"} for i in d["items"]]}
         for d in datasets],
        ensure_ascii=False, sort_keys=True)
    snapshot["digest"] = hashlib.sha256(digest_src.encode("utf-8")).hexdigest()
    return snapshot


async def publish_suite_version(
    db: AsyncSession, suite: EvalSuite, *, created_by: Optional[int] = None,
) -> EvalSuiteVersion:
    last = (await db.execute(
        select(EvalSuiteVersion.version_no)
        .where(EvalSuiteVersion.suite_id == suite.id)
        .order_by(EvalSuiteVersion.version_no.desc()).limit(1)
    )).scalar_one_or_none()
    version_no = int(last or 0) + 1
    snapshot = await build_suite_snapshot(db, suite, version_no)
    if not snapshot["datasets"]:
        raise ValueError("套件没有数据集，无法发布版本")
    row = EvalSuiteVersion(
        suite_id=suite.id, version_no=version_no, snapshot=snapshot,
        created_by=created_by,
    )
    db.add(row)
    await db.flush()
    return row


async def latest_suite_version(
    db: AsyncSession, suite_id: int,
) -> Optional[EvalSuiteVersion]:
    return (await db.execute(
        select(EvalSuiteVersion)
        .where(EvalSuiteVersion.suite_id == suite_id)
        .order_by(EvalSuiteVersion.version_no.desc()).limit(1)
    )).scalar_one_or_none()


async def ensure_suite_version(
    db: AsyncSession, suite_id: int, *, created_by: Optional[int] = None,
) -> EvalSuiteVersion:
    version = await latest_suite_version(db, suite_id)
    if version is not None:
        return version
    suite = (await db.execute(
        select(EvalSuite).where(EvalSuite.id == suite_id))).scalar_one_or_none()
    if suite is None:
        raise ValueError("套件不存在")
    return await publish_suite_version(db, suite, created_by=created_by)


def suite_dataset_ids(version: EvalSuiteVersion) -> List[int]:
    return [int(d["dataset_id"]) for d in (version.snapshot or {}).get("datasets") or []]
