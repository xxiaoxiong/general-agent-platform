"""Goal Runtime：目标契约与目标迭代（任务书 Phase 1/2）。

设计边界：
- Goal 是目标控制层，不新建第二套 Agent Runtime；
- ``GoalContract`` 是一轮目标（可跨多个 Run 续做）的持久化事实来源，
  验收条件带稳定 ID 与状态，用户修改目标时递增 ``revision``；
- ``GoalIteration`` 记录每一轮的结构化结果、证据引用与停滞签名，
  单轮 Agent 结束不等于整个 Goal 结束；
- 进度由必需验收项确定性计算，不允许模型随意填写。
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)

from app.models.base import BaseModel


class GoalContract(BaseModel):
    """目标契约（一个目标可跨多个 Run 持续执行）。"""

    __tablename__ = "goal_contracts"
    __table_args__ = (
        UniqueConstraint("goal_id", name="uq_goal_contracts_goal"),
        Index("ix_goal_contracts_session_status", "session_id", "status"),
        Index("ix_goal_contracts_run", "run_id"),
        Index("ix_goal_contracts_user_created", "user_id", "created_at"),
    )

    goal_id = Column(String(36), nullable=False)
    # 当前执行该目标的 Run；origin_run_id 保留首次创建来源，便于跨 Run 追溯
    run_id = Column(String(36), ForeignKey("agent_runs.id"), nullable=False)
    origin_run_id = Column(String(36), nullable=True)
    session_id = Column(Integer, ForeignKey("agent_sessions.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    agent_id = Column(Integer, nullable=True)

    revision = Column(Integer, default=1, nullable=False)
    objective = Column(Text, nullable=False)
    # 验收条件：id / description / verification_type / required / status /
    # check_ids / evidence_refs / last_checked_at / failure_reason / next_step / source
    acceptance_criteria = Column(JSON, default=list, nullable=False)
    constraints = Column(JSON, default=list, nullable=False)
    deliverables = Column(JSON, default=list, nullable=False)
    stop_conditions = Column(JSON, default=list, nullable=False)
    # 版本变更记录（追加式）：revision / at / source / summary / changes
    revisions = Column(JSON, default=list, nullable=False)

    # active / completed / blocked / paused / cancelled / failed / superseded
    status = Column(String(24), default="active", nullable=False, index=True)
    iterations = Column(Integer, default=0, nullable=False)
    progress = Column(JSON, default=dict, nullable=False)
    last_iteration_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)


class GoalIteration(BaseModel):
    """目标的一轮执行与验收结果（结构化，不只依赖自然语言总结）。"""

    __tablename__ = "goal_iterations"
    __table_args__ = (
        UniqueConstraint("goal_id", "iteration_no", name="uq_goal_iteration_no"),
        Index("ix_goal_iterations_run", "run_id"),
    )

    goal_id = Column(
        String(36), ForeignKey("goal_contracts.goal_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    run_id = Column(String(36), ForeignKey("agent_runs.id"), nullable=False)
    iteration_no = Column(Integer, nullable=False)
    # running / completed / continue / paused / blocked / failed
    status = Column(String(24), default="running", nullable=False)
    # deep_agent / team / document / research / work_orchestrator
    executor = Column(String(32), default="deep_agent", nullable=False)
    plan = Column(JSON, default=list, nullable=False)
    result = Column(JSON, default=dict, nullable=False)
    evidence_refs = Column(JSON, default=list, nullable=False)
    stall_signature = Column(String(64), nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
