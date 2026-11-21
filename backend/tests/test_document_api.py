"""真实文档资产、版本和规范接口的权限与持久化测试。"""

from io import BytesIO
from typing import TypedDict

import pytest
import pytest_asyncio
from docx import Document
from fastapi import Depends, FastAPI, Header
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_current_user
from app.api.v2.runs import router as runs
from app.api.v2.documents import documents, profiles
from app.core.config import settings
from app.core.database import Base, get_db
from app.models.agent import Agent, AgentRun
from app.models.document import DocumentAsset, DocumentVersion, RunInputRequest
from app.models.user import User
from app.services.documents import storage
from app.services.agent_runtime.persistence import CheckpointerFactory
from app.services.documents.renderer import render_docx
from app.services.documents.schemas import DocumentDraft


@pytest_asyncio.fixture
async def api(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'documents.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add_all([
            User(id=1, username="author", email="author@test.local", hashed_password="x"),
            User(id=2, username="reader", email="reader@test.local", hashed_password="x"),
            User(id=3, username="admin", email="admin@test.local", hashed_password="x", is_superuser=True),
            Agent(id=11, name="文档助手", owner_id=1, is_active=True),
        ])
        await db.commit()

    async def as_user(x_test_user: int = Header(1), db: AsyncSession = Depends(get_db)):
        return await db.get(User, x_test_user)

    async def database():
        async with sessions() as db:
            yield db

    monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path / "documents"))
    app = FastAPI()
    app.include_router(profiles, prefix="/api/v2")
    app.include_router(documents, prefix="/api/v2")
    app.include_router(runs, prefix="/api/v2")
    app.dependency_overrides[get_current_user] = as_user
    app.dependency_overrides[get_db] = database
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test/api/v2/") as client:
            yield client, sessions
    finally:
        await engine.dispose()


def _definition(name):
    return {
        "name": name, "document_type": "project_report",
        "sections": [{"id": "overview", "title": "总体进展", "order": 1}],
    }


@pytest.mark.asyncio
async def test_review_reader_uses_full_pre_decision_checkpoint_and_owner_guard(api, monkeypatch):
    client, sessions = api
    long_context = "这是审阅时必须完整展示的章内上下文。" * 60
    old = DocumentDraft(title="操作指南", sections=[
        {"id": "parent", "title": "系统管理", "order": 1},
        {"id": "accounts", "title": "用户管理", "order": 2, "level": 2, "blocks": [
            {"type": "paragraph", "id": "before", "text": long_context},
            {"type": "table", "id": "fields", "rows": [["字段", "描述"], ["账号", "用户名"]]},
        ]},
    ])
    new = old.model_copy(deep=True)
    new.sections[1].blocks[1].rows[1][1] = "使用实名注册的用户名"
    dropped = {"type": "paragraph", "id": "candidate", "text": "建议稿中的未采用段落"}
    rejected = DocumentDraft.model_validate({**new.model_dump(), "sections": [
        new.sections[0].model_dump(), {**new.sections[1].model_dump(), "blocks": [
            *(block.model_dump() for block in new.sections[1].blocks), dropped,
        ]},
    ]})
    changes = [
        {"id": "change_1", "section_id": "accounts", "block_id": "fields", "scope": "block", "kind": "table"},
        {"id": "change_2", "section_id": "accounts", "block_id": "candidate", "scope": "block", "kind": "text"},
    ]

    class State(TypedDict, total=False):
        request: str
        draft: dict
        previous: dict
        edit_changes: list[dict]
        accepted_ids: list[str]

    saver = InMemorySaver()
    graph = StateGraph(State)
    graph.add_node("propose", lambda _: {"previous": old.model_dump(), "draft": rejected.model_dump(), "edit_changes": changes})
    graph.add_node("decide", lambda _: {"draft": new.model_dump(), "accepted_ids": ["change_1"]})
    graph.add_edge(START, "propose")
    graph.add_edge("propose", "decide")
    graph.add_edge("decide", END)
    run_id = "review-reader"
    await graph.compile(checkpointer=saver).ainvoke({"request": "完善用户管理的字段说明"}, {"configurable": {"thread_id": run_id}})

    async def checkpointer():
        return saver

    monkeypatch.setattr(CheckpointerFactory.get(), "get_checkpointer", checkpointer)
    async with sessions() as db:
        db.add(AgentRun(id=run_id, thread_id=run_id, agent_id=11, user_id=1, status="completed",
                        input_payload={"execution_context": {"task_type": "document_edit"}}))
        await db.flush()
        db.add(RunInputRequest(id="decision", run_id=run_id, ordinal=3, status="submitted", prompt="请选择",
                               input_schema={}, required_fields=["accepted_change_ids"],
                               response={"accepted_change_ids": ["change_1"]}))
        await db.commit()
    path = f"runs/{run_id}/document-review/context"
    for user in (2, 3):
        assert (await client.get(path, headers={"X-Test-User": str(user)})).status_code == 404
    response = await client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "private, no-store"
    context = response.json()
    group = context["groups"][0]
    assert group["path"] == ["系统管理", "用户管理"] and group["mode"] == "edits"
    assert group["before"][0]["blocks"][0]["text"] == long_context
    assert group["after"][0]["blocks"][1]["rows"][1][1] == "使用实名注册的用户名"
    assert group["after"][0]["blocks"][2]["text"] == dropped["text"]
    assert context["decision"] == {"accepted_change_ids": ["change_1"], "rejected_change_ids": ["change_2"]}


