"""Database engine and session management.

Supports two backends:
- **PostgreSQL** (asyncpg) — default for production.  Includes GCP Cloud SQL
  connector support via ``AUTOMATION_GCP_DB_INSTANCE``.
- **SQLite** (aiosqlite) — lightweight local backend for open-source /
  self-hosted deployments.  Enabled by setting ``AUTOMATION_DB_URL`` to a
  ``sqlite+aiosqlite:///`` URL.

Which backend is used is determined by :pyclass:`ServiceSettings`:

1. ``db_url`` starting with ``sqlite`` → SQLite
2. ``gcp_db_instance`` set → GCP Cloud SQL (PostgreSQL)
3. Otherwise → direct PostgreSQL via ``db_host`` / ``db_port`` / …
"""

import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from sqlalchemy import event as sa_event
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from automation.config import ServiceSettings, get_config


logger = logging.getLogger("automation.db")


@dataclass
class EngineResult:
    """Result of create_engine containing the engine and optional connector."""

    engine: AsyncEngine
    connector: Any = None  # google.cloud.sql.connector.Connector when using GCP

    async def dispose(self) -> None:
        """Dispose the engine and close the connector if present."""
        await self.engine.dispose()
        if self.connector is not None:
            await self.connector.close_async()


async def create_engine(settings: ServiceSettings | None = None) -> EngineResult:
    """Create a database engine based on settings.

    Returns an EngineResult containing the engine and optional GCP connector.
    Call result.dispose() on shutdown to properly clean up resources.
    """
    if settings is None:
        settings = get_config().service

    if settings.is_sqlite:
        return _create_sqlite_engine(settings)

    if settings.gcp_db_instance:
        return await _create_gcp_engine(settings)

    return _create_pg_engine(settings)


# -- SQLite ----------------------------------------------------------------

def _create_sqlite_engine(settings: ServiceSettings) -> EngineResult:
    """Create an async SQLite engine via aiosqlite.

    Enables WAL journal mode and foreign key enforcement on every new
    connection so SQLite behaves closer to PostgreSQL for our use case.
    """
    engine = create_async_engine(
        settings.db_url,
        echo=False,
        # SQLite does not support pool_size/max_overflow the same way
        # but StaticPool is fine for single-process usage
    )

    @sa_event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return EngineResult(engine=engine)


# -- PostgreSQL (direct) ---------------------------------------------------

def _create_pg_engine(settings: ServiceSettings) -> EngineResult:
    """Create a direct asyncpg PostgreSQL engine."""
    if settings.db_url:
        url = settings.db_url
    else:
        url = URL.create(
            "postgresql+asyncpg",
            username=settings.db_user,
            password=settings.db_pass,
            host=settings.db_host,
            port=settings.db_port,
            database=settings.db_name,
        )
    engine = create_async_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,
    )
    return EngineResult(engine=engine)


# -- GCP Cloud SQL ---------------------------------------------------------

async def _create_gcp_engine(settings: ServiceSettings) -> EngineResult:
    """Create engine using GCP Cloud SQL connector (async).

    Uses create_async_connector() which auto-detects the current running
    event loop, avoiding ConnectorLoopError when connections are created
    from background tasks (scheduler, dispatcher, watchdog).
    """
    from google.cloud.sql.connector import create_async_connector

    # create_async_connector() auto-detects and binds to the current event loop
    connector = await create_async_connector()
    instance = (
        f"{settings.gcp_project}:{settings.gcp_region}:{settings.gcp_db_instance}"
    )

    async def getconn():
        return await connector.connect_async(
            instance,
            "asyncpg",
            user=settings.db_user,
            password=settings.db_pass,
            db=settings.db_name,
        )

    engine = create_async_engine(
        "postgresql+asyncpg://",
        async_creator=getconn,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=settings.db_pool_recycle,
    )
    return EngineResult(engine=engine, connector=connector)


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
