"""Database engine and session management.

Follows the same patterns as OpenHands enterprise:
- asyncpg for PostgreSQL
- GCP Cloud SQL connector for production
"""

import logging
from collections.abc import AsyncGenerator

from fastapi import Request
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from automation.config import Settings, get_settings

logger = logging.getLogger('automation.db')


async def create_engine(settings: Settings | None = None) -> AsyncEngine:
    """Create a new PostgreSQL database engine based on settings."""
    if settings is None:
        settings = get_settings()

    if settings.gcp_db_instance:
        return await _create_gcp_engine(settings)

    url = URL.create(
        'postgresql+asyncpg',
        username=settings.db_user,
        password=settings.db_pass,
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
    )
    return create_async_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=1800,
        pool_pre_ping=True,
    )


async def _create_gcp_engine(settings: Settings) -> AsyncEngine:
    """Create engine using GCP Cloud SQL connector (async)."""
    import asyncpg
    from google.cloud.sql.connector import Connector
    from sqlalchemy.dialects.postgresql.asyncpg import (
        AsyncAdapt_asyncpg_connection,
        AsyncAdapt_asyncpg_dbapi,
    )
    from sqlalchemy.util import await_only

    connector = Connector()
    instance = (
        f'{settings.gcp_project}:{settings.gcp_region}:{settings.gcp_db_instance}'
    )

    async def _connect():
        return await connector.connect_async(
            instance,
            'asyncpg',
            user=settings.db_user,
            password=settings.db_pass,
            db=settings.db_name,
        )

    dbapi = AsyncAdapt_asyncpg_dbapi(asyncpg)

    def adapted_creator():
        return AsyncAdapt_asyncpg_connection(
            dbapi,
            await_only(_connect()),
            prepared_statement_cache_size=100,
        )

    return create_async_engine(
        'postgresql+asyncpg://',
        creator=adapted_creator,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create a session factory for the given engine."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session from app.state."""
    session_factory: async_sessionmaker[AsyncSession] = (
        request.app.state.session_factory
    )
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
