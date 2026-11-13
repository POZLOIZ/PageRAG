from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from pagerag.models import Base
from pagerag.settings import Settings


def create_engine(database_url: str) -> AsyncEngine:
    # MySQL 会主动断开闲置连接；服务空闲一段时间后复用池中连接会报
    # "MySQL server has gone away"。取连接前先探活，并定期回收旧连接。
    return create_async_engine(
        database_url,
        connect_args={"connect_timeout": 10},
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(database_url: str) -> None:
    """按当前模型建表。已存在的表不改动；结构稳定后再接 Alembic 迁移。"""
    engine = create_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


class Database:
    """供服务层与 API 共用的数据库入口。"""

    def __init__(self, settings: Settings) -> None:
        self.engine = create_engine(settings.database_url)
        self.sessions = create_session_factory(self.engine)

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
