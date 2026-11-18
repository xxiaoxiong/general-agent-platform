"""社区通用测评数据集：内置种子（data/community_datasets.json）导入。

数据来自社区公开基准（GSM8K / GAIA 等），JSON 内记录出处与 license；
seed 幂等：按数据集名跳过已存在的，不重复导入。
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evaluation import EvalDataset, EvalDatasetItem

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
    """把内置社区数据集导入为 EvalDataset（按 name 幂等）。"""
    defs = load_community_datasets()
    created, skipped = [], []
    for d in defs:
        name = str(d.get("name") or "").strip()
        if not name:
            continue
        exists = (await db.execute(
            select(EvalDataset).where(EvalDataset.name == name))).scalar_one_or_none()
        if exists is not None:
            skipped.append(name)
            continue
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
        for pos, it in enumerate(d.get("items") or []):
            db.add(EvalDatasetItem(
                dataset_id=dataset.id,
                position=pos,
                input=str(it.get("input") or ""),
                expected=str(it.get("expected") or ""),
                criteria=str(it.get("criteria") or ""),
                scoring_method=(it.get("scoring_method") or None),
                scoring_config=dict(it.get("scoring_config") or {}),
                item_metadata=dict(it.get("metadata") or {}),
            ))
        created.append(name)
    await db.commit()
    return {"created": created, "skipped": skipped,
            "available": [str(d.get("name")) for d in defs]}
