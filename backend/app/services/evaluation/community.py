"""社区通用测评数据集：内置种子（data/community_datasets.json）导入。

数据来自社区公开基准（GSM8K / GAIA 等），JSON 内记录出处与 license；
seed 幂等：按数据集名跳过已存在的，不重复导入。
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evaluation import (
    EvalDataset, EvalDatasetItem, EvalRunItem, EvalSuite, EvalSuiteDataset,
)

logger = logging.getLogger(__name__)

_DATA_FILE = Path(__file__).resolve().parent / "data" / "community_datasets.json"


def load_community_datasets() -> List[Dict[str, Any]]:
    """读取内置社区数据集定义（纯函数，便于测试）。"""
    if not _DATA_FILE.exists():
        return []
    with _DATA_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return list(data.get("datasets") or [])


async def seed_community_datasets(db: AsyncSession,
                                  created_by: int | None = None) -> Dict[str, Any]:
    """把内置社区数据集导入为 EvalDataset（按 name 幂等）。

    - 不存在：创建数据集 + 用例；
    - 已存在且来源为 community：整体刷新元数据与用例（便于随版本升级基准）；
    - 已存在但为手工/导入来源：跳过，不覆盖用户数据。
    """
    defs = load_community_datasets()
    created, updated, skipped = [], [], []
    seeded_ids: List[int] = []
    for d in defs:
        name = str(d.get("name") or "").strip()
        if not name:
            continue
        exists = (await db.execute(
            select(EvalDataset).where(EvalDataset.name == name))).scalar_one_or_none()
        if exists is not None and exists.source != "community":
            skipped.append(name)
            continue
        if exists is None:
            dataset = EvalDataset(
                name=name,
                description=str(d.get("description") or ""),
                source="community",
                category=str(d.get("category") or "general"),
                scoring_method=str(d.get("scoring_method") or "llm_judge"),
                reference=str(d.get("reference") or "")[:500],
                created_by=created_by,
            )
            db.add(dataset)
            await db.flush()
            created.append(name)
        else:
            dataset = exists
            dataset.description = str(d.get("description") or dataset.description)
            dataset.category = str(d.get("category") or dataset.category)
            dataset.scoring_method = str(
                d.get("scoring_method") or dataset.scoring_method)
            dataset.reference = str(d.get("reference") or dataset.reference)[:500]
            # 整体替换用例（保持位置与基准版本一致）；历史结果行只留快照，先解除外键引用
            await db.execute(
                update(EvalRunItem).where(EvalRunItem.dataset_item_id.in_(
                    select(EvalDatasetItem.id).where(
                        EvalDatasetItem.dataset_id == dataset.id)
                )).values(dataset_item_id=None))
            await db.execute(delete(EvalDatasetItem).where(
                EvalDatasetItem.dataset_id == dataset.id))
            updated.append(name)
        for pos, it in enumerate(d.get("items") or []):
            db.add(EvalDatasetItem(
                dataset_id=dataset.id,
                position=int(it.get("position") if it.get("position") is not None else pos),
                input=str(it.get("input") or ""),
                expected=str(it.get("expected") or ""),
                criteria=str(it.get("criteria") or ""),
                scoring_method=(it.get("scoring_method") or None),
                scoring_config=dict(it.get("scoring_config") or {}),
                item_metadata=dict(it.get("metadata") or {}),
                complexity=str(it.get("complexity") or "L2"),
                task_type=str(it.get("task_type") or "direct_qa"),
                capability=(it.get("capability") or None),
                required_tools=list(it.get("required_tools") or []),
                expected_min_steps=it.get("expected_min_steps"),
                turns=list(it.get("turns") or []),
            ))
        seeded_ids.append(dataset.id)
    suite_info = await _ensure_default_suite(db, seeded_ids)
    await db.commit()
    return {"created": created, "updated": updated, "skipped": skipped,
            "available": [str(d.get("name")) for d in defs],
            "default_suite": suite_info}


async def _ensure_default_suite(db: AsyncSession, dataset_ids: List[int]) -> Dict[str, Any]:
    """维护默认社区能力套件（组合全部社区数据集，并在内容变化时发布新版本）。"""
    if not dataset_ids:
        return {}
    from app.services.evaluation.suites import (
        build_suite_snapshot, latest_suite_version, publish_suite_version,
    )

    name = "社区通用能力基准（默认）"
    suite = (await db.execute(
        select(EvalSuite).where(EvalSuite.name == name))).scalar_one_or_none()
    if suite is None:
        suite = EvalSuite(
            name=name, description="内置社区数据集组成的默认能力测评套件",
        )
        db.add(suite)
        await db.flush()
    await db.execute(delete(EvalSuiteDataset).where(
        EvalSuiteDataset.suite_id == suite.id))
    for pos, dataset_id in enumerate(dataset_ids):
        db.add(EvalSuiteDataset(
            suite_id=suite.id, dataset_id=dataset_id, position=pos))
    await db.flush()

    latest = await latest_suite_version(db, suite.id)
    next_no = (latest.version_no + 1) if latest is not None else 1
    snapshot = await build_suite_snapshot(db, suite, next_no)
    changed = latest is None or (latest.snapshot or {}).get("digest") != snapshot.get("digest")
    if changed:
        await publish_suite_version(db, suite)
    latest = await latest_suite_version(db, suite.id)
    return {
        "suite_id": suite.id,
        "name": suite.name,
        "version_no": latest.version_no if latest is not None else None,
        "version_changed": changed,
    }
