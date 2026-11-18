"""Agent 测评数据模型：数据集 / 测评运行 / 测评结果。

- eval_datasets：测评数据集（含社区通用基准导入），可指定默认评分方式；
- eval_dataset_items：数据集用例（输入 / 期望 / 评分标准）；
- eval_runs：一次测评任务（对某 Agent × 某数据集批量跑用例）；
- eval_run_items：单用例测评结果（快照 + 实际输出 + 评分 + 运行统计）。

新表由 init_db() 自动创建（开发环境），生产环境由 Alembic 迁移创建。
"""
from datetime import datetime

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text,
    Index, func,
)

from app.models.base import Base


class EvalDataset(Base):
    """测评数据集：一组结构化用例 + 默认评分方式。"""

    __tablename__ = "eval_datasets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False, unique=True, index=True)
    description = Column(Text, default="")
    # 来源标记：manual（手工创建）/ community（社区导入）/ import（JSONL 导入）
    source = Column(String(32), default="manual", nullable=False)
    # 分类标签：general / math / tool-use 等
    category = Column(String(64), default="general")
    # 数据集默认评分方式：exact_match / contains / keyword / regex / llm_judge / none
    scoring_method = Column(String(24), default="llm_judge", nullable=False)
    # 社区数据集出处（URL / 论文 / license），导入时记录
    reference = Column(String(500), nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class EvalDatasetItem(Base):
    """数据集用例：input 为提示词/问题，expected 为期望答案或判定依据。"""

    __tablename__ = "eval_dataset_items"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(Integer, ForeignKey("eval_datasets.id"), nullable=False, index=True)
    # 用例在数据集内的顺序（测评按 position 顺序执行）
    position = Column(Integer, nullable=False, default=0)

    input = Column(Text, nullable=False)          # 输入（用户消息/问题）
    expected = Column(Text, default="")           # 期望输出（参考答案/判分依据）
    criteria = Column(Text, default="")           # 评分标准（给 LLM 裁判的补充说明）
    # 用例级评分方式覆盖；缺省用数据集默认
    scoring_method = Column(String(24), nullable=True)
    scoring_config = Column(JSON, default=dict)   # 评分参数（keywords/regex/pattern 等）
    item_metadata = Column("metadata", JSON, default=dict)  # 外部基准的原始 id/难度等

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_eval_dataset_item_dataset_pos", "dataset_id", "position"),
    )


class EvalRun(Base):
    """一次测评任务：Agent × 数据集 的批量运行与聚合结果。"""

    __tablename__ = "eval_runs"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(160), nullable=False)
    dataset_id = Column(Integer, ForeignKey("eval_datasets.id"), nullable=False, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True)
    agent_version_id = Column(Integer, ForeignKey("agent_versions.id"), nullable=True)
    # pending / running / completed / failed / cancelled / cancel_requested
    status = Column(String(24), nullable=False, default="pending", index=True)

    config = Column(JSON, default=dict)       # approval_mode / use_memory / timeout_s / limit 等
    aggregate = Column(JSON, default=dict)    # total/completed/passed/failed/pass_rate/avg_score/...
    error = Column(Text, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_eval_run_dataset_created", "dataset_id", "created_at"),
    )


class EvalRunItem(Base):
    """单用例测评结果：输入/期望/评分方式快照 + 实际输出 + 评分 + 运行统计。"""

    __tablename__ = "eval_run_items"

    id = Column(Integer, primary_key=True, index=True)
    eval_run_id = Column(Integer, ForeignKey("eval_runs.id"), nullable=False, index=True)
    dataset_item_id = Column(Integer, ForeignKey("eval_dataset_items.id"), nullable=True)
    position = Column(Integer, nullable=False, default=0)

    # 快照：数据集后续改动不影响历史测评结果
    snap_input = Column(Text, nullable=False)
    snap_expected = Column(Text, default="")
    snap_criteria = Column(Text, default="")
    snap_scoring_method = Column(String(24), nullable=False, default="llm_judge")
    snap_scoring_config = Column(JSON, default=dict)

    # 关联的 AgentRun（每用例一次真实 Run）；失败/跳过时为空
    run_id = Column(String(36), nullable=True, index=True)
    # pending / running / passed / failed / error / skipped
    status = Column(String(16), nullable=False, default="pending", index=True)

    actual_output = Column(Text, default="")   # Agent 实际输出
    error = Column(Text, nullable=True)        # 执行/评分异常
    score = Column(Float, nullable=True)       # 归一化得分 0~10
    passed = Column(Boolean, nullable=True)    # 是否通过
    score_detail = Column(JSON, default=dict)  # 评分明细（裁判 reasoning 等）
    stats = Column(JSON, default=dict)         # duration_s / tokens / steps / tool_calls

    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_eval_run_item_run_pos", "eval_run_id", "position"),
    )
