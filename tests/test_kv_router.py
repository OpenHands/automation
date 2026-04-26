"""Tests for KV store API endpoints.

Testing Strategy
================

This module uses two different test client fixtures depending on the test type:

1. `kv_client` - For most tests (single-request tests)
   - Overrides `get_session` to use a SHARED async_session
   - All requests go through the same database session/connection
   - Simpler setup, good for testing individual endpoint behavior
   - ⚠️ NOT suitable for concurrent request tests (causes deadlocks)

2. `concurrent_kv_client` - For concurrency tests ONLY
   - Does NOT override `get_session`
   - Each request gets its own session from the session factory
   - Enables true concurrent database operations with separate connections
   - Required for testing FOR UPDATE locking behavior

Single-Document Backend
-----------------------
The KV store uses a single-document backend where each automation has exactly
ONE row containing all its state as an encrypted JSON document. API "keys"
are top-level fields in that document.

This design eliminates deadlock risk: all operations serialize through a single
row lock per automation.
"""

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from automation.app import app
from automation.db import get_session
from automation.kv_router import get_automation_id_from_token
from automation.models import Automation, AutomationKV
from automation.utils.kv import decrypt_value, encrypt_value


# Test UUIDs
TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
TEST_AUTOMATION_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TEST_RUN_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

# Test secret for JWT and encryption
TEST_KV_SECRET = "test-kv-secret-key-for-testing-only"


# =============================================================================
# Test Data Helpers (Single-Document Backend)
# =============================================================================


async def create_test_state(
    session: AsyncSession,
    automation_id: uuid.UUID,
    state: dict[str, Any],
    secret: str = TEST_KV_SECRET,
) -> AutomationKV:
    """Create a state row for an automation with the given state dict.

    In the single-document model, each automation has ONE row containing
    all keys as top-level fields in the encrypted JSON document.
    """
    encrypted = encrypt_value(secret, state)
    row = AutomationKV(
        automation_id=automation_id,
        state_encrypted=encrypted,
    )
    session.add(row)
    await session.flush()
    return row


async def get_test_state(
    session: AsyncSession,
    automation_id: uuid.UUID,
    secret: str = TEST_KV_SECRET,
) -> dict[str, Any] | None:
    """Get the current state dict for an automation.

    Returns None if no state row exists.
    """
    result = await session.execute(
        select(AutomationKV).where(AutomationKV.automation_id == automation_id)
    )
    row = result.scalars().first()
    if row is None:
        return None
    return decrypt_value(secret, row.state_encrypted)


# =============================================================================
# Test Client Fixtures
# =============================================================================


@pytest.fixture
async def kv_client(async_engine, async_session_factory, async_session, monkeypatch):
    """Create an async test client with KV token auth (shared session)."""
    monkeypatch.setenv("AUTOMATION_KV_SECRET", TEST_KV_SECRET)

    from automation.config import clear_config_cache

    clear_config_cache()

    async def override_get_session():
        yield async_session

    async def override_get_automation_id():
        return TEST_AUTOMATION_ID

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_automation_id_from_token] = override_get_automation_id

    app.state.engine = async_engine
    app.state.session_factory = async_session_factory

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client

    app.dependency_overrides.clear()
    clear_config_cache()


@pytest.fixture(autouse=True)
async def automation_with_kv(async_session):
    """Create a test automation with KV store enabled."""
    automation = Automation(
        id=TEST_AUTOMATION_ID,
        user_id=TEST_USER_ID,
        org_id=TEST_ORG_ID,
        name="Test Automation with KV",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="s3://bucket/code.tar.gz",
        entrypoint="uv run script.py",
        enable_kv_store=True,
    )
    async_session.add(automation)
    await async_session.commit()
    return automation


# =============================================================================
# Token and Encryption Tests
# =============================================================================


class TestKVTokenAuth:
    """Tests for KV token authentication."""

    def test_create_and_verify_token(self):
        """Token can be created and verified."""
        from automation.utils.kv import create_kv_token, verify_kv_token

        token = create_kv_token(
            secret=TEST_KV_SECRET,
            automation_id=TEST_AUTOMATION_ID,
            run_id=TEST_RUN_ID,
        )

        result = verify_kv_token(TEST_KV_SECRET, token)
        assert result == TEST_AUTOMATION_ID

    def test_invalid_token_raises_error(self):
        """Invalid token raises KVTokenError."""
        from automation.utils.kv import KVTokenError, verify_kv_token

        with pytest.raises(KVTokenError):
            verify_kv_token(TEST_KV_SECRET, "invalid-token")


