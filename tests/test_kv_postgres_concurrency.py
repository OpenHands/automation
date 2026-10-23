"""PostgreSQL-only concurrency tests for the per-key KV store.

The SQLite suites assert the observable API contract; they cannot exercise the
``SELECT ... FOR UPDATE`` locking that serializes writers, because SQLite skips
it (``using_sqlite()`` is true). This module runs against the real PostgreSQL
fixture in ``tests/conftest.py`` and deliberately does *not* override
``get_session``: each request takes its own connection from the app's session
factory, so overlapping requests actually contend on the row locks.

Covers:
- concurrent read-modify-write on one key does not lose updates (lock-sensitive);
- the global ``$version`` advances exactly once per committed write
  (lock-sensitive);
- overlapping multi-key batches stay consistent at the API level;
- a writer blocked behind a held lock fails fast with a clean 409 (not a 500 or
  a hang) once ``lock_timeout`` expires (lock-sensitive).

"Lock-sensitive" means the assertion was confirmed to fail when ``FOR UPDATE``
is removed from ``_lock_meta_row``/``_lock_key_rows``. The batch-interleaving
test is *not* lock-sensitive -- transaction commit alone guarantees
all-or-nothing -- so it is kept only as a contract check, not as evidence that
the row locks work.
"""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.app import app
from openhands.automation.config import clear_config_cache, get_config
from openhands.automation.db import get_session, set_sqlite_mode
from openhands.automation.kv_router import KVAuthContext, get_kv_auth_context
from openhands.automation.models import Automation, AutomationKV, AutomationKVMeta
from openhands.automation.utils.kv import decrypt_value


TEST_AUTOMATION_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TEST_KV_SECRET = "test-kv-secret-key-for-testing-only"
_BASE = get_config().service.base_path
_KV = f"{_BASE}/v1/kv"


