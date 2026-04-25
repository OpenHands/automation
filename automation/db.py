"""Database engine and session management.

Follows the same patterns as OpenHands enterprise:
- asyncpg for PostgreSQL
- GCP Cloud SQL connector for production
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
    """Create a new PostgreSQL database engine based on settings.

    Returns an EngineResult containing the engine and optional GCP connector.
    Call result.dispose() on shutdown to properly clean up resources.
    """
    if settings is None:
        settings = get_config().service

    if settings.gcp_db_instance:
        return await _create_gcp_engine(settings)

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
