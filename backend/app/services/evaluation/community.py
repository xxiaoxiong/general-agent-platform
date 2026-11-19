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

from app.models.evaluation import EvalDataset, EvalDatasetItem, EvalRunItem

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
                required_tools=list(it.get("required_tools") or []),
                expected_min_steps=it.get("expected_min_steps"),
                turns=list(it.get("turns") or []),
            ))
    await db.commit()
    return {"created": created, "updated": updated, "skipped": skipped,
            "available": [str(d.get("name")) for d in defs]}