@pytest.fixture
async def kv_pg_client(async_engine, async_session_factory, async_session, monkeypatch):
    """KV client whose requests each take their own pooled connection."""
    monkeypatch.setenv("AUTOMATION_KV_SECRET", TEST_KV_SECRET)
    clear_config_cache()
    set_sqlite_mode(False)

    async_session.add(
        Automation(
            id=TEST_AUTOMATION_ID,
            user_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            name="postgres concurrency test",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
    )
    await async_session.commit()

    async def override_ctx():
        return KVAuthContext(automation_id=TEST_AUTOMATION_ID, auth_method="kv_token")

    # get_session is intentionally NOT overridden: the real dependency yields a
    # fresh session (and connection) per request, which is what makes the
    # FOR UPDATE path do anything at all.
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides[get_kv_auth_context] = override_ctx

    app.state.engine = async_engine
    app.state.session_factory = async_session_factory

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client

    app.dependency_overrides.clear()
    clear_config_cache()
    set_sqlite_mode(False)


async def _read_decrypted(session_factory) -> tuple[dict[str, object], int | None]:
    """Read back the automation's plaintext state and global version."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(AutomationKV).where(
                    AutomationKV.automation_id == TEST_AUTOMATION_ID
                )
            )
        ).scalars()
        meta = (
            (
                await session.execute(
                    select(AutomationKVMeta).where(
                        AutomationKVMeta.automation_id == TEST_AUTOMATION_ID
                    )
                )
            )
            .scalars()
            .first()
        )
    state = {
        row.key: decrypt_value(TEST_KV_SECRET, row.value_encrypted) for row in rows
    }
    return state, (None if meta is None else int(meta.version))


class TestLostUpdateUnderContention:
    """Concurrent read-modify-write on one key must not lose updates."""

    async def test_concurrent_increments_do_not_lose_updates(
        self, kv_pg_client, async_session_factory
    ):
        """N concurrent increments of one key must total N.

        ``incr`` is a read-modify-write: decrypt, add, re-encrypt. Without the
        ``FOR UPDATE`` on the key row, concurrent increments read the same
        starting value and overwrite each other, so the final value lands below
        N. This is the lost-update the row locks exist to prevent, and it is
        invisible to the SQLite suites because SQLite skips ``FOR UPDATE``.
        """
        workers = 8
        responses = await asyncio.gather(
            *[
                kv_pg_client.post(f"{_KV}/counter/incr", json={"by": 1})
                for _ in range(workers)
            ]
        )
        for response in responses:
            assert response.status_code == 200, response.text

        # Every worker must have observed a distinct value, so the reported
        # values are exactly 1..N with no duplicates.
        reported = sorted(response.json()["value"] for response in responses)
        assert reported == list(range(1, workers + 1)), reported

        stored, version = await _read_decrypted(async_session_factory)
        assert stored["counter"] == workers, stored
        assert version == workers, version

    async def test_overlapping_batches_are_atomic(
        self, kv_pg_client, async_session_factory
    ):
        """Two writers setting the same two keys never produce a mixed pair.

        Each batch writes ``k1`` and ``k2`` to the same token, so a partially
        applied batch would leave the pair disagreeing. Note this holds because
        the batch runs in one transaction -- it passes with ``FOR UPDATE``
        removed, so it guards the contract rather than the locking.
        """
        rounds = 6
        for round_index in range(rounds):
            token_a = f"a{round_index}"
            token_b = f"b{round_index}"

            responses = await asyncio.gather(
                kv_pg_client.post(
                    f"{_KV}/batch",
                    json={
                        "operations": [
                            {"op": "set", "key": "k1", "value": token_a},
                            {"op": "set", "key": "k2", "value": token_a},
                        ]
                    },
                ),
                kv_pg_client.post(
                    f"{_KV}/batch",
                    json={
                        "operations": [
                            {"op": "set", "key": "k1", "value": token_b},
                            {"op": "set", "key": "k2", "value": token_b},
                        ]
                    },
                ),
            )

            # Neither writer sends if_version, so a 409 here would mean the lock
            # path turned ordinary contention into a spurious conflict.
            for response in responses:
                assert response.status_code == 200, response.text

            stored, _ = await _read_decrypted(async_session_factory)
            assert stored["k1"] == stored["k2"], (
                f"round {round_index}: batch applied partially: {stored}"
            )
            assert stored["k1"] in {token_a, token_b}

    async def test_version_advances_once_per_committed_batch(
        self, kv_pg_client, async_session_factory
    ):
        """Concurrent batches each bump the shared version exactly once."""
        batches = 5
        responses = await asyncio.gather(
            *[
                kv_pg_client.post(
                    f"{_KV}/batch",
                    json={"operations": [{"op": "set", "key": f"key-{i}", "value": i}]},
                )
                for i in range(batches)
            ]
        )
        for response in responses:
            assert response.status_code == 200, response.text

        reported = sorted(response.json()["version"] for response in responses)
        assert reported == list(range(1, batches + 1)), reported

        _, version = await _read_decrypted(async_session_factory)
        assert version == batches


class TestLockTimeout:
    """A blocked writer must fail fast with a clean 409, not hang or 500."""

    async def test_blocked_writer_gets_409_with_retry_after(
        self, kv_pg_client, async_engine, async_session_factory, monkeypatch
    ):
        """A writer waiting on a held metadata lock surfaces 409 + Retry-After."""
        monkeypatch.setenv("AUTOMATION_KV_LOCK_TIMEOUT_MS", "250")
        clear_config_cache()

        # Commit a metadata row so the lock below targets an existing row.
        async with async_session_factory() as seed:
            seed.add(AutomationKVMeta(automation_id=TEST_AUTOMATION_ID, version=0))
            await seed.commit()

        # Hold that row's lock for the duration of the request, mirroring the
        # first thing every write path does.
        holder_factory = async_sessionmaker(
            async_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with holder_factory() as holder:
            await holder.execute(
                text(
                    "SELECT version FROM automation_kv_meta "
                    "WHERE automation_id = :aid FOR UPDATE"
                ),
                {"aid": TEST_AUTOMATION_ID},
            )

            response = await kv_pg_client.put(f"{_KV}/blocked-key", json="value")

            assert response.status_code == 409, response.text
            assert "kv_store_busy" in response.json()["detail"]
            assert response.headers.get("Retry-After") == "1"

            await holder.rollback()

        # The blocked write left no key row and did not bump the version.
        stored, version = await _read_decrypted(async_session_factory)
        assert "blocked-key" not in stored, stored
        assert version == 0, version
