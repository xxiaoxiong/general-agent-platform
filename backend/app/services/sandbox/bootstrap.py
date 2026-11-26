"""Sandbox 子系统启停（main lifespan 调用）。"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.services.sandbox.profiles import ensure_default_profiles
from app.services.sandbox.reconciler import get_reconciler

logger = logging.getLogger("ai_plat.sandbox")


async def _seed_profiles_with_retry(attempts: int = 5) -> None:
    """多 Uvicorn Worker 并发播种资源档位：唯一键冲突/死锁视为其他进程已完成。

    MySQL 死锁（1213）会回滚整个事务，重试时先读后插即可收敛；播种失败不阻断启动
    （`get_profile` 仍有兜底），但会记录警告。
    """
    for attempt in range(1, attempts + 1):
        try:
            async with AsyncSessionLocal() as db:
                await ensure_default_profiles(db)
                await db.commit()
            return
        except (IntegrityError, OperationalError) as exc:
            logger.info("资源档位播种竞争（第 %s 次），重试：%s", attempt, exc)
            await asyncio.sleep(0.4 * attempt)
    logger.warning("资源档位播种未能完成（其他 Worker 可能已完成），继续启动")


async def start_sandbox_subsystem() -> None:
    if not settings.SANDBOX_ENABLED:
        logger.info("Workspace Sandbox 未启用（SANDBOX_ENABLED=false）")
        return
    await _seed_profiles_with_retry()
    await get_reconciler().start()
    logger.info(
        "Workspace Sandbox 已启用（provider=%s, image=%s, preview_domain=%s）",
        settings.SANDBOX_PROVIDER, settings.SANDBOX_IMAGE, settings.PREVIEW_BASE_DOMAIN,
    )


async def stop_sandbox_subsystem() -> None:
    if not settings.SANDBOX_ENABLED:
        return
    await get_reconciler().stop()
