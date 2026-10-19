"""Tests for running migrations on startup."""

import asyncio
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool
from starlette.datastructures import State

from openhands.automation import app as app_module
from openhands.automation.config import clear_config_cache, get_config


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_LOCK_ID = 849320147


@pytest.fixture(autouse=True)
def reset_config_cache():
    clear_config_cache()
    yield
    clear_config_cache()


@pytest.fixture
def idle_startup(monkeypatch):
    """Run the lifespan with a fake engine and background loops that exit."""
    monkeypatch.delenv("AUTOMATION_RUN_MIGRATIONS_ON_STARTUP", raising=False)
    # The lifespan sets process-wide state; keep it from leaking into later tests.
    monkeypatch.setattr(app_module, "set_sqlite_mode", MagicMock())
    monkeypatch.setattr(app_module.app, "state", State())
    migrate = AsyncMock()
    monkeypatch.setattr(app_module, "run_migrations", migrate)
    for loop in (
        "scheduler_loop",
        "dispatcher_loop",
        "watchdog_loop",
        "git_sync_loop",
        "stream_supervisor_loop",
    ):
        monkeypatch.setattr(app_module, loop, AsyncMock())
    engine_result = MagicMock(is_sqlite=False, dispose=AsyncMock())
    monkeypatch.setattr(
        app_module, "create_engine", AsyncMock(return_value=engine_result)
    )
    return migrate, engine_result


@pytest.mark.parametrize(
    ("flag", "is_sqlite", "migrates"),
    [
        (None, False, False),
        ("false", False, False),
        ("true", False, True),
        ("1", False, True),
        (None, True, True),
    ],
)
async def test_lifespan_migrates_sqlite_always_and_postgres_when_enabled(
    monkeypatch, idle_startup, flag, is_sqlite, migrates
):
    migrate, engine_result = idle_startup
    engine_result.is_sqlite = is_sqlite
    if flag is not None:
        monkeypatch.setenv("AUTOMATION_RUN_MIGRATIONS_ON_STARTUP", flag)

    async with app_module.lifespan(app_module.app):
        pass

    assert migrate.await_count == (1 if migrates else 0)


@pytest.fixture
def postgres_server(postgres_container):
    return {
        "host": postgres_container.get_container_host_ip(),
        "port": int(postgres_container.get_exposed_port(5432)),
        "username": postgres_container.username,
        "password": postgres_container.password,
    }


def _url(server: dict, database: str, driver: str = "postgresql+pg8000") -> URL:
    return URL.create(driver, database=database, **server)


