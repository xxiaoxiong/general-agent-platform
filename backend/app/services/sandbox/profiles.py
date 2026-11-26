"""资源档位（任务书 §6.1）：small / standard / large，由管理员配置数值。

第一阶段提供默认档位并在启动时幂等播种；数值可由管理员后台修改，
Agent 与普通用户不能传入自定义 CPU/RAM/Disk。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.sandbox import SandboxResourceProfile

DEFAULT_PROFILES = (
    {
        "name": "small", "display_name": "小型（轻量脚本 / 单服务）",
        "cpu_request_millis": 250, "cpu_limit_millis": 500,
        "memory_request_bytes": 256 * 1024 * 1024, "memory_limit_bytes": 512 * 1024 * 1024,
        "disk_quota_bytes": 2 * 1024 * 1024 * 1024, "pid_limit": 128,
        "max_processes": 64, "max_services": 2, "max_ports": 2,
        "idle_timeout_seconds": 900,
    },
    {
        "name": "standard", "display_name": "标准（Web 项目 / 前后端）",
        "cpu_request_millis": 500, "cpu_limit_millis": 1000,
        "memory_request_bytes": 512 * 1024 * 1024, "memory_limit_bytes": 1024 * 1024 * 1024,
        "disk_quota_bytes": 5 * 1024 * 1024 * 1024, "pid_limit": 256,
        "max_processes": 128, "max_services": 4, "max_ports": 4,
        "idle_timeout_seconds": 1800,
    },
    {
        "name": "large", "display_name": "大型（依赖安装 / 多服务构建）",
        "cpu_request_millis": 1000, "cpu_limit_millis": 2000,
        "memory_request_bytes": 1024 * 1024 * 1024, "memory_limit_bytes": 2048 * 1024 * 1024,
        "disk_quota_bytes": 10 * 1024 * 1024 * 1024, "pid_limit": 512,
        "max_processes": 256, "max_services": 6, "max_ports": 6,
        "idle_timeout_seconds": 3600,
    },
)


async def ensure_default_profiles(db: AsyncSession) -> None:
    """幂等播种默认档位（不覆盖管理员已修改的数值）。"""
    existing = set((await db.execute(select(SandboxResourceProfile.name))).scalars().all())
    for item in DEFAULT_PROFILES:
        if item["name"] in existing:
            continue
        db.add(SandboxResourceProfile(
            **item,
            network_policy=settings.SANDBOX_NETWORK_POLICY,
            is_default=(item["name"] == settings.SANDBOX_DEFAULT_PROFILE),
        ))
    await db.flush()


async def get_profile(db: AsyncSession, name: str | None = None) -> SandboxResourceProfile:
    target = (name or settings.SANDBOX_DEFAULT_PROFILE or "standard").strip()
    profile = (await db.execute(
        select(SandboxResourceProfile).where(SandboxResourceProfile.name == target)
    )).scalar_one_or_none()
    if profile is not None:
        return profile
    profile = (await db.execute(
        select(SandboxResourceProfile).where(SandboxResourceProfile.is_default.is_(True))
    )).scalars().first()
    if profile is not None:
        return profile
    # 极端场景（管理员删除了全部档位）：兜底使用 standard 默认值落库
    fallback = next(p for p in DEFAULT_PROFILES if p["name"] == "standard")
    profile = SandboxResourceProfile(**fallback, is_default=True)
    try:
        async with db.begin_nested():
            db.add(profile)
            await db.flush()
    except IntegrityError:
        profile = (await db.execute(
            select(SandboxResourceProfile).where(
                SandboxResourceProfile.name == fallback["name"])
        )).scalar_one()
    return profile
