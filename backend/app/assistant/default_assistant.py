"""默认通用助手（P3-01）：用户无需创建 Agent 即可直接对话。

产品语义（任务书 §2.2 / UX-01）：
- 登录后直接进入工作台，平台自动提供默认通用助手；
- 默认助手绑定 `general` 能力包（对话、计算、时间、基础文件处理）；
- 模型不绑定具体 ModelConfig —— 由 ModelRouter 按默认模型策略选择；
- 按用户懒创建（owner_id 隔离，A22 口径），一次创建长期复用；
- 用户可对默认助手自然语言调整（走 Assistants API），但删除后
  再次进入工作台会重新创建（保证 UX-01 恒可用）。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent

DEFAULT_ASSISTANT_NAME = "通用助手"
DEFAULT_ASSISTANT_DESCRIPTION = "开箱即用的默认智能助手：问答、计算、时间与基础文件处理。"
DEFAULT_ASSISTANT_SYSTEM_PROMPT = (
    "你是 AI-GeneralPlat 的通用智能助手。请直接、准确地帮助用户解决问题："
    "能直接回答的直接回答；需要计算、查时间或处理文件时使用工具；"
    "不确定时明确说明不确定性，不要编造事实。"
)
DEFAULT_CAPABILITY_IDS = ["general"]


def default_assistant_config() -> dict:
    """默认助手的创建参数（测试与 seed 共用，保证口径一致）。"""
    return {
        "name": DEFAULT_ASSISTANT_NAME,
        "description": DEFAULT_ASSISTANT_DESCRIPTION,
        "system_prompt": DEFAULT_ASSISTANT_SYSTEM_PROMPT,
        "capability_ids": list(DEFAULT_CAPABILITY_IDS),
        "tools": [],
        "is_default": True,
    }


async def get_default_assistant(
    db: AsyncSession, user_id: Optional[int]
) -> Optional[Agent]:
    """查询用户的默认助手（不创建）。"""
    res = await db.execute(
        select(Agent).where(
            Agent.owner_id == user_id,
            Agent.is_default == True,  # noqa: E712
            Agent.is_active == True,  # noqa: E712
        ).order_by(Agent.id.desc()).limit(1)
    )
    return res.scalar_one_or_none()


async def ensure_default_assistant(
    db: AsyncSession, user_id: Optional[int], *, created_by: Optional[int] = None
) -> Agent:
    """获取或懒创建默认通用助手（幂等；并发创建容忍多行，取最新一条为规范）。

    - 已存在：直接返回，不重复创建；
    - 不存在：创建后由调用方决定是否提交（本函数只 flush，事务边界交给调用方，
      以便与 workspace bootstrap 的其他写入合并提交）。
    """
    agent = await get_default_assistant(db, user_id)
    if agent is not None:
        return agent
    cfg = default_assistant_config()
    agent = Agent(
        owner_id=user_id if user_id is not None else created_by,
        is_active=True,
        **cfg,
    )
    db.add(agent)
    await db.flush()
    return agent
