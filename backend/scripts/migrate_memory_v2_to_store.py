"""memory_v2.db → LangGraph Store 长期记忆迁移（P2-04 §4.5 八步）。

步骤对应：
1. 盘点 SQLite 记忆类型和字段（memories_v2：user_id/agent_id/memory_type/
   content/importance/tags/metadata/is_pinned/时间）；
2. 导出为结构化行（只读打开旧库）；
3. Store namespace 映射：("user_memory", str(user_id))，文件 AGENTS.md
   （与 MemoryMiddleware sources 对齐）；
4. 只读导入预演（--dry-run，默认）：只生成对账报告，不写 Store；
5. 比对数量、内容摘要（sha256）和用户归属；
6. --apply 执行导入（幂等：同内容覆盖写，重复执行结果一致）；
7. 旧库保留观察期（本脚本绝不删除 memory_v2.db）；
8. 新系统检索测试通过后由 Phase 5 切换并删除旧 API。

用法：
    python scripts/migrate_memory_v2_to_store.py --db data/memory_v2.db --dry-run
    python scripts/migrate_memory_v2_to_store.py --db data/memory_v2.db --apply
（AGENT_STORE_DSN 从环境/配置读取，与生产 Store 同源）
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def export_memories(db_path: str) -> List[Dict[str, Any]]:
    """步骤 1-2：只读盘点/导出旧记忆（不写旧库）。"""
    if not os.path.exists(db_path):
        raise SystemExit(f"旧记忆库不存在: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT user_id, agent_id, memory_type, content, importance, tags, "
            "metadata, is_pinned, created_at, updated_at FROM memories_v2 ORDER BY user_id, id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def render_agents_md(entries: List[Dict[str, Any]]) -> str:
    """步骤 3：记忆行 → AGENTS.md（MemoryMiddleware 可加载的 Markdown）。"""
    lines = ["# Agent Memory", ""]
    by_type: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for e in entries:
        by_type[str(e.get("memory_type") or "fact")].append(e)
    for mtype in sorted(by_type):
        lines.append(f"## {mtype}")
        for e in sorted(by_type[mtype], key=lambda x: -(x.get("importance") or 0)):
            tags = f"  #{' #'.join(t for t in (e.get('tags') or '').split(',') if t.strip())}" if e.get("tags") else ""
            pinned = " [pinned]" if e.get("is_pinned") else ""
            content = (e.get("content") or "").replace("\n", " ").strip()
            lines.append(f"- ({e.get('created_at') or ''}){pinned} {content}{tags}")
        lines.append("")
    return "\n".join(lines)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


async def _load_store():
    from app.core.config import settings
    from app.services.agent_runtime.persistence import CheckpointerFactory

    if not (getattr(settings, "AGENT_STORE_DSN", "") or "").strip():
        raise SystemExit("AGENT_STORE_DSN 未配置，无法迁移（Store 是长期记忆唯一事实来源）")
    return await CheckpointerFactory.get().get_store()


async def run_migration(db_path: str, apply: bool) -> None:
    rows = export_memories(db_path)
    by_user: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_user[r["user_id"]].append(r)

    print(f"盘点：共 {len(rows)} 条记忆，{len(by_user)} 个用户")
    type_counts: Dict[str, int] = defaultdict(int)
    for r in rows:
        type_counts[str(r.get("memory_type"))] += 1
    print(f"类型分布：{dict(type_counts)}")

    store = await _load_store()
    report = []
    for user_id, entries in sorted(by_user.items(), key=lambda kv: str(kv[0])):
        content = render_agents_md(entries)
        ns = ("user_memory", str(user_id))
        report.append((user_id, len(entries), digest(content)))
        if apply:
            await store.aput(ns, "AGENTS.md", {
                "content": content,
                "encoding": "utf-8",
                "created_at": datetime.utcnow().isoformat(),
                "modified_at": datetime.utcnow().isoformat(),
            })

    print("\n对账（user_id | 条数 | 内容摘要）：")
    for user_id, count, dgst in report:
        print(f"  {user_id} | {count} | {dgst}")

    if apply:
        # 步骤 5：回读比对
        mismatches = 0
        for user_id, count, dgst in report:
            item = await store.aget(("user_memory", str(user_id)), "AGENTS.md")
            if item is None or digest(item.value.get("content", "")) != dgst:
                mismatches += 1
                print(f"  [不一致] user {user_id} 回读校验失败")
        print(f"\n导入完成：{len(report)} 个用户，回读不一致 {mismatches}（0 为通过）")
        print("旧库保留观察期，确认新系统检索正常后再删除旧 API（不得删除 memory_v2.db 备份）。")
    else:
        print("\n预演完成（未写入任何数据）。确认对账无误后加 --apply 执行导入。")


def main() -> None:
    parser = argparse.ArgumentParser(description="memory_v2.db → LangGraph Store 迁移")
    parser.add_argument("--db", required=True, help="旧 SQLite 记忆库路径（只读）")
    parser.add_argument("--dry-run", action="store_true", help="只读预演（默认行为）")
    parser.add_argument("--apply", action="store_true", help="执行导入")
    args = parser.parse_args()
    asyncio.run(run_migration(args.db, apply=args.apply))


if __name__ == "__main__":
    main()
