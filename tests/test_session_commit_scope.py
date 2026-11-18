"""The request session commits before the client receives the response.

Regression test for https://github.com/OpenHands/automation/issues/594: with
the default request scope, ``get_session`` committed after the response was
sent, so on SQLite in WAL mode a dependent request could read the pre-commit
snapshot.
"""

import sqlite3
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, Depends, FastAPI, Response
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.app import app
from openhands.automation.auth import authenticate_request
from openhands.automation.db import _create_sqlite_engine, get_session
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


@pytest.mark.parametrize("commit_fails", [False, True])
async def test_upload_commits_or_rolls_back_before_the_response_starts(
    tmp_path: Path, mock_authenticated_user, commit_fails: bool
):
    database = tmp_path / "automation.sqlite"
    engine_result = _create_sqlite_engine(f"sqlite+aiosqlite:///{database}")
    engine = engine_result.engine
    async with engine.begin() as connection:
        journal_mode = await connection.execute(text("PRAGMA journal_mode"))
        assert journal_mode.scalar_one() == "wal"
        await connection.run_sync(Base.metadata.create_all)
        if commit_fails:
            await connection.execute(
                text("CREATE TABLE commit_parent (id INTEGER PRIMARY KEY)")
            )
            await connection.execute(
                text(
                    "CREATE TABLE commit_guard ("
                    "parent_id INTEGER REFERENCES commit_parent(id) "
                    "DEFERRABLE INITIALLY DEFERRED)"
                )
            )
            await connection.execute(
                text(
                    "CREATE TRIGGER reject_upload AFTER INSERT ON tarball_uploads "
                    "BEGIN INSERT INTO commit_guard (parent_id) VALUES (1); END"
                )
            )
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
        if commit_fails:
            with pytest.raises(IntegrityError):
                await app(scope, receive, send)
        else:
            await app(scope, receive, send)
    finally:
        app.dependency_overrides.clear()
        app.state.session_factory = previous_factory
        await engine_result.dispose()

    with sqlite3.connect(database) as reader:
        (rows,) = reader.execute("SELECT count(*) FROM tarball_uploads").fetchone()
    if commit_fails:
        assert all(status != 201 and count == 0 for status, count in observed)
        assert rows == 0
    else:
        assert observed == [(201, 1)]
        assert rows == 1


@pytest.mark.parametrize("commit_fails", [False, True])
async def test_background_task_runs_only_after_successful_commit(
    tmp_path: Path, commit_fails: bool
):
    database = tmp_path / "background.sqlite"
    engine_result = _create_sqlite_engine(f"sqlite+aiosqlite:///{database}")
    engine = engine_result.engine
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE writes (id INTEGER PRIMARY KEY)"))
        if commit_fails:
            await connection.execute(
                text("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            )
            await connection.execute(
                text(
                    "CREATE TABLE commit_guard ("
                    "parent_id INTEGER REFERENCES parent(id) "
                    "DEFERRABLE INITIALLY DEFERRED)"
                )
            )
            await connection.execute(
                text(
                    "CREATE TRIGGER reject_write AFTER INSERT ON writes "
                    "BEGIN INSERT INTO commit_guard (parent_id) VALUES (1); END"
                )
            )

    test_app = FastAPI()
    test_app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    task_observations: list[int] = []

    def observe_committed_row():
        with sqlite3.connect(database) as reader:
            (rows,) = reader.execute("SELECT count(*) FROM writes").fetchone()
            task_observations.append(rows)

    @test_app.post("/write")
    async def write(
        background_tasks: BackgroundTasks,
        session: AsyncSession = Depends(get_session, scope="function"),
    ) -> Response:
        await session.execute(text("INSERT INTO writes (id) VALUES (1)"))
        background_tasks.add_task(observe_committed_row)
        return Response(status_code=201)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            if commit_fails:
                with pytest.raises(IntegrityError):
                    await client.post("/write")
            else:
                response = await client.post("/write")
                assert response.status_code == 201
    finally:
        await engine_result.dispose()

    with sqlite3.connect(database) as reader:
        (rows,) = reader.execute("SELECT count(*) FROM writes").fetchone()
    assert rows == (0 if commit_fails else 1)
    assert task_observations == ([] if commit_fails else [1])
