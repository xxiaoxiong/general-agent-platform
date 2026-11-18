"""自进化闭环数据模型：评估 / 进化报告 / 变更审计（含回滚与验证）。

- agent_evaluations：按 Run 粒度的评估结果（确定性指标 + LLM-as-judge）；
- evolution_reports：优化方向报告（建议清单 + 前后指标对比）；
- evolution_change_log：建议应用/回滚审计（before/after + 验证结果）。
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, Float,
    ForeignKey, JSON, Index, func,
)
from app.models.base import Base


class AgentEvaluation(Base):
    __tablename__ = "agent_evaluations"

    id = Column(Integer, primary_key=True, index=True)
    # 一个 Run 只评估一次（INSERT UNIQUE 抢占防并发重复）
    run_id = Column(String(36), ForeignKey("agent_runs.id"), nullable=False, unique=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True)
    agent_version_id = Column(Integer, ForeignKey("agent_versions.id"), nullable=True, index=True)
    session_id = Column(Integer, ForeignKey("agent_sessions.id"), nullable=True)

    status = Column(String(16), nullable=False, default="pending", index=True)  # pending/computed/failed
    success = Column(Boolean, nullable=True)          # 任务是否判定成功
    overall = Column(Float, nullable=True)            # 综合分 0~10

    deterministic = Column(JSON, default=dict)        # 确定性指标字典
    judge_scores = Column(JSON, default=dict)         # LLM-as-judge 分维度评分
    judge_model_config_id = Column(Integer, nullable=True)
    langfuse_trace_id = Column(String(64), nullable=True)

    error = Column(Text, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    evaluated_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_agent_eval_agent_created", "agent_id", "created_at"),
    )


class EvolutionReport(Base):
    __tablename__ = "evolution_reports"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True)
    window_start = Column(DateTime, nullable=False)
    window_end = Column(DateTime, nullable=False)
    baseline_version_no = Column(Integer, nullable=True)

    status = Column(String(16), nullable=False, default="draft", index=True)
    # draft / confirmed / applied / validated / rolled_back

    summary_text = Column(Text, default="")           # 人读摘要（Markdown）
    suggestions = Column(JSON, default=list)          # 建议清单（结构见 evolution_advisor）
    metrics_before = Column(JSON, default=dict)       # 窗口基线指标
    metrics_after = Column(JSON, default=dict)        # 应用后验证指标

    created_by = Column(String(64), nullable=True)
    validated_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_evolution_report_agent_created", "agent_id", "created_at"),
    )


class EvolutionChangeLog(Base):
    __tablename__ = "evolution_change_log"

    id = Column(Integer, primary_key=True, index=True)
    report_id = Column(Integer, ForeignKey("evolution_reports.id"), nullable=False, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True)

    change_type = Column(String(24), nullable=False)  # prompt/param/model/tools_flow
    target = Column(String(128), nullable=False)      # system_prompt/max_iterations/model_config_id/...
    before = Column(JSON, nullable=True)
    after = Column(JSON, nullable=True)

    applied_version_no = Column(Integer, nullable=True)  # 应用后发布的新版本号
    status = Column(String(16), nullable=False, default="applied", index=True)
    # applied / validated / reverted
    validation = Column(JSON, nullable=True)          # 前后指标对比结果

    revert_of_change_id = Column(Integer, nullable=True)  # 若本行是回滚操作，指向原变更
    applied_by = Column(String(64), nullable=True)
    applied_at = Column(DateTime, nullable=False, server_default=func.now())
    reverted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_evolution_change_agent_applied", "agent_id", "applied_at"),
    )
