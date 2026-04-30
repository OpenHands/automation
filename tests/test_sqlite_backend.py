"""Tests for SQLite database backend support.

Verifies that the automation service works correctly with SQLite as the
database backend, enabling local/open-source deployments without PostgreSQL.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from automation.config import Settings
from automation.db import EngineResult, create_engine as create_db_engine
from automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
    Base,
    CustomWebhook,
    TarballUpload,
    UploadStatus,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_url(tmp_path):
    """Return a temporary SQLite database URL."""
    db_path = tmp_path / "test_automations.db"
    return f"sqlite+aiosqlite:///{db_path}"


@pytest.fixture
async def sqlite_engine(sqlite_url):
    """Create an async SQLite engine and initialise the schema."""
    engine = create_async_engine(sqlite_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def sqlite_session_factory(sqlite_engine):
    """Create an async session factory backed by SQLite."""
    return async_sessionmaker(
        sqlite_engine, class_=AsyncSession, expire_on_commit=False
    )


@pytest.fixture
async def sqlite_session(sqlite_session_factory):
    """Yield a single async SQLite session."""
    async with sqlite_session_factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestSQLiteConfig:
    """Settings.is_sqlite correctly detects SQLite URLs."""

    def test_is_sqlite_with_sqlite_url(self):
        settings = Settings(db_url="sqlite+aiosqlite:///./automations.db")
        assert settings.is_sqlite is True

    def test_is_sqlite_with_postgres_url(self):
        settings = Settings(db_url="postgresql+asyncpg://u:p@host/db")
        assert settings.is_sqlite is False

    def test_is_sqlite_with_no_url(self):
        settings = Settings(db_url=None)
        assert settings.is_sqlite is False


# ---------------------------------------------------------------------------
# Engine creation tests
# ---------------------------------------------------------------------------


class TestSQLiteEngine:
    """create_engine() returns a working SQLite engine."""

    async def test_create_sqlite_engine(self, sqlite_url):
        settings = Settings(db_url=sqlite_url)
        result = await create_db_engine(settings)
        assert isinstance(result, EngineResult)
        assert result.connector is None
        # Verify the engine actually works
        async with result.engine.begin() as conn:
            row = await conn.execute(text("SELECT 1"))
            assert row.scalar() == 1
        await result.dispose()

    async def test_sqlite_wal_mode_enabled(self, sqlite_url):
        """WAL journal mode is set on new connections."""
        settings = Settings(db_url=sqlite_url)
        result = await create_db_engine(settings)
        async with result.engine.begin() as conn:
            row = await conn.execute(text("PRAGMA journal_mode"))
            mode = row.scalar()
            assert mode == "wal"
        await result.dispose()

    async def test_sqlite_foreign_keys_enabled(self, sqlite_url):
        """Foreign key enforcement is turned on."""
        settings = Settings(db_url=sqlite_url)
        result = await create_db_engine(settings)
        async with result.engine.begin() as conn:
            row = await conn.execute(text("PRAGMA foreign_keys"))
            assert row.scalar() == 1
        await result.dispose()


# ---------------------------------------------------------------------------
# Model CRUD tests (verifies JSON columns, indexes, etc. work on SQLite)
# ---------------------------------------------------------------------------


class TestSQLiteModels:
    """Basic CRUD operations work against an SQLite backend."""

    async def test_create_automation(self, sqlite_session: AsyncSession):
        """Can insert and retrieve an Automation with a JSON trigger."""
        automation = Automation(
            user_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            name="Test Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *"},
            tarball_path="oh-internal://abc",
            entrypoint="python main.py",
        )
        sqlite_session.add(automation)
        await sqlite_session.commit()
        await sqlite_session.refresh(automation)

        assert automation.id is not None
        assert automation.trigger["type"] == "cron"
        assert automation.trigger["schedule"] == "0 9 * * *"

    async def test_create_run(self, sqlite_session: AsyncSession):
        """Can insert an AutomationRun linked to an Automation."""
        automation = Automation(
            user_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            name="Run Test",
            trigger={"type": "cron", "schedule": "*/5 * * * *"},
            tarball_path="oh-internal://def",
            entrypoint="python main.py",
        )
        sqlite_session.add(automation)
        await sqlite_session.flush()

        run = AutomationRun(
            automation_id=automation.id,
            status=AutomationRunStatus.PENDING,
        )
        sqlite_session.add(run)
        await sqlite_session.commit()
        await sqlite_session.refresh(run)

        assert run.id is not None
        assert run.status == AutomationRunStatus.PENDING
        assert run.automation_id == automation.id

    async def test_run_with_event_payload(self, sqlite_session: AsyncSession):
        """JSON event_payload column works on SQLite."""
        automation = Automation(
            user_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            name="Event Test",
            trigger={"type": "event", "source": "github"},
            tarball_path="oh-internal://ghi",
            entrypoint="python main.py",
        )
        sqlite_session.add(automation)
        await sqlite_session.flush()

        payload = {"action": "opened", "number": 42, "nested": {"key": "val"}}
        run = AutomationRun(
            automation_id=automation.id,
            status=AutomationRunStatus.RUNNING,
            event_payload=payload,
        )
        sqlite_session.add(run)
        await sqlite_session.commit()
        await sqlite_session.refresh(run)

        assert run.event_payload == payload
        assert run.event_payload["nested"]["key"] == "val"

    async def test_create_tarball_upload(self, sqlite_session: AsyncSession):
        """TarballUpload model works on SQLite."""
        upload = TarballUpload(
            user_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            name="test-tarball",
            status=UploadStatus.COMPLETED,
            size_bytes=1024,
            storage_path="uploads/test.tar.gz",
        )
        sqlite_session.add(upload)
        await sqlite_session.commit()
        await sqlite_session.refresh(upload)

        assert upload.id is not None
        assert upload.status == UploadStatus.COMPLETED

    async def test_create_custom_webhook(self, sqlite_session: AsyncSession):
        """CustomWebhook model works on SQLite."""
        webhook = CustomWebhook(
            org_id=uuid.uuid4(),
            name="Test Webhook",
            source="stripe",
            webhook_secret="whsec_test123",
        )
        sqlite_session.add(webhook)
        await sqlite_session.commit()
        await sqlite_session.refresh(webhook)

        assert webhook.id is not None
        assert webhook.source == "stripe"


# ---------------------------------------------------------------------------
# Scheduler compatibility (FOR UPDATE SKIP LOCKED is skipped on SQLite)
# ---------------------------------------------------------------------------


class TestSQLiteScheduler:
    """Scheduler polling works on SQLite without FOR UPDATE SKIP LOCKED."""

    async def test_poll_and_schedule(self, sqlite_session_factory):
        """poll_and_schedule creates PENDING runs on SQLite."""
        from automation.scheduler import poll_and_schedule

        # Create a due automation
        now = datetime.now(timezone.utc)
        async with sqlite_session_factory() as session:
            automation = Automation(
                user_id=uuid.uuid4(),
                org_id=uuid.uuid4(),
                name="Scheduler Test",
                trigger={
                    "type": "cron",
                    "schedule": "* * * * *",
                    "timezone": "UTC",
                },
                tarball_path="oh-internal://test",
                entrypoint="python main.py",
                enabled=True,
                last_triggered_at=now - timedelta(hours=1),
                last_polled_at=None,
            )
            session.add(automation)
            await session.commit()

        runs = await poll_and_schedule(sqlite_session_factory)
        assert len(runs) >= 1
        assert all(r.status == AutomationRunStatus.PENDING for r in runs)

    async def test_fetch_enabled_automations_no_lock(self, sqlite_session_factory):
        """_fetch_enabled_automations works on SQLite (no row locking)."""
        from automation.scheduler import _fetch_enabled_automations

        now = datetime.now(timezone.utc)
        async with sqlite_session_factory() as session:
            automation = Automation(
                user_id=uuid.uuid4(),
                org_id=uuid.uuid4(),
                name="Fetch Test",
                trigger={"type": "cron", "schedule": "0 0 * * *", "timezone": "UTC"},
                tarball_path="oh-internal://test",
                entrypoint="python main.py",
                enabled=True,
                last_polled_at=None,
            )
            session.add(automation)
            await session.commit()

        async with sqlite_session_factory() as session:
            poll_threshold = now - timedelta(seconds=60)
            automations = await _fetch_enabled_automations(
                session, batch_size=10, poll_threshold=poll_threshold
            )
            assert len(automations) >= 1


# ---------------------------------------------------------------------------
# Dispatcher compatibility
# ---------------------------------------------------------------------------


class TestSQLiteDispatcher:
    """Dispatcher polling works on SQLite without FOR UPDATE SKIP LOCKED."""

    async def test_poll_pending_runs_no_lock(self, sqlite_session_factory):
        """_poll_pending_runs works on SQLite (no row locking)."""
        from automation.dispatcher import _poll_pending_runs

        async with sqlite_session_factory() as session:
            automation = Automation(
                user_id=uuid.uuid4(),
                org_id=uuid.uuid4(),
                name="Dispatcher Test",
                trigger={"type": "cron", "schedule": "0 0 * * *"},
                tarball_path="oh-internal://test",
                entrypoint="python main.py",
                enabled=True,
            )
            session.add(automation)
            await session.flush()

            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.PENDING,
            )
            session.add(run)
            await session.commit()

        async with sqlite_session_factory() as session:
            runs = await _poll_pending_runs(session, batch_size=10)
            assert len(runs) == 1
            assert runs[0].status == AutomationRunStatus.PENDING
