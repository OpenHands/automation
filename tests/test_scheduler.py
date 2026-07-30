"""Tests for the scheduler module."""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from openhands.automation.db import set_sqlite_mode, using_sqlite
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
    Base,
)
from openhands.automation.scheduler import (
    POLL_INTERVAL_SECONDS,
    poll_and_schedule,
    scheduler_loop,
)
from openhands.automation.utils import (
    get_next_fire_time,
    get_prev_fire_time,
    is_automation_due,
    utcnow,
)
from openhands.automation.utils.run import create_pending_run


UTC = UTC

# Test UUIDs
TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")


def _utc(*args: int) -> datetime:
    """Create a UTC-aware datetime for test assertions."""
    return datetime(*args, tzinfo=UTC)


@pytest.fixture
async def sqlite_session_factory():
    """Create an isolated in-memory SQLite session factory."""
    previous_sqlite_mode = using_sqlite()
    sqlalchemy_logger = logging.getLogger("sqlalchemy.engine")
    previous_sqlalchemy_level = sqlalchemy_logger.level
    set_sqlite_mode(True)
    sqlalchemy_logger.setLevel(logging.WARNING)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    finally:
        await engine.dispose()
        set_sqlite_mode(previous_sqlite_mode)
        sqlalchemy_logger.setLevel(previous_sqlalchemy_level)


@pytest.fixture
def scheduler_telemetry_events(monkeypatch):
    """Capture scheduler telemetry without invoking the telemetry backend."""
    event_names: list[str] = []

    async def record_event(event_name, *args, **kwargs):
        event_names.append(event_name)

    monkeypatch.setattr(
        "openhands.automation.scheduler.capture_automation_event",
        record_event,
    )
    return event_names


def _due_automation(name: str, now: datetime) -> Automation:
    return Automation(
        user_id=TEST_USER_ID,
        org_id=TEST_ORG_ID,
        name=name,
        trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
        tarball_path="s3://bucket/code.tar.gz",
        entrypoint="uv run main.py",
        enabled=True,
        last_triggered_at=now - timedelta(minutes=2),
        created_at=now - timedelta(minutes=5),
    )


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite timestamps for deterministic comparisons."""
    return value.replace(tzinfo=UTC)


class TestGetNextFireTime:
    """Tests for get_next_fire_time function."""

    def test_next_fire_time_daily(self):
        """Daily cron schedule returns correct next fire time."""
        # Every day at 9:00 AM UTC
        base_time = _utc(2026, 3, 15, 8, 0, 0)
        next_fire = get_next_fire_time("0 9 * * *", base_time=base_time)

        assert next_fire == _utc(2026, 3, 15, 9, 0, 0)

    def test_next_fire_time_weekly(self):
        """Weekly cron schedule returns correct next fire time."""
        # Every Friday at 9:00 AM (Friday = 5)
        # March 15, 2026 is a Sunday
        base_time = _utc(2026, 3, 15, 10, 0, 0)
        next_fire = get_next_fire_time("0 9 * * 5", base_time=base_time)

        # Next Friday is March 20, 2026
        assert next_fire == _utc(2026, 3, 20, 9, 0, 0)

    def test_next_fire_time_already_past_today(self):
        """Returns tomorrow if today's fire time has passed."""
        # Every day at 9:00 AM, but current time is 10:00 AM
        base_time = _utc(2026, 3, 15, 10, 0, 0)
        next_fire = get_next_fire_time("0 9 * * *", base_time=base_time)

        # Should be tomorrow at 9:00 AM
        assert next_fire == _utc(2026, 3, 16, 9, 0, 0)

    def test_next_fire_time_every_minute(self):
        """Every minute schedule works correctly."""
        base_time = _utc(2026, 3, 15, 10, 30, 45)
        next_fire = get_next_fire_time("* * * * *", base_time=base_time)

        assert next_fire == _utc(2026, 3, 15, 10, 31, 0)

    def test_next_fire_time_with_timezone(self):
        """Timezone is correctly applied to cron schedule."""
        # Schedule: 9:00 AM America/New_York
        # Base time: 12:00 UTC on March 15, 2026 (which is 8:00 AM EDT)
        # March 15 is after DST starts (March 8, 2026), so EDT = UTC-4
        base_time = _utc(2026, 3, 15, 12, 0, 0)  # 12:00 UTC = 8:00 AM EDT
        next_fire = get_next_fire_time(
            "0 9 * * *", timezone="America/New_York", base_time=base_time
        )

        # Next fire should be 9:00 AM EDT = 13:00 UTC
        assert next_fire == _utc(2026, 3, 15, 13, 0, 0)

    def test_next_fire_time_timezone_different_day(self):
        """Timezone conversion can shift the fire time to a different day."""
        # Schedule: 2:00 AM America/Los_Angeles (UTC-8 in winter, UTC-7 in summer)
        # Base time: 8:00 UTC on March 15, 2026
        # March 15 is after DST starts, so PDT = UTC-7
        # 8:00 UTC = 1:00 AM PDT, so next 2:00 AM PDT is same day
        base_time = _utc(2026, 3, 15, 8, 0, 0)  # 8:00 UTC = 1:00 AM PDT
        next_fire = get_next_fire_time(
            "0 2 * * *", timezone="America/Los_Angeles", base_time=base_time
        )

        # Next fire: 2:00 AM PDT = 9:00 UTC same day
        assert next_fire == _utc(2026, 3, 15, 9, 0, 0)


