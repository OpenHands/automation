"""Per-key KV store tests that run on SQLite (no Docker required).

Covers the behavior that changed in issue #523: per-value (not aggregate)
size limits, per-key row storage with a global version, batch atomicity, and
paginated key listing. The PostgreSQL-only locking *behavior* is covered by
``tests/test_kv_router.py``; this module asserts the observable API contract
on an in-memory SQLite database so it runs in every environment.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from openhands.automation.app import app
from openhands.automation.config import clear_config_cache, get_config
from openhands.automation.db import get_session, set_sqlite_mode
from openhands.automation.kv_router import KVAuthContext, get_kv_auth_context
from openhands.automation.models import Automation, AutomationKV, AutomationKVMeta, Base


TEST_AUTOMATION_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TEST_KV_SECRET = "test-kv-secret-key-for-testing-only"
_BASE = get_config().service.base_path
_KV = f"{_BASE}/v1/kv"


@pytest.fixture
async def kv_sqlite_client(monkeypatch):
    monkeypatch.setenv("AUTOMATION_KV_SECRET", TEST_KV_SECRET)
    monkeypatch.setenv("AUTOMATION_DB_URL", "sqlite+aiosqlite:///:memory:")
    clear_config_cache()

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    set_sqlite_mode(True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as s:
        s.add(
            Automation(
                id=TEST_AUTOMATION_ID,
                user_id=uuid.uuid4(),
                org_id=uuid.uuid4(),
                name="per-key test",
                trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run script.py",
            )
        )
        await s.commit()

    async def override_get_session():
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    async def override_ctx():
        return KVAuthContext(automation_id=TEST_AUTOMATION_ID, auth_method="kv_token")

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_kv_auth_context] = override_ctx

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, factory

    app.dependency_overrides.clear()
    await engine.dispose()
    set_sqlite_mode(False)
    clear_config_cache()


async def _count_rows(factory, model):
    async with factory() as s:
        result = await s.execute(select(model))
        return len(result.scalars().all())


class TestAggregateLimitRegression:
    async def test_many_small_keys_do_not_trip_limit(self, kv_sqlite_client):
        """The exact issue #523 repro: ~300 200-byte keys previously 413'd."""
        client, factory = kv_sqlite_client
        value = "x" * 200
        for i in range(400):
            response = await client.put(f"{_KV}/key-{i:04d}", json=value)
            assert response.status_code == 201, response.text

        listed = await client.get(_KV, params={"limit": 1000})
        assert listed.json()["total"] == 400
        assert await _count_rows(factory, AutomationKV) == 400
        assert await _count_rows(factory, AutomationKVMeta) == 1

    async def test_oversized_single_value_returns_413(self, kv_sqlite_client):
        client, _ = kv_sqlite_client
        limit = get_config().kv.kv_max_value_size
        response = await client.put(f"{_KV}/big", json="x" * (limit + 1))

        assert response.status_code == 413
        detail = response.json()["detail"]
        assert detail["error"] == "value_too_large"
        assert detail["limit"] == limit
        assert detail["key"] == "big"
        assert detail["size"] > limit

    async def test_batch_checks_limit_per_value(self, kv_sqlite_client):
        client, _ = kv_sqlite_client
        limit = get_config().kv.kv_max_value_size
        per_value = "x" * 4000
        operations = [
            {"op": "set", "key": f"batch-{i}", "value": per_value} for i in range(30)
        ]
        response = await client.post(f"{_KV}/batch", json={"operations": operations})

        assert response.status_code == 200, response.text
        assert limit < len(per_value) * len(operations)

    async def test_batch_oversized_value_returns_413(self, kv_sqlite_client):
        client, _ = kv_sqlite_client
        limit = get_config().kv.kv_max_value_size
        response = await client.post(
            f"{_KV}/batch",
            json={
                "operations": [{"op": "set", "key": "big", "value": "x" * (limit + 1)}]
            },
        )
        assert response.status_code == 413
        assert response.json()["detail"]["error"] == "value_too_large"


class TestPagination:
    async def test_list_keys_paginates(self, kv_sqlite_client):
        client, _ = kv_sqlite_client
        for i in range(25):
            await client.put(f"{_KV}/k{i:03d}", json=i)

        first = await client.get(_KV, params={"limit": 10, "offset": 0})
        body = first.json()
        assert body["count"] == 10
        assert body["total"] == 25
        assert body["limit"] == 10
        assert body["offset"] == 0

        second = await client.get(_KV, params={"limit": 10, "offset": 10})
        assert set(body["keys"]).isdisjoint(second.json()["keys"])

        last = await client.get(_KV, params={"limit": 10, "offset": 20})
        assert last.json()["count"] == 5
        assert last.json()["total"] == 25


class TestAtomicityAndVersion:
    async def test_batch_is_all_or_nothing(self, kv_sqlite_client):
        client, _ = kv_sqlite_client
        await client.put(f"{_KV}/counter", json="not-a-number")

        response = await client.post(
            f"{_KV}/batch",
            json={
                "operations": [
                    {"op": "set", "key": "before", "value": "ok"},
                    {"op": "incr", "key": "counter"},  # fails: not an integer
                ]
            },
        )

        assert response.status_code == 400
        # The first operation must not have persisted.
        assert (await client.get(f"{_KV}/before")).status_code == 404

    async def test_version_is_global_and_shared_across_keys(self, kv_sqlite_client):
        client, _ = kv_sqlite_client
        assert (await client.put(f"{_KV}/a", json=1)).status_code == 201
        assert (await client.put(f"{_KV}/b", json=2)).status_code == 201

        resp = await client.get(f"{_KV}/a", params={"meta": "true"})
        assert resp.json()["version"] == 2

        # if_version refers to the shared counter, not a per-key counter.
        assert (await client.put(f"{_KV}/b?if_version=2", json=3)).status_code == 200
        assert (await client.put(f"{_KV}/b?if_version=1", json=4)).status_code == 409

    async def test_if_version_batch(self, kv_sqlite_client):
        client, _ = kv_sqlite_client
        await client.put(f"{_KV}/a", json=1)  # version 1
        resp = await client.post(
            f"{_KV}/batch",
            json={
                "if_version": 1,
                "operations": [{"op": "set", "key": "a", "value": 2}],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["version"] == 2

        resp = await client.post(
            f"{_KV}/batch",
            json={
                "if_version": 1,
                "operations": [{"op": "set", "key": "a", "value": 3}],
            },
        )
        assert resp.status_code == 409
