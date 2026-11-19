"""提示词模板删除回归，数据库启用真实外键约束。"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_current_user
from app.api.v1.prompts import router
from app.core.database import get_db
from app.models.base import BaseModel
from app.models.prompt import PromptTemplate, PromptTemplateVersion
from app.models.user import User


@pytest.mark.asyncio
async def test_delete_template_removes_only_owned_versions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda conn: BaseModel.metadata.create_all(
                    conn, tables=[User.__table__, PromptTemplate.__table__, PromptTemplateVersion.__table__]
                )
            )

        async with sessions.begin() as db:
            owner = User(username="prompt_owner", email="owner@example.com", hashed_password="fixture")
            other = User(username="other_owner", email="other@example.com", hashed_password="fixture")
            db.add_all([owner, other])
            await db.flush()
            template = PromptTemplate(name="测试模板", content="测试", owner_id=owner.id)
            retained = PromptTemplate(name="保留模板", content="保留", owner_id=other.id)
            db.add_all([template, retained])
            await db.flush()
            db.add_all([
                PromptTemplateVersion(template_id=template.id, version=1, content="测试", created_by=owner.id),
                PromptTemplateVersion(template_id=template.id, version=2, content="修改", created_by=owner.id),
                PromptTemplateVersion(template_id=retained.id, version=1, content="保留", created_by=other.id),
            ])

        async def isolated_db():
            async with sessions.begin() as db:
                yield db

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = isolated_db
        app.dependency_overrides[get_current_user] = lambda: owner

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            forbidden = await client.delete(f"/prompts/{retained.id}")
            assert forbidden.status_code == 403
            deleted = await client.delete(f"/prompts/{template.id}")
            assert deleted.status_code == 200
            assert deleted.json() == {"message": "删除成功"}

        async with sessions() as db:
            assert await db.get(PromptTemplate, template.id) is None
            assert await db.get(PromptTemplate, retained.id) is not None
            versions = list(await db.scalars(select(PromptTemplateVersion.template_id)))
            assert versions == [retained.id]
    finally:
        await engine.dispose()