class TestKVEncryption:
    """Tests for KV value encryption."""

    def test_encrypt_decrypt_dict(self):
        """Dict values can be encrypted and decrypted."""
        original = {"key": "value", "nested": {"a": 1}}
        encrypted = encrypt_value(TEST_KV_SECRET, original)
        decrypted = decrypt_value(TEST_KV_SECRET, encrypted)
        assert decrypted == original

    def test_encrypt_decrypt_list(self):
        """List values can be encrypted and decrypted."""
        original = [1, 2, {"key": "value"}]
        encrypted = encrypt_value(TEST_KV_SECRET, original)
        decrypted = decrypt_value(TEST_KV_SECRET, encrypted)
        assert decrypted == original


# =============================================================================
# API Endpoint Tests
# =============================================================================


class TestListKeys:
    """Tests for GET /kv endpoint."""

    async def test_list_keys_empty(self, kv_client):
        """List keys returns empty when no keys exist."""
        response = await kv_client.get("/api/automation/v1/kv")

        assert response.status_code == 200
        data = response.json()
        assert data["keys"] == []
        assert data["count"] == 0

    async def test_list_keys_with_data(self, kv_client, async_session):
        """List keys returns all keys for the automation."""
        await create_test_state(
            async_session,
            TEST_AUTOMATION_ID,
            {"config": {"test": True}, "counter": 42, "queue": []},
        )
        await async_session.commit()

        response = await kv_client.get("/api/automation/v1/kv")

        assert response.status_code == 200
        data = response.json()
        assert set(data["keys"]) == {"config", "counter", "queue"}
        assert data["count"] == 3