class TestGetPrevFireTime:
    """Tests for get_prev_fire_time function."""

    def test_prev_fire_time_daily(self):
        """Daily cron schedule returns correct previous fire time."""
        # Every day at 9:00 AM UTC
        base_time = _utc(2026, 3, 15, 10, 0, 0)  # 10:00 UTC
        prev_fire = get_prev_fire_time("0 9 * * *", base_time=base_time)

        # Previous fire was 9:00 UTC same day
        assert prev_fire == _utc(2026, 3, 15, 9, 0, 0)

    def test_prev_fire_time_with_timezone(self):
        """Timezone is correctly applied when computing previous fire time."""
        # Schedule: 9:00 AM America/New_York
        # Base time: 14:00 UTC on March 15, 2026 (which is 10:00 AM EDT)
        # March 15 is after DST, so EDT = UTC-4
        base_time = _utc(2026, 3, 15, 14, 0, 0)  # 14:00 UTC = 10:00 AM EDT
        prev_fire = get_prev_fire_time(
            "0 9 * * *", timezone="America/New_York", base_time=base_time
        )

        # Previous fire was 9:00 AM EDT = 13:00 UTC same day
        assert prev_fire == _utc(2026, 3, 15, 13, 0, 0)


class TestIsAutomationDue:
    """Tests for is_automation_due function."""

    def test_disabled_automation_not_due(self):
        """Disabled automations are never due."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            trigger={"type": "cron", "schedule": "* * * * *"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run main.py",
            enabled=False,
        )

        assert is_automation_due(automation) is False

    def test_deleted_automation_not_due(self):
        """Deleted automations are never due."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            trigger={"type": "cron", "schedule": "* * * * *"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run main.py",
            enabled=True,
            deleted_at=utcnow(),
        )

        assert is_automation_due(automation) is False

    def test_non_cron_trigger_not_due(self):
        """Non-cron trigger types are not due (for now)."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            trigger={"type": "github", "event": "push"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run main.py",
            enabled=True,
        )

        assert is_automation_due(automation) is False

    def test_never_triggered_created_before_schedule_is_due(self):
        """Automation created before a scheduled time is due after that time passes."""
        # Created at 10:25, schedule is every 30 mins (0,30)
        # At 10:35, prev_fire_time is 10:30, which is after created_at (10:25)
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            trigger={"type": "cron", "schedule": "0,30 * * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run main.py",
            enabled=True,
            last_triggered_at=None,
            created_at=_utc(2026, 3, 15, 10, 25, 0),
        )

        now = _utc(2026, 3, 15, 10, 35, 0)
        assert is_automation_due(automation, now) is True

    def test_never_triggered_created_after_schedule_not_due(self):
        """Automation created after a scheduled time waits for next schedule."""
        # Created at 10:35, schedule is every 30 mins (0,30)
        # At 10:40, prev_fire_time is 10:30, which is BEFORE created_at (10:35)
        # Should NOT be due - wait for 11:00
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            trigger={"type": "cron", "schedule": "0,30 * * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run main.py",
            enabled=True,
            last_triggered_at=None,
            created_at=_utc(2026, 3, 15, 10, 35, 0),
        )

        now = _utc(2026, 3, 15, 10, 40, 0)
        assert is_automation_due(automation, now) is False

    def test_never_triggered_due_at_next_schedule(self):
        """Automation created after a scheduled time becomes due at next schedule."""
        # Created at 10:35, schedule is every 30 mins (0,30)
        # At 11:05, prev_fire_time is 11:00, which is after created_at (10:35)
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            trigger={"type": "cron", "schedule": "0,30 * * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run main.py",
            enabled=True,
            last_triggered_at=None,
            created_at=_utc(2026, 3, 15, 10, 35, 0),
        )

        now = _utc(2026, 3, 15, 11, 5, 0)
        assert is_automation_due(automation, now) is True

    def test_recently_triggered_not_due(self):
        """Automation triggered in current period is not due again."""
        # Every minute schedule
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run main.py",
            enabled=True,
            last_triggered_at=_utc(2026, 3, 15, 10, 30, 5),
        )

        # Same minute, later
        now = _utc(2026, 3, 15, 10, 30, 30)
        assert is_automation_due(automation, now) is False

    def test_automation_due_next_period(self):
        """Automation is due when a new period starts."""
        # Every minute schedule, last triggered at 10:29:05
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run main.py",
            enabled=True,
            last_triggered_at=_utc(2026, 3, 15, 10, 29, 5),
        )

        # Now at 10:30:30 - the 10:30 fire time should make it due
        now = _utc(2026, 3, 15, 10, 30, 30)
        assert is_automation_due(automation, now) is True

    def test_daily_automation_not_due_same_day(self):
        """Daily automation triggered today is not due again today."""
        # Every day at 9:00 AM
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run main.py",
            enabled=True,
            last_triggered_at=_utc(2026, 3, 15, 9, 0, 5),
        )

        # Later the same day
        now = _utc(2026, 3, 15, 14, 0, 0)
        assert is_automation_due(automation, now) is False

    def test_daily_automation_due_next_day(self):
        """Daily automation is due the next day."""
        # Every day at 9:00 AM
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run main.py",
            enabled=True,
            last_triggered_at=_utc(2026, 3, 15, 9, 0, 5),
        )

        # Next day after 9:00 AM
        now = _utc(2026, 3, 16, 9, 30, 0)
        assert is_automation_due(automation, now) is True

    def test_automation_due_with_timezone(self):
        """Automation with non-UTC timezone fires at correct time."""
        # Schedule: 9:00 AM America/New_York (EDT = UTC-4 in March)
        # Created at 12:00 UTC (8:00 AM EDT) - before 9 AM EDT
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            trigger={
                "type": "cron",
                "schedule": "0 9 * * *",
                "timezone": "America/New_York",
            },
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run main.py",
            enabled=True,
            last_triggered_at=None,
            created_at=_utc(2026, 3, 15, 12, 0, 0),  # 8:00 AM EDT
        )

        # At 12:30 UTC (8:30 AM EDT) - before 9 AM EDT, not due
        now_before = _utc(2026, 3, 15, 12, 30, 0)
        assert is_automation_due(automation, now_before) is False

        # At 13:30 UTC (9:30 AM EDT) - after 9 AM EDT, should be due
        now_after = _utc(2026, 3, 15, 13, 30, 0)
        assert is_automation_due(automation, now_after) is True

    def test_automation_not_due_with_timezone_before_schedule(self):
        """Automation with timezone is not due before its scheduled time."""
        # Schedule: 9:00 AM America/Los_Angeles (PDT = UTC-7 in March)
        # Created at 14:00 UTC (7:00 AM PDT)
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            trigger={
                "type": "cron",
                "schedule": "0 9 * * *",
                "timezone": "America/Los_Angeles",
            },
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run main.py",
            enabled=True,
            last_triggered_at=None,
            created_at=_utc(2026, 3, 15, 14, 0, 0),  # 7:00 AM PDT
        )

        # At 15:00 UTC (8:00 AM PDT) - still before 9 AM PDT
        now = _utc(2026, 3, 15, 15, 0, 0)
        assert is_automation_due(automation, now) is False

        # At 16:30 UTC (9:30 AM PDT) - after 9 AM PDT, should be due
        now_due = _utc(2026, 3, 15, 16, 30, 0)
        assert is_automation_due(automation, now_due) is True


class TestPollAndSchedule:
    """Tests for poll_and_schedule function (atomic poll + run creation)."""

    @pytest.mark.parametrize(
        "active_status",
        [AutomationRunStatus.PENDING, AutomationRunStatus.RUNNING],
    )
    async def test_poll_skips_due_automation_with_active_run(
        self,
        sqlite_session_factory,
        active_status,
        scheduler_telemetry_events,
        caplog,
    ):
        """An in-flight run prevents a second cron-created run."""
        now = _utc(2026, 7, 30, 12, 0, 30)
        original_last_triggered_at = now - timedelta(minutes=2)
        async with sqlite_session_factory() as session:
            automation = _due_automation("Blocked", now)
            session.add(automation)
            await session.flush()
            session.add(
                AutomationRun(
                    automation_id=automation.id,
                    status=active_status,
                )
            )
            await session.commit()
            automation_id = automation.id

        with caplog.at_level(logging.INFO, logger="automation.scheduler"):
            created_runs = await poll_and_schedule(sqlite_session_factory, now=now)

        assert created_runs == []
        assert scheduler_telemetry_events == []
        async with sqlite_session_factory() as session:
            run_count = await session.scalar(
                select(func.count())
                .select_from(AutomationRun)
                .where(AutomationRun.automation_id == automation_id)
            )
            assert run_count == 1
            updated = await session.get(Automation, automation_id)
            assert updated is not None
            assert _as_utc(updated.last_polled_at) == now
            assert _as_utc(updated.last_triggered_at) == original_last_triggered_at

        skip_record = next(
            record
            for record in caplog.records
            if record.getMessage() == "Skipping cron run for active automation"
        )
        assert skip_record.automation_id == str(automation_id)
        assert skip_record.existing_run_status == active_status.value
        assert skip_record.trigger_source == "cron"
        assert skip_record.skip_reason == "automation_run_in_flight"

    @pytest.mark.parametrize(
        "terminal_status",
        [
            AutomationRunStatus.COMPLETED,
            AutomationRunStatus.FAILED,
            AutomationRunStatus.CANCELLED,
            AutomationRunStatus.SKIPPED,
        ],
    )
    async def test_poll_overlap_guard_allows_terminal_run(
        self, sqlite_session_factory, terminal_status, scheduler_telemetry_events
    ):
        """Terminal history does not block a new cron run."""
        now = _utc(2026, 7, 30, 12, 0, 30)
        async with sqlite_session_factory() as session:
            automation = _due_automation("Terminal history", now)
            session.add(automation)
            await session.flush()
            session.add(
                AutomationRun(automation_id=automation.id, status=terminal_status)
            )
            await session.commit()
            automation_id = automation.id

        created_runs = await poll_and_schedule(sqlite_session_factory, now=now)

        assert [run.automation_id for run in created_runs] == [automation_id]
        assert scheduler_telemetry_events == [
            "automation_run_scheduled",
            "automation_run_created",
        ]

    async def test_poll_overlap_guard_allows_automation_without_run(
        self, sqlite_session_factory, scheduler_telemetry_events
    ):
        """A due automation without run history is scheduled normally."""
        now = _utc(2026, 7, 30, 12, 0, 30)
        async with sqlite_session_factory() as session:
            automation = _due_automation("Free", now)
            session.add(automation)
            await session.commit()
            automation_id = automation.id

        created_runs = await poll_and_schedule(sqlite_session_factory, now=now)

        assert [run.automation_id for run in created_runs] == [automation_id]
        assert scheduler_telemetry_events == [
            "automation_run_scheduled",
            "automation_run_created",
        ]

    async def test_poll_overlap_guard_is_per_automation(
        self, sqlite_session_factory, scheduler_telemetry_events
    ):
        """One batch query isolates active runs by automation."""
        now = _utc(2026, 7, 30, 12, 0, 30)
        async with sqlite_session_factory() as session:
            blocked = _due_automation("Blocked", now)
            free = _due_automation("Free", now)
            session.add_all([blocked, free])
            await session.flush()
            session.add(
                AutomationRun(
                    automation_id=blocked.id,
                    status=AutomationRunStatus.PENDING,
                )
            )
            await session.commit()
            blocked_id = blocked.id
            free_id = free.id

        active_run_queries: list[str] = []

        def capture_active_run_query(
            connection, cursor, statement, parameters, context, executemany
        ):
            if "FROM automation_runs" in statement:
                active_run_queries.append(statement)

        engine = sqlite_session_factory.kw["bind"]
        event.listen(
            engine.sync_engine, "before_cursor_execute", capture_active_run_query
        )
        try:
            created_runs = await poll_and_schedule(sqlite_session_factory, now=now)
        finally:
            event.remove(
                engine.sync_engine,
                "before_cursor_execute",
                capture_active_run_query,
            )

        assert [run.automation_id for run in created_runs] == [free_id]
        assert len(active_run_queries) == 1
        assert scheduler_telemetry_events == [
            "automation_run_scheduled",
            "automation_run_created",
        ]
        async with sqlite_session_factory() as session:
            blocked_run_count = await session.scalar(
                select(func.count())
                .select_from(AutomationRun)
                .where(AutomationRun.automation_id == blocked_id)
            )
            assert blocked_run_count == 1

    async def test_poll_overlap_guard_preserves_batch_rotation(
        self, sqlite_session_factory, scheduler_telemetry_events
    ):
        """A skipped automation rotates behind an eligible automation."""
        now = _utc(2026, 7, 30, 12, 0, 30)
        async with sqlite_session_factory() as session:
            blocked = _due_automation("Blocked first", now)
            blocked.last_polled_at = now - timedelta(hours=2)
            free = _due_automation("Free second", now)
            free.last_polled_at = now - timedelta(hours=1)
            session.add_all([blocked, free])
            await session.flush()
            session.add(
                AutomationRun(
                    automation_id=blocked.id,
                    status=AutomationRunStatus.RUNNING,
                )
            )
            await session.commit()
            free_id = free.id

        first_runs = await poll_and_schedule(
            sqlite_session_factory, batch_size=1, now=now
        )
        second_runs = await poll_and_schedule(
            sqlite_session_factory, batch_size=1, now=now
        )

        assert first_runs == []
        assert [run.automation_id for run in second_runs] == [free_id]
        assert scheduler_telemetry_events == [
            "automation_run_scheduled",
            "automation_run_created",
        ]

    async def test_poll_overlap_guard_leaves_not_due_automation_untriggered(
        self, sqlite_session_factory, scheduler_telemetry_events
    ):
        """A polled automation that is not due remains untriggered."""
        now = _utc(2026, 7, 30, 12, 0, 30)
        async with sqlite_session_factory() as session:
            automation = _due_automation("Not due", now)
            automation.last_triggered_at = now
            session.add(automation)
            await session.commit()
            automation_id = automation.id

        created_runs = await poll_and_schedule(sqlite_session_factory, now=now)

        assert created_runs == []
        assert scheduler_telemetry_events == []
        async with sqlite_session_factory() as session:
            updated = await session.get(Automation, automation_id)
            assert updated is not None
            assert _as_utc(updated.last_polled_at) == now
            assert _as_utc(updated.last_triggered_at) == now

    async def test_poll_creates_runs_for_due_automations(self, async_session_factory):
        """Creates pending runs for automations that are due."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Due Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                last_triggered_at=None,
                created_at=utcnow() - timedelta(minutes=5),
            )
            session.add(automation)
            await session.commit()
            automation_id = automation.id

        runs = await poll_and_schedule(async_session_factory)

        assert len(runs) == 1
        assert runs[0].automation_id == automation_id
        assert runs[0].status == AutomationRunStatus.PENDING

    async def test_poll_disables_impossible_cron_without_blocking_batch(
        self, async_session_factory
    ):
        """A bad stored cron row does not abort scheduling other automations."""
        now = utcnow()
        created_at = now - timedelta(minutes=5)

        async with async_session_factory() as session:
            invalid_automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Impossible Cron",
                trigger={"type": "cron", "schedule": "0 0 31 2 *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                last_polled_at=None,
                created_at=created_at,
            )
            due_automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Due Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                last_polled_at=None,
                last_triggered_at=None,
                created_at=created_at,
            )
            session.add_all([invalid_automation, due_automation])
            await session.commit()
            invalid_id = invalid_automation.id
            due_id = due_automation.id

        runs = await poll_and_schedule(async_session_factory, now=now)

        assert len(runs) == 1
        assert runs[0].automation_id == due_id

        async with async_session_factory() as session:
            result = await session.execute(
                select(Automation).where(Automation.id == invalid_id)
            )
            invalid_automation = result.scalars().one()
            assert invalid_automation.enabled is False
            assert invalid_automation.last_polled_at is not None

    async def test_poll_excludes_disabled(self, async_session_factory):
        """Disabled automations are not returned."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Disabled Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=False,
            )
            session.add(automation)
            await session.commit()

        runs = await poll_and_schedule(async_session_factory)

        assert len(runs) == 0

    async def test_poll_excludes_deleted(self, async_session_factory):
        """Deleted automations are not returned."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Deleted Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                deleted_at=utcnow(),
            )
            session.add(automation)
            await session.commit()

        runs = await poll_and_schedule(async_session_factory)

        assert len(runs) == 0

    async def test_poll_excludes_recently_triggered(self, async_session_factory):
        """Recently triggered automations are not returned as due."""
        now = utcnow()
        async with async_session_factory() as session:
            # Triggered AFTER the most recent cron fire time → not due.
            # Use 'now' as last_triggered_at so prev_fire_time is always earlier.
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Recently Triggered",
                trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                last_triggered_at=now,
            )
            session.add(automation)
            await session.commit()

        runs = await poll_and_schedule(async_session_factory)

        assert len(runs) == 0

    async def test_poll_updates_last_polled_at(self, async_session_factory):
        """Polling updates last_polled_at for due automations."""
        async with async_session_factory() as session:
            # Create an automation that IS due: every-minute schedule,
            # created well in the past so prev_fire_time > created_at.
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Test Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                last_polled_at=None,
                created_at=utcnow() - timedelta(minutes=5),
            )
            session.add(automation)
            await session.commit()
            automation_id = automation.id

        await poll_and_schedule(async_session_factory)

        async with async_session_factory() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(Automation).where(Automation.id == automation_id)
            )
            updated = result.scalars().first()
            assert updated.last_polled_at is not None

    async def test_poll_skips_recently_polled(self, async_session_factory):
        """Automations polled within POLL_INTERVAL_SECONDS are skipped."""
        now = utcnow()
        recent_poll_time = now - timedelta(seconds=POLL_INTERVAL_SECONDS // 2)

        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Recently Polled",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                last_polled_at=recent_poll_time,
            )
            session.add(automation)
            await session.commit()

        runs = await poll_and_schedule(async_session_factory)

        assert len(runs) == 0

    async def test_poll_returns_old_polled_automations(self, async_session_factory):
        """Automations polled longer than POLL_INTERVAL_SECONDS ago are returned."""
        now = utcnow()
        old_poll_time = now - timedelta(seconds=POLL_INTERVAL_SECONDS + 10)

        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Old Polled",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                last_polled_at=old_poll_time,
                last_triggered_at=None,
                created_at=now - timedelta(minutes=5),
            )
            session.add(automation)
            await session.commit()
            automation_id = automation.id

        runs = await poll_and_schedule(async_session_factory)

        assert len(runs) == 1
        assert runs[0].automation_id == automation_id

    async def test_poll_respects_batch_size(self, async_session_factory):
        """Polling respects batch_size limit."""
        now = utcnow()
        async with async_session_factory() as session:
            # Create more automations than the batch size
            for i in range(5):
                automation = Automation(
                    user_id=TEST_USER_ID,
                    org_id=TEST_ORG_ID,
                    name=f"Automation {i}",
                    trigger={
                        "type": "cron",
                        "schedule": "* * * * *",
                        "timezone": "UTC",
                    },
                    tarball_path="s3://bucket/code.tar.gz",
                    entrypoint="uv run main.py",
                    enabled=True,
                    last_polled_at=None,
                    last_triggered_at=None,
                    created_at=now - timedelta(minutes=5),
                )
                session.add(automation)
            await session.commit()

        runs = await poll_and_schedule(async_session_factory, batch_size=2)

        assert len(runs) == 2

    async def test_poll_orders_by_oldest_polled_first(self, async_session_factory):
        """Polling returns oldest-polled automations first."""
        now = utcnow()
        created_at = now - timedelta(minutes=5)

        async with async_session_factory() as session:
            # Create automations with different last_polled_at times
            old_automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Old",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                last_polled_at=now - timedelta(hours=2),
                last_triggered_at=None,
                created_at=created_at,
            )
            newer_automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Newer",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                last_polled_at=now - timedelta(hours=1),
                last_triggered_at=None,
                created_at=created_at,
            )
            never_polled = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Never Polled",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                last_polled_at=None,
                last_triggered_at=None,
                created_at=created_at,
            )
            session.add_all([newer_automation, old_automation, never_polled])
            await session.commit()

        runs = await poll_and_schedule(async_session_factory, batch_size=2)

        assert len(runs) == 2

    async def test_batch_rotates_to_different_automation_after_poll(
        self, async_session_factory
    ):
        """With batch_size=1, consecutive polls pick different automations.

        This verifies that updating last_polled_at moves the automation to the
        back of the queue, so the next poll picks a different one.
        """
        now = utcnow()
        created_at = now - timedelta(minutes=5)

        async with async_session_factory() as session:
            # Create two due automations with NULL last_polled_at
            automation_a = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Automation A",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                last_polled_at=None,
                last_triggered_at=None,
                created_at=created_at,
            )
            automation_b = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Automation B",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                last_polled_at=None,
                last_triggered_at=None,
                created_at=created_at,
            )
            session.add_all([automation_a, automation_b])
            await session.commit()
            id_a = automation_a.id
            id_b = automation_b.id

        # First poll with batch_size=1: should pick one automation
        runs_first = await poll_and_schedule(async_session_factory, batch_size=1)
        assert len(runs_first) == 1
        first_automation_id = runs_first[0].automation_id

        # Second poll with batch_size=1: should pick the OTHER automation
        # because the first one now has a recent last_polled_at
        runs_second = await poll_and_schedule(async_session_factory, batch_size=1)
        assert len(runs_second) == 1
        second_automation_id = runs_second[0].automation_id

        # Verify we picked different automations
        assert first_automation_id != second_automation_id
        assert {first_automation_id, second_automation_id} == {id_a, id_b}

    async def test_last_polled_at_updated_even_when_not_due(
        self, async_session_factory
    ):
        """last_polled_at is updated for all polled automations, not just due ones.

        This ensures fair batch rotation even when an automation is polled but
        not triggered (e.g., cron not yet due).
        """
        now = utcnow()

        async with async_session_factory() as session:
            # Create a NOT due automation: recently triggered, so prev_fire_time
            # is before last_triggered_at
            not_due_automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Not Due Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                last_polled_at=None,
                last_triggered_at=now,  # Just triggered, so not due again yet
                created_at=now - timedelta(minutes=5),
            )
            session.add(not_due_automation)
            await session.commit()
            automation_id = not_due_automation.id

        # Poll with the same 'now' used for last_triggered_at so that
        # prev_fire_time (always ≤ now) can never exceed last_triggered_at,
        # making the result deterministic regardless of minute boundaries.
        runs = await poll_and_schedule(async_session_factory, now=now)
        assert len(runs) == 0

        # But last_polled_at should still be updated
        async with async_session_factory() as session:
            result = await session.execute(
                select(Automation).where(Automation.id == automation_id)
            )
            updated = result.scalars().first()
            assert updated.last_polled_at is not None

    async def test_batch_rotates_with_mix_of_due_and_not_due(
        self, async_session_factory
    ):
        """With batch_size=1, rotation works correctly with due and non-due automations.

        First poll picks a non-due automation and updates its last_polled_at.
        Second poll picks the due automation (not the same non-due one).
        """
        now = utcnow()
        created_at = now - timedelta(minutes=5)

        async with async_session_factory() as session:
            # Non-due automation (recently triggered)
            not_due = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Not Due",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                last_polled_at=None,
                last_triggered_at=now,  # Just triggered
                created_at=created_at,
            )
            # Due automation (never triggered)
            due = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Due",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                last_polled_at=None,
                last_triggered_at=None,
                created_at=created_at,
            )
            session.add_all([not_due, due])
            await session.commit()
            not_due_id = not_due.id
            due_id = due.id

        # Both polls use the same 'now' so prev_fire_time (≤ now) can never
        # exceed the not_due automation's last_triggered_at (= now), keeping
        # the "not due" classification stable across minute boundaries.
        runs_first = await poll_and_schedule(
            async_session_factory, batch_size=1, now=now
        )

        # Second poll with batch_size=1: should pick the OTHER automation
        runs_second = await poll_and_schedule(
            async_session_factory, batch_size=1, now=now
        )

        # Together, we should have exactly 1 run (from the due automation)
        all_runs = runs_first + runs_second
        assert len(all_runs) == 1
        assert all_runs[0].automation_id == due_id

        # Verify both automations have last_polled_at set
        async with async_session_factory() as session:
            result = await session.execute(
                select(Automation).where(Automation.id.in_([not_due_id, due_id]))
            )
            automations = result.scalars().all()
            for automation in automations:
                assert automation.last_polled_at is not None


