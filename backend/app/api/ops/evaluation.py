"""Agent 测评 API（v2，仅超级管理员）。

数据集管理（CRUD / 用例 / JSONL 导入 / 社区种子）+
测评任务（创建即后台执行 / 列表 / 详情 / 结果明细 / 取消 / 恢复 / Markdown 导出）。

列表查询一律窄列投影（大 Text 列不参与排序与全行加载，见 AGENTS.md 陷阱 #2），
预览用 func.substr 截断，全文经详情端点获取。
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_superuser
from app.core.database import get_db
from app.models.agent import Agent
from app.models.evaluation import (
    EvalDataset, EvalDatasetItem, EvalGrade, EvalRun, EvalRunItem,
    EvalSuite, EvalSuiteDataset, EvalSuiteVersion,
)

router = APIRouter(prefix="/evaluation", tags=["evaluation"],
                   dependencies=[Depends(get_current_superuser)])


# ────────────────────────────── 数据集 ──────────────────────────────


class DatasetCreate(BaseModel):
    name: str = Field(..., max_length=120)
    description: str = ""
    category: str = "general"
    scoring_method: str = "llm_judge"
    reference: Optional[str] = None
    items: List[Dict[str, Any]] = []


class DatasetUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=120)
    description: Optional[str] = None
    category: Optional[str] = None
    scoring_method: Optional[str] = None
    reference: Optional[str] = None


class ItemCreate(BaseModel):
    input: str
    expected: str = ""
    criteria: str = ""
    scoring_method: Optional[str] = None
    scoring_config: Dict[str, Any] = {}
    item_metadata: Dict[str, Any] = Field(default_factory=dict)
    # 复杂任务体系（客观能力对照维度）
    complexity: str = "L2"                 # L1 简单 / L2 中等 / L3 复杂 / L4 困难
    task_type: str = "direct_qa"           # direct_qa / multi_step_reasoning / ...
    capability: Optional[str] = None       # 能力维度（空=按 task_type 映射）
    required_tools: List[str] = Field(default_factory=list)
    expected_min_steps: Optional[int] = None
    turns: List[str] = Field(default_factory=list)   # 多轮任务后续用户消息


class DatasetImportRequest(BaseModel):
    """JSONL 文本（每行一个 JSON）或直接传数组。"""
    name: Optional[str] = None
    description: str = ""
    category: str = "general"
    scoring_method: str = "llm_judge"
    reference: Optional[str] = None
    dataset_id: Optional[int] = None   # 追加到已有数据集
    content: Optional[str] = None      # JSONL 文本
    items: Optional[List[Dict[str, Any]]] = None


def _dataset_dict(d: EvalDataset, item_count: Optional[int] = None) -> Dict[str, Any]:
    return {
        "id": d.id,
        "name": d.name,
        "description": d.description or "",
        "source": d.source,
        "category": d.category,
        "scoring_method": d.scoring_method,
        "reference": d.reference,
        "item_count": item_count,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


def _item_dict(it: EvalDatasetItem) -> Dict[str, Any]:
    return {
        "id": it.id,
        "dataset_id": it.dataset_id,
        "position": it.position,
        "input": it.input,
        "expected": it.expected or "",
        "criteria": it.criteria or "",
        "scoring_method": it.scoring_method,
        "scoring_config": it.scoring_config or {},
        "metadata": it.item_metadata or {},
        "complexity": it.complexity or "L2",
        "task_type": it.task_type or "direct_qa",
        "capability": it.capability,
        "required_tools": list(it.required_tools or []),
        "expected_min_steps": it.expected_min_steps,
        "turns": list(it.turns or []),
    }


def _effective_item_statuses(item: Any) -> Dict[str, str]:
    """旧数据兼容：新状态列为 pending 时按 legacy status 推导展示口径。"""
    outcome = getattr(item, "outcome", None) or "pending"
    execution = getattr(item, "execution_status", None) or "pending"
    verification = getattr(item, "verification_status", None) or "pending"
    grading = getattr(item, "grading_status", None) or "pending"
    status = (getattr(item, "status", None) or "pending").lower()
    if outcome == "pending":
        outcome = {
            "passed": "pass", "failed": "fail", "error": "inconclusive",
            "skipped": "cancelled", "unscored": "unscored",
        }.get(status, "pending")
    if execution == "pending" and status in ("passed", "failed"):
        execution = "succeeded"
    if verification == "pending" and status in ("passed", "failed"):
        verification = "unscored"
    if grading == "pending" and status in ("passed", "failed"):
        grading = "unscored"
    return {
        "outcome": outcome,
        "execution_status": execution,
        "verification_status": verification,
        "grading_status": grading,
    }


def _parse_items(raw: List[Dict[str, Any]]) -> List[EvalDatasetItem]:
    """容错解析导入用例：input/question/prompt 字段皆可，复杂维度缺省取默认。"""
    out = []
    for pos, r in enumerate(raw):
        text = str(r.get("input") or r.get("question") or r.get("prompt") or "").strip()
        if not text:
            continue
        meta = dict(r.get("metadata") or r.get("item_metadata") or {})
        out.append(EvalDatasetItem(
            position=int(r.get("position") if r.get("position") is not None else pos),
            input=text,
            expected=str(r.get("expected") or r.get("answer") or ""),
            criteria=str(r.get("criteria") or ""),
            scoring_method=(r.get("scoring_method") or None),
            scoring_config=dict(r.get("scoring_config") or {}),
            item_metadata=meta,
            complexity=str(r.get("complexity") or meta.get("complexity") or "L2"),
            task_type=str(r.get("task_type") or meta.get("task_type") or "direct_qa"),
            capability=(r.get("capability") or meta.get("capability") or None),
            required_tools=list(r.get("required_tools") or meta.get("required_tools") or []),
            expected_min_steps=(r.get("expected_min_steps")
                                if r.get("expected_min_steps") is not None
                                else meta.get("expected_min_steps")),
            turns=list(r.get("turns") or meta.get("turns") or []),
        ))
    return out


@router.get("/datasets")
async def list_datasets(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    datasets = (await db.execute(
        select(EvalDataset).order_by(EvalDataset.id))).scalars().all()
    counts = dict((await db.execute(
        select(EvalDatasetItem.dataset_id, func.count())
        .group_by(EvalDatasetItem.dataset_id))).all())
    return {"items": [_dataset_dict(d, counts.get(d.id, 0)) for d in datasets]}


@router.post("/datasets")
async def create_dataset(body: DatasetCreate,
                         db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    exists = (await db.execute(
        select(EvalDataset).where(EvalDataset.name == body.name))).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(409, "数据集名称已存在")
    dataset = EvalDataset(
        name=body.name, description=body.description, source="manual",
        category=body.category, scoring_method=body.scoring_method,
        reference=body.reference,
    )
    db.add(dataset)
    await db.flush()
    for it in _parse_items(body.items):
        it.dataset_id = dataset.id
        db.add(it)
    await db.commit()
    await db.refresh(dataset)
    return _dataset_dict(dataset, len(body.items))


@router.get("/datasets/{dataset_id}")
async def get_dataset(dataset_id: int,
                      db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    dataset = (await db.execute(
        select(EvalDataset).where(EvalDataset.id == dataset_id))).scalar_one_or_none()
    if dataset is None:
        raise HTTPException(404, "数据集不存在")
    items = (await db.execute(
        select(EvalDatasetItem).where(EvalDatasetItem.dataset_id == dataset_id)
        .order_by(EvalDatasetItem.position))).scalars().all()
    return {**_dataset_dict(dataset, len(items)),
            "items": [_item_dict(i) for i in items]}


@router.put("/datasets/{dataset_id}")
async def update_dataset(dataset_id: int, body: DatasetUpdate,
                         db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    dataset = (await db.execute(
        select(EvalDataset).where(EvalDataset.id == dataset_id))).scalar_one_or_none()
    if dataset is None:
        raise HTTPException(404, "数据集不存在")
    for field in ("name", "description", "category", "scoring_method", "reference"):
        val = getattr(body, field)
        if val is not None:
            setattr(dataset, field, val)
    await db.commit()
    await db.refresh(dataset)
    return _dataset_dict(dataset)


@router.delete("/datasets/{dataset_id}")
async def delete_dataset(dataset_id: int,
                         db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    dataset = (await db.execute(
        select(EvalDataset).where(EvalDataset.id == dataset_id))).scalar_one_or_none()
    if dataset is None:
        raise HTTPException(404, "数据集不存在")
    used = (await db.execute(
        select(func.count()).select_from(EvalRun)
        .where(EvalRun.dataset_id == dataset_id))).scalar() or 0
    if used:
        raise HTTPException(409, f"数据集已被 {used} 个测评任务使用，无法删除")
    in_suite = (await db.execute(
        select(func.count()).select_from(EvalSuiteDataset)
        .where(EvalSuiteDataset.dataset_id == dataset_id))).scalar() or 0
    if in_suite:
        raise HTTPException(409, f"数据集被 {in_suite} 个测评套件引用，无法删除")
    await db.execute(
        EvalDatasetItem.__table__.delete().where(
            EvalDatasetItem.dataset_id == dataset_id))
    await db.delete(dataset)
    await db.commit()
    return {"deleted": True}


@router.post("/datasets/{dataset_id}/items")
async def add_item(dataset_id: int, body: ItemCreate,
                   db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    dataset = (await db.execute(
        select(EvalDataset).where(EvalDataset.id == dataset_id))).scalar_one_or_none()
    if dataset is None:
        raise HTTPException(404, "数据集不存在")
    max_pos = (await db.execute(
        select(func.max(EvalDatasetItem.position))
        .where(EvalDatasetItem.dataset_id == dataset_id))).scalar()
    item = EvalDatasetItem(
        dataset_id=dataset_id,
        position=int(max_pos) + 1 if max_pos is not None else 0,
        input=body.input, expected=body.expected, criteria=body.criteria,
        scoring_method=body.scoring_method,
        scoring_config=body.scoring_config,
        item_metadata=body.item_metadata,
        complexity=body.complexity, task_type=body.task_type,
        capability=body.capability,
        required_tools=body.required_tools,
        expected_min_steps=body.expected_min_steps,
        turns=body.turns,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return _item_dict(item)


@router.put("/datasets/{dataset_id}/items/{item_id}")
async def update_item(dataset_id: int, item_id: int, body: ItemCreate,
                      db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    item = (await db.execute(
        select(EvalDatasetItem).where(
            EvalDatasetItem.id == item_id,
            EvalDatasetItem.dataset_id == dataset_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "用例不存在")
    item.input = body.input
    item.expected = body.expected
    item.criteria = body.criteria
    item.scoring_method = body.scoring_method
    item.scoring_config = body.scoring_config
    item.item_metadata = body.item_metadata
    item.complexity = body.complexity
    item.task_type = body.task_type
    item.capability = body.capability
    item.required_tools = body.required_tools
    item.expected_min_steps = body.expected_min_steps
    item.turns = body.turns
    await db.commit()
    await db.refresh(item)
    return _item_dict(item)


@router.delete("/datasets/{dataset_id}/items/{item_id}")
async def delete_item(dataset_id: int, item_id: int,
                      db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    item = (await db.execute(
        select(EvalDatasetItem).where(
            EvalDatasetItem.id == item_id,
            EvalDatasetItem.dataset_id == dataset_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "用例不存在")
    await db.delete(item)
    await db.commit()
    return {"deleted": True}


@router.post("/datasets/import")
async def import_items(body: DatasetImportRequest,
                       db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """JSONL 文本或数组导入：指定 dataset_id 追加，否则按 name 新建数据集。"""
    import json as _json

    raw: List[Dict[str, Any]] = list(body.items or [])
    if body.content:
        for line in body.content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = _json.loads(line)
            except _json.JSONDecodeError as e:
                raise HTTPException(422, f"JSONL 解析失败: {e}")
            if isinstance(obj, dict):
                raw.append(obj)
    parsed = _parse_items(raw)
    if not parsed:
        raise HTTPException(422, "没有可导入的用例（至少需要 input 字段）")

    if body.dataset_id:
        dataset = (await db.execute(
            select(EvalDataset).where(EvalDataset.id == body.dataset_id))
        ).scalar_one_or_none()
        if dataset is None:
            raise HTTPException(404, "数据集不存在")
        max_pos = (await db.execute(
            select(func.max(EvalDatasetItem.position))
            .where(EvalDatasetItem.dataset_id == dataset.id))).scalar()
        base = int(max_pos) + 1 if max_pos is not None else 0
    else:
        name = (body.name or "").strip() or f"导入数据集-{datetime.utcnow():%Y%m%d%H%M%S}"
        exists = (await db.execute(
            select(EvalDataset).where(EvalDataset.name == name))).scalar_one_or_none()
        if exists is not None:
            raise HTTPException(409, "数据集名称已存在")
        dataset = EvalDataset(
            name=name, description=body.description, source="import",
            category=body.category, scoring_method=body.scoring_method,
            reference=body.reference,
        )
        db.add(dataset)
        await db.flush()
        base = 0
    for i, it in enumerate(parsed):
        it.dataset_id = dataset.id
        it.position = base + i
        db.add(it)
    await db.commit()
    return {"dataset_id": dataset.id, "imported": len(parsed)}


@router.post("/datasets/seed-community")
async def seed_community(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """一键导入内置社区通用数据集（GSM8K / BFCL 等，按名称幂等）。"""
    from app.services.evaluation.community import seed_community_datasets

    result = await seed_community_datasets(db)
    return result


# ────────────────────────────── 测评套件 ──────────────────────────────


class SuiteCreate(BaseModel):
    name: str = Field(..., max_length=120)
    description: str = ""
    dataset_ids: List[int] = Field(default_factory=list)


class SuiteUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=120)
    description: Optional[str] = None
    dataset_ids: Optional[List[int]] = None


def _suite_dict(
    suite: EvalSuite, dataset_ids: Optional[List[int]] = None,
    version: Optional[EvalSuiteVersion] = None,
) -> Dict[str, Any]:
    return {
        "id": suite.id,
        "name": suite.name,
        "description": suite.description or "",
        "dataset_ids": dataset_ids or [],
        "latest_version": {
            "id": version.id,
            "version_no": version.version_no,
            "digest": (version.snapshot or {}).get("digest"),
            "total_items": (version.snapshot or {}).get("total_items"),
            "created_at": version.created_at.isoformat() if version.created_at else None,
        } if version is not None else None,
        "created_at": suite.created_at.isoformat() if suite.created_at else None,
        "updated_at": suite.updated_at.isoformat() if suite.updated_at else None,
    }


async def _suite_dataset_ids(db: AsyncSession, suite_id: int) -> List[int]:
    return list((await db.execute(
        select(EvalSuiteDataset.dataset_id)
        .where(EvalSuiteDataset.suite_id == suite_id)
        .order_by(EvalSuiteDataset.position, EvalSuiteDataset.id)
    )).scalars().all())


async def _replace_suite_datasets(
    db: AsyncSession, suite_id: int, dataset_ids: List[int],
) -> None:
    await db.execute(
        EvalSuiteDataset.__table__.delete().where(
            EvalSuiteDataset.suite_id == suite_id))
    for pos, dataset_id in enumerate(dict.fromkeys(int(d) for d in dataset_ids)):
        exists = (await db.execute(
            select(EvalDataset.id).where(EvalDataset.id == dataset_id))).scalar()
        if exists is None:
            raise HTTPException(422, f"数据集不存在: {dataset_id}")
        db.add(EvalSuiteDataset(suite_id=suite_id, dataset_id=dataset_id, position=pos))


@router.get("/suites")
async def list_suites(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    from app.services.evaluation.suites import latest_suite_version

    suites = (await db.execute(
        select(EvalSuite).order_by(EvalSuite.id))).scalars().all()
    items = []
    for suite in suites:
        dataset_ids = await _suite_dataset_ids(db, suite.id)
        version = await latest_suite_version(db, suite.id)
        items.append(_suite_dict(suite, dataset_ids, version))
    return {"items": items}


@router.post("/suites")
async def create_suite(body: SuiteCreate,
                       db: AsyncSession = Depends(get_db),
                       current_user=Depends(get_current_superuser)) -> Dict[str, Any]:
    if not body.dataset_ids:
        raise HTTPException(422, "套件至少包含一个数据集")
    exists = (await db.execute(
        select(EvalSuite).where(EvalSuite.name == body.name))).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(409, "套件名称已存在")
    suite = EvalSuite(name=body.name, description=body.description,
                      created_by=current_user.id)
    db.add(suite)
    await db.flush()
    await _replace_suite_datasets(db, suite.id, body.dataset_ids)
    await db.commit()
    await db.refresh(suite)
    return _suite_dict(suite, await _suite_dataset_ids(db, suite.id))


@router.get("/suites/{suite_id}")
async def get_suite(suite_id: int,
                    db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    from app.services.evaluation.suites import latest_suite_version

    suite = (await db.execute(
        select(EvalSuite).where(EvalSuite.id == suite_id))).scalar_one_or_none()
    if suite is None:
        raise HTTPException(404, "套件不存在")
    dataset_ids = await _suite_dataset_ids(db, suite.id)
    version = await latest_suite_version(db, suite.id)
    return {
        **_suite_dict(suite, dataset_ids, version),
        "versions": [
            {
                "id": v.id, "version_no": v.version_no,
                "digest": (v.snapshot or {}).get("digest"),
                "total_items": (v.snapshot or {}).get("total_items"),
                "created_at": v.created_at.isoformat() if v.created_at else None,
            }
            for v in (await db.execute(
                select(EvalSuiteVersion)
                .where(EvalSuiteVersion.suite_id == suite.id)
                .order_by(EvalSuiteVersion.version_no.desc()))).scalars().all()
        ],
    }


@router.put("/suites/{suite_id}")
async def update_suite(suite_id: int, body: SuiteUpdate,
                       db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    from app.services.evaluation.suites import latest_suite_version

    suite = (await db.execute(
        select(EvalSuite).where(EvalSuite.id == suite_id))).scalar_one_or_none()
    if suite is None:
        raise HTTPException(404, "套件不存在")
    if body.name is not None:
        conflict = (await db.execute(
            select(EvalSuite.id).where(
                EvalSuite.name == body.name, EvalSuite.id != suite_id))).scalar()
        if conflict is not None:
            raise HTTPException(409, "套件名称已存在")
        suite.name = body.name
    if body.description is not None:
        suite.description = body.description
    if body.dataset_ids is not None:
        if not body.dataset_ids:
            raise HTTPException(422, "套件至少包含一个数据集")
        await _replace_suite_datasets(db, suite.id, body.dataset_ids)
    await db.commit()
    await db.refresh(suite)
    return _suite_dict(suite, await _suite_dataset_ids(db, suite.id),
                       await latest_suite_version(db, suite.id))


@router.delete("/suites/{suite_id}")
async def delete_suite(suite_id: int,
                       db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    suite = (await db.execute(
        select(EvalSuite).where(EvalSuite.id == suite_id))).scalar_one_or_none()
    if suite is None:
        raise HTTPException(404, "套件不存在")
    used = (await db.execute(
        select(func.count()).select_from(EvalRun)
        .where(EvalRun.suite_id == suite_id))).scalar() or 0
    if used:
        raise HTTPException(409, f"套件已被 {used} 个测评任务使用，无法删除")
    await db.execute(
        EvalSuiteDataset.__table__.delete().where(
            EvalSuiteDataset.suite_id == suite_id))
    await db.execute(
        EvalSuiteVersion.__table__.delete().where(
            EvalSuiteVersion.suite_id == suite_id))
    await db.delete(suite)
    await db.commit()
    return {"deleted": True}


@router.post("/suites/{suite_id}/versions")
async def publish_suite(suite_id: int,
                        db: AsyncSession = Depends(get_db),
                        current_user=Depends(get_current_superuser)) -> Dict[str, Any]:
    from app.services.evaluation.suites import publish_suite_version

    suite = (await db.execute(
        select(EvalSuite).where(EvalSuite.id == suite_id))).scalar_one_or_none()
    if suite is None:
        raise HTTPException(404, "套件不存在")
    try:
        version = await publish_suite_version(
            db, suite, created_by=current_user.id)
    except ValueError as e:
        raise HTTPException(422, str(e))
    await db.commit()
    return {
        "id": version.id, "suite_id": version.suite_id,
        "version_no": version.version_no,
        "digest": (version.snapshot or {}).get("digest"),
        "total_items": (version.snapshot or {}).get("total_items"),
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }


# ────────────────────────────── 测评任务 ──────────────────────────────


class RunCreateRequest(BaseModel):
    name: Optional[str] = None
    dataset_id: Optional[int] = None
    suite_id: Optional[int] = None
    suite_version_id: Optional[int] = None
    agent_id: int
    agent_version_id: Optional[int] = None
    approval_mode: str = "auto"
    use_memory: bool = False
    timeout_s: int = Field(180, ge=10, le=3600)
    limit: int = Field(0, ge=0, le=500)
    # Campaign 级资源上限（0=不限）：Trial 之间检查，超出后跳过未执行 Trial
    campaign_timeout_s: int = Field(0, ge=0, le=86400)
    campaign_max_tokens: int = Field(0, ge=0)
    # 同一 Case 重复执行次数（稳定性：pass@k / pass^k）
    trials_per_case: int = Field(1, ge=1, le=10)


def _run_dict(r: EvalRun) -> Dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "dataset_id": r.dataset_id,
        "suite_id": r.suite_id,
        "suite_version_id": r.suite_version_id,
        "trials_per_case": int(r.trials_per_case or 1),
        "agent_id": r.agent_id,
        "agent_version_id": r.agent_version_id,
        "status": r.status,
        "config": r.config or {},
        "environment": r.environment or {},
        "aggregate": r.aggregate or {},
        "error": r.error,
        "attempt": int(r.attempt or 0),
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.post("/runs")
async def create_run(body: RunCreateRequest, background: BackgroundTasks,
                     db: AsyncSession = Depends(get_db),
                     current_user=Depends(get_current_superuser)) -> Dict[str, Any]:
    from app.services.evaluation.runner import (
        create_eval_run, start_eval_run_background,
    )

    name = (body.name or "").strip() or (
        f"测评-{datetime.utcnow():%Y%m%d-%H%M%S}")
    if not body.dataset_id and not (body.suite_id or body.suite_version_id):
        raise HTTPException(422, "dataset_id 或 suite_id 至少提供一个")
    try:
        eval_run = await create_eval_run(
            db, name=name, dataset_id=body.dataset_id, agent_id=body.agent_id,
            created_by=current_user.id,
            suite_id=body.suite_id,
            suite_version_id=body.suite_version_id,
            agent_version_id=body.agent_version_id,
            trials_per_case=body.trials_per_case,
            config={
                "approval_mode": body.approval_mode,
                "use_memory": body.use_memory,
                "timeout_s": body.timeout_s,
                "limit": body.limit,
                "trials_per_case": body.trials_per_case,
                "campaign_timeout_s": body.campaign_timeout_s,
                "campaign_max_tokens": body.campaign_max_tokens,
            },
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    # 后台任务使用自己的数据库会话，先提交任务和用例快照。
    await db.commit()
    background.add_task(start_eval_run_background, eval_run.id)
    return _run_dict(eval_run)


@router.get("/runs")
async def list_runs(
    dataset_id: Optional[int] = None,
    agent_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    # 窄列投影 + Python 侧排序（陷阱 #2）
    stmt = select(
        EvalRun.id, EvalRun.name, EvalRun.dataset_id, EvalRun.agent_id,
        EvalRun.suite_id, EvalRun.suite_version_id, EvalRun.trials_per_case,
        EvalRun.agent_version_id, EvalRun.status, EvalRun.aggregate,
        EvalRun.config, EvalRun.environment, EvalRun.error,
        EvalRun.attempt, EvalRun.started_at, EvalRun.finished_at, EvalRun.created_at,
    )
    if dataset_id:
        stmt = stmt.where(EvalRun.dataset_id == dataset_id)
    if agent_id:
        stmt = stmt.where(EvalRun.agent_id == agent_id)
    rows = (await db.execute(stmt)).all()
    rows = sorted(rows, key=lambda r: r.created_at or datetime.min, reverse=True)
    return {
        "items": [
            {
                "id": r.id, "name": r.name, "dataset_id": r.dataset_id,
                "agent_id": r.agent_id,
                "suite_id": r.suite_id, "suite_version_id": r.suite_version_id,
                "trials_per_case": int(r.trials_per_case or 1),
                "agent_version_id": r.agent_version_id,
                "status": r.status,
                "aggregate": r.aggregate or {}, "config": r.config or {},
                "environment": r.environment or {},
                "error": r.error, "attempt": int(r.attempt or 0),
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: int,
                  db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    run = (await db.execute(
        select(EvalRun).where(EvalRun.id == run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "测评任务不存在")
    dataset = (await db.execute(
        select(EvalDataset.name).where(EvalDataset.id == run.dataset_id))).scalar()
    agent = (await db.execute(
        select(Agent.name).where(Agent.id == run.agent_id))).scalar()
    suite_name = None
    suite_version_no = None
    if run.suite_id:
        suite_name = (await db.execute(
            select(EvalSuite.name).where(EvalSuite.id == run.suite_id))).scalar()
    if run.suite_version_id:
        suite_version_no = (await db.execute(
            select(EvalSuiteVersion.version_no)
            .where(EvalSuiteVersion.id == run.suite_version_id))).scalar()
    return {
        **_run_dict(run), "dataset_name": dataset, "agent_name": agent,
        "suite_name": suite_name, "suite_version_no": suite_version_no,
    }


@router.get("/runs/{run_id}/items")
async def list_run_items(
    run_id: int,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    run = (await db.execute(
        select(EvalRun.id).where(EvalRun.id == run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "测评任务不存在")
    # 窄列 + substr 预览（大 Text 列不全量加载；stats 为小 JSON，可整列取）
    stmt = select(
        EvalRunItem.id, EvalRunItem.dataset_item_id, EvalRunItem.dataset_id,
        EvalRunItem.position, EvalRunItem.trial_index,
        EvalRunItem.status, EvalRunItem.execution_status,
        EvalRunItem.verification_status, EvalRunItem.grading_status,
        EvalRunItem.outcome,
        EvalRunItem.score, EvalRunItem.passed,
        EvalRunItem.run_id, EvalRunItem.error,
        EvalRunItem.snap_complexity, EvalRunItem.snap_task_type,
        EvalRunItem.snap_capability, EvalRunItem.stats,
        func.substr(EvalRunItem.snap_input, 1, 160).label("input_preview"),
        func.substr(EvalRunItem.actual_output, 1, 160).label("output_preview"),
    ).where(EvalRunItem.eval_run_id == run_id)
    if status:
        stmt = stmt.where(EvalRunItem.status == status)
    rows = (await db.execute(stmt.order_by(EvalRunItem.position))).all()
    return {
        "items": [
            {
                "id": r.id, "dataset_item_id": r.dataset_item_id,
                "dataset_id": r.dataset_id,
                "position": r.position, "trial_index": int(r.trial_index or 0),
                "status": r.status,
                **_effective_item_statuses(r),
                "score": r.score, "passed": r.passed, "run_id": r.run_id,
                "error": r.error,
                "complexity": r.snap_complexity or "L2",
                "task_type": r.snap_task_type or "direct_qa",
                "capability": r.snap_capability,
                "steps": (r.stats or {}).get("steps"),
                "tool_calls": (r.stats or {}).get("tool_calls"),
                "output_chars": (r.stats or {}).get("output_chars"),
                "used_tools": (r.stats or {}).get("used_tools") or [],
                "missing_tools": (r.stats or {}).get("missing_tools") or [],
                "artifacts": (r.stats or {}).get("artifacts") or [],
                "input_preview": (r.input_preview or "").replace("\n", " "),
                "output_preview": (r.output_preview or "").replace("\n", " "),
            }
            for r in rows
        ]
    }


@router.get("/run-items/{item_id}")
async def get_run_item(item_id: int,
                       db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    item = (await db.execute(
        select(EvalRunItem).where(EvalRunItem.id == item_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "测评结果不存在")
    grades = (await db.execute(
        select(EvalGrade).where(EvalGrade.trial_id == item_id)
        .order_by(EvalGrade.id.desc()).limit(20))).scalars().all()
    return {
        "id": item.id,
        "eval_run_id": item.eval_run_id,
        "dataset_item_id": item.dataset_item_id,
        "dataset_id": item.dataset_id,
        "position": item.position,
        "trial_index": int(item.trial_index or 0),
        "status": item.status,
        **_effective_item_statuses(item),
        "snap_input": item.snap_input,
        "snap_expected": item.snap_expected or "",
        "snap_criteria": item.snap_criteria or "",
        "snap_scoring_method": item.snap_scoring_method,
        "snap_scoring_config": item.snap_scoring_config or {},
        "complexity": item.snap_complexity or "L2",
        "task_type": item.snap_task_type or "direct_qa",
        "capability": item.snap_capability,
        "snap_meta": item.snap_meta or {},
        "run_id": item.run_id,
        "actual_output": item.actual_output or "",
        "error": item.error,
        "score": item.score,
        "passed": item.passed,
        "score_detail": item.score_detail or {},
        "stats": item.stats or {},
        "grades": [
            {
                "id": g.id, "kind": g.kind, "name": g.name, "status": g.status,
                "score": g.score, "passed": g.passed,
                "detail": g.detail or {}, "evidence": g.evidence or {},
                "model_config_id": g.model_config_id,
                "prompt_version": g.prompt_version,
                "created_at": g.created_at.isoformat() if g.created_at else None,
            }
            for g in grades
        ],
        "started_at": item.started_at.isoformat() if item.started_at else None,
        "finished_at": item.finished_at.isoformat() if item.finished_at else None,
    }


@router.post("/run-items/{item_id}/regrade")
async def regrade_item(item_id: int,
                       body: Optional[Dict[str, Any]] = None,
                       db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """对已有 Trial 单独重新评分（保留执行结果，不重跑 Agent）。"""
    from app.services.evaluation.artifacts import collect_workspace_artifacts
    from app.services.evaluation.runner import (
        _grade_trial, _record_grades, _refresh_aggregate,
    )

    item = (await db.execute(
        select(EvalRunItem).where(EvalRunItem.id == item_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "测评结果不存在")
    if item.execution_status != "succeeded":
        raise HTTPException(409, "该 Trial 没有成功的执行结果，无法单独重新评分")
    run = (await db.execute(
        select(EvalRun).where(EvalRun.id == item.eval_run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "测评任务不存在")
    body = body or {}
    method = (str(body.get("scoring_method") or item.snap_scoring_method)
              .strip().lower())
    if method not in ("exact_match", "contains", "keyword", "regex", "constraint",
                      "json_schema", "tool_usage", "artifact", "mock_state",
                      "project_verification", "hybrid", "llm_judge", "none"):
        raise HTTPException(422, f"不支持的评分方式: {method}")
    config = body.get("scoring_config")
    scoring_config = dict(config if isinstance(config, dict)
                          else (item.snap_scoring_config or {}))

    stats = dict(item.stats or {})
    session_id = stats.get("session_id")
    user_id = run.created_by
    if user_id is None:
        from app.models.user import User
        user_id = (await db.execute(
            select(User.id).order_by(User.id).limit(1))).scalar()
    workspace = await collect_workspace_artifacts(
        user_id, run.agent_id, session_id)
    artifacts = list(workspace.get("files") or [])
    if not artifacts:
        artifacts = list(stats.get("artifacts") or [])
    workspace_text = str(workspace.get("text") or "")
    turns = list(stats.get("turns") or [])
    if len(turns) > 1:
        judge_input = "\n".join(
            f"[用户] {t.get('input') or ''}\n[助手] {t.get('output') or ''}"
            for t in turns[:-1])
    else:
        judge_input = item.snap_input or ""
    graded = await _grade_trial(
        db,
        item_id=item.id,
        snap_method=method,
        snap_config=scoring_config,
        snap_expected=item.snap_expected or "",
        snap_criteria=item.snap_criteria or "",
        snap_meta=dict(item.snap_meta or {}),
        judge_input=judge_input,
        actual_output=(item.actual_output or "") + workspace_text,
        output_text=item.actual_output or "",
        stats=stats,
        artifacts=artifacts,
        workspace_text=workspace_text,
        execution_status="succeeded",
    )
    await db.execute(
        update(EvalRunItem).where(EvalRunItem.id == item.id).values(
            status=graded["legacy_status"],
            verification_status=graded["verification_status"],
            grading_status=graded["grading_status"],
            outcome=graded["outcome"],
            score=graded["score"], passed=graded["passed"],
            score_detail=graded["detail"], stats=graded["stats"],
            # 重新评分显式选择的评分方式/配置作为当前评分口径保存；
            # 原执行快照（输入/期望/输出）不变，历史评分保留在 eval_grades
            snap_scoring_method=method,
            snap_scoring_config=scoring_config,
            error=None))
    await _record_grades(
        db, item.id, verification=graded["verification"],
        grading=graded["grading"])
    await db.commit()
    await _refresh_aggregate(db, item.eval_run_id)
    return {
        "item_id": item.id,
        "outcome": graded["outcome"],
        "status": graded["legacy_status"],
        "verification_status": graded["verification_status"],
        "grading_status": graded["grading_status"],
        "score": graded["score"],
        "passed": graded["passed"],
    }


class RegressionRequest(BaseModel):
    dataset_id: Optional[int] = None
    dataset_name: Optional[str] = None


class ReviewRequest(BaseModel):
    decision: str = Field(..., description="needs_review / confirm_pass / confirm_fail / reject_score")
    note: str = ""


@router.post("/run-items/{item_id}/review")
async def review_item(
    item_id: int, body: ReviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_superuser),
) -> Dict[str, Any]:
    """人工复核 Trial 评分（预留人工复核与评分版本管理能力）。

    - needs_review：标记待复核（保留原 Outcome 与评分，仅改 grading_status）
    - confirm_pass / confirm_fail：人工确认为通过/未通过
    - reject_score：拒绝当前评分，结果转 inconclusive（原评分保留在 eval_grades）
    """
    decision = (body.decision or "").strip().lower()
    if decision not in ("needs_review", "confirm_pass", "confirm_fail", "reject_score"):
        raise HTTPException(422, "decision 取值非法")
    item = (await db.execute(
        select(EvalRunItem).where(EvalRunItem.id == item_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "测评结果不存在")

    values: Dict[str, Any] = {}
    if decision == "needs_review":
        values["grading_status"] = "needs_review"
    elif decision == "confirm_pass":
        values.update({"grading_status": "graded", "outcome": "pass",
                       "status": "passed", "passed": True})
    elif decision == "confirm_fail":
        values.update({"grading_status": "graded", "outcome": "fail",
                       "status": "failed", "passed": False})
    else:
        values.update({"grading_status": "needs_review", "outcome": "inconclusive",
                       "status": "error", "passed": None, "score": None})

    detail = dict(item.score_detail or {})
    detail["manual_review"] = {
        "decision": decision, "note": body.note[:2000],
        "reviewer": current_user.id,
        "reviewed_at": datetime.utcnow().isoformat(),
    }
    values["score_detail"] = detail
    await db.execute(
        update(EvalRunItem).where(EvalRunItem.id == item_id).values(**values))
    db.add(EvalGrade(
        trial_id=item_id, kind="manual", name="human_review",
        status=decision, score=values.get("score", item.score),
        passed=values.get("passed", item.passed),
        detail={"note": body.note[:2000], "reviewer": current_user.id,
                "decision": decision},
    ))
    await db.commit()
    from app.services.evaluation.runner import _refresh_aggregate

    await _refresh_aggregate(db, item.eval_run_id)
    row = (await db.execute(
        select(EvalRunItem).where(EvalRunItem.id == item_id))).scalar_one()
    return {
        "item_id": item_id, "outcome": row.outcome,
        "grading_status": row.grading_status,
        "passed": row.passed, "score": row.score,
        "manual_review": detail["manual_review"],
    }


@router.get("/run-items/{item_id}/artifact")
async def get_trial_artifact(
    item_id: int, path: str = Query(..., min_length=1, max_length=500),
    db: AsyncSession = Depends(get_db),
):
    """查看 Trial 工作区最终产物内容（只读、路径逃逸防护、大小限制）。"""
    from pathlib import Path as _Path

    from fastapi.responses import FileResponse

    from app.services.evaluation.artifacts import redact_text, workspace_root

    item = (await db.execute(
        select(EvalRunItem).where(EvalRunItem.id == item_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "测评结果不存在")
    run = (await db.execute(
        select(EvalRun).where(EvalRun.id == item.eval_run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "测评任务不存在")
    user_id = run.created_by
    if user_id is None:
        from app.models.user import User
        user_id = (await db.execute(
            select(User.id).order_by(User.id).limit(1))).scalar()
    session_id = (item.stats or {}).get("session_id")
    root = workspace_root(user_id, run.agent_id, session_id)
    if root is None:
        raise HTTPException(404, "该 Trial 没有可访问的工作区产物")
    try:
        target = (_Path(root) / path).resolve()
    except (OSError, ValueError):
        raise HTTPException(400, "非法产物路径")
    if _Path(root).resolve() not in target.parents or not target.is_file():
        raise HTTPException(404, "产物文件不存在")
    size = target.stat().st_size
    if size > 2 * 1024 * 1024:
        raise HTTPException(413, "产物过大，不支持在线预览")
    # 文本脱敏后在线预览；二进制直接下载（浏览器按类型处理）
    try:
        text = target.read_text(encoding="utf-8")
        return PlainTextResponse(redact_text(text),
                                 media_type="text/plain; charset=utf-8")
    except UnicodeDecodeError:
        return FileResponse(target, filename=target.name)


@router.post("/run-items/{item_id}/to-regression")
async def trial_to_regression(
    item_id: int, body: RegressionRequest,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """把失败/证据不足的 Trial 快照沉淀为回归用例（纯数据复制）。"""
    from app.services.evaluation.capabilities import resolve_capability

    item = (await db.execute(
        select(EvalRunItem).where(EvalRunItem.id == item_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "测评结果不存在")
    if (item.outcome or item.status) in ("pass", "passed"):
        raise HTTPException(409, "已通过的用例无需沉淀为回归用例")

    if body.dataset_id:
        dataset = (await db.execute(
            select(EvalDataset).where(EvalDataset.id == body.dataset_id)
        )).scalar_one_or_none()
        if dataset is None:
            raise HTTPException(404, "目标数据集不存在")
    else:
        name = (body.dataset_name or "回归用例（自动沉淀）").strip()
        dataset = (await db.execute(
            select(EvalDataset).where(EvalDataset.name == name)
        )).scalar_one_or_none()
        if dataset is None:
            dataset = EvalDataset(
                name=name, description="由失败/证据不足 Trial 自动沉淀的回归用例",
                source="manual", category="regression",
                scoring_method=item.snap_scoring_method or "llm_judge",
            )
            db.add(dataset)
            await db.flush()

    duplicate = (await db.execute(
        select(EvalDatasetItem.id).where(
            EvalDatasetItem.dataset_id == dataset.id,
            EvalDatasetItem.input == (item.snap_input or ""),
        ))).scalar()
    if duplicate is not None:
        return {"dataset_id": dataset.id, "item_id": duplicate,
                "created": False, "reason": "用例已存在"}

    max_pos = (await db.execute(
        select(func.max(EvalDatasetItem.position))
        .where(EvalDatasetItem.dataset_id == dataset.id))).scalar()
    meta = dict(item.snap_meta or {})
    regression_item = EvalDatasetItem(
        dataset_id=dataset.id,
        position=int(max_pos) + 1 if max_pos is not None else 0,
        input=item.snap_input or "",
        expected=item.snap_expected or "",
        criteria=item.snap_criteria or "",
        scoring_method=item.snap_scoring_method,
        scoring_config=dict(item.snap_scoring_config or {}),
        item_metadata={
            **dict(meta.get("metadata") or {}),
            "regression_from_trial": item.id,
            "regression_from_run": item.eval_run_id,
        },
        complexity=item.snap_complexity or "L2",
        task_type=item.snap_task_type or "direct_qa",
        capability=item.snap_capability or resolve_capability(
            None, item.snap_task_type),
        required_tools=list(meta.get("required_tools") or []),
        expected_min_steps=meta.get("expected_min_steps"),
        turns=list(meta.get("turns") or []),
    )
    db.add(regression_item)
    await db.commit()
    await db.refresh(regression_item)
    return {
        "dataset_id": dataset.id, "item_id": regression_item.id,
        "position": regression_item.position, "created": True,
    }


@router.get("/run-items/{item_id}/trace")
async def get_run_item_trace(
    item_id: int,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    event_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """用例执行轨迹（纯客观）：逐轮 Run 的事件时间线 + 工具执行账本。"""
    from app.models.agent import AgentRunEvent, AgentToolExecution

    item = (await db.execute(
        select(EvalRunItem).where(EvalRunItem.id == item_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "测评结果不存在")
    run_ids: List[str] = []
    if item.run_id:
        run_ids.append(item.run_id)
    for t in ((item.stats or {}).get("turns") or []):
        rid = t.get("run_id")
        if rid and rid not in run_ids:
            run_ids.append(rid)

    runs_trace = []
    for rid in run_ids:
        ev_stmt = select(AgentRunEvent.sequence, AgentRunEvent.event_type,
                         AgentRunEvent.payload, AgentRunEvent.created_at) \
            .where(AgentRunEvent.run_id == rid)
        if event_type:
            ev_stmt = ev_stmt.where(AgentRunEvent.event_type == event_type)
        total_events = (await db.execute(
            select(func.count()).select_from(AgentRunEvent)
            .where(AgentRunEvent.run_id == rid))).scalar() or 0
        events = (await db.execute(
            ev_stmt.order_by(AgentRunEvent.sequence).offset(offset).limit(limit)
        )).all()
        tools = (await db.execute(
            select(AgentToolExecution).where(AgentToolExecution.run_id == rid)
        )).scalars().all()
        tools = sorted(tools, key=lambda r: (r.started_at or r.created_at or datetime.min))
        runs_trace.append({
            "run_id": rid,
            "total_events": int(total_events),
            "offset": offset,
            "limit": limit,
            "events": [
                {
                    "sequence": e.sequence,
                    "event_type": e.event_type,
                    "payload": str(e.payload)[:300] if e.payload else None,
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                }
                for e in events
            ],
            "tools": [
                {
                    "tool_name": t.tool_name,
                    "status": t.status,
                    "attempt_count": int(t.attempt_count or 0),
                    "args": str(t.args_snapshot)[:300] if t.args_snapshot else None,
                    "result": str(t.result_payload)[:300] if t.result_payload else None,
                    "error": str(t.error)[:300] if t.error else None,
                    "started_at": t.started_at.isoformat() if t.started_at else None,
                    "finished_at": t.finished_at.isoformat() if t.finished_at else None,
                }
                for t in tools
            ],
        })
    return {"item_id": item.id, "runs": runs_trace}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: int,
                     db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    from app.services.evaluation.runner import cancel_eval_run

    run = (await db.execute(
        select(EvalRun).where(EvalRun.id == run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "测评任务不存在")
    return await cancel_eval_run(db, run)


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: int, background: BackgroundTasks,
                     db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    from app.services.evaluation.runner import (
        resume_eval_run, start_eval_run_background,
    )

    run = (await db.execute(
        select(EvalRun).where(EvalRun.id == run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "测评任务不存在")
    try:
        result = await resume_eval_run(db, run)
    except ValueError as e:
        raise HTTPException(409, str(e))
    background.add_task(start_eval_run_background, run.id)
    return result


@router.get("/compare")
async def compare_runs(
    base_run_id: int,
    target_run_id: int,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """两个 Campaign 的配对比较（按 Case 聚合多次 Trial，客观统计）。"""
    from app.services.evaluation.runner import _case_key, _outcome

    base = (await db.execute(
        select(EvalRun).where(EvalRun.id == base_run_id))).scalar_one_or_none()
    target = (await db.execute(
        select(EvalRun).where(EvalRun.id == target_run_id))).scalar_one_or_none()
    if base is None or target is None:
        raise HTTPException(404, "测评任务不存在")

    async def _cases(run_id: int) -> Dict[Any, Dict[str, Any]]:
        rows = (await db.execute(
            select(EvalRunItem).where(EvalRunItem.eval_run_id == run_id)
        )).scalars().all()
        grouped: Dict[Any, List[EvalRunItem]] = {}
        for row in rows:
            grouped.setdefault(_case_key(row), []).append(row)
        out: Dict[Any, Dict[str, Any]] = {}
        for key, trials in grouped.items():
            outcomes = [_outcome(t) for t in trials]
            if "pass" in outcomes:
                outcome = "pass"
            elif "fail" in outcomes:
                outcome = "fail"
            else:
                outcome = next((o for o in outcomes if o not in ("pending",)), "pending")
            first = trials[0]
            out[key] = {
                "input_preview": (first.snap_input or "").replace("\n", " ")[:160],
                "complexity": first.snap_complexity or "L2",
                "task_type": first.snap_task_type or "direct_qa",
                "capability": first.snap_capability,
                "outcome": outcome,
            }
        return out

    base_cases = await _cases(base.id)
    target_cases = await _cases(target.id)
    common = sorted(set(base_cases) & set(target_cases), key=str)
    rows: List[Dict[str, Any]] = []
    improved = regressed = unchanged_pass = unchanged_fail = 0
    for key in common:
        b, t = base_cases[key]["outcome"], target_cases[key]["outcome"]
        change = "unchanged"
        if b == "fail" and t == "pass":
            change, improved = "improved", improved + 1
        elif b == "pass" and t == "fail":
            change, regressed = "regressed", regressed + 1
        elif b == "pass" and t == "pass":
            change, unchanged_pass = "unchanged_pass", unchanged_pass + 1
        elif b == "fail" and t == "fail":
            change, unchanged_fail = "unchanged_fail", unchanged_fail + 1
        rows.append({
            "case": str(key),
            "input_preview": base_cases[key]["input_preview"],
            "complexity": base_cases[key]["complexity"],
            "task_type": base_cases[key]["task_type"],
            "capability": base_cases[key]["capability"],
            "base_outcome": b,
            "target_outcome": t,
            "change": change,
        })

    def _run_meta(run: EvalRun) -> Dict[str, Any]:
        agg = run.aggregate or {}
        return {
            "run_id": run.id, "name": run.name,
            "agent_id": run.agent_id, "agent_version_id": run.agent_version_id,
            "suite_version_id": run.suite_version_id,
            "pass_rate": agg.get("pass_rate"),
            "avg_score": agg.get("avg_score"),
            "p95_duration_s": agg.get("p95_duration_s"),
            "cost_per_success_tokens": agg.get("cost_per_success_tokens"),
            "outcomes": agg.get("outcomes") or {},
            "pass_k": agg.get("pass_k") or {},
        }

    base_meta, target_meta = _run_meta(base), _run_meta(target)
    return {
        "base": base_meta,
        "target": target_meta,
        # 同套件版本才具备严格可复现的版本对比条件
        "same_suite_version": bool(
            base.suite_version_id
            and base.suite_version_id == target.suite_version_id),
        "summary": {
            "cases_paired": len(common),
            "improved": improved,
            "regressed": regressed,
            "unchanged_pass": unchanged_pass,
            "unchanged_fail": unchanged_fail,
            "only_base": len(set(base_cases) - set(target_cases)),
            "only_target": len(set(target_cases) - set(base_cases)),
        },
        "deltas": {
            "pass_rate": (
                None if base_meta["pass_rate"] is None or target_meta["pass_rate"] is None
                else round(target_meta["pass_rate"] - base_meta["pass_rate"], 4)),
            "avg_score": (
                None if base_meta["avg_score"] is None or target_meta["avg_score"] is None
                else round(target_meta["avg_score"] - base_meta["avg_score"], 3)),
            "p95_duration_s": (
                None if base_meta["p95_duration_s"] is None
                or target_meta["p95_duration_s"] is None
                else round(target_meta["p95_duration_s"] - base_meta["p95_duration_s"], 2)),
        },
        "items": rows[:500],
    }


@router.get("/quality-audit")
async def quality_audit(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """数据集质量审计（纯客观统计，不做主观判断）。"""
    datasets = (await db.execute(
        select(EvalDataset).order_by(EvalDataset.id))).scalars().all()
    items: List[Dict[str, Any]] = []
    totals = {
        "cases": 0, "deterministic": 0, "judge_only": 0, "no_scoring": 0,
        "missing_expected": 0, "missing_capability": 0, "duplicate_inputs": 0,
    }
    for dataset in datasets:
        rows = (await db.execute(
            select(EvalDatasetItem).where(
                EvalDatasetItem.dataset_id == dataset.id))).scalars().all()
        deterministic = judge_only = no_scoring = missing_expected = 0
        seen: Dict[str, int] = {}
        duplicates = 0
        missing_capability = 0
        for item in rows:
            method = (item.scoring_method or dataset.scoring_method or "llm_judge").lower()
            if method in ("llm_judge",):
                judge_only += 1
            elif method == "none":
                no_scoring += 1
            else:
                deterministic += 1
            if not (item.expected or "").strip():
                missing_expected += 1
            if not item.capability:
                missing_capability += 1
            key = " ".join((item.input or "").lower().split())
            seen[key] = seen.get(key, 0) + 1
            if seen[key] == 2:
                duplicates += 1
        items.append({
            "dataset_id": dataset.id,
            "name": dataset.name,
            "source": dataset.source,
            "cases": len(rows),
            "deterministic_cases": deterministic,
            "judge_only_cases": judge_only,
            "no_scoring_cases": no_scoring,
            "missing_expected": missing_expected,
            "missing_capability": missing_capability,
            "duplicate_inputs": duplicates,
        })
        totals["cases"] += len(rows)
        totals["deterministic"] += deterministic
        totals["judge_only"] += judge_only
        totals["no_scoring"] += no_scoring
        totals["missing_expected"] += missing_expected
        totals["missing_capability"] += missing_capability
        totals["duplicate_inputs"] += duplicates
    if totals["cases"]:
        totals["deterministic_ratio"] = round(
            totals["deterministic"] / totals["cases"], 4)
    return {"totals": totals, "datasets": items}


@router.get("/runs/{run_id}/markdown")
async def export_markdown(run_id: int,
                          db: AsyncSession = Depends(get_db)):
    """测评结果导出 Markdown（汇总 + 逐用例明细）。"""
    run = (await db.execute(
        select(EvalRun).where(EvalRun.id == run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "测评任务不存在")
    dataset = (await db.execute(
        select(EvalDataset.name).where(EvalDataset.id == run.dataset_id))).scalar()
    agent = (await db.execute(
        select(Agent.name).where(Agent.id == run.agent_id))).scalar()
    items = (await db.execute(
        select(EvalRunItem).where(EvalRunItem.eval_run_id == run_id)
        .order_by(EvalRunItem.position))).scalars().all()
    agg = run.aggregate or {}
    env = run.environment or {}
    suite_name = None
    suite_version_no = None
    if run.suite_id:
        suite_name = (await db.execute(
            select(EvalSuite.name).where(EvalSuite.id == run.suite_id))).scalar()
    if run.suite_version_id:
        suite_version_no = (await db.execute(
            select(EvalSuiteVersion.version_no)
            .where(EvalSuiteVersion.id == run.suite_version_id))).scalar()
    version_no = None
    if run.agent_version_id:
        from app.models.agent import AgentVersion
        version_no = (await db.execute(
            select(AgentVersion.version_no)
            .where(AgentVersion.id == run.agent_version_id))).scalar()

    lines = [
        f"# 测评报告：{run.name}",
        "",
        f"- 数据集：{dataset or run.dataset_id}（{len(items)} 条 Trial）",
        f"- 套件：{suite_name or '—'}"
        + (f"（v{suite_version_no}）" if suite_version_no else ""),
        f"- Agent：{agent or run.agent_id}"
        + (f"（版本 v{version_no}，ID={run.agent_version_id}）"
           if run.agent_version_id else "（版本未知）"),
        f"- 环境快照：{env.get('runtime', {}).get('app_version') or '—'} / "
        f"{env.get('pinned_at') or '—'}",
        f"- 状态：{run.status}",
        f"- 通过率：{agg.get('pass_rate') if agg.get('pass_rate') is not None else '—'}"
        f"（通过 {agg.get('passed', 0)} / 有效 {agg.get('effective_sample', 0)}）",
        f"- 结果分解：{agg.get('outcomes') or '—'}",
        f"- 平均分：{agg.get('avg_score') if agg.get('avg_score') is not None else '—'}",
        f"- 平均耗时：{agg.get('avg_duration_s') or '—'}s；"
        f"P95：{agg.get('p95_duration_s') or '—'}s；"
        f"总 tokens：{agg.get('total_tokens', 0)}；总工具调用：{agg.get('total_tool_calls', 0)}",
        f"- pass@k / pass^k：{agg.get('pass_k') or '—'}",
        f"- 置信区间（95%）：{agg.get('confidence_interval') or '—'}",
        f"- 创建时间：{run.created_at.isoformat() if run.created_at else '—'}",
        "",
        "## 按能力维度（客观数据）",
        "",
        "| 能力维度 | 用例数 | 通过 | 通过率 | 平均分 | 证据充分 |",
        "|----------|--------|------|--------|--------|----------|",
    ]
    for cap, s in (agg.get("by_capability") or {}).items():
        lines.append(
            f"| {s.get('name') or cap} | {s.get('total')} | {s.get('passed')} "
            f"| {s.get('pass_rate')} | {s.get('avg_score')} "
            f"| {'是' if s.get('evidence_sufficient') else '否（样本不足）'} |")
    lines += [
        "",
        "## 按复杂度分层（客观数据）",
        "",
        "| 复杂度 | 用例数 | 通过 | 通过率 | 平均分 | 平均步数 | 平均工具调用 |",
        "|--------|--------|------|--------|--------|----------|--------------|",
    ]
    for lvl, s in (agg.get("by_complexity") or {}).items():
        lines.append(
            f"| {lvl} | {s.get('total')} | {s.get('passed')} | {s.get('pass_rate')} "
            f"| {s.get('avg_score')} | {s.get('avg_steps')} | {s.get('avg_tool_calls')} |")
    lines += [
        "",
        "## 按任务类型分层（客观数据）",
        "",
        "| 任务类型 | 用例数 | 通过 | 通过率 | 平均分 | 平均步数 | 平均工具调用 |",
        "|----------|--------|------|--------|--------|----------|--------------|",
    ]
    for tt, s in (agg.get("by_task_type") or {}).items():
        lines.append(
            f"| {tt} | {s.get('total')} | {s.get('passed')} | {s.get('pass_rate')} "
            f"| {s.get('avg_score')} | {s.get('avg_steps')} | {s.get('avg_tool_calls')} |")
    cap = agg.get("capability") or {}
    if cap:
        lines += [
            "",
            "## Agent 能力事实（客观数据）",
            "",
            f"- 平均步数：{cap.get('avg_steps')}；最大步数：{cap.get('max_steps')}",
            f"- 多步执行用例（步数≥2）：{cap.get('multi_step_count')}",
            f"- 全程未用工具用例：{cap.get('zero_tool_count')}",
            f"- 工具错误率：{cap.get('tool_error_rate')}",
            f"- 未调用必需工具用例：{cap.get('missing_required_tools_count')}",
            f"- 超出执行上限用例：{cap.get('limit_exceeded_count')}",
            f"- 平均输出字数：{cap.get('avg_output_chars')}；"
            f"最大输出字数：{cap.get('max_output_chars')}",
            f"- 客观约束达成率：{cap.get('constraint_ratio')}"
            f"（{cap.get('constraint_passed', 0)}/{cap.get('constraint_total', 0)}，"
            f"覆盖 {cap.get('constraint_items', 0)} 条用例）",
        ]
    tool_usage = agg.get("tool_usage") or {}
    if tool_usage:
        lines += ["", "## 工具使用统计", "",
                  "| 工具 | 调用 | 失败 |", "|------|------|------|"]
        for name, u in sorted(tool_usage.items()):
            lines.append(f"| {name} | {u.get('calls')} | {u.get('failed')} |")
    problems = agg.get("problem_items") or []
    if problems:
        lines += ["", "## 问题用例清单（客观事实）", "",
                  "| # | 复杂度 | 任务类型 | 状态 | 问题 | 步数 | 工具调用 | 未用工具 |",
                  "|---|--------|----------|------|------|------|----------|----------|"]
        for p in problems:
            lines.append(
                f"| {p.get('position')} | {p.get('complexity')} | {p.get('task_type')} "
                f"| {p.get('status')} | {','.join(p.get('problems') or [])} "
                f"| {p.get('steps')} | {p.get('tool_calls')} "
                f"| {','.join(p.get('missing_tools') or []) or '—'} |")
    lines += [
        "",
        "## 用例明细",
        "",
        "| # | 复杂度 | 任务类型 | 结果 | 执行 | 验证 | 评分 | 得分 | 步数 | 工具调用 | 输出字数 | 输入（摘录） | 实际输出（摘录） |",
        "|---|--------|----------|------|------|------|------|------|------|----------|----------|--------------|------------------|",
    ]
    for it in items:
        inp = (it.snap_input or "").replace("\n", " ").replace("|", "\\|")[:60]
        out = (it.actual_output or "").replace("\n", " ").replace("|", "\\|")[:60]
        score = f"{it.score}" if it.score is not None else "—"
        st = it.stats or {}
        view = _effective_item_statuses(it)
        lines.append(
            f"| {it.position + 1} | {it.snap_complexity or 'L2'} "
            f"| {it.snap_task_type or 'direct_qa'} "
            f"| {view['outcome']} "
            f"| {view['execution_status']} "
            f"| {view['verification_status']} "
            f"| {view['grading_status']} | {score} "
            f"| {st.get('steps', '—')} | {st.get('tool_calls', '—')} "
            f"| {st.get('output_chars', '—')} | {inp} | {out} |")
    lines += ["", "## 评分明细", ""]
    for it in items:
        detail = it.score_detail or {}
        reasoning = detail.get("reasoning") or detail.get("method") or ""
        st = it.stats or {}
        used = "、".join(st.get("used_tools") or []) or "—"
        missing = "、".join(st.get("missing_tools") or [])
        lines += [
            f"### 用例 {it.position + 1}（{it.snap_complexity or 'L2'}/"
            f"{it.snap_task_type or 'direct_qa'}，{it.status}，"
            f"得分 {it.score if it.score is not None else '—'}）",
            "",
            f"- 期望：{(it.snap_expected or '—')[:300]}",
            f"- 评分理由：{str(reasoning)[:500] or '—'}",
            f"- 执行事实：步数 {st.get('steps', '—')}，工具调用 {st.get('tool_calls', '—')}"
            f"（使用：{used}）"
            + (f"，未用必需工具：{missing}" if missing else ""),
            f"- 输出规模：{st.get('output_chars', '—')} 字；"
            f"轮次：{st.get('turns_count', 1)}"
            + (f"；交付物：{st.get('artifact_chars')} 字" if st.get('artifact_chars') else ""),
            f"- Run：{it.run_id or '—'}",
            "",
        ]
        # 客观约束检查清单（constraint 评分）
        checks = detail.get("checks") or []
        if checks:
            lines += ["| 约束 | 通过 | 说明 |", "|------|------|------|"]
            for c in checks:
                desc = str(c.get("desc") or "").replace("|", "\\|")[:120]
                mark = "✓" if c.get("passed") else "✗"
                lines.append(f"| {c.get('type')} | {mark} | {desc} |")
            lines.append("")
    md = "\n".join(lines)
    return PlainTextResponse(
        md, media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="eval-report-{run_id}.md"'},
    )
