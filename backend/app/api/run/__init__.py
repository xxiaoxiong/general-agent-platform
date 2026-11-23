"""Run 域 API（URL 前缀 /api/v2/runs）。

产品语义（§8.4 Cancel / Resume / Reconnect）：
- POST /runs 创建 Run 并返回 run_id（X-Run-Id 响应头），执行在 Runtime
  Worker 中推进，事件持久化于 agent_run_events；
- GET /runs/{id}/events 以 v2 协议（§7.1）SSE 订阅/回放，
  ``Last-Event-ID``（或 after_seq）断线续传，不丢失不重复不乱序；
- 停止订阅与取消分离：前端停止只断开 SSE，取消必须显式调用 cancel；
- 审批决策携带稳定 action_id（FR-06），resume 后重新订阅事件流；
- 所有事件以持久化日志为事实来源（FR-07），API 与执行 Worker 可不同进程。

模块划分：core（生命周期）/ admin（人工核对、项目返工）/ review（文档审阅）。
"""
from __future__ import annotations

from fastapi import APIRouter

from . import admin, core, review

router = APIRouter(tags=["runs-v2"])
router.include_router(core.router)
router.include_router(admin.router)
router.include_router(review.router)

__all__ = ["router"]
