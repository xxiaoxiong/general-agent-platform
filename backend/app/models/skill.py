"""Skill 管理模型（扩展性治理：技能管理模块）。

Skill = 指令文本（SKILL.md：YAML frontmatter + 正文），只做 prompt 注入，
不携带工具授权（工具面始终由 Capability Registry / ToolPolicy 决定）。

- 内置（builtin）与目录（AGENT_SKILLS_DIR）技能仍由 SkillRegistry 提供；
- 本表承载**管理面创建的技能**（API 增删改查 + 启停 + Agent 级绑定）；
- 新表由 init_db() 自动创建（不动已有表结构，无需 Alembic 迁移）。
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.models.base import Base


class Skill(Base):
    __tablename__ = "skills"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, unique=True, index=True)
    description = Column(Text, default="")
    version = Column(String(20), default="1.0")
    # SKILL.md 全文（frontmatter + 正文）；frontmatter 缺失时服务层自动补齐
    body = Column(Text, nullable=False)
    # 启用状态（禁用后不再注入任何 Agent）
    enabled = Column(Boolean, default=True, nullable=False)
    # Agent 级绑定：None = 全局注入（默认）；[] = 不注入任何 Agent；[1,2] = 仅注入指定 Agent
    agent_ids = Column(JSON, nullable=True, default=None)
    # 来源标记：managed（管理面创建）/ community（社区导入）等
    source = Column(String(32), default="managed", nullable=False)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
