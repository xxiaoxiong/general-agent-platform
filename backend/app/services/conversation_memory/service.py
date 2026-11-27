"""自动历史检索编排：模型判断 → 有界混合检索 → 预算组装 → 可追溯来源。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.conversation_memory import ConversationIndexEntry

from .context_builder import build_history_context
from .indexer import current_index_version
from .planner import current_request, plan_retrieval, recent_excerpt
from .repository import accessible_project_ids, load_settings, memory_enabled_for_run
from .retrieval import ConversationMemoryService, HistoryItem, HistoryRetrievalResult

logger = logging.getLogger("ai_plat.conversation_memory")


@dataclass
class PreparedHistory:
    enabled: bool = False
    reason: str = ""
    context: str = ""
    sources: List[Dict[str, Any]] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)
    planner: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    queries: List[str] = field(default_factory=list)
    retrieval_id: str = ""

    def event_payload(self) -> Dict[str, Any]:
        # 事件脱敏会把包含 "token" 的键名整体打码；对外使用 budget_used 字段
        stats = {key: value for key, value in self.stats.items() if key != "injected_tokens"}
        stats["budget_used"] = int(self.stats.get("injected_tokens") or 0)
        return {
            "sources": self.sources,
            "mode": "auto",
            "queries": self.queries,
            "retrieval_id": self.retrieval_id,
            "planner": self.planner,
            "stats": stats,
            "notes": self.notes[:4],
        }


async def _indexed_count(
    db: AsyncSession, *, user_id: int, project_ids: Sequence[int], index_version: str,
) -> int:
    scope = ConversationIndexEntry.user_id == int(user_id)
    if project_ids:
        scope = or_(
            scope,
            and_(
                ConversationIndexEntry.visibility_scope == "project",
                ConversationIndexEntry.project_id.in_([int(x) for x in project_ids]),
            ),
        )
    return int(await db.scalar(
        select(func.count()).select_from(ConversationIndexEntry).where(
            ConversationIndexEntry.is_deleted.is_(False),
            ConversationIndexEntry.is_excluded.is_(False),
            ConversationIndexEntry.index_version == index_version,
            scope,
        )
    ) or 0)


def _merge_results(results: Sequence[HistoryRetrievalResult]) -> HistoryRetrievalResult:
    merged: Dict[int, HistoryItem] = {}
    notes: List[str] = []
    retrieval_id = results[0].retrieval_id if results else ""
    for result in results:
        notes.extend(result.notes)
        for item in result.items:
            current = merged.get(item.entry_id)
            if current is None or item.score > current.score:
                merged[item.entry_id] = item
    items = sorted(merged.values(), key=lambda item: item.score, reverse=True)
    return HistoryRetrievalResult(
        retrieval_id=retrieval_id,
        query=" | ".join(result.query for result in results if result.query),
        scope="auto",
        items=items,
        timings={
            "total_ms": round(sum(result.timings.get("total_ms", 0.0) for result in results), 2)
        },
        candidates={
            "queries": len(results),
            "unique": len(items),
        },
        notes=list(dict.fromkeys(notes)),
    )


async def prepare_history_context(
    *,
    run_id: str,
    user_id: int,
    project_id: Optional[int],
    messages: Optional[List[Dict[str, Any]]],
    provider: Dict[str, Any],
    model_config: Dict[str, Any],
    agent_config: Optional[Dict[str, Any]] = None,
) -> PreparedHistory:
    """自动预取入口；任何异常都降级为“无历史上下文”，不抛给 Run。"""
    prepared = PreparedHistory()
    try:
        if not bool(getattr(settings, "CONVERSATION_MEMORY_ENABLED", True)):
            prepared.reason = "global_disabled"
            return prepared
        async with AsyncSessionLocal() as db:
            user_settings = await load_settings(db, int(user_id))
            if not memory_enabled_for_run(agent_config or {}, user_settings):
                prepared.reason = "user_disabled"
                return prepared
            if not bool(user_settings.get("auto_retrieval_enabled", True)):
                prepared.reason = "auto_disabled"
                return prepared
            if str(getattr(settings, "CONVERSATION_MEMORY_PLANNER_MODE", "llm")) == "off":
                prepared.reason = "planner_off"
                return prepared
            request = current_request(messages)
            if not request:
                prepared.reason = "empty_request"
                return prepared
            project_ids: List[int] = []
            if project_id and bool(user_settings.get("project_memory_enabled", True)):
                project_ids = await accessible_project_ids(db, int(user_id))
                if int(project_id) not in project_ids:
                    project_id = None
            index_version = current_index_version()
            count = await _indexed_count(
                db, user_id=int(user_id), project_ids=project_ids, index_version=index_version,
            )
            if count <= 0:
                prepared.reason = "no_index"
                prepared.stats = {"indexed_entries": 0}
                return prepared

        history_hint = (
            f"当前用户共有 {count} 条已索引历史片段；"
            f"项目共享历史{'可用' if project_ids else '不可用'}。"
        )
        outcome = await plan_retrieval(
            provider=provider,
            model_config=model_config,
            request=request,
            recent=recent_excerpt(messages),
            project_context=(
                f"当前会话属于项目 {project_id}；用户的项目历史可能包含相关决策。"
                if project_id else "当前会话未绑定项目，仅能检索用户本人历史。"
            ),
            history_hint=history_hint,
            run_id=run_id,
        )
        prepared.planner = {
            "fallback": outcome.fallback,
            "latency_ms": outcome.latency_ms,
            "error": outcome.error[:200],
            "need_history": bool(outcome.plan.need_history) if outcome.plan else False,
            "scope_hint": outcome.plan.scope_hint if outcome.plan else "",
            "reason": outcome.plan.reason if outcome.plan else "",
        }
        try:
            from app.observability import metrics

            if outcome.fallback:
                metrics.inc_conversation_planner("fallback")
            elif outcome.plan and outcome.plan.need_history:
                metrics.inc_conversation_planner("need_history")
            else:
                metrics.inc_conversation_planner("no_history")
        except Exception:  # noqa: BLE001
            pass
        if outcome.fallback or outcome.plan is None or not outcome.plan.need_history:
            prepared.reason = "planner_fallback" if outcome.fallback else "no_history"
            return prepared

        queries = list(outcome.plan.queries)
        prepared.queries = queries
        scope = "current_project" if (
            outcome.plan.scope_hint == "current_project" and project_id
        ) else "all"
        results: List[HistoryRetrievalResult] = []
        async with AsyncSessionLocal() as db:
            service = ConversationMemoryService(db)
            for query in queries:
                try:
                    results.append(await service.search(
                        user_id=int(user_id), query=query, top_k=5,
                        project_id=int(project_id) if project_id else None,
                        scope=scope,
                    ))
                except Exception as exc:  # noqa: BLE001 单查询失败不影响其他查询
                    logger.warning("历史检索查询失败（忽略）：%s", exc)
            try:
                await db.commit()
            except Exception:  # noqa: BLE001 标记失效条目的写操作失败可忽略
                pass
        merged = _merge_results(results)
        prepared.retrieval_id = merged.retrieval_id
        prepared.notes = merged.notes[:6]
        context, sources, stats = build_history_context(merged)
        prepared.context = context
        prepared.sources = sources
        prepared.stats = {**stats, "candidates": merged.candidates}
        prepared.enabled = bool(context and sources)
        prepared.reason = "ok" if prepared.enabled else "no_result"
        try:
            from app.observability import metrics

            metrics.observe_conversation_context_tokens(int(stats.get("injected_tokens") or 0))
        except Exception:  # noqa: BLE001
            pass
        return prepared
    except Exception as exc:  # noqa: BLE001 历史检索不得拖垮 Run
        logger.warning("自动历史检索失败，按无历史上下文继续：%s", exc)
        prepared.enabled = False
        prepared.context = ""
        prepared.sources = []
        prepared.reason = f"error:{type(exc).__name__}"
        return prepared
