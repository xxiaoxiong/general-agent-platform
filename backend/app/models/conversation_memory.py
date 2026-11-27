"""历史会话索引与记忆设置模型（Conversation Memory 阶段一）。

设计要点（任务书 §5 / §8 / §9）：
- 原始会话（``agent_sessions.messages``）仍是唯一事实来源；本模块只存派生索引；
- 两级索引：``turn``（一轮用户+助手）与 ``segment``（连续多轮主题片段）；
- 权限元数据全部由服务端写入（user_id / project_id / visibility_scope），不由模型生成；
- 删除/排除先置位（检索立即不可见），Qdrant 清理异步执行；
- 索引 Job 与消息写入同事务入队，服务重启后 pending 可继续。
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Index, Integer, String, Text, UniqueConstraint,
)

from app.models.base import BaseModel


class ConversationIndexEntry(BaseModel):
    """会话索引条目（Message/Turn Index 与 Conversation Segment Index）。"""

    __tablename__ = "conversation_index_entries"
    __table_args__ = (
        # 同一会话同一消息（或同一 segment 指纹）只保留一条，重复索引为幂等更新
        UniqueConstraint("session_id", "entry_key", name="uq_conversation_index_entry"),
        Index("ix_conversation_index_owner", "user_id", "is_deleted", "is_excluded"),
        Index("ix_conversation_index_session", "session_id", "is_deleted"),
        Index("ix_conversation_index_project", "project_id", "visibility_scope", "is_deleted"),
        Index("ix_conversation_index_created", "user_id", "message_created_at"),
        Index("ix_conversation_index_version", "index_version", "embedding_status"),
    )

    entry_key = Column(String(160), nullable=False)
    session_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, nullable=False)
    agent_id = Column(Integer, nullable=True)
    project_id = Column(Integer, nullable=True)
    # turn | segment
    entry_type = Column(String(16), default="turn", nullable=False)
    # 来源消息 ID 列表（turn 通常为 [user_message_id, assistant_message_id]）
    message_ids = Column(JSON, default=list, nullable=False)
    role = Column(String(16), default="turn", nullable=False)
    content = Column(Text, nullable=False)
    content_hash = Column(String(64), nullable=False, index=True)
    token_count = Column(Integer, default=0, nullable=False)
    # 原始消息时间（用于时间排序/展示，不是索引写入时间）
    message_created_at = Column(DateTime, nullable=True)
    index_version = Column(String(80), default="", nullable=False)
    # user（仅所有者可见）| project（显式共享给项目成员）
    visibility_scope = Column(String(16), default="user", nullable=False)
    is_deleted = Column(Boolean, default=False, nullable=False)
    is_excluded = Column(Boolean, default=False, nullable=False)
    # pending | indexed | degraded（Embedding 不可用，仅全文可用）
    embedding_status = Column(String(16), default="pending", nullable=False)
    embedding_model = Column(String(160), default="", nullable=False)
    embedding_dimension = Column(Integer, default=0, nullable=False)


class ConversationIndexJob(BaseModel):
    """索引 Job（Durable Outbox）：与消息写入/删除同事务入队。"""

    __tablename__ = "conversation_index_jobs"
    __table_args__ = (
        UniqueConstraint("job_key", name="uq_conversation_index_job_key"),
        Index("ix_conversation_index_jobs_status_created", "status", "created_at"),
        Index("ix_conversation_index_jobs_session", "session_id", "status"),
        Index("ix_conversation_index_jobs_lease", "status", "lease_expires_at"),
    )

    job_key = Column(String(200), nullable=False)
    # index（单会话增量）/ delete_conversation / reindex_user（用户全量重建）
    job_type = Column(String(32), default="index", nullable=False)
    session_id = Column(Integer, nullable=True, index=True)
    user_id = Column(Integer, nullable=True, index=True)
    message_ids = Column(JSON, default=list, nullable=False)
    status = Column(String(16), default="pending", nullable=False, index=True)
    attempts = Column(Integer, default=0, nullable=False)
    lease_expires_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    finished_at = Column(DateTime, nullable=True)


class ConversationMemorySetting(BaseModel):
    """用户级记忆开关（历史检索 / 自动预取 / 项目共享读取）。"""

    __tablename__ = "conversation_memory_settings"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_conversation_memory_setting_user"),
    )

    user_id = Column(Integer, nullable=False, index=True)
    history_enabled = Column(Boolean, default=True, nullable=False)
    auto_retrieval_enabled = Column(Boolean, default=True, nullable=False)
    project_memory_enabled = Column(Boolean, default=True, nullable=False)


class ConversationMemoryExclusion(BaseModel):
    """用户显式排除的会话（不参与后续检索）。"""

    __tablename__ = "conversation_memory_exclusions"
    __table_args__ = (
        UniqueConstraint("user_id", "conversation_id", name="uq_conversation_memory_exclusion"),
    )

    user_id = Column(Integer, nullable=False, index=True)
    conversation_id = Column(Integer, nullable=False, index=True)
    reason = Column(String(200), nullable=True)


class ConversationMemoryShare(BaseModel):
    """会话显式共享到项目（可撤销；仅会话所有者可创建/撤销）。

    ``conversation_id`` 唯一：一个会话最多共享到一个项目（即其当前项目）。
    """

    __tablename__ = "conversation_memory_shares"
    __table_args__ = (
        UniqueConstraint("conversation_id", name="uq_conversation_memory_share_conversation"),
        Index("ix_conversation_memory_share_project", "project_id"),
    )

    conversation_id = Column(Integer, nullable=False, index=True)
    project_id = Column(Integer, nullable=False)
    shared_by = Column(Integer, nullable=False)
    revoked_at: Optional[datetime] = Column(DateTime, nullable=True)
