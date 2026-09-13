"""Tests for the startup migration gate in the application lifespan."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from openhands.automation import app as app_module
from openhands.automation.config import ServiceSettings
from openhands.automation.db import EngineResult, set_sqlite_mode


POSTGRES_URL = "postgresql+asyncpg://user:pass@db.example.com/automations"
SQLITE_URL = "sqlite+aiosqlite:///:memory:"

BACKGROUND_LOOPS = (
    "scheduler_loop",
    "dispatcher_loop",
    "watchdog_loop",
    "git_sync_loop",
    "stream_supervisor_loop",
)


async def _idle_loop(*args, shutdown_event: asyncio.Event, **kwargs):
    await shutdown_event.wait()


@pytest.fixture(autouse=True)
def reset_sqlite_mode():
    yield
    set_sqlite_mode(False)


async def _run_lifespan(monkeypatch, *, db_url, is_sqlite, auto_migrate):
    """Start and stop the lifespan, returning the mocked alembic upgrade."""
    settings = ServiceSettings(db_url=db_url, auto_migrate=auto_migrate)
    engine_result = EngineResult(
        engine=create_async_engine(SQLITE_URL), is_sqlite=is_sqlite
    )

    async def fake_create_engine(_settings=None):
        return engine_result

    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(app_module, "create_engine", fake_create_engine)
    for loop_name in BACKGROUND_LOOPS:
        monkeypatch.setattr(app_module, loop_name, _idle_loop)

    upgrade = MagicMock()
    with patch("alembic.command.upgrade", upgrade):
        async with app_module.lifespan(FastAPI()):
            pass
    return upgrade


class TestStartupMigrationGate:
    """AUTOMATION_AUTO_MIGRATE decides whether startup runs migrations."""

    async def test_postgres_does_not_migrate_by_default(self, monkeypatch):
        upgrade = await _run_lifespan(
            monkeypatch, db_url=POSTGRES_URL, is_sqlite=False, auto_migrate=None
        )

        upgrade.assert_not_called()

    async def test_postgres_migrates_when_opted_in(self, monkeypatch):
        upgrade = await _run_lifespan(
            monkeypatch, db_url=POSTGRES_URL, is_sqlite=False, auto_migrate=True
        )

        upgrade.assert_called_once()
        config = upgrade.call_args.args[0]
        assert config.get_main_option("sqlalchemy.url") == (
            "postgresql+pg8000://user:pass@db.example.com/automations"
        )

    async def test_postgres_does_not_migrate_when_opted_out(self, monkeypatch):
        upgrade = await _run_lifespan(
            monkeypatch, db_url=POSTGRES_URL, is_sqlite=False, auto_migrate=False
        )

        upgrade.assert_not_called()

    async def test_sqlite_migrates_by_default(self, monkeypatch):
        upgrade = await _run_lifespan(
            monkeypatch, db_url=SQLITE_URL, is_sqlite=True, auto_migrate=None
        )

        upgrade.assert_called_once()
        config = upgrade.call_args.args[0]
        assert config.get_main_option("sqlalchemy.url") == "sqlite:///:memory:"

    async def test_sqlite_does_not_migrate_when_opted_out(self, monkeypatch):
        upgrade = await _run_lifespan(
            monkeypatch, db_url=SQLITE_URL, is_sqlite=True, auto_migrate=False
        )

        upgrade.assert_not_called()
