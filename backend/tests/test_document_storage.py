"""文档发布事务失败、提交结果不明与孤儿维护工具回归。"""

import os
import time
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import Base
from app.models.agent import Agent, AgentRun
from app.models.user import User
from app.services.documents import repository, storage
from app.services.documents.cleanup import reconcile_files
from app.services.documents.errors import DocumentError


def test_version_storage_identity_refuses_absolute_parent_and_foreign_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path))
    expected = storage.version_path(1, 12, 2)
    for supplied in (str(expected), "../v2.docx", "2/documents/12/v2.docx"):
        with pytest.raises(DocumentError, match="存储标识不一致"):
            storage.version_path(1, 12, 2, supplied)
    parent = expected.parent
    parent.parent.mkdir(parents=True)
    private_folder = tmp_path / "another-user"
    private_folder.mkdir()
    try:
        parent.symlink_to(private_folder, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows 当前账户未启用符号链接权限")
        raise
    with pytest.raises(DocumentError, match="符号链接"):
        storage.version_path(1, 12, 2)


@pytest_asyncio.fixture
async def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path / "files"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'storage.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add(User(id=1, username="storage", email="storage@test.local", hashed_password="placeholder"))
        db.add(Agent(id=1, name="文档助手", owner_id=1, is_active=True))
        await db.flush()
        db.add_all(AgentRun(id=run_id, thread_id=run_id, user_id=1, agent_id=1, status=status)
                   for run_id, status in [("first", "running"), ("second", "running"), ("done", "failed")])
        await db.commit()
    try:
        yield sessions
    finally:
        await engine.dispose()


async def _publish(db, run_id, data, document_id=None, previous_version=None):
    with storage.staging_docx(1, run_id) as staged:
        staged.write_bytes(data)
        return await repository.publish_document(
            db, user_id=1, run_id=run_id, session_id=None, supplied_document_id=document_id,
            expected_current_version=previous_version, title="测试文档", document_type="report",
            profile_id=None, profile_version_no=None, filename="测试文档.docx", staged=staged, preview={},
            validation_report={"passed": True}, change_summary="测试发布", version_diff={}, fence=AsyncMock(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("editing", [False, True])
async def test_known_rollback_cleans_only_its_file(sessions, monkeypatch, editing):
    document_id = None
    if editing:
        async with sessions() as db:
            asset, _ = await _publish(db, "first", b"first version")
            document_id = asset.id
    async with sessions() as db:
        monkeypatch.setattr(db, "commit", AsyncMock(side_effect=ConnectionError("simulated rollback")))
        with pytest.raises(ConnectionError, match="simulated rollback"):
            await _publish(db, "second", b"failed version", document_id, 1 if editing else None)
    async with sessions() as db:
        assert await repository.version_by_run(db, "second", 1) is None
    versions = list(storage.root().glob("*/documents/*/v*.docx"))
    assert [item.read_bytes() for item in versions] == ([b"first version"] if editing else [])


@pytest.mark.asyncio
async def test_commit_acknowledgement_loss_recovers_the_durable_version(sessions, monkeypatch):
    async with sessions() as db:
        real_commit = db.commit

        async def commit_with_lost_response():
            await real_commit()
            raise ConnectionError("simulated acknowledgement loss")

        monkeypatch.setattr(db, "commit", commit_with_lost_response)
        asset, version = await _publish(db, "first", b"durable version")
        assert storage.verified_version(1, asset.id, 1, version.relative_path, version.content_hash,
                                        version.size).read_bytes() == b"durable version"
    async with sessions() as db:
        result = await repository.version_by_run(db, "first", 1)
        assert result[1].id == version.id
        _, replay = await _publish(db, "first", b"must not overwrite")
        assert replay.id == version.id
    assert storage.version_path(1, asset.id, 1).read_bytes() == b"durable version"


@pytest.mark.asyncio
async def test_unresolved_commit_preserves_file_for_locked_reconciliation(sessions, monkeypatch):
    async with sessions() as db:
        async def unavailable(*_):
            raise ConnectionError("recovery database unavailable")

        monkeypatch.setattr(db, "commit", AsyncMock(side_effect=ConnectionError("original failure")))
        with monkeypatch.context() as unavailable_db:
            unavailable_db.setattr(repository, "locked_version_record", unavailable)
            with pytest.raises(ConnectionError, match="original failure"):
                await _publish(db, "first", b"check later")
    candidates = list(storage.root().glob("*/documents/*/v*.docx"))
    assert len(candidates) == 1 and candidates[0].read_bytes() == b"check later"
    results = await reconcile_files(cutoff=time.time() + 20, sessions=sessions, apply=True)
    assert len(results) == 1 and results[0]["type"] == "version"
    assert not candidates[0].exists()


@pytest.mark.asyncio
async def test_cleanup_dry_run_and_apply_skip_durable_versions_active_runs_and_foreign_paths(sessions):
    async with sessions() as db:
        asset, _ = await _publish(db, "first", b"committed")
    durable = storage.version_path(1, asset.id, 1)
    unrecorded = storage.version_path(1, asset.id, 2)
    unrecorded.write_bytes(b"orphan edit")
    orphan = storage.version_path(1, 999, 1)
    orphan.parent.mkdir()
    orphan.write_bytes(b"orphan creation")
    recent = storage.version_path(1, 999, 2)
    recent.write_bytes(b"recent orphan")
    unrelated = orphan.parent / "notes.docx"
    unrelated.write_bytes(b"otherwise named file")
    with storage.staging_docx(1, "second") as active, storage.staging_docx(1, "done") as stale:
        active.write_bytes(b"active work")
        stale.write_bytes(b"stale work")
        now = time.time()
        for item in (durable, unrecorded, orphan, active, active.parent, stale, stale.parent):
            os.utime(item, (now - 7200, now - 7200))
        dry = await reconcile_files(cutoff=now - 3600, sessions=sessions)
        assert {(item["document_id"], item["version_no"]) for item in dry if item["type"] == "version"} == {
            (asset.id, 2), (999, 1),
        }
        assert [item["run_id"] for item in dry if item["type"] == "staging"] == ["done"]
        assert all(item.is_file() for item in (durable, unrecorded, orphan, recent, unrelated, active, stale))
        assert await reconcile_files(cutoff=now - 3600, sessions=sessions, apply=True) == dry
        assert all(item.is_file() for item in (durable, recent, unrelated, active))
        assert not any(item.exists() for item in (unrecorded, orphan, stale.parent))
