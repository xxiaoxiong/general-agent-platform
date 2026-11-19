"""账号删除在返回响应之前检查真实外键，并保留关联资源。"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_current_superuser
from app.api.v1.users import router
from app.core.database import get_db
from app.models.audit import AuditLog
from app.models.base import BaseModel
from app.models.prompt import PromptTemplate
from app.models.user import Role, User, UserRole


@pytest.mark.asyncio
async def test_delete_user_preserves_owned_resources_and_removes_unreferenced_memberships():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    try:
        async with engine.begin() as connection:
            await connection.run_sync(BaseModel.metadata.create_all)

        async with sessions.begin() as db:
            admin, owner, member = (
                User(username=name, email=f"{name}@example.com", hashed_password="fixture")
                for name in ("admin_fixture", "owner_fixture", "member_fixture")
            )
            role = Role(name="user", display_name="成员")
            db.add_all([admin, owner, member, role])
            await db.flush()
            db.add_all([
                UserRole(user_id=owner.id, role_id=role.id),
                UserRole(user_id=member.id, role_id=role.id),
                PromptTemplate(name="保留提示词", content="测试", owner_id=owner.id),
            ])

        async def isolated_db():
            async with sessions.begin() as db:
                yield db

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = isolated_db
        app.dependency_overrides[get_current_superuser] = lambda: admin

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            conflict = await client.delete(f"/users/{owner.id}")
            assert conflict.status_code == 409
            assert "禁用账号" in conflict.json()["detail"]
            removed = await client.delete(f"/users/{member.id}")
            assert removed.status_code == 200

        async with sessions() as db:
            assert await db.get(User, owner.id) is not None
            assert await db.get(User, member.id) is None
            assert list(await db.scalars(select(UserRole.user_id))) == [owner.id]
            assert list(await db.scalars(select(PromptTemplate.owner_id))) == [owner.id]
            assert list(await db.scalars(select(AuditLog.resource_id))) == [str(member.id)]
    finally:
        await engine.dispose()
