"""AgentVersion：不可变发布版本（P2-01）。

- 发布即快照：把 Agent 当前配置固化为不可变 ``snapshot``（版本号递增）；
- Run 创建时绑定版本（``ensure_version``：无版本时自动发布 v1），
  执行期只读，不随 Agent 行后续修改变化；
- 运行时配置从快照还原（``config_from_version``），与旧 agent_config 兼容。

版本内容（任务书 §4.1）：system instructions / model policy / capability IDs /
toolset version / skill references / subagent profiles / resource bindings /
memory policy / execution budget / security policy / version number /
created_by / published_at。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.agent import Agent, AgentVersion

# 能力包默认集（P2-02 Capability Registry 接管后由 resolve_capabilities 驱动）
DEFAULT_CAPABILITY_IDS: List[str] = ["general"]


def build_snapshot(
    agent: Agent,
    *,
    capability_ids: Optional[List[str]] = None,
    created_by: Optional[int] = None,
    version_no: int = 1,
) -> Dict[str, Any]:
    """由 Agent 行构建不可变版本快照。"""
    return {
        "version_no": version_no,
        # system instructions
        "system_prompt": agent.system_prompt or "",
        # model policy（模型选择由平台决定；这里固化绑定的模型配置）
        "model_policy": {
            "model_config_id": agent.model_config_id,
            "temperature": str(agent.temperature or "0.7"),
            "max_iterations": agent.max_iterations or 10,
            "max_tokens": agent.max_tokens or 4096,
        },
        # capability IDs + toolset（P3-01：优先 Agent 行 capability_ids）
        "capability_ids": list(
            capability_ids
            or getattr(agent, "capability_ids", None)
            or DEFAULT_CAPABILITY_IDS
        ),
        "tools": list(agent.tools or []),
        "toolset_version": 1,
        # skill references / subagent profiles（P2-05/P2-06 注册表接管后填充）
        "skill_sources": [],
        "subagent_profiles": [],
        # resource bindings
        "resource_bindings": {
            "knowledge_base_id": agent.knowledge_base_id,
            "database_connection_id": agent.database_connection_id,
        },
        # memory policy（P2-04 Store 接管后细化）
        "memory_policy": {"use_memory_context": True},
        # execution budget
        "execution_budget": {
            "max_steps": settings.AGENT_MAX_STEPS,
            "max_tool_calls": settings.AGENT_MAX_TOOL_CALLS,
            "max_model_calls": settings.AGENT_MAX_MODEL_CALLS,
            "run_timeout_seconds": settings.AGENT_RUN_TIMEOUT,
        },
        # security policy
        "security_policy": {
            "sandbox_mode": getattr(settings, "AGENT_SANDBOX_MODE", "restricted"),
            "enable_hitl": getattr(settings, "AGENT_ENABLE_HITL", True),
            "allow_network": getattr(settings, "AGENT_ALLOW_NETWORK", True),
        },
        # 审计
        "created_by": created_by,
        "published_at": datetime.utcnow().isoformat(),
    }


async def publish_version(
    db: AsyncSession,
    agent_id: int,
    *,
    created_by: Optional[int] = None,
    capability_ids: Optional[List[str]] = None,
) -> AgentVersion:
    """发布新版本（版本号自增，快照固化当前配置）。"""
    agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    if agent is None:
        raise ValueError(f"Agent {agent_id} 不存在")
    last = (await db.execute(
        select(AgentVersion.version_no)
        .where(AgentVersion.agent_id == agent_id)
        .order_by(AgentVersion.version_no.desc()).limit(1)
    )).scalar_one_or_none()
    version_no = (last or 0) + 1
    row = AgentVersion(
        agent_id=agent_id,
        version_no=version_no,
        snapshot=build_snapshot(
            agent, capability_ids=capability_ids,
            created_by=created_by, version_no=version_no,
        ),
        created_by=created_by,
    )
    db.add(row)
    await db.flush()
    return row


async def latest_version(db: AsyncSession, agent_id: int) -> Optional[AgentVersion]:
    return (await db.execute(
        select(AgentVersion)
        .where(AgentVersion.agent_id == agent_id)
        .order_by(AgentVersion.version_no.desc()).limit(1)
    )).scalar_one_or_none()


async def ensure_version(
    db: AsyncSession, agent_id: int, *, created_by: Optional[int] = None
) -> Optional[AgentVersion]:
    """取最新发布版本；真实 Agent 无版本时自动发布 v1。

    Agent 行不存在（合成 agent_config，如工作流内联配置/测试）时返回 None，
    Run 不绑定版本——版本绑定针对平台管理的真实 Agent。
    """
    version = await latest_version(db, agent_id)
    if version is not None:
        return version
    agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    if agent is None:
        return None
    return await publish_version(db, agent_id, created_by=created_by)


def config_from_version(version: AgentVersion) -> Dict[str, Any]:
    """版本快照 → 运行时 agent_config（与旧调用方形态兼容）。"""
    snap: Dict[str, Any] = dict(version.snapshot or {})
    model_policy = snap.get("model_policy") or {}
    resources = snap.get("resource_bindings") or {}
    return {
        "id": version.agent_id,
        "agent_version_id": version.id,
        "system_prompt": snap.get("system_prompt", ""),
        "model_config_id": model_policy.get("model_config_id"),
        "temperature": model_policy.get("temperature", "0.7"),
        "max_iterations": model_policy.get("max_iterations", 10),
        "max_tokens": model_policy.get("max_tokens", 4096),
        "tools": snap.get("tools") or [],
        "capability_ids": snap.get("capability_ids") or [],
        "knowledge_base_id": resources.get("knowledge_base_id"),
        "database_connection_id": resources.get("database_connection_id"),
    }
