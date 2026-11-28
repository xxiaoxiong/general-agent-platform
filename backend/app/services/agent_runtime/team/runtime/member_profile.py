"""成员 Profile：角色、Prompt、模型、工具白名单与预算（任务书 §3.1/§3.5）。

Profile 只描述“这个成员被允许做什么”，真正的工具授权仍由父 Run 的
ToolPolicy + Team Registry 在服务端执行前校验；模型不能通过参数扩大权限。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.config import settings

_ROLE_PROMPT = (
    "你是团队中的「{role}」。你只负责完成分配给你的单个任务，"
    "交付完整、可核验的结果，并如实说明证据缺口与不确定性。"
)


@dataclass(frozen=True)
class MemberProfile:
    role: str
    name: str
    description: str = ""
    system_prompt: str = ""
    # 空 = 继承父 Agent 已授权的只读工具面；非空 = 在该交集内再收窄
    tools: Optional[frozenset[str]] = None
    # 角色指定模型（服务端校验存在/启用/支持工具调用）；空 = 继承团队默认模型
    model_config_id: Optional[int] = None
    max_model_calls: int = 8
    max_tool_calls: int = 12
    timeout_seconds: float = 300.0
    max_output_tokens: int = 4096
    # 阶段一默认关闭递归派生（任务书 §3.5）
    allow_spawn: bool = False
    skills: tuple[str, ...] = field(default_factory=tuple)

    def validate(self) -> None:
        if not self.role.strip():
            raise ValueError("成员 role 不能为空")
        if len(self.role) > 60:
            raise ValueError("成员 role 过长（>60）")
        if int(self.max_model_calls) < 1:
            raise ValueError("成员 max_model_calls 必须大于 0")
        if int(self.max_tool_calls) < 0:
            raise ValueError("成员 max_tool_calls 不得为负数")
        if float(self.timeout_seconds) < 0:
            raise ValueError("成员 timeout_seconds 不得为负数")
        if int(self.max_output_tokens) < 256:
            raise ValueError("成员 max_output_tokens 不得小于 256")
        if self.allow_spawn:
            raise ValueError("阶段一不允许成员递归派生子成员")

    def to_snapshot(self) -> dict[str, Any]:
        """配置快照（不含密钥；模型 ID 只保存服务端校验后的名称/ID）。"""
        return {
            "role": self.role,
            "name": self.name,
            "description": self.description,
            "system_prompt": self.system_prompt[:6000],
            "tools": sorted(self.tools) if self.tools is not None else None,
            "model_config_id": self.model_config_id,
            "max_model_calls": int(self.max_model_calls),
            "max_tool_calls": int(self.max_tool_calls),
            "timeout_seconds": float(self.timeout_seconds),
            "max_output_tokens": int(self.max_output_tokens),
            "allow_spawn": bool(self.allow_spawn),
            "skills": list(self.skills),
        }

    def budget_snapshot(self) -> dict[str, int | float]:
        return {
            "max_model_calls": int(self.max_model_calls),
            "max_tool_calls": int(self.max_tool_calls),
            "timeout_seconds": float(self.timeout_seconds),
        }

    @classmethod
    def from_task(
        cls,
        task: dict[str, Any],
        *,
        team_config: Optional[dict[str, Any]] = None,
    ) -> "MemberProfile":
        """由任务角色生成默认 Profile，再应用团队配置里的角色级覆盖。"""
        role = str(task.get("role") or "团队成员").strip()[:60]
        team_config = team_config or {}
        overrides = (team_config.get("members") or {}).get(role) or {}
        tools = overrides.get("tools")
        profile = cls(
            role=role,
            name=str(overrides.get("name") or role)[:60],
            description=str(task.get("title") or "")[:200],
            system_prompt=str(
                overrides.get("system_prompt")
                or _ROLE_PROMPT.format(role=role)
            )[:6000],
            tools=frozenset(str(item) for item in tools) if tools else None,
            model_config_id=(
                int(overrides["model_config_id"])
                if overrides.get("model_config_id") else None
            ),
            max_model_calls=int(
                overrides.get("max_model_calls")
                or settings.AGENT_TEAM_MEMBER_MAX_MODEL_CALLS
            ),
            max_tool_calls=int(
                overrides.get("max_tool_calls")
                if overrides.get("max_tool_calls") is not None
                else settings.AGENT_TEAM_MEMBER_MAX_TOOL_CALLS
            ),
            timeout_seconds=float(
                overrides.get("timeout_seconds")
                if overrides.get("timeout_seconds") is not None
                else settings.AGENT_TEAM_MEMBER_TIMEOUT
            ),
            max_output_tokens=int(overrides.get("max_output_tokens") or 4096),
            allow_spawn=False,
        )
        profile.validate()
        return profile

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> "MemberProfile":
        tools = snapshot.get("tools")
        profile = cls(
            role=str(snapshot.get("role") or "团队成员"),
            name=str(snapshot.get("name") or snapshot.get("role") or "团队成员"),
            description=str(snapshot.get("description") or ""),
            system_prompt=str(snapshot.get("system_prompt") or ""),
            tools=frozenset(str(item) for item in tools) if tools else None,
            model_config_id=(
                int(snapshot["model_config_id"]) if snapshot.get("model_config_id") else None
            ),
            max_model_calls=int(snapshot.get("max_model_calls") or 8),
            max_tool_calls=int(
                snapshot.get("max_tool_calls")
                if snapshot.get("max_tool_calls") is not None else 12
            ),
            timeout_seconds=float(snapshot.get("timeout_seconds") or 300.0),
            max_output_tokens=int(snapshot.get("max_output_tokens") or 4096),
            allow_spawn=False,
        )
        profile.validate()
        return profile

    @classmethod
    def leader(cls, *, team_config: Optional[dict[str, Any]] = None) -> "MemberProfile":
        team_config = team_config or {}
        leader_cfg = team_config.get("leader") or {}
        profile = cls(
            role="团队负责人",
            name=str(leader_cfg.get("name") or "团队负责人"),
            description="负责规划、分配、复核、沟通与最终交付",
            system_prompt=str(leader_cfg.get("system_prompt") or ""),
            model_config_id=(
                int(leader_cfg["model_config_id"])
                if leader_cfg.get("model_config_id") else None
            ),
            max_model_calls=int(
                leader_cfg.get("max_model_calls") or 20
            ),
            max_tool_calls=int(leader_cfg.get("max_tool_calls") or 40),
            timeout_seconds=float(
                leader_cfg.get("timeout_seconds") or settings.AGENT_TEAM_MEMBER_TIMEOUT
            ),
            max_output_tokens=int(leader_cfg.get("max_output_tokens") or 8192),
            allow_spawn=False,
        )
        profile.validate()
        return profile
