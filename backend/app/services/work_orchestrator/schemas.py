"""Work Orchestrator 数据契约（任务书 §4 / §7.2 / §9）。

设计约束：
- 模型生成的静态计划（WorkPlan）与运行时状态（WorkPlanRecord.steps）分离；
- 全部模型输出走 ``extra="forbid"`` 严格 Schema，字段/数组/文本均有硬上限；
- ``executor_profile`` 使用服务端 Enum 白名单，模型不能指定任意 Python 类、工具名、
  URL、工作区路径或权限；
- 运行时状态只保存摘要与产物/证据引用，不把完整工具输出、源码或密钥写入计划。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ── 服务端执行器白名单（任务书 §6）──────────────────────────────────────────
ExecutorProfile = Literal[
    "general_agent",
    "research",
    "data_analysis",
    "database",
    "coding",
    "business_action",
    "document_docx",
    "team",
    "software_project",
]

StepStatus = Literal[
    "pending", "running", "waiting_approval", "completed",
    "failed", "skipped", "needs_reconciliation",
]
PlanStatus = Literal[
    "planning", "ready", "running", "verifying", "paused",
    "completed", "failed", "cancelled", "shadow", "degraded",
]
VerificationStatus = Literal["passed", "partial", "failed"]

MAX_STEPS = 6
MAX_PLAN_BYTES = 60_000

ShortText = Annotated[str, Field(max_length=1000)]
StepText = Annotated[str, Field(max_length=2000)]

_EXECUTOR_ALIASES = {
    "generalagent": "general_agent", "general": "general_agent",
    "agent": "general_agent", "deepagent": "general_agent",
    "deep_agent": "general_agent", "chat": "general_agent",
    "research": "research", "researcher": "research", "web_research": "research",
    "search": "research",
    "dataanalysis": "data_analysis", "data_analysis": "data_analysis",
    "analysis": "data_analysis", "data": "data_analysis", "excel": "data_analysis",
    "database": "database", "db": "database", "sql": "database",
    "databasequery": "database", "database_query": "database",
    "coding": "coding", "code": "coding", "developer": "coding",
    "businessaction": "business_action", "business_action": "business_action",
    "business": "business_action", "automation": "business_action",
    "documentdocx": "document_docx", "document_docx": "document_docx",
    "document": "document_docx", "docx": "document_docx", "word": "document_docx",
    "documentgraph": "document_docx", "document_graph": "document_docx",
    "team": "team", "teamgraph": "team", "team_graph": "team",
    "softwareproject": "software_project", "software_project": "software_project",
    "project": "software_project", "teamproject": "software_project",
    "team_project": "software_project", "teamprojectgraph": "software_project",
    "team_project_graph": "software_project",
}


def _norm(value: str) -> str:
    return "".join(ch for ch in str(value or "").strip().lower() if ch not in " _-")


def _clip_items(value: list[str], limit: int) -> list[str]:
    seen: list[str] = []
    for item in value:
        text = str(item or "").strip()[:limit]
        if text and text not in seen:
            seen.append(text)
    return seen


class WorkStep(BaseModel):
    """一个可独立执行、可独立验收的工作步骤（静态计划，不含运行时状态）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    title: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=2000)
    executor_profile: ExecutorProfile
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_STEPS)
    input_refs: list[str] = Field(default_factory=list, max_length=8)
    required_capabilities: list[str] = Field(default_factory=list, max_length=8)
    deliverables: list[str] = Field(default_factory=list, max_length=8)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("executor_profile", mode="before")
    @classmethod
    def _normalize_executor(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return _EXECUTOR_ALIASES.get(_norm(value), value)

    @field_validator("depends_on", "input_refs", "required_capabilities",
                     "deliverables", "acceptance_criteria")
    @classmethod
    def _clip_list(cls, value: list[str]) -> list[str]:
        return _clip_items(value, 200)


class PlannerMetadata(BaseModel):
    """规划模型调用元信息（不得包含密钥、原始 Prompt 或隐藏推理链）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    planner_version: str = Field(default="v1", max_length=20)
    model_id: str = Field(default="", max_length=120)
    latency_ms: int = Field(default=0, ge=0)
    fallback: bool = False
    repair_attempted: bool = False
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    error: str = Field(default="", max_length=500)


class WorkPlan(BaseModel):
    """模型生成、服务端校验后才会执行的 WorkPlan。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["v1"] = "v1"
    plan_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:24], max_length=40)
    revision: int = Field(default=1, ge=1, le=20)
    objective: str = Field(min_length=1, max_length=2000)
    constraints: list[ShortText] = Field(default_factory=list, max_length=8)
    context_refs: list[str] = Field(default_factory=list, max_length=12)
    deliverables: list[ShortText] = Field(default_factory=list, max_length=8)
    acceptance_criteria: list[ShortText] = Field(default_factory=list, max_length=8)
    steps: list[WorkStep] = Field(min_length=1, max_length=MAX_STEPS)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    planner_metadata: PlannerMetadata = Field(default_factory=PlannerMetadata)

    @field_validator("constraints", "deliverables", "acceptance_criteria")
    @classmethod
    def _clip_short(cls, value: list[str]) -> list[str]:
        return _clip_items(value, 300)

    @field_validator("context_refs")
    @classmethod
    def _clip_refs(cls, value: list[str]) -> list[str]:
        return _clip_items(value, 120)

    @model_validator(mode="after")
    def _unique_step_ids(self) -> "WorkPlan":
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("步骤 ID 必须唯一")
        return self

    def byte_size(self) -> int:
        return len(self.model_dump_json().encode("utf-8"))


