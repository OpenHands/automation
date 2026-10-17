"""The request session commits before the client receives the response.

Regression test for https://github.com/OpenHands/automation/issues/594: with
the default request scope, ``get_session`` committed after the response was
sent, so on SQLite in WAL mode a dependent request could read the pre-commit
snapshot.
"""

import sqlite3
from pathlib import Path

from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from openhands.automation.app import app
from openhands.automation.auth import authenticate_request
from openhands.automation.db import get_session
from openhands.automation.models import Base
from openhands.automation.storage import get_file_store
from openhands.automation.storage.local import LocalFileStore


def _get_session_scopes(dependant) -> list[str | None]:
    scopes = []
    for sub in dependant.dependencies:
        if sub.call is get_session:
            scopes.append(sub.scope)
        scopes.extend(_get_session_scopes(sub))
    return scopes


def test_every_route_session_is_function_scoped():
    scopes = {
        f"{','.join(sorted(route.methods))} {route.path}": _get_session_scopes(
            route.dependant
        )
        for route in app.routes
        if isinstance(route, APIRoute)
    }
    # The guard must have found the dependency it polices, or it passes vacuously.
    assert scopes["POST /api/automation/v1/uploads"] == ["function"]
    assert scopes["PUT /api/automation/v1/kv/{key}"]
    unscoped = [
        route
        for route, found in scopes.items()
        if any(scope != "function" for scope in found)
    ]
    assert unscoped == []


async def test_upload_row_is_committed_before_the_response_starts(
    tmp_path: Path, mock_authenticated_user
):
    database = tmp_path / "automation.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    previous_factory = getattr(app.state, "session_factory", None)
    app.state.session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async def override_authenticate():
        return mock_authenticated_user

    app.dependency_overrides[authenticate_request] = override_authenticate
    app.dependency_overrides[get_file_store] = lambda: LocalFileStore(
        str(tmp_path / "storage")
    )

    body = b"\x1f\x8b" + b"\x00" * 64
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/automation/v1/uploads",
        "raw_path": b"/api/automation/v1/uploads",
        "query_string": b"name=bundle.tar.gz",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/gzip"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    observed: list[tuple[int, int]] = []

    async def send(message):
        if message["type"] == "http.response.start":
            # A separate connection sees only committed rows, as a second
            # client request would.
            with sqlite3.connect(database) as reader:
                (rows,) = reader.execute(
                    "SELECT count(*) FROM tarball_uploads"
                ).fetchone()
            observed.append((message["status"], rows))

    try:
        await app(scope, receive, send)
    finally:
        app.dependency_overrides.clear()
        app.state.session_factory = previous_factory
        await engine.dispose()

    assert observed == [(201, 1)]
