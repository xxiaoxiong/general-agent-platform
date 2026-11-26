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

# ── 测评状态口径（任务书 §4.3：执行 / 验证 / 评分 / 最终结果分离）──
EXECUTION_STATUSES = (
    "pending", "running", "succeeded", "agent_failed", "timeout",
    "infra_error", "cancelled",
)
VERIFICATION_STATUSES = ("pending", "passed", "failed", "unscored", "error")
GRADING_STATUSES = ("pending", "graded", "unscored", "error", "needs_review")
OUTCOMES = ("pending", "pass", "fail", "inconclusive", "infra_error",
            "cancelled", "unscored")

# 能力维度（任务书 §4.5）：任务测量什么，与复杂度（任务多难）正交
CAPABILITY_DIMENSIONS = (
    "task_understanding", "planning_reasoning", "tool_use", "multi_tool",
    "context_memory", "error_recovery", "delivery", "reliability_safety",
)


class EvalSuite(Base):
    """测评套件：把多个 Dataset 组合成一套测评标准。"""

    __tablename__ = "eval_suites"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False, unique=True, index=True)
    description = Column(Text, default="")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class EvalSuiteDataset(Base):
    """套件 → 数据集组成（顺序即套件内执行顺序）。"""

    __tablename__ = "eval_suite_datasets"

    id = Column(Integer, primary_key=True, index=True)
    suite_id = Column(Integer, ForeignKey("eval_suites.id"), nullable=False, index=True)
    dataset_id = Column(Integer, ForeignKey("eval_datasets.id"), nullable=False, index=True)
    position = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_eval_suite_dataset_suite_pos", "suite_id", "position"),
    )


class EvalSuiteVersion(Base):
    """套件版本：固定一版测评内容（数据集 + 用例全量快照），保证历史可复现。"""

    __tablename__ = "eval_suite_versions"

    id = Column(Integer, primary_key=True, index=True)
    suite_id = Column(Integer, ForeignKey("eval_suites.id"), nullable=False, index=True)
    version_no = Column(Integer, nullable=False)
    # 快照：{"version_no", "datasets": [{"dataset_id","name","scoring_method","items":[...]}], "digest"}
    snapshot = Column(JSON, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_eval_suite_version_suite_no", "suite_id", "version_no", unique=True),
    )


class EvalGrade(Base):
    """评分记录：Verifier / Judge 对 Trial 的评分历史（支持重新评分与追溯）。"""

    __tablename__ = "eval_grades"

    id = Column(Integer, primary_key=True, index=True)
    trial_id = Column(Integer, ForeignKey("eval_run_items.id"), nullable=False, index=True)
    # verifier（确定性验证）/ judge（主观质量评估）
    kind = Column(String(16), nullable=False, default="verifier")
    name = Column(String(64), nullable=False, default="")
    status = Column(String(16), nullable=False, default="unscored")
    score = Column(Float, nullable=True)
    passed = Column(Boolean, nullable=True)
    detail = Column(JSON, default=dict)
    evidence = Column(JSON, default=dict)
    model_config_id = Column(Integer, nullable=True)
    prompt_version = Column(String(32), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_eval_grade_trial_kind", "trial_id", "kind"),
    )


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

    # 复杂任务体系：客观能力对照维度（评测 Agent 能执行多复杂的任务）
    # 复杂度分级：L1 直接回答 / L2 单步工具或简单推理 / L3 多步推理或多工具协同 / L4 长程复合·对抗鲁棒
    complexity = Column(String(16), nullable=False, default="L2")
    # 任务类型：direct_qa / multi_step_reasoning / tool_use / instruction_following / multi_turn / adversarial
    task_type = Column(String(32), nullable=False, default="direct_qa")
    # 能力维度（task_understanding / planning_reasoning / tool_use / multi_tool /
    # context_memory / error_recovery / delivery / reliability_safety）；
    # 为空时按 task_type 的固定映射归档
    capability = Column(String(32), nullable=True)
    required_tools = Column(JSON, default=list)       # 任务设计要求使用的工具（客观对照，可空）
    expected_min_steps = Column(Integer, nullable=True)  # 期望最少执行步数（客观对照，可空）
    turns = Column(JSON, default=list)                # 多轮任务的后续用户消息（input 为首轮）

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_eval_dataset_item_dataset_pos", "dataset_id", "position"),
    )


