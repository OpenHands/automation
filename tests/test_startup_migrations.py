"""Tests for running migrations on startup."""

import asyncio
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

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


def _url(server: dict, database: str) -> URL:
    return URL.create("postgresql+pg8000", database=database, **server)


def _run_admin_sql(server: dict, statement: str) -> None:
    engine = create_engine(
        _url(server, "postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
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
            other_connections = conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid()"
                )
            ).scalar()
            lock_free = conn.execute(
                text(f"SELECT pg_try_advisory_lock({MIGRATION_LOCK_ID})")
            ).scalar()
    finally:
        engine.dispose()
    assert applied == heads
    assert other_connections == 0
    assert lock_free


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

    # A connection left open would keep the database busy for other replicas.
    _assert_at_head_and_released(postgres_server, empty_database)


async def test_service_and_another_replica_can_migrate_at_once(
    point_migrations_at, empty_database, postgres_server
):
    """The advisory lock makes one wait for the other, then find head."""
    point_migrations_at(empty_database)
    other_replica = await _upgrade_in_another_process()

    _, returncode = await asyncio.gather(
        app_module.run_migrations(get_config().service), other_replica.wait()
    )

    assert returncode == 0
    _assert_at_head_and_released(postgres_server, empty_database)


async def test_run_migrations_raises_the_database_error(point_migrations_at):
    point_migrations_at("no_such_database")

    with pytest.raises(RuntimeError, match="no_such_database"):
        await app_module.run_migrations(get_config().service)
