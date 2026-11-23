"""Run 域共享辅助（配置装载 / 事件流排空 / 附件注记 / Run 定位）。"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent

logger = logging.getLogger(__name__)


async def _load_agent_config(db: AsyncSession, agent: Agent, *, use_memory: bool) -> dict:
    """由 Agent 行构建运行配置（P2-01：优先绑定不可变版本快照执行）。"""
    from app.services.assistant.versions import config_from_version, ensure_version

    version = await ensure_version(db, agent.id)
    if version is not None:
        cfg = dict(config_from_version(version))
    else:
        # 合成配置（无真实 Agent 行的场景不进 v2；防御性兜底）
        cfg = {
            "id": agent.id,
            "system_prompt": agent.system_prompt or "",
            "model_config_id": agent.model_config_id,
            "temperature": agent.temperature,
            "max_iterations": agent.max_iterations,
            "max_tokens": agent.max_tokens,
            "tools": list(agent.tools or []),
            "capability_ids": list(getattr(agent, "capability_ids", None) or []),
            "knowledge_base_id": agent.knowledge_base_id,
            "database_connection_id": agent.database_connection_id,
        }
    cfg["name"] = agent.name
    # P2-04：长期记忆走 Store MemoryMiddleware（/memory/AGENTS.md），不再走旧 SQLite MemoryManager
    cfg["memory_policy"] = {"use_memory_context": bool(use_memory)}
    return cfg


async def _drain_legacy_stream(stream) -> None:
    """后台排空旧契约事件流（执行由 Worker 驱动；事件已持久化，v2 经 /events 消费）。"""
    try:
        async for _ in stream:
            pass
    except Exception:  # noqa: BLE001
        logger.exception("v2 后台事件流排空异常")


def _attachment_note(attachments: List[Dict[str, Any]]) -> str:
    """附件读取注记（随用户消息持久化）：告知模型文件已入会话工作区可用 read_file 读取。

    路径白名单：仅 ``uploads/`` 前缀且无 ``..`` 段，防注记被注入任意路径；
    注记使用 ``/workspace/uploads/`` 前缀（文件工具的虚拟盘路由），
    无前缀路径会被 StateBackend 兜底导致 read_file 找不到文件。
    """
    lines = []
    for a in attachments or []:
        path = str(a.get("path") or "").replace("\\", "/")
        if not path.startswith("uploads/") or ".." in path.split("/"):
            continue
        line = f"- /workspace/{path}"
        text_path = str(a.get("text_path") or "").replace("\\", "/")
        if text_path.startswith("uploads/") and ".." not in text_path.split("/"):
            line += f"（文本副本：/workspace/{text_path}）"
        lines.append(line)
    if not lines:
        return ""
    return (
        "\n\n[附件]（文件已存入会话工作区。回答前请务必先用 read_file 逐个读取以下文件——"
        "图片文件 read_file 后可直接看到图片内容——读完再基于内容回答；"
        "原始附件与文本副本仅供读取。不要把文本副本当作新的 Word 成品。"
        "在未读取前不要猜测内容、不要让用户重新上传）\n" + "\n".join(lines)
    )


async def _get_run_or_404(run_id: str, user_id: int) -> dict:
    from app.services.agent_runtime import get_runtime_service

    info = await get_runtime_service().get_run(run_id, user_id)
    if not info:
        raise HTTPException(status_code=404, detail="运行不存在")
    return info