class EvalRun(Base):
    """一次测评任务（Campaign）：Agent × 数据集/套件 的批量运行与聚合结果。

    兼容说明：``dataset_id`` 保留为「主数据集」锚点（套件任务取套件首个数据集），
    套件全量组成与快照保存在 ``suite_version_id`` 指向的 EvalSuiteVersion；
    每条 Trial 的归属数据集记录在 EvalRunItem.dataset_id。
    """

    __tablename__ = "eval_runs"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(160), nullable=False)
    dataset_id = Column(Integer, ForeignKey("eval_datasets.id"), nullable=False, index=True)
    # 套件测评（可空：旧数据集测评无套件）
    suite_id = Column(Integer, ForeignKey("eval_suites.id"), nullable=True, index=True)
    suite_version_id = Column(Integer, ForeignKey("eval_suite_versions.id"),
                              nullable=True, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True)
    # 被测对象固定版本（P0）：Campaign 创建时固定，执行期不再解析最新版本
    agent_version_id = Column(Integer, ForeignKey("agent_versions.id"), nullable=True)
    # pending / running / completed / failed / cancelled / cancel_requested
    status = Column(String(24), nullable=False, default="pending", index=True)
    # 同一 Case 重复 Trial 数（稳定性评估：pass@k / pass^k）
    trials_per_case = Column(Integer, nullable=False, default=1, server_default="1")

    config = Column(JSON, default=dict)       # approval_mode / use_memory / timeout_s / limit 等
    # 被测对象与环境快照：agent/runtime/model/suite 版本、工具摘要、环境信息
    environment = Column(JSON, default=dict)
    aggregate = Column(JSON, default=dict)    # total/completed/passed/failed/pass_rate/...
    error = Column(Text, nullable=True)

    # ── 持久化 Evaluation Worker（Lease / Heartbeat / 恢复）──
    cancel_requested = Column(Boolean, default=False, nullable=False, server_default="0")
    worker_id = Column(String(128), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)
    lease_version = Column(Integer, default=0, nullable=False, server_default="0")
    attempt = Column(Integer, default=0, nullable=False, server_default="0")

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_eval_run_dataset_created", "dataset_id", "created_at"),
        Index("ix_eval_run_status_lease", "status", "lease_expires_at"),
    )


class EvalRunItem(Base):
    """单条测评结果（Trial）：输入/期望/评分方式快照 + 实际输出 + 评分 + 运行统计。

    ``status`` 为兼容旧 API 的结果镜像（pending/running/passed/failed/error/skipped），
    新状态口径见 execution_status / verification_status / grading_status / outcome。
    """

    __tablename__ = "eval_run_items"

    id = Column(Integer, primary_key=True, index=True)
    eval_run_id = Column(Integer, ForeignKey("eval_runs.id"), nullable=False, index=True)
    dataset_item_id = Column(Integer, ForeignKey("eval_dataset_items.id"), nullable=True)
    # 套件任务中该 Trial 归属的数据集（旧数据为空，按 eval_runs.dataset_id 解释）
    dataset_id = Column(Integer, ForeignKey("eval_datasets.id"), nullable=True, index=True)
    position = Column(Integer, nullable=False, default=0)
    # 重复测评序号（0 起）：同一 Case 的第几次 Trial
    trial_index = Column(Integer, nullable=False, default=0, server_default="0")
    # 基础设施故障重试次数（Attempt）；Agent 自身失败不自动重试
    attempt = Column(Integer, nullable=False, default=0, server_default="0")

    # 快照：数据集后续改动不影响历史测评结果
    snap_input = Column(Text, nullable=False)
    snap_expected = Column(Text, default="")
    snap_criteria = Column(Text, default="")
    snap_scoring_method = Column(String(24), nullable=False, default="llm_judge")
    snap_scoring_config = Column(JSON, default=dict)
    # 复杂任务体系快照（历史测评不受数据集后续改动影响）
    snap_complexity = Column(String(16), nullable=False, default="L2")
    snap_task_type = Column(String(32), nullable=False, default="direct_qa")
    snap_capability = Column(String(32), nullable=True)
    snap_meta = Column(JSON, default=dict)  # required_tools / expected_min_steps / turns / metadata

    # 关联的 AgentRun（每用例一次真实 Run）；失败/跳过时为空
    run_id = Column(String(36), nullable=True, index=True)
    # 兼容旧 API 的结果镜像：pending / running / passed / failed / error / skipped / unscored
    status = Column(String(16), nullable=False, default="pending", index=True)
    # 分离状态（任务书 §4.3）
    execution_status = Column(String(24), nullable=False, default="pending", index=True)
    verification_status = Column(String(16), nullable=False, default="pending")
    grading_status = Column(String(16), nullable=False, default="pending")
    outcome = Column(String(24), nullable=False, default="pending", index=True)

    actual_output = Column(Text, default="")   # Agent 实际输出
    error = Column(Text, nullable=True)        # 执行/评分异常
    score = Column(Float, nullable=True)       # 归一化得分 0~10
    passed = Column(Boolean, nullable=True)    # 是否通过
    score_detail = Column(JSON, default=dict)  # 评分明细（裁判 reasoning 等）
    stats = Column(JSON, default=dict)         # duration_s / tokens / steps / tool_calls / artifacts

    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_eval_run_item_run_pos", "eval_run_id", "position"),
        Index("ix_eval_run_item_case_trial", "eval_run_id", "dataset_item_id", "trial_index"),
    )
