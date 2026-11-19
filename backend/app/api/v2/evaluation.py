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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_superuser
from app.core.database import get_db
from app.models.agent import Agent
from app.models.evaluation import EvalDataset, EvalDatasetItem, EvalRun, EvalRunItem

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
        "required_tools": list(it.required_tools or []),
        "expected_min_steps": it.expected_min_steps,
        "turns": list(it.turns or []),
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


# ────────────────────────────── 测评任务 ──────────────────────────────


class RunCreateRequest(BaseModel):
    name: Optional[str] = None
    dataset_id: int
    agent_id: int
    approval_mode: str = "auto"
    use_memory: bool = False
    timeout_s: int = Field(180, ge=10, le=3600)
    limit: int = Field(0, ge=0, le=500)


def _run_dict(r: EvalRun) -> Dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "dataset_id": r.dataset_id,
        "agent_id": r.agent_id,
        "agent_version_id": r.agent_version_id,
        "status": r.status,
        "config": r.config or {},
        "aggregate": r.aggregate or {},
        "error": r.error,
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
    try:
        eval_run = await create_eval_run(
            db, name=name, dataset_id=body.dataset_id, agent_id=body.agent_id,
            created_by=current_user.id,
            config={
                "approval_mode": body.approval_mode,
                "use_memory": body.use_memory,
                "timeout_s": body.timeout_s,
                "limit": body.limit,
            },
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    # 后台执行（与请求生命周期解耦）
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
        EvalRun.status, EvalRun.aggregate, EvalRun.config, EvalRun.error,
        EvalRun.started_at, EvalRun.finished_at, EvalRun.created_at,
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
                "agent_id": r.agent_id, "status": r.status,
                "aggregate": r.aggregate or {}, "config": r.config or {},
                "error": r.error,
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
    return {**_run_dict(run), "dataset_name": dataset, "agent_name": agent}


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
        EvalRunItem.id, EvalRunItem.dataset_item_id, EvalRunItem.position,
        EvalRunItem.status, EvalRunItem.score, EvalRunItem.passed,
        EvalRunItem.run_id, EvalRunItem.error,
        EvalRunItem.snap_complexity, EvalRunItem.snap_task_type, EvalRunItem.stats,
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
                "position": r.position, "status": r.status,
                "score": r.score, "passed": r.passed, "run_id": r.run_id,
                "error": r.error,
                "complexity": r.snap_complexity or "L2",
                "task_type": r.snap_task_type or "direct_qa",
                "steps": (r.stats or {}).get("steps"),
                "tool_calls": (r.stats or {}).get("tool_calls"),
                "output_chars": (r.stats or {}).get("output_chars"),
                "used_tools": (r.stats or {}).get("used_tools") or [],
                "missing_tools": (r.stats or {}).get("missing_tools") or [],
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
    return {
        "id": item.id,
        "eval_run_id": item.eval_run_id,
        "dataset_item_id": item.dataset_item_id,
        "position": item.position,
        "status": item.status,
        "snap_input": item.snap_input,
        "snap_expected": item.snap_expected or "",
        "snap_criteria": item.snap_criteria or "",
        "snap_scoring_method": item.snap_scoring_method,
        "snap_scoring_config": item.snap_scoring_config or {},
        "complexity": item.snap_complexity or "L2",
        "task_type": item.snap_task_type or "direct_qa",
        "snap_meta": item.snap_meta or {},
        "run_id": item.run_id,
        "actual_output": item.actual_output or "",
        "error": item.error,
        "score": item.score,
        "passed": item.passed,
        "score_detail": item.score_detail or {},
        "stats": item.stats or {},
        "started_at": item.started_at.isoformat() if item.started_at else None,
        "finished_at": item.finished_at.isoformat() if item.finished_at else None,
    }


@router.get("/run-items/{item_id}/trace")
async def get_run_item_trace(item_id: int,
                             db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
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
        events = (await db.execute(
            select(AgentRunEvent.sequence, AgentRunEvent.event_type,
                   AgentRunEvent.payload, AgentRunEvent.created_at)
            .where(AgentRunEvent.run_id == rid)
            .order_by(AgentRunEvent.sequence).limit(200))).all()
        tools = (await db.execute(
            select(AgentToolExecution).where(AgentToolExecution.run_id == rid)
        )).scalars().all()
        tools = sorted(tools, key=lambda r: (r.started_at or r.created_at or datetime.min))
        runs_trace.append({
            "run_id": rid,
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

    lines = [
        f"# 测评报告：{run.name}",
        "",
        f"- 数据集：{dataset or run.dataset_id}（{len(items)} 条用例）",
        f"- Agent：{agent or run.agent_id}",
        f"- 状态：{run.status}",
        f"- 通过率：{agg.get('pass_rate') if agg.get('pass_rate') is not None else '—'}"
        f"（通过 {agg.get('passed', 0)} / 完成 {agg.get('completed', 0)}）",
        f"- 平均分：{agg.get('avg_score') if agg.get('avg_score') is not None else '—'}",
        f"- 平均耗时：{agg.get('avg_duration_s') or '—'}s；"
        f"总 tokens：{agg.get('total_tokens', 0)}；总工具调用：{agg.get('total_tool_calls', 0)}",
        f"- 创建时间：{run.created_at.isoformat() if run.created_at else '—'}",
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
        "| # | 复杂度 | 任务类型 | 状态 | 得分 | 步数 | 工具调用 | 输出字数 | 输入（摘录） | 实际输出（摘录） |",
        "|---|--------|----------|------|------|------|----------|----------|--------------|------------------|",
    ]
    for it in items:
        inp = (it.snap_input or "").replace("\n", " ").replace("|", "\\|")[:60]
        out = (it.actual_output or "").replace("\n", " ").replace("|", "\\|")[:60]
        score = f"{it.score}" if it.score is not None else "—"
        st = it.stats or {}
        lines.append(
            f"| {it.position + 1} | {it.snap_complexity or 'L2'} "
            f"| {it.snap_task_type or 'direct_qa'} | {it.status} | {score} "
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
