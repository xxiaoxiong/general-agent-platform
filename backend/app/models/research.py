"""Deep Research：研究计划、来源、证据与主张（任务书 Phase 3）。

结构边界（任务书 §6.3）：
- Source  = 发现并可读取的来源（URL / 标题 / 时间 / 哈希 / 正文位置）；
- Evidence= 来源中的原文片段 + Locator + 对应研究问题；
- Claim   = 主张 + 支持/反驳证据 + 核验状态；
- 搜索发现 ≠ 读取；读取 ≠ 相关；相关 ≠ 支持。状态由服务端确定性流转。
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON, Column, DateTime, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGTEXT

from app.models.base import BaseModel


class ResearchPlan(BaseModel):
    """持久化研究计划（可跨 Run 续做）。"""

    __tablename__ = "research_plans"
    __table_args__ = (
        UniqueConstraint("research_id", name="uq_research_plans_research"),
        Index("ix_research_plans_goal", "goal_id"),
        Index("ix_research_plans_run", "run_id"),
        Index("ix_research_plans_session", "session_id", "status"),
    )

    research_id = Column(String(36), nullable=False)
    goal_id = Column(String(36), nullable=True, index=True)
    run_id = Column(String(36), nullable=False)
    session_id = Column(Integer, nullable=True)
    user_id = Column(Integer, nullable=False)

    revision = Column(Integer, default=1, nullable=False)
    topic = Column(Text, default="", nullable=False)
    # {time_range, geography, source_preferences, excluded_sources}
    scope = Column(JSON, default=dict, nullable=False)
    # [{id, question, priority, status, queries, claim_refs, gaps}]
    questions = Column(JSON, default=list, nullable=False)
    report_outline = Column(JSON, default=list, nullable=False)
    # planning / researching / synthesizing / completed / blocked
    status = Column(String(24), default="planning", nullable=False)
    # 计划变更记录：revision / at / reason / changes
    revisions = Column(JSON, default=list, nullable=False)
    finished_at = Column(DateTime, nullable=True)


class ResearchSource(BaseModel):
    """研究来源（只登记真实读取过的来源才可进入 Evidence）。"""

    __tablename__ = "research_sources"
    __table_args__ = (
        UniqueConstraint("research_id", "source_id", name="uq_research_source"),
        Index("ix_research_sources_research", "research_id"),
    )

    research_id = Column(String(36), nullable=False)
    source_id = Column(String(40), nullable=False)
    url = Column(Text, nullable=False)
    title = Column(Text, default="", nullable=False)
    # official / docs / source_code / news / blog / paper / other
    source_type = Column(String(32), default="other", nullable=False)
    author = Column(String(200), nullable=True)
    published_at = Column(String(64), nullable=True)
    retrieved_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    domain = Column(String(255), nullable=True)
    http_status = Column(Integer, nullable=True)
    content_hash = Column(String(64), nullable=True)
    content_location = Column(String(500), nullable=True)
    # 全文供服务端证据定位；长文档分块读取不截断存储
    content_excerpt = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=True)
    # discovered / read（未读取成功的来源不得作为证据）
    status = Column(String(24), default="discovered", nullable=False)


class ResearchEvidence(BaseModel):
    """来源中的原文证据片段。"""

    __tablename__ = "research_evidence"
    __table_args__ = (
        UniqueConstraint("research_id", "evidence_id", name="uq_research_evidence"),
        Index("ix_research_evidence_research", "research_id"),
        Index("ix_research_evidence_question", "research_id", "question_id"),
    )

    research_id = Column(String(36), nullable=False)
    evidence_id = Column(String(40), nullable=False)
    source_id = Column(String(40), nullable=False)
    question_id = Column(String(40), nullable=True)
    quote = Column(Text, nullable=False)
    locator = Column(String(500), nullable=True)
    # extracted / verified / rejected
    status = Column(String(24), default="extracted", nullable=False)
    extracted_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ResearchClaim(BaseModel):
    """研究主张及支持/反驳证据。"""

    __tablename__ = "research_claims"
    __table_args__ = (
        UniqueConstraint("research_id", "claim_id", name="uq_research_claim"),
        Index("ix_research_claims_research", "research_id"),
        Index("ix_research_claims_question", "research_id", "question_id"),
    )

    research_id = Column(String(36), nullable=False)
    claim_id = Column(String(40), nullable=False)
    question_id = Column(String(40), nullable=True)
    statement = Column(Text, nullable=False)
    # pending / supported / conflicting / insufficient / verified
    status = Column(String(24), default="pending", nullable=False)
    support_evidence = Column(JSON, default=list, nullable=False)
    contradict_evidence = Column(JSON, default=list, nullable=False)
    verification = Column(JSON, default=dict, nullable=False)
