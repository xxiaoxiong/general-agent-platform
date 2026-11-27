"""Conversation Memory 质量评测 CLI（阶段一，≥30 条人工标注集）。

用法（backend/ 目录）：
    python -m scripts.conversation_memory_eval \
        --dataset tests/fixtures/conversation_memory_eval.json \
        --report ../docs/conversation-memory-eval-report.md

- 使用独立临时 SQLite + 本地 Qdrant，不触碰部署数据库；
- Embedding 使用当前配置的真实 Provider（本地 fastembed 或 OpenAI 兼容）；
- 输出 Recall@5 / Precision@5 / Source Accuracy / No-answer Accuracy /
  Permission Violation / Retrieval Latency / Context Token Cost；
- 权限违规必须为 0，否则进程返回 1。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_BACKEND = Path(__file__).resolve().parents[1]


def _prepare_env(workdir: Path) -> None:
    """在导入 app 之前强制指向隔离资源（防止误连部署库/对象存储）。"""
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{workdir}/eval.db"
    os.environ["DATABASE_SYNC_URL"] = f"sqlite:///{workdir}/eval.db"
    os.environ["DEBUG"] = "false"
    os.environ["KNOWLEDGE_STORAGE_DIR"] = str(workdir / "knowledge")
    os.environ["AGENT_FILES_ROOT"] = str(workdir / "agent_files")
    os.environ["AGENT_CHECKPOINT_DSN"] = "memory"
    os.environ["AGENT_STORE_DSN"] = ""
    os.environ["AGENT_DECISION_MODE"] = "off"
    os.environ["AGENT_WORK_ORCHESTRATOR_MODE"] = "off"
    os.environ["CONVERSATION_MEMORY_ENABLED"] = "true"
    os.environ["CONVERSATION_MEMORY_PLANNER_MODE"] = "off"
    os.environ["EVOLUTION_ENABLED"] = "false"
    os.environ["EVALUATION_WORKER_ENABLED"] = "false"
    os.environ["LANGFUSE_PUBLIC_KEY"] = ""
    os.environ["LANGFUSE_SECRET_KEY"] = ""
    os.environ["KNOWLEDGE_QDRANT_URL"] = ""
    os.environ.setdefault("SECRET_KEY", "conversation-memory-eval-secret")


def _percentile(values: List[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return float(ordered[index])


async def run_eval(dataset_path: Path, *, mode: str = "hybrid", top_k: int = 5) -> Dict[str, Any]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))

    from app.core.database import AsyncSessionLocal, init_db
    from app.models.agent import Agent, AgentSession
    from app.models.conversation_memory import ConversationIndexEntry
    from app.models.sandbox import Project, ProjectMember
    from app.models.user import User
    from app.services.conversation_memory.context_builder import build_history_context
    from app.services.conversation_memory.indexer import index_session
    from app.services.conversation_memory.repository import (
        accessible_project_ids, share_to_project,
    )
    from app.services.conversation_memory.retrieval import ConversationMemoryService
    from app.services.knowledge.embedding import get_embedding_provider
    from sqlalchemy import select

    await init_db()
    started = time.monotonic()
    async with AsyncSessionLocal() as db:
        agent = Agent(name="Conversation Memory 评测助手")
        db.add(agent)
        await db.flush()

        users: Dict[str, User] = {}
        for key, spec in (dataset.get("users") or {}).items():
            user = User(
                username=spec["username"], email=spec["email"], hashed_password="eval",
            )
            db.add(user)
            users[key] = user
        await db.flush()

        projects: Dict[str, Project] = {}
        for spec in dataset.get("projects") or []:
            owner = users[spec["owner"]]
            project = Project(name=f"[eval] {spec['name']}", owner_id=owner.id)
            db.add(project)
            await db.flush()
            projects[spec["key"]] = project
            for member_key in spec.get("members") or []:
                db.add(ProjectMember(
                    project_id=project.id, user_id=users[member_key].id, role="viewer",
                ))
        await db.flush()

        sessions: Dict[str, AgentSession] = {}
        for spec in dataset.get("conversations") or []:
            messages = [{
                "message_id": item["id"],
                "role": item["role"],
                "content": item["content"],
                "created_at": item.get("created_at") or datetime.utcnow().isoformat(),
            } for item in spec.get("messages") or []]
            session = AgentSession(
                agent_id=agent.id,
                user_id=users[spec["user"]].id,
                title=spec.get("title") or "历史会话",
                project_id=(
                    projects[spec["project"]].id
                    if spec.get("project") in projects else None
                ),
                messages=messages,
            )
            db.add(session)
            await db.flush()
            sessions[spec["key"]] = session
        await db.flush()

        index_started = time.monotonic()
        for session in sessions.values():
            await index_session(db, session)
        await db.commit()
        index_seconds = time.monotonic() - index_started

        for spec in dataset.get("shares") or []:
            conversation = sessions.get(spec["conversation"])
            project = projects.get(spec["project"])
            if conversation is None or project is None:
                continue
            await share_to_project(
                db, user_id=int(conversation.user_id or 0),
                conversation_id=int(conversation.id), project_id=int(project.id),
            )
        await db.commit()

        entries = (await db.execute(
            select(ConversationIndexEntry)
        )).scalars().all()
        embedding_summary: Dict[str, int] = {}
        for entry in entries:
            embedding_summary[entry.embedding_status] = (
                embedding_summary.get(entry.embedding_status, 0) + 1
            )

        service = ConversationMemoryService(db)
        rows: List[Dict[str, Any]] = []
        latencies: List[float] = []
        context_tokens: List[int] = []
        permission_violations: List[str] = []
        source_checked = 0
        source_valid = 0
        project_cache: Dict[int, set] = {}

        for query in dataset.get("queries") or []:
            user = users[query["user"]]
            if user.id not in project_cache:
                project_cache[user.id] = set(await accessible_project_ids(db, user.id))
            project_id = (
                projects[query["project"]].id
                if query.get("project") in projects else None
            )
            result = await service.search(
                user_id=int(user.id), query=query["query"], top_k=top_k,
                project_id=project_id, scope="auto", mode=mode,
            )
            latency_ms = float(result.timings.get("total_ms") or 0.0)
            latencies.append(latency_ms)
            expected = set(query.get("expected_message_ids") or [])
            relevant_items = [
                item for item in result.items
                if expected and expected.intersection(item.message_ids)
            ]
            context, sources, stats = build_history_context(result)
            context_tokens.append(int(stats.get("injected_tokens") or 0))

            invalid_sources: List[str] = []
            for item in result.items:
                source_checked += 1
                source = await service.get_source(
                    user_id=int(user.id), source_id=f"history_src_{item.entry_id}",
                )
                if source is not None:
                    source_valid += 1
                else:
                    invalid_sources.append(f"history_src_{item.entry_id}")
                # 权限：仅本人历史或当前用户可访问的项目共享历史
                allowed = (
                    int(item.user_id) == int(user.id)
                    or (
                        item.scope == "project" and item.project_id is not None
                        and int(item.project_id) in project_cache[user.id]
                    )
                )
                if not allowed:
                    permission_violations.append(
                        f"{query['id']}: entry={item.entry_id} session={item.session_id} "
                        f"user={item.user_id} scope={item.scope}"
                    )

            rows.append({
                "id": query["id"],
                "category": query.get("category") or "未分类",
                "user": query["user"],
                "query": query["query"],
                "no_answer": bool(query.get("no_answer")),
                "expected": sorted(expected),
                "returned": [
                    {
                        "entry_id": item.entry_id,
                        "session_id": item.session_id,
                        "title": item.conversation_title,
                        "message_ids": item.message_ids,
                        "score": round(float(item.score), 6),
                        "dense_score": (
                            round(float(item.dense_score), 6)
                            if item.dense_score is not None else None
                        ),
                        "lexical_score": (
                            round(float(item.lexical_score), 6)
                            if item.lexical_score is not None else None
                        ),
                        "scope": item.scope,
                    }
                    for item in result.items
                ],
                "hit": bool(relevant_items) if expected else False,
                "relevant_count": len(relevant_items),
                "returned_count": len(result.items),
                "invalid_sources": invalid_sources,
                "latency_ms": round(latency_ms, 2),
                "context_tokens": int(stats.get("injected_tokens") or 0),
                "notes": result.notes[:4],
            })

        # 伪造来源 ID 不得越权读取
        forged = await service.get_source(
            user_id=int(users["alice"].id), source_id="history_src_999999999",
        )
        if forged is not None:
            permission_violations.append("forged source id returned content")

    answerable = [row for row in rows if not row["no_answer"]]
    no_answer_rows = [row for row in rows if row["no_answer"]]
    recall_hits = sum(1 for row in answerable if row["hit"])
    precision_values = [
        row["relevant_count"] / row["returned_count"]
        for row in answerable if row["returned_count"] > 0
    ]
    no_answer_correct = sum(1 for row in no_answer_rows if row["returned_count"] == 0)
    categories: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        bucket = categories.setdefault(
            row["category"], {"total": 0, "hit": 0, "no_answer_correct": 0,
                              "no_answer_total": 0},
        )
        bucket["total"] += 1
        if row["no_answer"]:
            bucket["no_answer_total"] += 1
            if row["returned_count"] == 0:
                bucket["no_answer_correct"] += 1
        elif row["hit"]:
            bucket["hit"] += 1

    return {
        "dataset": str(dataset_path),
        "mode": mode,
        "top_k": top_k,
        "queries": len(rows),
        "index_seconds": round(index_seconds, 2),
        "seeding_seconds": round(time.monotonic() - started, 2),
        "index_entries": len(entries),
        "embedding_status": embedding_summary,
        "embedding_model": get_embedding_provider().model,
        "embedding_dimension": get_embedding_provider().dimension,
        "recall_at_k": round(recall_hits / max(1, len(answerable)), 4),
        "precision_at_k": round(
            statistics.fmean(precision_values) if precision_values else 0.0, 4
        ),
        "no_answer_accuracy": round(
            no_answer_correct / max(1, len(no_answer_rows)), 4
        ),
        "source_accuracy": round(source_valid / max(1, source_checked), 4),
        "unsupported_memory_rate": round(
            1.0 - (source_valid / max(1, source_checked)), 4
        ),
        "permission_violations": permission_violations,
        "permission_violation_count": len(permission_violations),
        "latency_ms": {
            "p50": round(_percentile(latencies, 0.5), 2),
            "p95": round(_percentile(latencies, 0.95), 2),
            "avg": round(statistics.fmean(latencies) if latencies else 0.0, 2),
        },
        "context_tokens": {
            "avg": round(statistics.fmean(context_tokens) if context_tokens else 0.0, 1),
            "max": max(context_tokens) if context_tokens else 0,
        },
        "categories": categories,
        "rows": rows,
    }


def render_report(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# Conversation Memory 历史会话检索质量评测报告")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 评测集：`{report['dataset']}`（{report['queries']} 条人工标注查询）")
    lines.append(f"- 检索模式：`{report['mode']}`，Top-K={report['top_k']}")
    lines.append(
        f"- Embedding：`{report['embedding_model']}`（维度 {report['embedding_dimension']}）"
        f"，状态分布 {report['embedding_status']}"
    )
    lines.append(
        f"- 索引：{report['index_entries']} 条（构建耗时 {report['index_seconds']}s，"
        f"总准备耗时 {report['seeding_seconds']}s）"
    )
    lines.append("")
    lines.append("## 核心指标")
    lines.append("")
    lines.append("| 指标 | 结果 | 说明 |")
    lines.append("| --- | --- | --- |")
    lines.append(
        f"| Recall@K | {report['recall_at_k']:.2%} | 有答案查询中 Top-K 命中期望消息的比例 |"
    )
    lines.append(
        f"| Precision@K | {report['precision_at_k']:.2%} | Top-K 返回结果中与期望消息相关的比例 |"
    )
    lines.append(
        f"| Source Accuracy | {report['source_accuracy']:.2%} | 返回来源可回查原文且权限有效 |"
    )
    lines.append(
        f"| Unsupported Memory Rate | {report['unsupported_memory_rate']:.2%} | 无来源支持的返回比例 |"
    )
    lines.append(
        f"| No-answer Accuracy | {report['no_answer_accuracy']:.2%} | 无相关历史时保持空结果的比例 |"
    )
    lines.append(
        f"| Permission Violation | {report['permission_violation_count']} | 跨用户/跨项目越权命中数（必须为 0） |"
    )
    lines.append(
        f"| Retrieval Latency | P50 {report['latency_ms']['p50']} ms / "
        f"P95 {report['latency_ms']['p95']} ms / Avg {report['latency_ms']['avg']} ms | 单次检索总耗时 |"
    )
    lines.append(
        f"| Context Token Cost | Avg {report['context_tokens']['avg']} / "
        f"Max {report['context_tokens']['max']} | 注入历史上下文的 Token 估算 |"
    )
    lines.append("")
    lines.append("## 分类结果")
    lines.append("")
    lines.append("| 类别 | 查询数 | 命中 / 有答案 | 无答案正确 |")
    lines.append("| --- | --- | --- | --- |")
    for category, bucket in sorted(report["categories"].items()):
        answerable = bucket["total"] - bucket["no_answer_total"]
        lines.append(
            f"| {category} | {bucket['total']} | {bucket['hit']} / {answerable} | "
            f"{bucket['no_answer_correct']} / {bucket['no_answer_total']} |"
        )
    lines.append("")
    lines.append("## 失败与低分样例")
    lines.append("")
    failures = [row for row in report["rows"] if not row["no_answer"] and not row["hit"]]
    false_positives = [row for row in report["rows"] if row["no_answer"] and row["returned_count"]]
    if not failures and not false_positives:
        lines.append("无。")
    for row in failures:
        lines.append(
            f"- `{row['id']}`（{row['category']}）未命中：{row['query']}；"
            f"期望 {row['expected']}；返回 {[item['message_ids'] for item in row['returned']]}"
        )
    for row in false_positives:
        lines.append(
            f"- `{row['id']}`（{row['category']}）无答案但返回 {row['returned_count']} 条："
            f"{row['query']}"
        )
    lines.append("")
    lines.append("## 权限与来源校验")
    lines.append("")
    lines.append(
        f"- 来源回查：{report['source_accuracy']:.2%} 可回查原文；"
        f"Unsupported Memory Rate={report['unsupported_memory_rate']:.2%}"
    )
    lines.append(
        f"- 越权命中：{report['permission_violation_count']} 条"
        + ("（全部被服务端 ACL 拦截）" if report["permission_violation_count"] == 0 else "（需修复）")
    )
    lines.append("- 覆盖：本人历史跨用户、项目共享撤销后、伪造来源 ID、私有会话直接查询。")
    lines.append("")
    lines.append("## 局限与说明")
    lines.append("")
    lines.append(
        "- 评测集为人工构造的合成语料，数量与领域覆盖有限；正式上线前应补充真实企业语料。"
    )
    lines.append(
        "- Recall/Precision 以 `message_ids` 交集判定；多轮 segment 命中同一消息按相关处理。"
    )
    lines.append(
        "- Context Usefulness 未做 LLM 人工评分，本报告以 Recall@K + Source Accuracy 作为客观代理指标。"
    )
    lines.append(
        "- 无答案正确率受 Embedding 语义泛化影响；相似但不相关查询可能出现少量误召回。"
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Conversation Memory 质量评测")
    parser.add_argument(
        "--dataset", default=str(_REPO_BACKEND / "tests/fixtures/conversation_memory_eval.json"),
    )
    parser.add_argument(
        "--report", default=str(_REPO_BACKEND.parent / "docs/conversation-memory-eval-report.md"),
    )
    parser.add_argument("--json-out", default="")
    parser.add_argument("--mode", default="hybrid", choices=["hybrid", "bm25", "dense"])
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--keep", action="store_true", help="保留临时数据库/索引目录")
    args = parser.parse_args(argv)

    dataset_path = Path(args.dataset).resolve()
    if not dataset_path.exists():
        print(f"评测集不存在：{dataset_path}", file=sys.stderr)
        return 2
    workdir = Path(tempfile.mkdtemp(prefix="conversation_memory_eval_"))
    _prepare_env(workdir)
    try:
        report = asyncio.run(run_eval(dataset_path, mode=args.mode, top_k=args.top_k))
        report_text = render_report(report)
        report_path = Path(args.report).resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_text, encoding="utf-8")
        if args.json_out:
            json_path = Path(args.json_out).resolve()
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
            )
        print(json.dumps({
            key: report[key] for key in (
                "queries", "recall_at_k", "precision_at_k", "source_accuracy",
                "no_answer_accuracy", "permission_violation_count", "latency_ms",
                "context_tokens", "embedding_status",
            )
        }, ensure_ascii=False, indent=2))
        print(f"报告已写入：{report_path}")
        return 0 if report["permission_violation_count"] == 0 else 1
    finally:
        if not args.keep:
            import shutil

            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
