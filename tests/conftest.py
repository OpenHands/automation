"""Pytest fixtures for automation service tests."""

import logging
import os
from collections.abc import AsyncGenerator

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer


# Disable JSON logging before importing automation modules to ensure log propagation
# works correctly with pytest's caplog fixture
os.environ["LOG_JSON"] = "0"

from automation.app import app  # noqa: E402
from automation.auth import (  # noqa: E402
    AuthenticatedUser,
    AuthMethod,
    authenticate_request,
    create_http_client,
)
from automation.config import Settings  # noqa: E402
from automation.db import get_session  # noqa: E402
from automation.models import Base  # noqa: E402


@pytest.fixture(autouse=True)
def ensure_log_propagation():
    """Ensure automation loggers propagate to root for caplog capture."""
    loggers_to_fix = [
        "automation",
        "automation.scheduler",
        "automation.dispatcher",
    ]
    original_propagate = {}
    for name in loggers_to_fix:
        logger = logging.getLogger(name)
        original_propagate[name] = logger.propagate
        logger.propagate = True

    yield

    # Restore original propagation settings
    for name, propagate in original_propagate.items():
        logging.getLogger(name).propagate = propagate


@pytest.fixture(scope="session")
def postgres_container():
    """Start a PostgreSQL container for the test session."""
    with PostgresContainer("postgres:15") as postgres:
        yield postgres


@pytest.fixture
async def async_engine(postgres_container):
    """Create an async PostgreSQL engine for testing."""
    # Convert sync URL to async URL
    sync_url = postgres_container.get_connection_url()
    async_url = sync_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")

    engine = create_async_engine(async_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    # Clean up tables after each test
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def async_session_factory(async_engine):
    """Create an async session factory for testing."""
    return async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


@pytest.fixture
async def async_session(async_session_factory) -> AsyncGenerator[AsyncSession, None]:
    """Create an async session for testing."""
    async with async_session_factory() as session:
        yield session


@pytest.fixture
def mock_authenticated_user():
    """Return a mock authenticated user."""
    import uuid

    return AuthenticatedUser(
        user_id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        org_id=uuid.UUID("87654321-4321-8765-4321-876543218765"),
        email="test@example.com",
        role="owner",
        permissions=["view_org_settings"],
        auth_method=AuthMethod.API_KEY,
        api_key="test-api-key",
    )


@pytest.fixture
async def async_client(
    async_engine, async_session_factory, async_session, mock_authenticated_user
) -> AsyncGenerator[AsyncClient, None]:
    """Create an async test client with mocked auth and DB session."""

    async def override_get_session():
        yield async_session

    async def override_authenticate():
        return mock_authenticated_user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[authenticate_request] = override_authenticate

    # Set app.state for endpoints that access it directly (e.g., /ready)
    app.state.engine = async_engine
    app.state.session_factory = async_session_factory
    # Create a mock http_client for tests (auth is overridden, but state must exist)
    app.state.http_client = create_http_client()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client

    await app.state.http_client.aclose()
    app.dependency_overrides.clear()


@pytest.fixture
def sync_client(async_engine, async_session_factory):
    """Create a sync test client for simple endpoint tests."""
    import asyncio

    app.state.engine = async_engine
    app.state.session_factory = async_session_factory
    http_client = create_http_client()
    app.state.http_client = http_client
    yield TestClient(app)
    # Cleanup http_client to prevent resource leak
    asyncio.get_event_loop().run_until_complete(http_client.aclose())


@pytest.fixture
def mock_settings():
    """Return a mock Settings instance for dispatcher tests."""
    return Settings(
        openhands_api_base_url="https://test.example.com",
        service_key="test-service-key",
        base_url="http://localhost:8000",
    )