@pytest.mark.asyncio
async def test_profile_versions_remain_immutable_and_private(api):
    client, _ = api
    invalid = await client.post("document-profiles", json={"definition": {**_definition("缺少章节"), "sections": []}})
    assert invalid.status_code == 422
    created = await client.post("document-profiles", json={"definition": _definition("项目报告初稿")})
    assert created.status_code == 201, created.text
    first = created.json()
    path = f"document-profiles/{first['id']}"
    assert (first["status"], first["current_version"], first["published_version"]) == ("draft", 1, None)
    assert first["can_manage"] is True
    response = await client.put(path, json={
        "definition": _definition("项目报告新版"), "expected_version": 1, "change_summary": "更新规范名称",
    })
    assert response.status_code == 200, response.text
    assert response.json()["current_version"] == 2
    stale = await client.put(path, json={"definition": _definition("过期修改"), "expected_version": 1})
    assert stale.status_code == 409
    versions = (await client.get(f"{path}/versions")).json()["items"]
    assert [(v["version_no"], v["definition"]["name"]) for v in versions] == [
        (2, "项目报告新版"), (1, "项目报告初稿"),
    ]
    for user in (2, 3):
        headers = {"X-Test-User": str(user)}
        assert (await client.get(path, headers=headers)).status_code == 404
        assert (await client.get(f"{path}/versions", headers=headers)).status_code == 404
        assert (await client.delete(path, headers=headers)).status_code == 404
    published = await client.post(f"{path}/publish")
    assert published.json()["published_version"] == 2
    assert (await client.get("document-profiles", params={"status": "published"})).json()["total"] == 1
    archived = await client.post(f"{path}/archive")
    assert archived.json()["status"] == "archived"
    assert (await client.get("document-profiles", params={"status": "published"})).json()["total"] == 0


@pytest.mark.asyncio
async def test_organization_profile_reads_only_published_version(api):
    client, _ = api
    body = {"definition": _definition("团队规范"), "visibility": "organization"}
    assert (await client.post("document-profiles", json=body)).status_code == 403
    admin = {"X-Test-User": "3"}
    created = await client.post("document-profiles", json=body, headers=admin)
    assert created.status_code == 201, created.text
    path = f"document-profiles/{created.json()['id']}"
    assert (await client.get(path)).status_code == 404
    assert (await client.post(f"{path}/publish", headers=admin)).status_code == 200
    changed = await client.put(path, headers=admin, json={"definition": _definition("仅管理员看到的草稿")})
    assert changed.status_code == 200, changed.text
    assert changed.json()["current_version"] == 2
    visible = (await client.get(path)).json()
    assert visible["name"] == "团队规范"
    assert visible["can_manage"] is False
    assert visible["current_version"] == 1
    assert [v["version_no"] for v in (await client.get(f"{path}/versions")).json()["items"]] == [1]
    assert (await client.put(path, json=body)).status_code == 403
    assert (await client.post(f"{path}/archive", headers=admin)).status_code == 200
    assert (await client.get(path)).status_code == 404


