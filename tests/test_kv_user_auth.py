"""Tests for user-authenticated KV store access.

These tests verify that KV endpoints accept user authentication (API key /
X-Session-API-Key) in addition to the existing per-run KV JWT tokens.  User
auth requires an ``automation_id`` query parameter and checks org membership,
``view_automations`` for reads and the right to modify the automation for
writes.

Uses SQLite (no Docker required) and overrides ``authenticate_request`` to
mock user identity.
"""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from openhands.automation.app import app
from openhands.automation.auth import (
    AuthenticatedUser,
    AuthMethod,
    get_http_client,
)
from openhands.automation.config import clear_config_cache, get_config
from openhands.automation.db import get_session, set_sqlite_mode
from openhands.automation.kv_router import _try_resolve_user
from openhands.automation.models import (
    Automation,
    AutomationKV,
    AutomationRunAsKind,
    Base,
)
from openhands.automation.utils.kv import (
    create_kv_token,
    decrypt_value,
    encrypt_value,
)


TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
OTHER_ORG_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
TEST_AUTOMATION_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER_AUTOMATION_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
TEST_RUN_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
TEAMMATE_USER_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
TEAMMATE_AUTOMATION_ID = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
TEST_KV_SECRET = "test-kv-secret-key-for-testing-only"

# Base path prefix for all KV endpoints (matches app configuration)
_BASE = get_config().service.base_path  # e.g. "/api/automation"
_KV = f"{_BASE}/v1/kv"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_user():
    return AuthenticatedUser(
        user_id=TEST_USER_ID,
        org_id=TEST_ORG_ID,
        email="test@example.com",
        role="member",
        permissions=["view_org_settings", "view_automations"],
        auth_method=AuthMethod.API_KEY,
        api_key="test-api-key",
    )


@pytest.fixture
def mock_admin():
    return AuthenticatedUser(
        user_id=TEST_USER_ID,
        org_id=TEST_ORG_ID,
        email="test@example.com",
        role="admin",
        permissions=["view_org_settings", "view_automations", "manage_automations"],
        auth_method=AuthMethod.API_KEY,
        api_key="test-api-key",
    )


@pytest.fixture
async def sqlite_engine(monkeypatch):
    monkeypatch.setenv("AUTOMATION_KV_SECRET", TEST_KV_SECRET)
    monkeypatch.setenv("AUTOMATION_DB_URL", "sqlite+aiosqlite:///:memory:")
    clear_config_cache()

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    set_sqlite_mode(True)
    yield engine
    await engine.dispose()
    set_sqlite_mode(False)
    clear_config_cache()


@pytest.fixture
async def sqlite_session(sqlite_engine):
    factory = async_sessionmaker(
        sqlite_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        yield session


@pytest.fixture
async def kv_user_client(sqlite_engine, sqlite_session, mock_user, monkeypatch):
    """Client with user auth (member role) and KV secret configured."""

    factory = async_sessionmaker(
        sqlite_engine, class_=AsyncSession, expire_on_commit=False
    )

    async def override_get_session():
        async with factory() as s:
            yield s

    async def override_try_user():
        return mock_user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[_try_resolve_user] = override_try_user

    app.state.engine = sqlite_engine
    app.state.session_factory = factory
    app.state.http_client = None

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client

    app.dependency_overrides.clear()
    clear_config_cache()


@pytest.fixture
async def kv_admin_client(sqlite_engine, sqlite_session, mock_admin, monkeypatch):
    """Client with admin user auth."""

    factory = async_sessionmaker(
        sqlite_engine, class_=AsyncSession, expire_on_commit=False
    )

    async def override_get_session():
        async with factory() as s:
            yield s

    async def override_try_user():
        return mock_admin

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[_try_resolve_user] = override_try_user

    app.state.engine = sqlite_engine
    app.state.session_factory = factory
    app.state.http_client = None

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client

    app.dependency_overrides.clear()
    clear_config_cache()


@pytest.fixture
async def kv_token_client(sqlite_engine, sqlite_session, monkeypatch):
    """Client with KV token auth (no user auth needed).

    Uses real get_kv_auth_context with real _try_resolve_user (which will
    return None since no valid user credential is provided).  KV token
    path skips user auth entirely.  Overrides get_session and
    get_http_client (the latter is needed by _try_resolve_user).
    """

    factory = async_sessionmaker(
        sqlite_engine, class_=AsyncSession, expire_on_commit=False
    )

    async def override_get_session():
        async with factory() as s:
            yield s

    # get_http_client is a dependency of _try_resolve_user.  On the KV token
    # path, _try_resolve_user is still resolved by FastAPI but its result
    # (None) is ignored.  Provide a dummy client so it doesn't 503.
    async def override_http_client():
        return None

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_http_client] = override_http_client

    app.state.engine = sqlite_engine
    app.state.session_factory = factory
    app.state.http_client = None

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client

    app.dependency_overrides.clear()
    clear_config_cache()


