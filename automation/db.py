"""Database engine and session management.

Follows the same patterns as OpenHands enterprise:
- asyncpg for PostgreSQL
- aiosqlite for local dev
- GCP Cloud SQL connector for production
"""

import logging
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from automation.config import Settings, get_settings


logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def get_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine
    if _engine is not None:
        return _engine

    if settings is None:
        settings = get_settings()

    if settings.gcp_db_instance:
        _engine = await _create_gcp_engine(settings)
    elif settings.sqlite_path is not None:
        path = settings.sqlite_path or "automations.db"
        _engine = create_async_engine(
            f"sqlite+aiosqlite:///{path}",
            poolclass=NullPool,
            pool_pre_ping=True,
        )
    else:
        from sqlalchemy.engine import URL

        url = URL.create(
            "postgresql+asyncpg",
            username=settings.db_user,
            password=settings.db_pass,
            host=settings.db_host,
            port=settings.db_port,
            database=settings.db_name,
        )
        _engine = create_async_engine(
            url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=1800,
            pool_pre_ping=True,
        )

    return _engine


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
        f"{settings.gcp_project}:{settings.gcp_region}:{settings.gcp_db_instance}"
    )

    async def _connect():
        return await connector.connect_async(
            instance,
            "asyncpg",
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
        "postgresql+asyncpg://",
        creator=adapted_creator,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


async def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is not None:
        return _session_factory

    engine = await get_engine()
    _session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session."""
    factory = await get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def dispose_engine() -> None:
    """Shutdown hook to dispose of the engine."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
