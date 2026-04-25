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

Why This Matters
----------------
The KV store uses `SELECT ... FOR UPDATE` to implement atomic operations like
increment/decrement and list push/pop. When multiple requests try to modify
the same key:

- With separate sessions: Requests queue up waiting for the lock, execute
  sequentially, and produce correct results.

- With a shared session: All requests try to use the same connection. The
  first request acquires the lock, and subsequent requests DEADLOCK waiting
  for a lock they can never acquire (because they're on the same connection).

If you're adding new concurrency tests, use `concurrent_kv_client`.
If you're adding single-request tests, use `kv_client`.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from automation.app import app
from automation.db import get_session
from automation.kv_router import get_automation_id_from_token
from automation.models import Automation, AutomationKV
from automation.utils.kv import encrypt_value


# Test UUIDs
TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
TEST_AUTOMATION_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TEST_RUN_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

# Test secret for JWT and encryption
TEST_KV_SECRET = "test-kv-secret-key-for-testing-only"


# =============================================================================
# Test Client Fixtures
# =============================================================================


@pytest.fixture
async def kv_client(async_engine, async_session_factory, async_session, monkeypatch):
    """Create an async test client with KV token auth (shared session).

    This fixture uses a SHARED async_session for all requests. It's suitable
    for single-request tests but will DEADLOCK if used for concurrent requests.

    For concurrency tests, use `concurrent_kv_client` instead.
    """
    # Set the KV secret so encryption/decryption uses the same key
    monkeypatch.setenv("AUTOMATION_KV_SECRET", TEST_KV_SECRET)

    # Clear the cached settings so the new env var is picked up
    from automation.config import get_settings

    get_settings.cache_clear()

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
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def automation_with_kv(async_session):
    """Create a test automation with KV store enabled.

    This fixture is autouse=True so that all KV router tests
    have a parent Automation record available for the foreign key.
    """
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

    def test_wrong_secret_raises_error(self):
        """Token verified with wrong secret raises error."""
        from automation.utils.kv import KVTokenError, create_kv_token, verify_kv_token

        token = create_kv_token(
            secret=TEST_KV_SECRET,
            automation_id=TEST_AUTOMATION_ID,
            run_id=TEST_RUN_ID,
        )

        with pytest.raises(KVTokenError):
            verify_kv_token("wrong-secret", token)


class TestKVEncryption:
    """Tests for KV value encryption."""

    def test_encrypt_decrypt_string(self):
        """String values can be encrypted and decrypted."""
        from automation.utils.kv import decrypt_value, encrypt_value

        original = "hello world"
        encrypted = encrypt_value(TEST_KV_SECRET, original)
        decrypted = decrypt_value(TEST_KV_SECRET, encrypted)

        assert decrypted == original
        assert encrypted != original

    def test_encrypt_decrypt_dict(self):
        """Dict values can be encrypted and decrypted."""
        from automation.utils.kv import decrypt_value, encrypt_value

        original = {"key": "value", "nested": {"a": 1}}
        encrypted = encrypt_value(TEST_KV_SECRET, original)
        decrypted = decrypt_value(TEST_KV_SECRET, encrypted)

        assert decrypted == original

    def test_encrypt_decrypt_list(self):
        """List values can be encrypted and decrypted."""
        from automation.utils.kv import decrypt_value, encrypt_value

        original = [1, 2, {"key": "value"}]
        encrypted = encrypt_value(TEST_KV_SECRET, original)
        decrypted = decrypt_value(TEST_KV_SECRET, encrypted)

        assert decrypted == original

    def test_encrypt_decrypt_number(self):
        """Numeric values can be encrypted and decrypted."""
        from automation.utils.kv import decrypt_value, encrypt_value

        original = 42
        encrypted = encrypt_value(TEST_KV_SECRET, original)
        decrypted = decrypt_value(TEST_KV_SECRET, encrypted)

        assert decrypted == original


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
        # Create some KV entries
        for key in ["config", "counter", "queue"]:
            kv = AutomationKV(
                automation_id=TEST_AUTOMATION_ID,
                key=key,
                value_encrypted=encrypt_value(TEST_KV_SECRET, {"test": True}),
            )
            async_session.add(kv)
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
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="config",
            value_encrypted=encrypt_value(TEST_KV_SECRET, value),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.get("/api/automation/v1/kv/config")

        assert response.status_code == 200
        data = response.json()
        assert data["key"] == "config"
        assert data["value"] == value

    async def test_get_value_with_path(self, kv_client, async_session):
        """Get nested path returns specific value."""
        value = {"database": {"host": "localhost", "port": 5432}}
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="config",
            value_encrypted=encrypt_value(TEST_KV_SECRET, value),
        )
        async_session.add(kv)
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
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="config",
            value_encrypted=encrypt_value(TEST_KV_SECRET, "test"),
        )
        async_session.add(kv)
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

    async def test_set_update_existing(self, kv_client, async_session):
        """Set updates existing key (returns 200 OK)."""
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="config",
            value_encrypted=encrypt_value(TEST_KV_SECRET, "old"),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.put(
            "/api/automation/v1/kv/config",
            json="new",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["value"] == "new"
        assert data["created"] is False

    async def test_set_nx_creates_new(self, kv_client):
        """Set with nx=true creates new key (returns 201 Created)."""
        response = await kv_client.put(
            "/api/automation/v1/kv/lock?nx=true",
            json={"owner": "run-123"},
        )

        assert response.status_code == 201
        data = response.json()
        assert data["created"] is True

    async def test_set_nx_fails_if_exists(self, kv_client, async_session):
        """Set with nx=true fails if key exists (returns 409 Conflict)."""
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="lock",
            value_encrypted=encrypt_value(TEST_KV_SECRET, {"owner": "other"}),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.put(
            "/api/automation/v1/kv/lock?nx=true",
            json={"owner": "run-123"},
        )

        assert response.status_code == 409
        data = response.json()
        assert data["created"] is False
        assert data["error"] == "key_exists"

    async def test_set_xx_fails_if_not_exists(self, kv_client):
        """Set with xx=true fails if key doesn't exist."""
        response = await kv_client.put(
            "/api/automation/v1/kv/nonexistent?xx=true",
            json="value",
        )

        assert response.status_code == 409
        assert response.json()["detail"] == "key_not_exists"


class TestPatchValue:
    """Tests for PATCH /kv/{key} endpoint."""

    async def test_patch_nested_path(self, kv_client, async_session):
        """Patch updates nested path."""
        value = {"database": {"host": "localhost", "port": 5432}}
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="config",
            value_encrypted=encrypt_value(TEST_KV_SECRET, value),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.patch(
            "/api/automation/v1/kv/config",
            json={"path": "database.port", "value": 5433},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["path"] == "database.port"
        assert data["value"] == 5433

    async def test_patch_not_found(self, kv_client):
        """Patch non-existent key returns 404."""
        response = await kv_client.patch(
            "/api/automation/v1/kv/nonexistent",
            json={"path": "key", "value": "value"},
        )

        assert response.status_code == 404


class TestDeleteKey:
    """Tests for DELETE /kv/{key} endpoint."""

    async def test_delete_existing(self, kv_client, async_session):
        """Delete removes existing key."""
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="config",
            value_encrypted=encrypt_value(TEST_KV_SECRET, "test"),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.delete("/api/automation/v1/kv/config")

        assert response.status_code == 200
        data = response.json()
        assert data["deleted"] is True

    async def test_delete_nonexistent(self, kv_client):
        """Delete non-existent key returns deleted=false."""
        response = await kv_client.delete("/api/automation/v1/kv/nonexistent")

        assert response.status_code == 200
        data = response.json()
        assert data["deleted"] is False


class TestIncrement:
    """Tests for POST /kv/{key}/incr endpoint."""

    async def test_incr_new_key(self, kv_client):
        """Increment new key initializes to 1."""
        response = await kv_client.post("/api/automation/v1/kv/counter/incr")

        assert response.status_code == 200
        data = response.json()
        assert data["value"] == 1

    async def test_incr_existing(self, kv_client, async_session):
        """Increment existing key adds 1."""
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="counter",
            value_encrypted=encrypt_value(TEST_KV_SECRET, 5),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.post("/api/automation/v1/kv/counter/incr")

        assert response.status_code == 200
        data = response.json()
        assert data["value"] == 6

    async def test_incr_by_amount(self, kv_client, async_session):
        """Increment by specific amount."""
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="counter",
            value_encrypted=encrypt_value(TEST_KV_SECRET, 10),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.post(
            "/api/automation/v1/kv/counter/incr",
            json={"by": 5},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["value"] == 15

    async def test_incr_non_numeric_fails(self, kv_client, async_session):
        """Increment non-numeric value fails."""
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="config",
            value_encrypted=encrypt_value(TEST_KV_SECRET, {"not": "numeric"}),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.post("/api/automation/v1/kv/config/incr")

        assert response.status_code == 400
        assert "type_mismatch" in response.json()["detail"]


class TestConcurrency:
    """Tests for concurrent atomic operations.

    IMPORTANT: These tests use `concurrent_kv_client`, NOT `kv_client`.
    See the module docstring for why this distinction matters.

    These tests verify that FOR UPDATE locking prevents race conditions
    when multiple requests modify the same key simultaneously. The tests
    fire N concurrent requests and verify the final state is correct,
    proving no operations were lost to race conditions.

    If these tests hang or timeout, it likely means someone accidentally
    used `kv_client` instead of `concurrent_kv_client`, causing a deadlock.
    """

    @pytest.fixture
    async def concurrent_kv_client(
        self, async_engine, async_session_factory, async_session, monkeypatch
    ):
        """Client for concurrency tests (separate session per request).

        CRITICAL: This fixture does NOT override get_session, unlike kv_client.
        This allows each concurrent request to get its own database session
        from the factory, enabling true parallel database operations.

        Why this matters:
        - KV operations use SELECT ... FOR UPDATE to lock rows
        - If all requests share one session/connection, they deadlock
        - With separate sessions, requests queue on the lock and succeed

        Use this fixture for ANY test that fires multiple concurrent requests.
        """
        monkeypatch.setenv("AUTOMATION_KV_SECRET", TEST_KV_SECRET)

        from automation.config import get_settings

        get_settings.cache_clear()

        async def override_get_automation_id():
            return TEST_AUTOMATION_ID

        # IMPORTANT: Only override auth, NOT the session!
        # Each request must get its own session from the factory.
        app.dependency_overrides[get_automation_id_from_token] = (
            override_get_automation_id
        )

        app.state.engine = async_engine
        app.state.session_factory = async_session_factory

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client

        app.dependency_overrides.clear()
        get_settings.cache_clear()

    async def test_concurrent_increments(self, concurrent_kv_client):
        """Concurrent increments produce correct final value.

        Fires N concurrent increment requests and verifies the final
        counter value equals N, proving no increments were lost.
        """
        import asyncio

        num_increments = 10

        # Fire N concurrent increment requests
        tasks = [
            concurrent_kv_client.post("/api/automation/v1/kv/concurrent_counter/incr")
            for _ in range(num_increments)
        ]
        responses = await asyncio.gather(*tasks)

        # All requests should succeed
        assert all(r.status_code == 200 for r in responses)

        # Verify final value equals number of increments
        get_response = await concurrent_kv_client.get(
            "/api/automation/v1/kv/concurrent_counter"
        )
        assert get_response.status_code == 200
        assert get_response.json()["value"] == num_increments

    async def test_concurrent_list_pushes(self, concurrent_kv_client):
        """Concurrent list pushes don't lose elements.

        Fires N concurrent rpush requests and verifies the final
        list length equals N, proving no pushes were lost.
        """
        import asyncio

        num_pushes = 10

        # Fire N concurrent rpush requests with unique values
        tasks = [
            concurrent_kv_client.post(
                "/api/automation/v1/kv/concurrent_list/rpush",
                json={"value": f"item-{i}"},
            )
            for i in range(num_pushes)
        ]
        responses = await asyncio.gather(*tasks)

        # All requests should succeed
        assert all(r.status_code == 200 for r in responses)

        # Verify list length equals number of pushes
        len_response = await concurrent_kv_client.get(
            "/api/automation/v1/kv/concurrent_list/len"
        )
        assert len_response.status_code == 200
        assert len_response.json()["length"] == num_pushes


class TestDecrement:
    """Tests for POST /kv/{key}/decr endpoint."""

    async def test_decr_new_key(self, kv_client):
        """Decrement new key initializes to -1."""
        response = await kv_client.post("/api/automation/v1/kv/counter/decr")

        assert response.status_code == 200
        data = response.json()
        assert data["value"] == -1

    async def test_decr_existing(self, kv_client, async_session):
        """Decrement existing key subtracts 1."""
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="counter",
            value_encrypted=encrypt_value(TEST_KV_SECRET, 5),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.post("/api/automation/v1/kv/counter/decr")

        assert response.status_code == 200
        data = response.json()
        assert data["value"] == 4


class TestListOperations:
    """Tests for list operations (lpush, rpush, lpop, rpop, len)."""

    async def test_rpush_new_list(self, kv_client):
        """Right push to new list creates single-element list."""
        response = await kv_client.post(
            "/api/automation/v1/kv/queue/rpush",
            json={"value": {"task": "first"}},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["length"] == 1

    async def test_rpush_existing(self, kv_client, async_session):
        """Right push appends to end of list."""
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="queue",
            value_encrypted=encrypt_value(TEST_KV_SECRET, ["first"]),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.post(
            "/api/automation/v1/kv/queue/rpush",
            json={"value": "second"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["length"] == 2

    async def test_lpush_existing(self, kv_client, async_session):
        """Left push prepends to front of list."""
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="queue",
            value_encrypted=encrypt_value(TEST_KV_SECRET, ["second"]),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.post(
            "/api/automation/v1/kv/queue/lpush",
            json={"value": "first"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["length"] == 2

    async def test_lpop_returns_first(self, kv_client, async_session):
        """Left pop returns and removes first element."""
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="queue",
            value_encrypted=encrypt_value(TEST_KV_SECRET, ["first", "second", "third"]),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.post("/api/automation/v1/kv/queue/lpop")

        assert response.status_code == 200
        data = response.json()
        assert data["value"] == "first"

    async def test_rpop_returns_last(self, kv_client, async_session):
        """Right pop returns and removes last element."""
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="queue",
            value_encrypted=encrypt_value(TEST_KV_SECRET, ["first", "second", "third"]),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.post("/api/automation/v1/kv/queue/rpop")

        assert response.status_code == 200
        data = response.json()
        assert data["value"] == "third"

    async def test_lpop_empty_list(self, kv_client, async_session):
        """Left pop from empty list returns null."""
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="queue",
            value_encrypted=encrypt_value(TEST_KV_SECRET, []),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.post("/api/automation/v1/kv/queue/lpop")

        assert response.status_code == 200
        data = response.json()
        assert data["value"] is None

    async def test_lpop_nonexistent_key(self, kv_client):
        """Left pop from non-existent key returns null."""
        response = await kv_client.post("/api/automation/v1/kv/nonexistent/lpop")

        assert response.status_code == 200
        data = response.json()
        assert data["value"] is None

    async def test_len_returns_length(self, kv_client, async_session):
        """Len returns list length."""
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="queue",
            value_encrypted=encrypt_value(TEST_KV_SECRET, [1, 2, 3, 4, 5]),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.get("/api/automation/v1/kv/queue/len")

        assert response.status_code == 200
        data = response.json()
        assert data["length"] == 5

    async def test_len_not_found(self, kv_client):
        """Len on non-existent key returns 404."""
        response = await kv_client.get("/api/automation/v1/kv/nonexistent/len")

        assert response.status_code == 404

    async def test_push_to_non_list_fails(self, kv_client, async_session):
        """Push to non-list value fails."""
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="config",
            value_encrypted=encrypt_value(TEST_KV_SECRET, {"not": "a list"}),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.post(
            "/api/automation/v1/kv/config/rpush",
            json={"value": "item"},
        )

        assert response.status_code == 400
        assert "type_mismatch" in response.json()["detail"]


class TestValueSizeLimit:
    """Tests for KV value size limit enforcement.

    The size limit is configurable via AUTOMATION_KV_MAX_VALUE_SIZE.
    Default is 64KB. These tests use a smaller limit for efficiency.
    """

    @pytest.fixture
    async def small_limit_client(
        self, async_engine, async_session_factory, async_session, monkeypatch
    ):
        """Client with a small value size limit (1KB) for testing."""
        monkeypatch.setenv("AUTOMATION_KV_SECRET", TEST_KV_SECRET)
        monkeypatch.setenv("AUTOMATION_KV_MAX_VALUE_SIZE", "1024")  # 1KB

        from automation.config import get_settings

        get_settings.cache_clear()

        async def override_get_session():
            yield async_session

        async def override_get_automation_id():
            return TEST_AUTOMATION_ID

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_automation_id_from_token] = (
            override_get_automation_id
        )

        app.state.engine = async_engine
        app.state.session_factory = async_session_factory

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client

        app.dependency_overrides.clear()
        get_settings.cache_clear()

    async def test_set_within_limit_succeeds(self, small_limit_client):
        """Setting a value within size limit succeeds."""
        small_value = {"data": "x" * 100}  # ~120 bytes

        response = await small_limit_client.put(
            "/api/automation/v1/kv/small_key",
            json=small_value,
        )

        assert response.status_code == 201

    async def test_set_exceeds_limit_returns_413(self, small_limit_client):
        """Setting a value exceeding size limit returns 413."""
        large_value = {"data": "x" * 2000}  # ~2KB, exceeds 1KB limit

        response = await small_limit_client.put(
            "/api/automation/v1/kv/large_key",
            json=large_value,
        )

        assert response.status_code == 413
        assert "exceeds limit" in response.json()["detail"]

    async def test_patch_exceeds_limit_returns_413(
        self, small_limit_client, async_session
    ):
        """Patching a value to exceed size limit returns 413."""
        # Start with a small value
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="growing_obj",
            value_encrypted=encrypt_value(TEST_KV_SECRET, {"field": "small"}),
        )
        async_session.add(kv)
        await async_session.commit()

        # Try to patch in a large value
        response = await small_limit_client.patch(
            "/api/automation/v1/kv/growing_obj",
            json={"path": "field", "value": "x" * 2000},
        )

        assert response.status_code == 413
        assert "exceeds limit" in response.json()["detail"]

    async def test_rpush_exceeds_limit_returns_413(
        self, small_limit_client, async_session
    ):
        """Pushing to a list to exceed size limit returns 413."""
        # Start with a list near the limit
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="growing_list",
            value_encrypted=encrypt_value(TEST_KV_SECRET, ["x" * 500]),
        )
        async_session.add(kv)
        await async_session.commit()

        # Try to push another large item
        response = await small_limit_client.post(
            "/api/automation/v1/kv/growing_list/rpush",
            json={"value": "x" * 600},
        )

        assert response.status_code == 413
        assert "exceeds limit" in response.json()["detail"]

    async def test_lpush_exceeds_limit_returns_413(
        self, small_limit_client, async_session
    ):
        """Left-pushing to a list to exceed size limit returns 413."""
        # Start with a list near the limit
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="growing_list_left",
            value_encrypted=encrypt_value(TEST_KV_SECRET, ["x" * 500]),
        )
        async_session.add(kv)
        await async_session.commit()

        # Try to lpush another large item
        response = await small_limit_client.post(
            "/api/automation/v1/kv/growing_list_left/lpush",
            json={"value": "x" * 600},
        )

        assert response.status_code == 413
        assert "exceeds limit" in response.json()["detail"]



class TestJSONValidation:
    """Tests for strict JSON validation.

    The KV store enforces strict JSON compliance:
    - NaN, Infinity, -Infinity are rejected (not valid JSON per RFC 8259)
    - Maximum nesting depth is enforced (32 levels, prevents DoS)
    - Non-serializable types are rejected

    These tests verify the validation returns 400 Bad Request with
    descriptive error messages.
    """

    async def test_nan_rejected(self, kv_client):
        """NaN values are rejected as invalid JSON."""
        # Note: Python's json module accepts NaN by default, but our
        # strict validation rejects it. We can't send literal NaN via
        # HTTP JSON, but we test the validation logic directly.
        from automation.utils.kv import KVValueError, _validate_json_value

        with pytest.raises(KVValueError) as exc_info:
            _validate_json_value(float("nan"))
        assert "non-JSON-compliant" in str(exc_info.value)

    async def test_infinity_rejected(self, kv_client):
        """Infinity values are rejected as invalid JSON."""
        from automation.utils.kv import KVValueError, _validate_json_value

        with pytest.raises(KVValueError) as exc_info:
            _validate_json_value(float("inf"))
        assert "non-JSON-compliant" in str(exc_info.value)

    async def test_negative_infinity_rejected(self, kv_client):
        """Negative infinity values are rejected as invalid JSON."""
        from automation.utils.kv import KVValueError, _validate_json_value

        with pytest.raises(KVValueError) as exc_info:
            _validate_json_value(float("-inf"))
        assert "non-JSON-compliant" in str(exc_info.value)

    async def test_deeply_nested_rejected(self, kv_client):
        """Deeply nested structures exceeding max depth are rejected."""
        from automation.utils.kv import KVValueError, _validate_json_value

        # Create a structure deeper than _MAX_NESTING_DEPTH (32)
        deep = {"level": 0}
        current = deep
        for i in range(35):
            current["nested"] = {"level": i + 1}
            current = current["nested"]

        with pytest.raises(KVValueError) as exc_info:
            _validate_json_value(deep)
        assert "nesting depth" in str(exc_info.value)

    async def test_valid_nested_accepted(self, kv_client):
        """Reasonably nested structures are accepted."""
        from automation.utils.kv import _validate_json_value

        # Create a structure within limits (10 levels)
        nested = {"level": 0}
        current = nested
        for i in range(10):
            current["nested"] = {"level": i + 1}
            current = current["nested"]

        # Should not raise
        result = _validate_json_value(nested)
        assert '"level"' in result

    async def test_valid_json_types_accepted(self, kv_client):
        """All standard JSON types are accepted."""
        from automation.utils.kv import _validate_json_value

        test_values = [
            None,
            True,
            False,
            42,
            3.14,
            "hello",
            [1, 2, 3],
            {"key": "value"},
            {"nested": {"list": [1, {"deep": True}]}},
        ]

        for value in test_values:
            # Should not raise
            _validate_json_value(value)


class TestKeyValidation:
    """Tests for key name validation at the API level.

    Verifies that invalid key names are rejected by the router.
    These tests complement the unit tests in test_kv_helpers.py.
    """

    async def test_valid_key_accepted(self, kv_client):
        """Normal key names work correctly."""
        response = await kv_client.put(
            "/api/automation/v1/kv/valid_key",
            json={"value": "test"},
        )
        assert response.status_code == 201

    async def test_key_with_unicode_accepted(self, kv_client):
        """Unicode key names are accepted."""
        response = await kv_client.put(
            "/api/automation/v1/kv/日本語キー",
            json={"value": "test"},
        )
        assert response.status_code == 201

    async def test_very_long_key_rejected(self, kv_client):
        """Key exceeding max length is rejected."""
        long_key = "x" * 300
        response = await kv_client.put(
            f"/api/automation/v1/kv/{long_key}",
            json={"value": "test"},
        )
        assert response.status_code == 400
        assert "exceeds" in response.json()["detail"]


class TestIncrIntegerOnly:
    """Tests for incr/decr integer-only behavior.

    Verifies that incr/decr operations reject non-integer values
    to prevent silent precision loss.
    """

    async def test_incr_on_integer_succeeds(self, kv_client, async_session):
        """Incrementing an integer value works."""
        # Set up an integer value
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="counter",
            value_encrypted=encrypt_value(TEST_KV_SECRET, 10),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.post(
            "/api/automation/v1/kv/counter/incr",
            json={"by": 5},
        )
        assert response.status_code == 200
        assert response.json()["value"] == 15

    async def test_incr_on_float_rejected(self, kv_client, async_session):
        """Incrementing a float value is rejected."""
        # Set up a float value
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="float_val",
            value_encrypted=encrypt_value(TEST_KV_SECRET, 3.14),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.post(
            "/api/automation/v1/kv/float_val/incr",
            json={"by": 1},
        )
        assert response.status_code == 400
        assert "float" in response.json()["detail"]

    async def test_incr_on_boolean_rejected(self, kv_client, async_session):
        """Incrementing a boolean value is rejected."""
        # Set up a boolean value
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="bool_val",
            value_encrypted=encrypt_value(TEST_KV_SECRET, True),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.post(
            "/api/automation/v1/kv/bool_val/incr",
        )
        assert response.status_code == 400
        assert "boolean" in response.json()["detail"]

    async def test_decr_on_float_rejected(self, kv_client, async_session):
        """Decrementing a float value is rejected."""
        # Set up a float value
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="float_decr",
            value_encrypted=encrypt_value(TEST_KV_SECRET, 5.5),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.post(
            "/api/automation/v1/kv/float_decr/decr",
        )
        assert response.status_code == 400
        assert "float" in response.json()["detail"]

    async def test_incr_creates_integer(self, kv_client):
        """Incrementing non-existent key creates an integer value."""
        response = await kv_client.post(
            "/api/automation/v1/kv/new_counter/incr",
            json={"by": 5},
        )
        assert response.status_code == 200
        assert response.json()["value"] == 5

        # Verify it's an integer by checking we can increment again
        response = await kv_client.post(
            "/api/automation/v1/kv/new_counter/incr",
        )
        assert response.status_code == 200
        assert response.json()["value"] == 6

    async def test_incr_preserves_integer_type(self, kv_client, async_session):
        """Incrementing preserves integer type (no float conversion)."""
        # Set up a large integer
        big_int = 10**20
        kv = AutomationKV(
            automation_id=TEST_AUTOMATION_ID,
            key="big_counter",
            value_encrypted=encrypt_value(TEST_KV_SECRET, big_int),
        )
        async_session.add(kv)
        await async_session.commit()

        response = await kv_client.post(
            "/api/automation/v1/kv/big_counter/incr",
            json={"by": 1},
        )
        assert response.status_code == 200
        # Verify exact integer math (no float precision loss)
        assert response.json()["value"] == big_int + 1