@pytest.fixture
async def test_automation(sqlite_session):
    """Create a test automation owned by TEST_USER_ID / TEST_ORG_ID."""
    automation = Automation(
        id=TEST_AUTOMATION_ID,
        user_id=TEST_USER_ID,
        org_id=TEST_ORG_ID,
        name="Test Automation",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="s3://bucket/code.tar.gz",
        entrypoint="uv run script.py",
    )
    sqlite_session.add(automation)

    # Also create an automation in a different org
    other = Automation(
        id=OTHER_AUTOMATION_ID,
        user_id=uuid.uuid4(),
        org_id=OTHER_ORG_ID,
        name="Other Org Automation",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="s3://bucket/code.tar.gz",
        entrypoint="uv run script.py",
    )
    sqlite_session.add(other)

    # And one in the same org that runs as another member
    teammate = Automation(
        id=TEAMMATE_AUTOMATION_ID,
        user_id=TEAMMATE_USER_ID,
        org_id=TEST_ORG_ID,
        name="Teammate Automation",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="s3://bucket/code.tar.gz",
        entrypoint="uv run script.py",
    )
    sqlite_session.add(teammate)
    await sqlite_session.commit()
    return automation


def seed_keys(
    session: AsyncSession,
    mapping: dict,
    automation_id: uuid.UUID = TEST_AUTOMATION_ID,
) -> None:
    """Seed per-key rows for an automation (TEST_AUTOMATION_ID by default)."""
    for key, value in mapping.items():
        session.add(
            AutomationKV(
                automation_id=automation_id,
                key=key,
                value_encrypted=encrypt_value(TEST_KV_SECRET, value),
            )
        )


@pytest.fixture
def kv_token():
    """A valid KV JWT token for TEST_AUTOMATION_ID."""
    return create_kv_token(
        secret=TEST_KV_SECRET,
        automation_id=TEST_AUTOMATION_ID,
        run_id=TEST_RUN_ID,
    )


# ---------------------------------------------------------------------------
# User auth: basic CRUD
# ---------------------------------------------------------------------------


