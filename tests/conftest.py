"""Pytest fixtures for automation service tests."""

import logging
import os
from collections.abc import AsyncGenerator

import httpx
import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer


# Disable JSON logging before importing automation modules to ensure log propagation
# works correctly with pytest's caplog fixture
os.environ["LOG_JSON"] = "0"

from openhands.automation.app import app  # noqa: E402
from openhands.automation.auth import (  # noqa: E402
    AuthenticatedUser,
    AuthMethod,
    authenticate_request,
    create_http_client,
)
from openhands.automation.config import Settings  # noqa: E402
from openhands.automation.db import get_session  # noqa: E402
from openhands.automation.models import Base  # noqa: E402


@pytest.fixture(autouse=True)
def ensure_log_propagation():
    """Ensure automation loggers propagate to root for caplog capture."""
    loggers_to_fix = [
        "openhands.automation",
        "openhands.automation.scheduler",
        "openhands.automation.dispatcher",
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
        permissions=["view_org_settings", "view_automations", "manage_automations"],
        auth_method=AuthMethod.API_KEY,
        api_key="test-api-key",
    )


@pytest.fixture
def mock_readonly_user():
    """Return a mock authenticated user without manage_automations permission."""
    import uuid

    return AuthenticatedUser(
        user_id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        org_id=uuid.UUID("87654321-4321-8765-4321-876543218765"),
        email="test@example.com",
        role="member",
        permissions=["view_org_settings", "view_automations"],
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


class AgentProfilesApi:
    """The OpenHands API's agent profile list, as a cloud-mode test sees it."""

    def __init__(self) -> None:
        self.profile_id = "0a1b2c3d-0000-4000-8000-000000000001"
        # A request has to carry a credential of its own: the profile is looked
        # up with the caller's credential, which mocked authentication does not
        # set.
        self.caller_auth = {"Authorization": "Bearer caller-key"}
        # What the API answers the lookup with; a test replaces it to misbehave.
        self.response = httpx.Response(
            200, json={"profiles": [{"id": self.profile_id}]}
        )
        self.raise_timeout = False
        self.requests: list[httpx.Request] = []

    def respond(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET" or request.url.path != "/api/agent-profiles":
            return httpx.Response(404)
        self.requests.append(request)
        if self.raise_timeout:
            raise httpx.ReadTimeout("simulated profile lookup timeout", request=request)
        return self.response


@pytest.fixture
async def agent_profiles_api(
    async_client, monkeypatch
) -> AsyncGenerator[AgentProfilesApi, None]:
    """Cloud mode, with the OpenHands API serving one agent profile."""
    from openhands.automation.config import clear_config_cache
    from openhands.automation.utils.model_profiles import clear_agent_profile_cache

    monkeypatch.delenv("AUTOMATION_AGENT_SERVER_URL", raising=False)
    clear_config_cache()
    clear_agent_profile_cache()
    api = AgentProfilesApi()
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(api.respond)
    )
    yield api
    clear_config_cache()
    clear_agent_profile_cache()


@pytest.fixture
async def readonly_client(
    async_engine, async_session_factory, async_session, mock_readonly_user
) -> AsyncGenerator[AsyncClient, None]:
    """Create an async test client with a user lacking manage_automations permission."""

    async def override_get_session():
        yield async_session

    async def override_authenticate():
        return mock_readonly_user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[authenticate_request] = override_authenticate

    app.state.engine = async_engine
    app.state.session_factory = async_session_factory
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