class WorkCheck(BaseModel):
    """单条验收检查（确定性证据，不接受模型自报完成）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=80)
    passed: bool
    detail: str = Field(default="", max_length=600)


class WorkArtifactRef(BaseModel):
    """步骤产物的最小引用（路径/大小/摘要/下载地址）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    path: str = Field(min_length=1, max_length=400)
    name: str = Field(default="", max_length=200)
    size: int = Field(default=0, ge=0)
    sha256: str = Field(default="", max_length=64)
    download_url: str = Field(default="", max_length=600)


class WorkVerificationResult(BaseModel):
    """统一验收结果协议（任务书 §9）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: VerificationStatus = "passed"
    checks: list[WorkCheck] = Field(default_factory=list, max_length=32)
    evidence_refs: list[str] = Field(default_factory=list, max_length=16)
    artifact_refs: list[WorkArtifactRef] = Field(default_factory=list, max_length=24)
    repairable: bool = False
    stop_reason: str = Field(default="", max_length=120)


class WorkStepOutcome(BaseModel):
    """Executor Adapter 统一返回值（任务书 §7.2）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    step_id: str = Field(min_length=1, max_length=40)
    status: StepStatus = "completed"
    summary: str = Field(default="", max_length=600)
    final_text: str = Field(default="", max_length=60_000)
    artifact_refs: list[WorkArtifactRef] = Field(default_factory=list, max_length=24)
    evidence_refs: list[str] = Field(default_factory=list, max_length=16)
    verification: WorkVerificationResult = Field(default_factory=WorkVerificationResult)
    error_code: str = Field(default="", max_length=80)
    retryable: bool = False
    checkpoint_ref: str = Field(default="", max_length=120)
    paused: bool = False


class WorkStepState(BaseModel):
    """步骤运行时状态（与静态 WorkStep 分离，单独持久化）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: StepStatus = "pending"
    attempts: int = Field(default=0, ge=0, le=10)
    summary: str = Field(default="", max_length=600)
    error_code: str = Field(default="", max_length=80)
    artifact_refs: list[WorkArtifactRef] = Field(default_factory=list, max_length=24)
    evidence_refs: list[str] = Field(default_factory=list, max_length=16)
    verification: Optional[WorkVerificationResult] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class WorkPlanRecord(BaseModel):
    """计划快照 + 运行时状态（写入 input_payload / Checkpoint 的可序列化结构）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mode: Literal["shadow", "auto"] = "auto"
    status: PlanStatus = "ready"
    plan: WorkPlan
    steps: dict[str, WorkStepState] = Field(default_factory=dict)
    current_step_id: Optional[str] = Field(default=None, max_length=40)
    repairs: int = Field(default=0, ge=0, le=10)
    diagnostics: list[str] = Field(default_factory=list, max_length=12)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    final_verification: Optional[WorkVerificationResult] = None

    @model_validator(mode="after")
    def _sync_steps(self) -> "WorkPlanRecord":
        for step in self.plan.steps:
            self.steps.setdefault(step.id, WorkStepState())
        for step_id in list(self.steps):
            if step_id not in {item.id for item in self.plan.steps}:
                self.steps.pop(step_id, None)
        return self

    @property
    def step_count(self) -> int:
        return len(self.plan.steps)

    @property
    def completed_count(self) -> int:
        return sum(1 for item in self.steps.values() if item.status == "completed")

    def step(self, step_id: str) -> WorkStep | None:
        return next((item for item in self.plan.steps if item.id == step_id), None)

    def ready_steps(self) -> list[WorkStep]:
        """所有前置步骤已完成且自身仍待执行的步骤（任务书 §7.1）。"""
        ready: list[WorkStep] = []
        for step in self.plan.steps:
            state = self.steps.get(step.id) or WorkStepState()
            if state.status != "pending":
                continue
            if all(
                (self.steps.get(dep) or WorkStepState()).status == "completed"
                for dep in step.depends_on
            ):
                ready.append(step)
        return ready

    def plan_summary(self) -> dict[str, Any]:
        """事件/摘要用轻量计划视图（不含完整 objective 正文）。"""
        return {
            "plan_id": self.plan.plan_id,
            "revision": self.plan.revision,
            "status": self.status,
            "mode": self.mode,
            "objective": self.plan.objective[:300],
            "steps": [
                {
                    "id": step.id,
                    "title": step.title,
                    "executor_profile": step.executor_profile,
                    "depends_on": list(step.depends_on),
                    "status": (self.steps.get(step.id) or WorkStepState()).status,
                    "artifact_count": len((self.steps.get(step.id) or WorkStepState()).artifact_refs),
                }
                for step in self.plan.steps
            ],
        }


def new_plan_record(plan: WorkPlan, *, mode: Literal["shadow", "auto"]) -> WorkPlanRecord:
    record = WorkPlanRecord(mode=mode, plan=plan, status="shadow" if mode == "shadow" else "ready")
    record.steps = {step.id: WorkStepState() for step in plan.steps}
    return record


def plan_record_from_snapshot(value: Any) -> WorkPlanRecord | None:
    """从 execution_context.work_plan 还原计划；非法/旧结构返回 None（安全回退）。"""
    if not isinstance(value, dict):
        return None
    try:
        return WorkPlanRecord.model_validate(value)
    except Exception:  # noqa: BLE001 旧快照不阻断执行
        return None