class TestGetValue:
    """Tests for GET /kv/{key} endpoint."""

    async def test_get_value_not_found(self, kv_client):
        """Get non-existent key returns 404."""
        response = await kv_client.get("/api/automation/v1/kv/nonexistent")

        assert response.status_code == 404
        assert response.json()["detail"] == "key_not_found"

    async def test_get_value_success(self, kv_client, async_session):
        """Get existing key returns value."""
        value = {"database": {"host": "localhost", "port": 5432}}
        await create_test_state(async_session, TEST_AUTOMATION_ID, {"config": value})
        await async_session.commit()

        response = await kv_client.get("/api/automation/v1/kv/config")

        assert response.status_code == 200
        data = response.json()
        assert data["key"] == "config"
        assert data["value"] == value

    async def test_get_value_with_path(self, kv_client, async_session):
        """Get nested path returns specific value."""
        value = {"database": {"host": "localhost", "port": 5432}}
        await create_test_state(async_session, TEST_AUTOMATION_ID, {"config": value})
        await async_session.commit()

        response = await kv_client.get(
            "/api/automation/v1/kv/config?path=database.host"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["key"] == "config"
        assert data["path"] == "database.host"
        assert data["value"] == "localhost"

    async def test_get_value_with_meta(self, kv_client, async_session):
        """Get with meta=true returns timestamps."""
        await create_test_state(async_session, TEST_AUTOMATION_ID, {"config": "test"})
        await async_session.commit()

        response = await kv_client.get("/api/automation/v1/kv/config?meta=true")

        assert response.status_code == 200
        data = response.json()
        assert "created_at" in data
        assert "updated_at" in data


class TestSetValue:
    """Tests for PUT /kv/{key} endpoint."""

    async def test_set_new_value(self, kv_client):
        """Set creates new key (returns 201 Created)."""
        response = await kv_client.put(
            "/api/automation/v1/kv/config",
            json={"setting": "value"},
        )

        assert response.status_code == 201
        data = response.json()
        assert data["key"] == "config"
        assert data["value"] == {"setting": "value"}
        assert data["created"] is True

    async def test_set_updates_existing_value(self, kv_client, async_session):
        """Set updates existing key (returns 200 OK)."""
        await create_test_state(async_session, TEST_AUTOMATION_ID, {"config": "old"})
        await async_session.commit()

        response = await kv_client.put(
            "/api/automation/v1/kv/config",
            json="new",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["value"] == "new"
        assert data["created"] is False

    async def test_set_with_nx_creates_new(self, kv_client):
        """Set with nx=true creates new key."""
        response = await kv_client.put(
            "/api/automation/v1/kv/lock?nx=true",
            json={"owner": "run-123"},
        )

        assert response.status_code == 201
        assert response.json()["created"] is True

    async def test_set_with_nx_fails_if_exists(self, kv_client, async_session):
        """Set with nx=true fails if key exists."""
        await create_test_state(
            async_session, TEST_AUTOMATION_ID, {"lock": {"owner": "other"}}
        )
        await async_session.commit()

        response = await kv_client.put(
            "/api/automation/v1/kv/lock?nx=true",
            json={"owner": "run-123"},
        )

        assert response.status_code == 409
        assert response.json()["error"] == "key_exists"

    async def test_set_with_xx_updates_existing(self, kv_client, async_session):
        """Set with xx=true updates existing key."""
        await create_test_state(async_session, TEST_AUTOMATION_ID, {"config": "old"})
        await async_session.commit()

        response = await kv_client.put(
            "/api/automation/v1/kv/config?xx=true",
            json="new",
        )

        assert response.status_code == 200
        assert response.json()["value"] == "new"

    async def test_set_with_xx_fails_if_not_exists(self, kv_client):
        """Set with xx=true fails if key doesn't exist."""
        response = await kv_client.put(
            "/api/automation/v1/kv/nonexistent?xx=true",
            json="value",
        )

        assert response.status_code == 409


class TestPatchValue:
    """Tests for PATCH /kv/{key} endpoint."""

    async def test_patch_updates_nested_path(self, kv_client, async_session):
        """Patch updates a nested path."""
        await create_test_state(
            async_session,
            TEST_AUTOMATION_ID,
            {"config": {"database": {"host": "old", "port": 5432}}},
        )
        await async_session.commit()

        response = await kv_client.patch(
            "/api/automation/v1/kv/config",
            json={"path": "database.host", "value": "new"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["path"] == "database.host"
        assert data["value"] == "new"

    async def test_patch_not_found(self, kv_client):
        """Patch returns 404 for non-existent key."""
        response = await kv_client.patch(
            "/api/automation/v1/kv/nonexistent",
            json={"path": "some.path", "value": "value"},
        )

        assert response.status_code == 404


class TestDeleteKey:
    """Tests for DELETE /kv/{key} endpoint."""

    async def test_delete_existing_key(self, kv_client, async_session):
        """Delete removes existing key."""
        await create_test_state(async_session, TEST_AUTOMATION_ID, {"config": "test"})
        await async_session.commit()

        response = await kv_client.delete("/api/automation/v1/kv/config")

        assert response.status_code == 200
        assert response.json()["deleted"] is True

    async def test_delete_nonexistent_key(self, kv_client):
        """Delete returns deleted=false for non-existent key."""
        response = await kv_client.delete("/api/automation/v1/kv/nonexistent")

        assert response.status_code == 200
        assert response.json()["deleted"] is False


class TestIncrement:
    """Tests for POST /kv/{key}/incr endpoint."""

    async def test_incr_creates_key(self, kv_client, async_session):
        """Incr creates key with value if it doesn't exist."""
        response = await kv_client.post("/api/automation/v1/kv/counter/incr")

        assert response.status_code == 200
        assert response.json()["value"] == 1

    async def test_incr_increments_existing(self, kv_client, async_session):
        """Incr increments existing integer value."""
        await create_test_state(async_session, TEST_AUTOMATION_ID, {"counter": 5})
        await async_session.commit()

        response = await kv_client.post("/api/automation/v1/kv/counter/incr")

        assert response.status_code == 200
        assert response.json()["value"] == 6

    async def test_incr_by_custom_amount(self, kv_client, async_session):
        """Incr with by parameter increments by that amount."""
        await create_test_state(async_session, TEST_AUTOMATION_ID, {"counter": 10})
        await async_session.commit()

        response = await kv_client.post(
            "/api/automation/v1/kv/counter/incr",
            json={"by": 5},
        )

        assert response.status_code == 200
        assert response.json()["value"] == 15

    async def test_incr_rejects_non_integer(self, kv_client, async_session):
        """Incr rejects non-integer values."""
        await create_test_state(
            async_session, TEST_AUTOMATION_ID, {"counter": {"not": "numeric"}}
        )
        await async_session.commit()

        response = await kv_client.post("/api/automation/v1/kv/counter/incr")

        assert response.status_code == 400
        assert "type_mismatch" in response.json()["detail"]


class TestDecrement:
    """Tests for POST /kv/{key}/decr endpoint."""

    async def test_decr_creates_key(self, kv_client):
        """Decr creates key with negative value if it doesn't exist."""
        response = await kv_client.post("/api/automation/v1/kv/counter/decr")

        assert response.status_code == 200
        assert response.json()["value"] == -1

    async def test_decr_decrements_existing(self, kv_client, async_session):
        """Decr decrements existing integer value."""
        await create_test_state(async_session, TEST_AUTOMATION_ID, {"counter": 5})
        await async_session.commit()

        response = await kv_client.post("/api/automation/v1/kv/counter/decr")

        assert response.status_code == 200
        assert response.json()["value"] == 4


class TestListOperations:
    """Tests for list push/pop operations."""

    async def test_lpush_creates_list(self, kv_client):
        """LPUSH creates list if it doesn't exist."""
        response = await kv_client.post(
            "/api/automation/v1/kv/queue/lpush",
            json={"value": "first"},
        )

        assert response.status_code == 200
        assert response.json()["length"] == 1

    async def test_rpush_creates_list(self, kv_client):
        """RPUSH creates list if it doesn't exist."""
        response = await kv_client.post(
            "/api/automation/v1/kv/queue/rpush",
            json={"value": "first"},
        )

        assert response.status_code == 200
        assert response.json()["length"] == 1

    async def test_lpush_prepends(self, kv_client, async_session):
        """LPUSH prepends to existing list."""
        await create_test_state(
            async_session, TEST_AUTOMATION_ID, {"queue": ["second"]}
        )
        await async_session.commit()

        response = await kv_client.post(
            "/api/automation/v1/kv/queue/lpush",
            json={"value": "first"},
        )

        assert response.status_code == 200
        assert response.json()["length"] == 2

        # Verify order
        state = await get_test_state(async_session, TEST_AUTOMATION_ID)
        assert state is not None
        assert state["queue"] == ["first", "second"]

    async def test_rpush_appends(self, kv_client, async_session):
        """RPUSH appends to existing list."""
        await create_test_state(async_session, TEST_AUTOMATION_ID, {"queue": ["first"]})
        await async_session.commit()

        response = await kv_client.post(
            "/api/automation/v1/kv/queue/rpush",
            json={"value": "second"},
        )

        assert response.status_code == 200
        assert response.json()["length"] == 2

        # Verify order
        state = await get_test_state(async_session, TEST_AUTOMATION_ID)
        assert state is not None
        assert state["queue"] == ["first", "second"]

    async def test_lpop_returns_first(self, kv_client, async_session):
        """LPOP returns and removes first element."""
        await create_test_state(
            async_session, TEST_AUTOMATION_ID, {"queue": ["first", "second", "third"]}
        )
        await async_session.commit()

        response = await kv_client.post("/api/automation/v1/kv/queue/lpop")

        assert response.status_code == 200
        assert response.json()["value"] == "first"

        # Verify remaining
        state = await get_test_state(async_session, TEST_AUTOMATION_ID)
        assert state is not None
        assert state["queue"] == ["second", "third"]

    async def test_rpop_returns_last(self, kv_client, async_session):
        """RPOP returns and removes last element."""
        await create_test_state(
            async_session, TEST_AUTOMATION_ID, {"queue": ["first", "second", "third"]}
        )
        await async_session.commit()

        response = await kv_client.post("/api/automation/v1/kv/queue/rpop")

        assert response.status_code == 200
        assert response.json()["value"] == "third"

        # Verify remaining
        state = await get_test_state(async_session, TEST_AUTOMATION_ID)
        assert state is not None
        assert state["queue"] == ["first", "second"]

    async def test_lpop_empty_returns_null(self, kv_client, async_session):
        """LPOP on empty list returns null."""
        await create_test_state(async_session, TEST_AUTOMATION_ID, {"queue": []})
        await async_session.commit()

        response = await kv_client.post("/api/automation/v1/kv/queue/lpop")

        assert response.status_code == 200
        assert response.json()["value"] is None

    async def test_rpop_nonexistent_returns_null(self, kv_client):
        """RPOP on non-existent key returns null."""
        response = await kv_client.post("/api/automation/v1/kv/nonexistent/rpop")

        assert response.status_code == 200
        assert response.json()["value"] is None

    async def test_len_returns_length(self, kv_client, async_session):
        """LEN returns list length."""
        await create_test_state(
            async_session, TEST_AUTOMATION_ID, {"queue": [1, 2, 3, 4, 5]}
        )
        await async_session.commit()

        response = await kv_client.get("/api/automation/v1/kv/queue/len")

        assert response.status_code == 200
        assert response.json()["length"] == 5

    async def test_len_not_found(self, kv_client):
        """LEN returns 404 for non-existent key."""
        response = await kv_client.get("/api/automation/v1/kv/nonexistent/len")

        assert response.status_code == 404

    async def test_lpush_rejects_non_list(self, kv_client, async_session):
        """LPUSH rejects non-list values."""
        await create_test_state(
            async_session, TEST_AUTOMATION_ID, {"notlist": {"not": "a list"}}
        )
        await async_session.commit()

        response = await kv_client.post(
            "/api/automation/v1/kv/notlist/lpush",
            json={"value": "item"},
        )

        assert response.status_code == 400
        assert "type_mismatch" in response.json()["detail"]


class TestKeyValidation:
    """Tests for key name validation."""

    async def test_empty_key_rejected(self, kv_client):
        """Empty key is rejected."""
        # FastAPI will return 404 for empty path segment, not 400
        response = await kv_client.get("/api/automation/v1/kv/")
        assert response.status_code in (404, 307)  # Redirect or not found

    async def test_very_long_key_rejected(self, kv_client):
        """Key exceeding 255 chars is rejected."""
        long_key = "x" * 300
        response = await kv_client.get(f"/api/automation/v1/kv/{long_key}")

        assert response.status_code == 400
        assert "invalid_key" in response.json()["detail"]


class TestSingleDocumentIsolation:
    """Tests verifying single-document design properties."""

    async def test_multiple_keys_in_one_doc(self, kv_client, async_session):
        """Multiple keys are stored in a single document."""
        # Create first key
        await kv_client.put("/api/automation/v1/kv/key1", json="value1")
        # Create second key
        await kv_client.put("/api/automation/v1/kv/key2", json="value2")
        # Create third key
        await kv_client.put("/api/automation/v1/kv/key3", json="value3")

        # Verify all keys are in one state document
        state = await get_test_state(async_session, TEST_AUTOMATION_ID)
        assert state is not None
        # Filter out system keys ($version) for comparison
        user_keys = {k: v for k, v in state.items() if not k.startswith("$")}
        assert user_keys == {"key1": "value1", "key2": "value2", "key3": "value3"}
        # $version should be present and incremented (3 writes)
        assert state.get("$version") == 3

        # Verify only ONE row exists in the database
        result = await async_session.execute(
            select(AutomationKV).where(AutomationKV.automation_id == TEST_AUTOMATION_ID)
        )
        rows = result.scalars().all()
        assert len(rows) == 1

    async def test_delete_last_key_removes_row(self, kv_client, async_session):
        """Deleting the last user key removes the state row entirely."""
        # Create a key
        await kv_client.put("/api/automation/v1/kv/onlykey", json="value")

        # Delete it
        await kv_client.delete("/api/automation/v1/kv/onlykey")

        # Verify row is gone (no user keys remain, so row is deleted)
        result = await async_session.execute(
            select(AutomationKV).where(AutomationKV.automation_id == TEST_AUTOMATION_ID)
        )
        rows = result.scalars().all()
        assert len(rows) == 0

    async def test_operations_preserve_other_keys(self, kv_client, async_session):
        """Operations on one key don't affect other keys."""
        # Create state with multiple keys
        await create_test_state(
            async_session,
            TEST_AUTOMATION_ID,
            {"counter": 10, "config": {"setting": True}, "queue": ["item"]},
        )
        await async_session.commit()

        # Increment counter
        await kv_client.post("/api/automation/v1/kv/counter/incr")

        # Verify other keys are unchanged
        state = await get_test_state(async_session, TEST_AUTOMATION_ID)
        assert state is not None
        assert state["counter"] == 11
        assert state["config"] == {"setting": True}
        assert state["queue"] == ["item"]


# =============================================================================
# Tests for if_version on individual endpoints
# =============================================================================


class TestIfVersionOnIndividualEndpoints:
    """Test if_version query parameter for optimistic concurrency."""

    async def test_set_with_matching_version_succeeds(self, kv_client, async_session):
        """PUT with matching if_version succeeds."""
        # Create initial key (version becomes 1)
        resp = await kv_client.put("/api/automation/v1/kv/foo", json="bar")
        assert resp.status_code == 201

        # Update with correct version
        resp = await kv_client.put("/api/automation/v1/kv/foo?if_version=1", json="baz")
        assert resp.status_code == 200
        assert resp.json()["value"] == "baz"

    async def test_set_with_mismatched_version_fails(self, kv_client, async_session):
        """PUT with wrong if_version returns 409."""
        # Create initial key (version becomes 1)
        resp = await kv_client.put("/api/automation/v1/kv/foo", json="bar")
        assert resp.status_code == 201

        # Try to update with wrong version
        resp = await kv_client.put(
            "/api/automation/v1/kv/foo?if_version=99", json="baz"
        )
        assert resp.status_code == 409
        data = resp.json()["detail"]
        assert data["error"] == "version_mismatch"
        assert data["expected_version"] == 99
        assert data["actual_version"] == 1

    async def test_patch_with_matching_version_succeeds(self, kv_client, async_session):
        """PATCH with matching if_version succeeds."""
        # Create initial key with dict value (version becomes 1)
        resp = await kv_client.put(
            "/api/automation/v1/kv/config", json={"host": "localhost"}
        )
        assert resp.status_code == 201

        # Patch with correct version
        resp = await kv_client.patch(
            "/api/automation/v1/kv/config?if_version=1",
            json={"path": "port", "value": 5432},
        )
        assert resp.status_code == 200

    async def test_patch_with_mismatched_version_fails(self, kv_client, async_session):
        """PATCH with wrong if_version returns 409."""
        # Create initial key (version becomes 1)
        resp = await kv_client.put(
            "/api/automation/v1/kv/config", json={"host": "localhost"}
        )
        assert resp.status_code == 201

        # Try to patch with wrong version
        resp = await kv_client.patch(
            "/api/automation/v1/kv/config?if_version=99",
            json={"path": "port", "value": 5432},
        )
        assert resp.status_code == 409
        data = resp.json()["detail"]
        assert data["error"] == "version_mismatch"

    async def test_delete_with_matching_version_succeeds(
        self, kv_client, async_session
    ):
        """DELETE with matching if_version succeeds."""
        # Create initial key (version becomes 1)
        resp = await kv_client.put("/api/automation/v1/kv/foo", json="bar")
        assert resp.status_code == 201

        # Delete with correct version
        resp = await kv_client.delete("/api/automation/v1/kv/foo?if_version=1")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    async def test_delete_with_mismatched_version_fails(self, kv_client, async_session):
        """DELETE with wrong if_version returns 409."""
        # Create initial key (version becomes 1)
        resp = await kv_client.put("/api/automation/v1/kv/foo", json="bar")
        assert resp.status_code == 201

        # Try to delete with wrong version
        resp = await kv_client.delete("/api/automation/v1/kv/foo?if_version=99")
        assert resp.status_code == 409
        data = resp.json()["detail"]
        assert data["error"] == "version_mismatch"
        assert data["expected_version"] == 99
        assert data["actual_version"] == 1

    async def test_version_increments_across_operations(self, kv_client, async_session):
        """Version increments consistently across different operations."""
        # Create (v=1)
        resp = await kv_client.put("/api/automation/v1/kv/foo", json="bar")
        assert resp.status_code == 201

        # Update (v=2)
        resp = await kv_client.put("/api/automation/v1/kv/foo", json="baz")
        assert resp.status_code == 200

        # Incr new key (v=3)
        resp = await kv_client.post("/api/automation/v1/kv/counter/incr")
        assert resp.status_code == 200

        # Get with meta to check version
        resp = await kv_client.get("/api/automation/v1/kv/foo?meta=true")
        assert resp.status_code == 200
        assert resp.json()["version"] == 3

        # Delete with version check should work
        resp = await kv_client.delete("/api/automation/v1/kv/foo?if_version=3")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
