"""P2-04：长期记忆 —— StoreBackend 跨 Session 持久化 / 用户隔离 / 迁移对账。

运行：
    pytest backend/tests/test_memory_store.py -q
"""
import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

from app.services.agent_runtime.backends import build_backend

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _store():
    from langgraph.store.memory import InMemoryStore

    return InMemoryStore()


class TestStoreBackendRouting:
    def test_memory_persists_across_sessions(self):
        """/memory/ 写入跨 Session（同用户）可见 —— 长期记忆语义。"""
        store = _store()
        b1 = build_backend(11, 22, 33, store=store)   # session 33
        asyncio.run(b1.awrite("/memory/AGENTS.md", "用户偏好：中文回复"))

        b2 = build_backend(11, 22, 44, store=store)   # 另一个 session
        result = asyncio.run(b2.aread("/memory/AGENTS.md"))
        assert "中文回复" in str(getattr(result, "content", result))

    def test_memory_isolated_between_users(self):
        """不同用户的记忆 namespace 隔离（AG-05）。"""
        store = _store()
        ba = build_backend(11, 22, 33, store=store)
        bb = build_backend(99, 22, 33, store=store)
        asyncio.run(ba.awrite("/memory/AGENTS.md", "A 的秘密偏好"))

        result = asyncio.run(bb.aread("/memory/AGENTS.md"))
        assert "A 的秘密偏好" not in str(getattr(result, "content", result))

    def test_workspace_still_isolated_per_session(self):
        store = _store()
        b1 = build_backend(11, 22, 33, store=store)
        b2 = build_backend(11, 22, 44, store=store)
        asyncio.run(b1.awrite("/workspace/note.txt", "session-33-data"))
        result = asyncio.run(b2.aread("/workspace/note.txt"))
        assert "session-33-data" not in str(getattr(result, "content", result)), \
            "工作区文件仍须按 Session 隔离（与长期记忆区分）"


class TestMigrationScript:
    def _mk_legacy_db(self, tmp_path) -> str:
        db = tmp_path / "memory_v2.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE memories_v2 (id INTEGER PRIMARY KEY, user_id INTEGER, agent_id INTEGER,"
            " memory_type TEXT DEFAULT 'fact', content TEXT, importance REAL DEFAULT 1.0,"
            " tags TEXT DEFAULT '', metadata TEXT DEFAULT '{}', is_pinned INTEGER DEFAULT 0,"
            " created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO memories_v2 (user_id, agent_id, memory_type, content, importance, tags, created_at)"
            " VALUES (1, 2, 'preference', '喜欢中文回复', 2.0, 'lang', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO memories_v2 (user_id, agent_id, memory_type, content, importance, tags, created_at)"
            " VALUES (1, 2, 'fact', '时区 Asia/Shanghai', 1.0, '', '2026-01-02')"
        )
        conn.execute(
            "INSERT INTO memories_v2 (user_id, agent_id, memory_type, content, importance, tags, created_at)"
            " VALUES (9, 2, 'fact', '其他用户记忆', 1.0, '', '2026-01-03')"
        )
        conn.commit()
        conn.close()
        return str(db)

    def test_export_and_render(self, tmp_path):
        sys.path.insert(0, str(BACKEND_DIR / "scripts"))
        import migrate_memory_v2_to_store as mig

        rows = mig.export_memories(self._mk_legacy_db(tmp_path))
        assert len(rows) == 3
        md = mig.render_agents_md([r for r in rows if r["user_id"] == 1])
        assert "喜欢中文回复" in md and "时区 Asia/Shanghai" in md
        assert "## preference" in md and "## fact" in md

    def test_dry_run_writes_nothing_and_apply_reconciles(self, tmp_path, monkeypatch):
        sys.path.insert(0, str(BACKEND_DIR / "scripts"))
        import migrate_memory_v2_to_store as mig

        from app.core.config import settings

        monkeypatch.setattr(settings, "AGENT_STORE_DSN", "memory", raising=False)
        db = self._mk_legacy_db(tmp_path)

        # dry-run：不写入
        asyncio.run(mig.run_migration(db, apply=False))

        # apply：导入 + 回读对账（须与导入同一事件循环，factory 换循环会重置内存 Store）
        async def apply_and_verify():
            await mig.run_migration(db, apply=True)
            store = await mig._load_store()
            item = await store.aget(("user_memory", "1"), "AGENTS.md")
            assert item is not None and "喜欢中文回复" in item.value["content"]
            other = await store.aget(("user_memory", "9"), "AGENTS.md")
            assert other is not None and "其他用户记忆" in other.value["content"]

        asyncio.run(apply_and_verify())
