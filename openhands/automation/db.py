"""Database engine and session management.

Supports two database backends:
- PostgreSQL (asyncpg): Default for cloud deployments
- SQLite (aiosqlite): For local/self-hosted deployments

The backend is selected based on the AUTOMATION_DB_URL setting:
- If db_url starts with "sqlite": Use SQLite
- Otherwise: Use PostgreSQL (with optional GCP Cloud SQL connector)
"""

import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from openhands.automation.config import ServiceSettings, get_config


logger = logging.getLogger("automation.db")
SUPPORTED_DB_SSL_MODES = {"prefer", "require", "disable"}


def _normalize_db_ssl_mode(db_ssl_mode: str | None) -> str | None:
    if db_ssl_mode is None:
        return None

    mode = db_ssl_mode.strip().lower()
    if not mode or mode == "prefer":
        return None
    if mode not in SUPPORTED_DB_SSL_MODES:
        raise ValueError(
            f'Unsupported AUTOMATION_DB_SSL_MODE "{db_ssl_mode}". '
            f"Supported values are: {', '.join(sorted(SUPPORTED_DB_SSL_MODES))}."
        )
    return mode


def _build_asyncpg_connect_args(db_ssl_mode: str | None) -> dict:
    mode = _normalize_db_ssl_mode(db_ssl_mode)
    if mode:
        return {"ssl": mode}
    return {}


def _build_pg8000_connect_args(db_ssl_mode: str | None) -> dict:
    mode = _normalize_db_ssl_mode(db_ssl_mode)
    if mode == "require":
        return {"ssl_context": True}
    if mode == "disable":
        return {"ssl_context": False}
    return {}


def is_sqlite_url(url: str) -> bool:
    """Check if a database URL is for SQLite."""
    return url.startswith("sqlite")


def normalize_sqlite_url_for_alembic(url: str) -> str:
    """Convert async SQLite URL to sync version for Alembic.

    Alembic doesn't support async drivers, so we need to convert
    sqlite+aiosqlite:// URLs to plain sqlite:// URLs.
    """
    if url.startswith("sqlite+aiosqlite"):
        return url.replace("sqlite+aiosqlite", "sqlite", 1)
    return url


@dataclass
class EngineResult:
    """Result of create_engine containing the engine and optional connector."""

    engine: AsyncEngine
    connector: Any = None  # google.cloud.sql.connector.Connector when using GCP
    is_sqlite: bool = False

    async def dispose(self) -> None:
        """Dispose the engine and close the connector if present."""
        await self.engine.dispose()
        if self.connector is not None:
            await self.connector.close_async()


async def create_engine(settings: ServiceSettings | None = None) -> EngineResult:
    """Create a database engine based on settings.

    Supports three configurations (checked in order):
    1. db_url with SQLite: Use aiosqlite for local deployments
    2. gcp_db_instance: Use Cloud SQL connector for GCP
    3. Default: Use asyncpg with host/port config

    Returns an EngineResult containing the engine and optional GCP connector.
    Call result.dispose() on shutdown to properly clean up resources.
    """
    if settings is None:
        settings = get_config().service

    # 1. Check for explicit db_url (supports SQLite for local mode)
    if settings.db_url:
        if is_sqlite_url(settings.db_url):
            return _create_sqlite_engine(settings.db_url)
        # PostgreSQL URL provided directly
        engine = create_async_engine(
            settings.db_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=settings.db_pool_recycle,
            pool_pre_ping=True,
        )
        return EngineResult(engine=engine, is_sqlite=False)

    # 2. GCP Cloud SQL connector
    if settings.gcp_db_instance:
        return await _create_gcp_engine(settings)

    # 3. Default: PostgreSQL with host/port config
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
        connect_args=_build_asyncpg_connect_args(settings.db_ssl_mode),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,
        # Fail fast if pool is exhausted rather than waiting indefinitely.
        # This surfaces pool exhaustion issues as errors instead of timeouts,
        # making it easier to diagnose and fix (e.g., by increasing pool_size).
        pool_timeout=settings.db_pool_timeout,
    )
    return EngineResult(engine=engine, is_sqlite=False)


def _create_sqlite_engine(db_url: str) -> EngineResult:
    """Create SQLite engine for local deployments.

    SQLite configuration notes:
    - Uses aiosqlite driver for async support
    - No connection pooling (SQLite handles this internally)
    - check_same_thread=False required for async usage
    """
    # Ensure the URL uses aiosqlite driver
    if not db_url.startswith("sqlite+aiosqlite"):
        db_url = db_url.replace("sqlite://", "sqlite+aiosqlite://", 1)

    engine = create_async_engine(
        db_url,
        # SQLite-specific settings
        connect_args={"check_same_thread": False},
        # No pooling for SQLite - it handles this internally
        pool_pre_ping=True,
    )
    logger.info("Created SQLite engine: %s", db_url.split("?")[0])
    return EngineResult(engine=engine, is_sqlite=True)


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
        # Fail fast if pool is exhausted rather than waiting indefinitely.
        pool_timeout=settings.db_pool_timeout,
    )
    return EngineResult(engine=engine, connector=connector)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create a session factory for the given engine."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# Module-level flag set after engine creation to indicate SQLite usage.
# Used by scheduler/dispatcher to skip PostgreSQL-specific features like
# FOR UPDATE SKIP LOCKED (not supported by SQLite).
_is_sqlite: bool = False


def set_sqlite_mode(is_sqlite: bool) -> None:
    """Set the SQLite mode flag. Called during app startup."""
    global _is_sqlite
    _is_sqlite = is_sqlite


def using_sqlite() -> bool:
    """Check if the database backend is SQLite.

    Returns True when running with SQLite, which affects:
    - FOR UPDATE SKIP LOCKED is skipped (not supported by SQLite)
    - Single-process mode is assumed (no row locking needed)
    """
    return _is_sqlite


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
