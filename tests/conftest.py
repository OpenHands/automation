"""Common test fixtures and utilities."""

from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from automation import config as config_module, db as db_module
from automation.config import Settings
from automation.models import Base


REPO_ROOT = Path(__file__).resolve().parent.parent

# Generate a deterministic test encryption key
TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset module-level singletons between tests."""
    config_module._settings = None
    db_module._engine = None
    db_module._session_factory = None
    # Reset encryption cache
    from automation import encryption as enc_module

    enc_module._fernet = None
    yield
    config_module._settings = None
    db_module._engine = None
    db_module._session_factory = None
    enc_module._fernet = None


@pytest.fixture
def settings():
    """Test settings using SQLite."""
    s = Settings(
        sqlite_path="",  # in-memory-ish, but we use a temp file
        encryption_key=TEST_ENCRYPTION_KEY,
        openhands_api_base_url="https://mock-openhands.test",
        scheduler_interval_seconds=9999,  # Don't run scheduler in tests
    )
    config_module._settings = s
    return s


@pytest_asyncio.fixture
async def db_engine(settings):
    """Create an in-memory SQLite engine for tests.

    Uses StaticPool so all connections share the same in-memory database.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Inject into db module
    db_module._engine = engine
    db_module._session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def client(db_engine, settings):
    """Async test client with mocked auth (default user)."""
    with (
        patch("automation.app.start_scheduler"),
        patch("automation.app.stop_scheduler"),
    ):
        from automation.app import app
        from automation.auth import AuthenticatedUser, authenticate_request

        # Default mock user for all requests
        default_user = AuthenticatedUser(
            user_id="test-user-123", api_key="sk-oh-testkey123"
        )

        async def _mock_auth(request: Request = None):
            return default_user

        app.dependency_overrides[authenticate_request] = _mock_auth

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def app_instance(db_engine, settings):
    """Return the app instance for tests needing custom auth.

    Depends on db_engine to ensure tables are created.
    """
    with (
        patch("automation.app.start_scheduler"),
        patch("automation.app.stop_scheduler"),
    ):
        from automation.app import app

        yield app
        app.dependency_overrides.clear()


def mock_auth_user(app, user_id: str = "test-user-123"):
    """Set auth override on the app for a specific user."""
    from automation.auth import AuthenticatedUser, authenticate_request

    user = AuthenticatedUser(user_id=user_id, api_key="sk-oh-testkey123")

    async def _mock_auth(request=None):
        return user

    app.dependency_overrides[authenticate_request] = _mock_auth
