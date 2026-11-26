"""Workspace Sandbox API 包：用户侧 /api/v2 + Runner 内部 /internal。"""
from fastapi import APIRouter

from . import files, internal, projects, services, workspaces

router = APIRouter()
router.include_router(projects.router)
router.include_router(workspaces.router)
router.include_router(services.router)
router.include_router(files.router)

internal_router = internal.router

__all__ = ["router", "internal_router"]
