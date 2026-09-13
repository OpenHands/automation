"""Exercise response cleanup with the service's actual middleware stack."""

import asyncio

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from openhands.automation import telemetry
from openhands.automation.app import app as service_app
from openhands.automation.db import get_session


def _app():
    app = FastAPI()
    for middleware in reversed(service_app.user_middleware):
        app.add_middleware(middleware.cls, *middleware.args, **middleware.kwargs)
    return app


@pytest.mark.asyncio
async def test_route_telemetry_releases_request_connections_first(
    tmp_path, monkeypatch
):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'pool.db'}",
        pool_size=2,
        max_overflow=0,
        pool_timeout=0.2,
    )
    pool = engine.pool
    assert isinstance(pool, AsyncAdaptedQueuePool)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE automation_service_metadata "
                "(key TEXT PRIMARY KEY, value TEXT)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO automation_service_metadata (key, value) "
                "VALUES (:key, :value)"
            ),
            {
                "key": telemetry.TELEMETRY_CONSENT_METADATA_KEY,
                "value": '{"__anonymous__": true}',
            },
        )
    app = _app()
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    barrier = asyncio.Barrier(2)
    held_connections = []

    @app.get("/api/automation/v1/pool-probe")
    async def probe(session: AsyncSession = Depends(get_session)):
        await session.execute(text("SELECT 1"))
        await barrier.wait()
        return {"ok": True}

    async def capture(request, **kwargs):
        # This is the same second-session lookup route telemetry performs.
        held_connections.append(pool.checkedout())
        assert await telemetry.get_stored_telemetry_consent(request=request)

    monkeypatch.setattr(telemetry, "capture_api_route_event", capture)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            responses = await asyncio.gather(
                client.get("/api/automation/v1/pool-probe"),
                client.get("/api/automation/v1/pool-probe"),
            )
        assert [response.status_code for response in responses] == [200, 200]
        # One peer's telemetry may already hold a slot, but both request
        # connections must not remain held while either callback starts.
        assert held_connections and max(held_connections) < 2
        assert pool.checkedout() == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_route_telemetry_preserves_status_and_exception(monkeypatch, failure):
    app = _app()
    captured = []

    @app.get("/api/automation/v1/status-probe", status_code=201)
    async def probe():
        if failure:
            raise ValueError("test failure")
        return {"ok": True}

    async def capture(request, **kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(telemetry, "capture_api_route_event", capture)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        if failure:
            with pytest.raises(ValueError, match="test failure"):
                await client.get("/api/automation/v1/status-probe")
        else:
            assert (
                await client.get("/api/automation/v1/status-probe")
            ).status_code == 201
    assert len(captured) == 1
    assert captured[0]["status_code"] == (500 if failure else 201)
    assert captured[0].get("exception_type") == ("ValueError" if failure else None)
    assert captured[0]["duration_ms"] >= 0