class TestSchedulerLoop:
    """Tests for scheduler_loop function."""

    async def test_scheduler_loop_exits_on_shutdown(self, async_session_factory):
        """Scheduler exits gracefully when shutdown event is set."""
        shutdown_event = asyncio.Event()

        # Start the scheduler with a short interval
        task = asyncio.create_task(
            scheduler_loop(
                async_session_factory,
                interval_seconds=1,
                shutdown_event=shutdown_event,
            )
        )

        # Give it a moment to start
        await asyncio.sleep(0.1)

        # Signal shutdown
        shutdown_event.set()

        # Should exit within a reasonable time
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except TimeoutError:
            task.cancel()
            pytest.fail("Scheduler did not exit on shutdown signal")

    async def test_scheduler_loop_polls_automations(
        self, async_session_factory, caplog
    ):
        """Scheduler polls and creates pending runs for due automations."""
        # Create a due automation (created in the past so it's due)
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Test Due Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                last_triggered_at=None,
                created_at=utcnow() - timedelta(minutes=5),
            )
            session.add(automation)
            await session.commit()
            automation_id = automation.id

        shutdown_event = asyncio.Event()

        # Run scheduler briefly with logging capture
        import logging

        with caplog.at_level(logging.INFO, logger="openhands.automation.scheduler"):
            task = asyncio.create_task(
                scheduler_loop(
                    async_session_factory,
                    interval_seconds=60,  # Long interval, we'll stop it quickly
                    shutdown_event=shutdown_event,
                )
            )

            # Let it run one poll cycle
            await asyncio.sleep(0.2)

            # Stop the scheduler
            shutdown_event.set()
            await asyncio.wait_for(task, timeout=2.0)

        # Check logs for the due automation
        assert any("Test Due Automation" in record.message for record in caplog.records)
        assert any(
            "Found 1 due automation" in record.message for record in caplog.records
        )
        assert any("Created pending run" in record.message for record in caplog.records)

        # Verify a pending run was created
        async with async_session_factory() as session:
            result = await session.execute(
                select(AutomationRun).where(
                    AutomationRun.automation_id == automation_id
                )
            )
            runs = result.scalars().all()
            assert len(runs) == 1
            assert runs[0].status == AutomationRunStatus.PENDING