@pytest.mark.asyncio
async def test_asset_preview_download_and_tasks_use_owned_assets(api):
    client, sessions = api
    draft = DocumentDraft.model_validate({
        "title": "项目总结",
        "sections": [{
            "id": "overview", "title": "进展", "order": 1,
            "blocks": [{"type": "paragraph", "text": "完成前后端验证。"}],
        }],
    })
    async with sessions() as db:
        db.add_all([
            AgentRun(id="doc-run", thread_id="doc-run", agent_id=11, user_id=1, status="completed",
                     input_payload={"execution_context": {"task_type": "document_generate"}}),
            AgentRun(id="other-run", thread_id="other-run", agent_id=11, user_id=2, status="completed",
                     input_payload={"execution_context": {"task_type": "profile_extract"}}),
            AgentRun(id="chat-run", thread_id="chat-run", agent_id=11, user_id=1, status="completed",
                     input_payload={"execution_context": {"task_type": "general_chat"}}),
        ])
        asset = DocumentAsset(owner_id=1, title=draft.title, document_type="project_report", created_by_run_id="doc-run")
        db.add(asset)
        await db.flush()
        document_id = asset.id
        with storage.staging_docx(1, "doc-run") as staging:
            render_docx(draft, staging)
            file_path = storage.put_version(staging, 1, document_id, 1)
        digest, size = storage.fingerprint(file_path)
        db.add(DocumentVersion(
            document_id=document_id, version_no=1, filename="项目总结.docx",
            relative_path=storage.relative_path(1, document_id, 1), mime_type=storage.DOCX_MIME,
            size=size, content_hash=digest, created_by_run_id="doc-run",
            validation_report={"passed": True}, preview=draft.model_dump(mode="json"),
        ))
        await db.commit()

    path = f"documents/{document_id}"
    preview_path = f"{path}/versions/1/preview"
    download_path = f"{path}/versions/1/download"
    assert (await client.get("documents")).json()["total"] == 1
    assert [item["id"] for item in (await client.get("documents/tasks")).json()["items"]] == ["doc-run"]
    other = {"X-Test-User": "2"}
    assert (await client.get("documents", headers=other)).json() == {"items": [], "total": 0}
    assert [item["id"] for item in (await client.get("documents/tasks", headers=other)).json()["items"]] == ["other-run"]
    for protected in (path, f"{path}/versions", preview_path, download_path):
        assert (await client.get(protected, headers=other)).status_code == 404
    assert (await client.delete(path, headers=other)).status_code == 404

    versions = (await client.get(f"{path}/versions")).json()["items"]
    assert versions[0]["filename"] == "项目总结.docx"
    assert "relative_path" not in versions[0]
    preview = await client.get(preview_path)
    assert preview.status_code == 200, preview.text
    assert preview.json() == draft.model_dump(mode="json")
    downloaded = await client.get(download_path)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.headers["content-type"] == storage.DOCX_MIME
    assert "attachment" in downloaded.headers["content-disposition"]
    assert "完成前后端验证。" in [p.text for p in Document(BytesIO(downloaded.content)).paragraphs]
    assert (await client.get(f"{path}/versions/2/preview")).status_code == 404

    file_path.write_bytes(b"inconsistent")
    assert (await client.get(download_path)).status_code == 409
    assert (await client.delete(path)).status_code == 204
    assert (await client.delete(path)).status_code == 204
    assert (await client.get(download_path)).status_code == 404
    assert not file_path.exists()