class TestUserAuthBasic:
    """Test that user auth works for basic KV operations."""

    async def test_list_keys_with_user_auth(
        self, kv_user_client, test_automation, sqlite_session
    ):
        """User can list keys with API key + automation_id query param."""
        # Seed some state
        seed_keys(sqlite_session, {"key1": "val1", "key2": "val2"})
        await sqlite_session.commit()

        resp = await kv_user_client.get(
            _KV,
            params={"automation_id": str(TEST_AUTOMATION_ID)},
            headers={"Authorization": "Bearer test-api-key"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["keys"]) == {"key1", "key2"}
        assert data["count"] == 2

    async def test_get_value_with_user_auth(
        self, kv_user_client, test_automation, sqlite_session
    ):
        """User can get a value with user auth."""
        seed_keys(sqlite_session, {"mykey": {"nested": 42}})
        await sqlite_session.commit()

        resp = await kv_user_client.get(
            f"{_KV}/mykey",
            params={"automation_id": str(TEST_AUTOMATION_ID)},
            headers={"Authorization": "Bearer test-api-key"},
        )
        assert resp.status_code == 200
        assert resp.json()["value"] == {"nested": 42}

    async def test_set_value_with_user_auth(self, kv_user_client, test_automation):
        """User can set a value with user auth."""
        resp = await kv_user_client.put(
            f"{_KV}/newkey",
            params={"automation_id": str(TEST_AUTOMATION_ID)},
            headers={"Authorization": "Bearer test-api-key"},
            json="hello world",
        )
        assert resp.status_code == 201
        assert resp.json()["key"] == "newkey"
        assert resp.json()["value"] == "hello world"

    async def test_delete_value_with_user_auth(
        self, kv_user_client, test_automation, sqlite_session
    ):
        """User can delete a value with user auth."""
        seed_keys(sqlite_session, {"todelete": "val"})
        await sqlite_session.commit()

        resp = await kv_user_client.delete(
            f"{_KV}/todelete",
            params={"automation_id": str(TEST_AUTOMATION_ID)},
            headers={"Authorization": "Bearer test-api-key"},
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True


# ---------------------------------------------------------------------------
# User auth: error cases
# ---------------------------------------------------------------------------


class TestUserAuthErrors:
    """Test error handling for user-authenticated KV access."""

    async def test_missing_automation_id_query_param(
        self, kv_user_client, test_automation
    ):
        """User auth without automation_id query param returns 400."""
        resp = await kv_user_client.get(
            _KV,
            headers={"Authorization": "Bearer test-api-key"},
        )
        assert resp.status_code == 400
        assert "automation_id" in resp.json()["detail"]

    async def test_wrong_org_automation(self, kv_user_client, test_automation):
        """User cannot access KV for an automation in another org."""
        resp = await kv_user_client.get(
            _KV,
            params={"automation_id": str(OTHER_AUTOMATION_ID)},
            headers={"Authorization": "Bearer test-api-key"},
        )
        # Should be 404 (don't leak existence)
        assert resp.status_code == 404

    async def test_nonexistent_automation(self, kv_user_client, test_automation):
        """User cannot access KV for a nonexistent automation."""
        random_id = uuid.uuid4()
        resp = await kv_user_client.get(
            _KV,
            params={"automation_id": str(random_id)},
            headers={"Authorization": "Bearer test-api-key"},
        )
        assert resp.status_code == 404

    async def test_x_session_api_key_header_works(
        self, kv_user_client, test_automation, sqlite_session
    ):
        """X-Session-API-Key header also works for user auth."""
        seed_keys(sqlite_session, {"k": "v"})
        await sqlite_session.commit()

        resp = await kv_user_client.get(
            _KV,
            params={"automation_id": str(TEST_AUTOMATION_ID)},
            headers={"X-Session-API-Key": "test-api-key"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# User auth: permissions
# ---------------------------------------------------------------------------


class TestUserAuthPermissions:
    """Reads need view_automations; writes need the right to modify the automation."""

    async def test_member_can_read_a_teammates_automation(
        self, kv_user_client, test_automation, sqlite_session
    ):
        """A member reads the KV data of an automation that runs as someone else."""
        seed_keys(sqlite_session, {"cursor": 42}, TEAMMATE_AUTOMATION_ID)
        await sqlite_session.commit()

        resp = await kv_user_client.get(
            f"{_KV}/cursor",
            params={"automation_id": str(TEAMMATE_AUTOMATION_ID)},
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert resp.status_code == 200
        assert resp.json()["value"] == 42

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("PUT", "/count", 2),
            ("PATCH", "/config", {"path": "a", "value": 2}),
            ("DELETE", "/count", None),
            ("POST", "/count/incr", None),
            ("POST", "/count/decr", None),
            ("POST", "/items/lpush", {"value": "b"}),
            ("POST", "/items/rpush", {"value": "b"}),
            ("POST", "/items/lpop", None),
            ("POST", "/items/rpop", None),
            (
                "POST",
                "/batch",
                {"operations": [{"op": "set", "key": "count", "value": 2}]},
            ),
        ],
    )
    async def test_member_cannot_change_a_teammates_automation(
        self, kv_user_client, test_automation, sqlite_session, method, path, body
    ):
        """Every write route refuses a member who may not modify the automation."""
        seeded = {"count": 1, "items": ["a"], "config": {"a": 1}}
        seed_keys(sqlite_session, seeded, TEAMMATE_AUTOMATION_ID)
        await sqlite_session.commit()

        resp = await kv_user_client.request(
            method,
            f"{_KV}{path}",
            params={"automation_id": str(TEAMMATE_AUTOMATION_ID)},
            headers={"Authorization": "Bearer test-api-key"},
            json=body,
        )

        assert resp.status_code == 403
        stored = await sqlite_session.execute(
            select(AutomationKV.key, AutomationKV.value_encrypted).where(
                AutomationKV.automation_id == TEAMMATE_AUTOMATION_ID
            )
        )
        assert {
            key: decrypt_value(TEST_KV_SECRET, value) for key, value in stored
        } == seeded

    async def test_admin_can_write_a_teammates_automation(
        self, kv_admin_client, test_automation
    ):
        """A holder of manage_automations writes any automation's KV in the org."""
        resp = await kv_admin_client.put(
            f"{_KV}/cursor",
            params={"automation_id": str(TEAMMATE_AUTOMATION_ID)},
            headers={"Authorization": "Bearer test-api-key"},
            json=43,
        )

        assert resp.status_code == 201

    async def test_caller_without_view_permission_cannot_read(
        self, kv_user_client, mock_user, test_automation
    ):
        """Without view_automations even the caller's own automation is refused."""
        mock_user.permissions = ["view_org_settings"]

        resp = await kv_user_client.get(
            _KV,
            params={"automation_id": str(TEST_AUTOMATION_ID)},
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert resp.status_code == 403

    async def test_service_account_cannot_write_the_automation_it_runs(
        self, kv_user_client, mock_user, test_automation, sqlite_session
    ):
        """A service account's key gets no write; its runs use the KV token."""
        mock_user.role = "service_account"
        test_automation.run_as_kind = AutomationRunAsKind.SERVICE
        await sqlite_session.commit()

        resp = await kv_user_client.put(
            f"{_KV}/cursor",
            params={"automation_id": str(TEST_AUTOMATION_ID)},
            headers={"Authorization": "Bearer test-api-key"},
            json=1,
        )

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# KV token auth still works
# ---------------------------------------------------------------------------


class TestKVTokenAuthStillWorks:
    """Verify that existing KV JWT token auth is not broken."""

    async def test_list_keys_with_kv_token(
        self, kv_token_client, test_automation, sqlite_session, kv_token
    ):
        """KV JWT token auth still works without automation_id query param."""
        seed_keys(sqlite_session, {"tk": "tv"})
        await sqlite_session.commit()

        resp = await kv_token_client.get(
            _KV,
            headers={"Authorization": f"Bearer {kv_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["keys"] == ["tk"]

    async def test_set_value_with_kv_token(
        self, kv_token_client, test_automation, kv_token
    ):
        """KV JWT token auth can set values."""
        resp = await kv_token_client.put(
            f"{_KV}/fromtoken",
            headers={"Authorization": f"Bearer {kv_token}"},
            json={"data": 123},
        )
        assert resp.status_code == 201

    async def test_kv_token_ignores_automation_id_param(
        self, kv_token_client, test_automation, sqlite_session, kv_token
    ):
        """KV JWT token auth ignores automation_id query param (uses token's claim)."""
        seed_keys(sqlite_session, {"real": "data"})
        await sqlite_session.commit()

        # Pass a different automation_id in query — should be ignored
        resp = await kv_token_client.get(
            _KV,
            params={"automation_id": str(OTHER_AUTOMATION_ID)},
            headers={"Authorization": f"Bearer {kv_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["keys"] == ["real"]


class TestKVTokenUnknownAutomation:
    """A validly signed token whose automation has no row is refused with 404.

    Matches user auth for an unknown ``automation_id``; before, reads came back
    empty and the first write failed the ``automation_kv_meta`` foreign key.
    """

    @pytest.fixture
    def ghost_token(self):
        return create_kv_token(
            secret=TEST_KV_SECRET,
            automation_id=uuid.UUID("00000000-0000-4000-8000-00000000dead"),
            run_id=TEST_RUN_ID,
        )

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("GET", "", None),
            ("GET", "/ghost", None),
            ("PUT", "/ghost", 1),
            ("PATCH", "/ghost", {"path": "a", "value": 1}),
            ("DELETE", "/ghost", None),
            ("POST", "/ghost/incr", None),
            ("POST", "/ghost/rpush", {"value": 1}),
            ("GET", "/ghost/len", None),
            ("POST", "/batch", {"operations": [{"op": "set", "key": "g", "value": 1}]}),
        ],
    )
    async def test_every_route_answers_404(
        self,
        kv_token_client,
        test_automation,
        sqlite_session,
        ghost_token,
        method,
        path,
        body,
    ):
        resp = await kv_token_client.request(
            method,
            f"{_KV}{path}",
            headers={"Authorization": f"Bearer {ghost_token}"},
            json=body,
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Automation not found"
        count = await sqlite_session.execute(
            select(func.count()).select_from(AutomationKV)
        )
        assert count.scalar_one() == 0

    async def test_soft_deleted_automation_token_still_works(
        self, kv_token_client, test_automation, sqlite_session, kv_token
    ):
        """Soft delete keeps the row, so the token works as user auth does."""
        test_automation.deleted_at = datetime.now(UTC)
        seed_keys(sqlite_session, {"k": 1})
        await sqlite_session.commit()

        headers = {"Authorization": f"Bearer {kv_token}"}
        resp = await kv_token_client.get(f"{_KV}/k", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["value"] == 1
        resp = await kv_token_client.put(f"{_KV}/k2", headers=headers, json=2)
        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Auth method detection
# ---------------------------------------------------------------------------


class TestAuthMethodDetection:
    """Test that the JWT-vs-API-key detection works correctly."""

    async def test_api_key_not_treated_as_jwt(self, kv_user_client, test_automation):
        """A non-JWT Bearer token (API key) falls through to user auth."""
        # API keys don't have dots, so they shouldn't be mistaken for JWTs
        resp = await kv_user_client.get(
            _KV,
            params={"automation_id": str(TEST_AUTOMATION_ID)},
            headers={"Authorization": "Bearer test-api-key"},
        )
        assert resp.status_code == 200

    async def test_invalid_jwt_returns_401(self, kv_token_client, test_automation):
        """A string that looks like a JWT but fails verification returns 401."""
        fake_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJ0ZXN0IjoidHJ1ZSJ9.invalid-signature"
        resp = await kv_token_client.get(
            _KV,
            headers={"Authorization": f"Bearer {fake_jwt}"},
        )
        assert resp.status_code == 401

    async def test_no_auth_returns_401(self, kv_token_client, test_automation):
        """No Authorization header at all returns 401.

        Without a KV token or user credential, authenticate_request
        raises 401.
        """
        resp = await kv_token_client.get(_KV)
        assert resp.status_code == 401