class TestCreatePendingRun:
    """Tests for create_pending_run function."""

    async def test_creates_pending_run(self, async_session_factory):
        """Creates a run with PENDING status."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Test Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            run = await create_pending_run(session, automation)
            await session.commit()

            assert run.id is not None
            assert run.automation_id == automation.id
            assert run.status == AutomationRunStatus.PENDING
            assert run.error_detail is None

    async def test_updates_last_triggered_at(self, async_session_factory):
        """Updates automation's last_triggered_at timestamp."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Test Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                last_triggered_at=None,
            )
            session.add(automation)
            await session.commit()
            automation_id = automation.id

            await create_pending_run(session, automation)
            await session.commit()

        # Verify last_triggered_at was updated
        async with async_session_factory() as session:
            result = await session.execute(
                select(Automation).where(Automation.id == automation_id)
            )
            updated = result.scalars().first()
            assert updated.last_triggered_at is not None

    async def test_multiple_runs_for_same_automation(self, async_session_factory):
        """Can create multiple runs for the same automation."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Test Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            run1 = await create_pending_run(session, automation)
            run2 = await create_pending_run(session, automation)
            await session.commit()

            assert run1.id != run2.id
            assert run1.automation_id == run2.automation_id

            # Verify both runs exist
            result = await session.execute(
                select(func.count())
                .select_from(AutomationRun)
                .where(AutomationRun.automation_id == automation.id)
            )
            count = result.scalar()
            assert count == 2
