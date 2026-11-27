"""文档与 v2 Run 使用已授权的数据源绑定。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from sqlalchemy import URL, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.database import engine as platform_engine
from app.models.database_connection import DatabaseConnection
from app.models.knowledge import KnowledgeBase
from app.models.user import User

DATABASE_TOOLS = frozenset({"db_query", "db_schema", "db_count", "db_aggregate"})


class SourceBindingError(ValueError):
    """助手的数据源绑定当前不可用。"""


async def check_source_bindings(
    db: AsyncSession, user_id: int | None, config: dict[str, Any]
) -> DatabaseConnection | None:
    connection_id = config.get("database_connection_id")
    knowledge_ids = [
        int(x) for x in (
            config.get("knowledge_base_ids")
            or ([config.get("knowledge_base_id")] if config.get("knowledge_base_id") else [])
        ) if x
    ]
    knowledge_ids = list(dict.fromkeys(knowledge_ids))
    connection = None
    if connection_id is not None:
        connection = await db.scalar(select(DatabaseConnection).where(
            DatabaseConnection.id == connection_id, DatabaseConnection.owner_id == user_id,
            DatabaseConnection.owner_id.is_not(None), DatabaseConnection.is_active.is_(True),
        ))
        if connection is None:
            raise SourceBindingError("助手绑定的数据库不存在或当前用户无权访问")
        if connection.db_type == "sqlite":
            path = Path(connection.database)
            try:
                owner_root = Path(settings.AGENT_FILES_ROOT).resolve() / str(user_id)
                in_workspace = owner_root.resolve() == owner_root and path.resolve().is_relative_to(owner_root)
            except (OSError, RuntimeError):
                raise SourceBindingError("SQLite 数据源路径无效") from None
            if not in_workspace and not await db.scalar(select(User.is_superuser).where(User.id == user_id)):
                raise SourceBindingError("当前用户无权读取该 SQLite 数据源位置")
    if knowledge_ids:
        user = await db.scalar(select(User).where(User.id == user_id))
        if user is None:
            raise SourceBindingError("助手绑定的知识库不存在或当前用户无权访问")
        from app.services.knowledge.acl import accessible_bases_filter

        allowed = (await db.execute(
            select(KnowledgeBase.id).where(
                KnowledgeBase.id.in_(knowledge_ids),
                accessible_bases_filter(user),
                KnowledgeBase.is_active.is_(True),
            )
        )).scalars().all()
        if set(int(x) for x in allowed) != set(knowledge_ids):
            raise SourceBindingError("助手绑定的知识库不存在或当前用户无权访问")
    return connection


@asynccontextmanager
async def read_bound_database(connection: DatabaseConnection):
    kind = connection.db_type
    query = dict(parse_qsl(connection.extra_params or "", keep_blank_values=True))
    if kind == "sqlite":
        path = Path(connection.database).resolve()
        platform_db = platform_engine.url
        platform_path = Path(platform_db.database).resolve() if (
            platform_db.get_backend_name() == "sqlite" and platform_db.database
        ) else None
        if (
            not path.is_file()
            or (platform_path is not None and platform_path.is_file() and path.samefile(platform_path))
        ):
            raise SourceBindingError("助手绑定的 SQLite 数据源不可访问")
        url = URL.create(
            "sqlite+aiosqlite", database=path.as_uri(), query={"mode": "ro", "uri": "true"}
        )
        readonly = "PRAGMA query_only=ON"
    elif kind in {"mysql", "postgresql"}:
        url = URL.create(
            "mysql+aiomysql" if kind == "mysql" else "postgresql+asyncpg",
            username=connection.username, password=connection.password, host=connection.host,
            port=connection.port, database=connection.database, query=query,
        )
        readonly = (
            "SET SESSION TRANSACTION READ ONLY" if kind == "mysql" else "SET TRANSACTION READ ONLY"
        )
    else:
        raise SourceBindingError("助手绑定的数据库类型不可用")
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with AsyncSession(engine) as db:
            try:
                await db.execute(text(readonly))
            except Exception:
                raise SourceBindingError("无法连接已绑定的数据库或启用只读事务") from None
            yield db
    finally:
        await engine.dispose()
