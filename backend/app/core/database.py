import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings

logger = logging.getLogger(__name__)

_IS_SQLITE = settings.DATABASE_URL.startswith("sqlite")

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_size=10,
    max_overflow=20,
    # SQLite 并发写入需要更长的锁等待（MySQL/PostgreSQL 无此问题）
    connect_args={"timeout": 30} if _IS_SQLITE else {},
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    if _IS_SQLITE:
        # WAL 模式：读写互不阻塞，避免并发会话在回滚日志模式下互锁
        # （journal_mode 持久化于库文件，仅需设置一次）
        async with engine.begin() as conn:
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    if settings.is_production:
        # 生产环境 Schema 由 Alembic 迁移管理（启动入口先执行 upgrade head）；
        # create_all 只会补新表、不会给已有表加字段，不能作为生产升级手段
        return
    # 多 Worker 同时首启时 create_all 存在竞态：两个进程都可能先判断"表不存在"
    # 再同时 CREATE，后到者报 1050/42P07 "已存在"。这属于其他进程已完成建表，
    # 短暂重试即可（create_all 自带存在性检查）；权限/连接等真实错误必须上抛。
    for attempt in range(5):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            return
        except Exception as exc:  # noqa: BLE001
            message = str(exc).lower()
            concurrent = any(token in message for token in (
                "already exists", "duplicate table", "1050", "42p07",
            ))
            if not concurrent or attempt == 4:
                raise
            logger.warning("init_db 建表与其它 Worker 竞态，重试（第 %d 次）：%s", attempt + 1, exc)
            await asyncio.sleep(0.5)