def _run_admin_sql(server: dict, statement: str, database: str = "postgres") -> None:
    engine = create_engine(
        _url(server, database), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        with engine.connect() as conn:
            conn.execute(text(statement))
    finally:
        engine.dispose()


@pytest.fixture
def point_migrations_at(monkeypatch, postgres_server):
    """Point migrations/env.py at a database on the test server."""

    def _point(database: str) -> None:
        for key in (
            "AUTOMATION_DB_URL",
            "AUTOMATION_GCP_DB_INSTANCE",
            "GCP_DB_INSTANCE",
            "AUTOMATION_DB_SSL_MODE",
            "DB_SSL_MODE",
            "PGSSLMODE",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("AUTOMATION_DB_HOST", postgres_server["host"])
        monkeypatch.setenv("AUTOMATION_DB_PORT", str(postgres_server["port"]))
        monkeypatch.setenv("AUTOMATION_DB_USER", postgres_server["username"])
        monkeypatch.setenv("AUTOMATION_DB_PASS", postgres_server["password"])
        monkeypatch.setenv("AUTOMATION_DB_NAME", database)

    return _point


@pytest.fixture
def empty_database(postgres_server):
    name = f"automation_test_{uuid.uuid4().hex[:12]}"
    _run_admin_sql(postgres_server, f'CREATE DATABASE "{name}"')
    try:
        yield name
    finally:
        _run_admin_sql(
            postgres_server, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'
        )


def _assert_at_head_and_released(server: dict, database: str) -> None:
    heads = set(ScriptDirectory(str(REPO_ROOT / "migrations")).get_heads())
    engine = create_engine(_url(server, database), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            applied = set(
                conn.execute(text("SELECT version_num FROM alembic_version")).scalars()
            )
            lock_free = conn.execute(
                text(f"SELECT pg_try_advisory_lock({MIGRATION_LOCK_ID})")
            ).scalar()
    finally:
        engine.dispose()
    assert applied == heads
    assert lock_free


def _migration_lock_waiters(server: dict, database: str) -> int:
    engine = create_engine(_url(server, database), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return conn.execute(
                text(
                    "SELECT count(*) FROM pg_locks"
                    " WHERE locktype = 'advisory' AND objid = :lock AND NOT granted"
                    " AND database = ("
                    "SELECT oid FROM pg_database WHERE datname = current_database())"
                ),
                {"lock": MIGRATION_LOCK_ID},
            ).scalar_one()
    finally:
        engine.dispose()


async def _wait_for_migration_lock_waiters(
    server: dict, database: str, count: int
) -> None:
    for _ in range(600):
        if _migration_lock_waiters(server, database) == count:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{count} replicas never queued on the migration lock")


async def _upgrade_in_another_process() -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "upgrade",
        "head",
        cwd=REPO_ROOT,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def test_run_migrations_brings_an_empty_database_to_head(
    point_migrations_at, empty_database, postgres_server
):
    point_migrations_at(empty_database)

    await app_module.run_migrations(get_config().service)

    # A connection left open would keep the lock and block other replicas.
    _assert_at_head_and_released(postgres_server, empty_database)


async def test_a_replica_waiting_on_the_lock_finds_the_database_at_head(
    point_migrations_at, empty_database, postgres_server
):
    """The waiting replica must not read the schema before the first commits."""
    point_migrations_at(empty_database)

    def slow_commit(conn) -> None:
        # Widen the gap between finishing the upgrade and committing it.
        time.sleep(1)

    gate = create_engine(_url(postgres_server, empty_database), poolclass=NullPool)
    event.listen(Engine, "commit", slow_commit)
    try:
        with gate.connect() as conn:
            # Hold the lock so both replicas queue on it, the service first.
            # PostgreSQL grants it to waiters in the order they asked.
            conn.execute(text(f"SELECT pg_advisory_lock({MIGRATION_LOCK_ID})"))
            service = asyncio.create_task(
                app_module.run_migrations(get_config().service)
            )
            await _wait_for_migration_lock_waiters(postgres_server, empty_database, 1)
            other_replica = await _upgrade_in_another_process()
            await _wait_for_migration_lock_waiters(postgres_server, empty_database, 2)
            conn.execute(text(f"SELECT pg_advisory_unlock({MIGRATION_LOCK_ID})"))
        await service
        returncode = await other_replica.wait()
    finally:
        event.remove(Engine, "commit", slow_commit)
        gate.dispose()

    assert returncode == 0
    _assert_at_head_and_released(postgres_server, empty_database)


async def test_a_failed_migration_reports_its_own_error(
    point_migrations_at, empty_database, postgres_server
):
    point_migrations_at(empty_database)
    # The first migration creates this table.
    _run_admin_sql(postgres_server, "CREATE TABLE automations (id int)", empty_database)

    with pytest.raises(RuntimeError, match='relation "automations" already exists'):
        await app_module.run_migrations(get_config().service)


@pytest.mark.parametrize("query", ["", "?ssl=disable"])
async def test_run_migrations_accepts_the_async_db_url_the_service_uses(
    monkeypatch, point_migrations_at, empty_database, postgres_server, query
):
    point_migrations_at("unused")
    url = _url(postgres_server, empty_database, "postgresql+asyncpg")
    monkeypatch.setenv(
        "AUTOMATION_DB_URL", url.render_as_string(hide_password=False) + query
    )

    await app_module.run_migrations(get_config().service)

    _assert_at_head_and_released(postgres_server, empty_database)


async def test_run_migrations_raises_the_database_error(point_migrations_at):
    point_migrations_at("no_such_database")

    with pytest.raises(RuntimeError, match="no_such_database"):
        await app_module.run_migrations(get_config().service)
