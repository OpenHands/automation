"""Tests for unhealthy automation classification and auto-disable behavior."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from openhands.automation.config import clear_config_cache
from openhands.automation.models import (
    Automation,
    AutomationDisableEvent,
    AutomationRun,
    AutomationRunStatus,
    Base,
)
from openhands.automation.utils.time import utcnow
from openhands.automation.utils.unhealthy import (
    is_permanent_failure_detail,
    maybe_disable_unhealthy_automation,
)


TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")


@pytest.fixture
async def sqlite_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _permanent_detail(kind: str = "auth") -> dict:
    return {
        "phase": "callback",
        "kind": kind,
        "detail": "Invalid API key",
        "transient": False,
        "user_action": "settings",
        "fingerprint": f"callback:sdk_callback:{kind}",
    }


def _transient_detail() -> dict:
    return {
        "phase": "callback",
        "kind": "rate_limit",
        "detail": "Provider returned 429",
        "transient": True,
        "user_action": "retry",
        "fingerprint": "callback:sdk_callback:rate_limit",
    }


async def _create_automation(session: AsyncSession) -> Automation:
    automation = Automation(
        user_id=TEST_USER_ID,
        org_id=TEST_ORG_ID,
        name="Unhealthy automation",
        trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
        tarball_path="https://example.com/automation.tar",
        entrypoint="python main.py",
        enabled=True,
    )
    session.add(automation)
    await session.flush()
    return automation


async def _add_terminal_run(
    session: AsyncSession,
    automation: Automation,
    *,
    status_detail: dict | None,
    index: int,
    status: AutomationRunStatus = AutomationRunStatus.FAILED,
) -> AutomationRun:
    now = utcnow() + timedelta(seconds=index)
    run = AutomationRun(
        automation_id=automation.id,
        status=status,
        status_detail=status_detail,
        created_at=now,
        completed_at=now,
    )
    session.add(run)
    await session.flush()
    return run


def test_permanent_failure_classifier_uses_sdk_semantics():
    assert is_permanent_failure_detail(_permanent_detail("auth")) is True
    assert is_permanent_failure_detail(_permanent_detail("config")) is True
    assert is_permanent_failure_detail(_permanent_detail("quota")) is True
    assert is_permanent_failure_detail(_transient_detail()) is False
    assert (
        is_permanent_failure_detail(
            {"kind": "config", "detail": "bad model", "transient": True}
        )
        is False
    )
    assert (
        is_permanent_failure_detail(
            {"kind": "internal", "detail": "service bug", "transient": False}
        )
        is False
    )


async def test_auto_disables_after_consecutive_permanent_failures(
    sqlite_session_factory,
):
    async with sqlite_session_factory() as session:
        automation = await _create_automation(session)
        await _add_terminal_run(
            session, automation, status_detail=_permanent_detail(), index=1
        )
        await _add_terminal_run(
            session, automation, status_detail=_permanent_detail(), index=2
        )
        await _add_terminal_run(
            session, automation, status_detail=_permanent_detail(), index=3
        )
        pending_run = AutomationRun(
            automation_id=automation.id,
            status=AutomationRunStatus.PENDING,
        )
        session.add(pending_run)
        await session.flush()

        disabled = await maybe_disable_unhealthy_automation(
            session,
            automation.id,
            threshold=3,
        )
        await session.refresh(automation)

        assert disabled is True
        assert automation.enabled is False
        assert automation.disabled_reason is not None
        assert "auth" in automation.disabled_reason
        assert automation.disabled_detail is not None
        assert automation.disabled_detail["consecutive_permanent_failures"] == 3
        await session.refresh(pending_run)
        assert pending_run.status == AutomationRunStatus.SKIPPED
        assert pending_run.completed_at is not None
        assert pending_run.status_detail is not None
        assert pending_run.status_detail["operation"] == "automation_disabled"

        events = (
            (
                await session.execute(
                    select(AutomationDisableEvent).where(
                        AutomationDisableEvent.automation_id == automation.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].source == "consecutive_permanent_failures"
        assert events[0].run_id is not None
        assert str(events[0].run_id) == automation.disabled_detail["run_id"]


async def test_direct_disable_records_event_history(sqlite_session_factory):
    from openhands.automation.utils.run import disable_automation

    async with sqlite_session_factory() as session:
        automation = await _create_automation(session)
        run = await _add_terminal_run(
            session,
            automation,
            status_detail=_permanent_detail(),
            index=1,
        )
        pending_run = AutomationRun(
            automation_id=automation.id,
            status=AutomationRunStatus.PENDING,
        )
        session.add(pending_run)
        await session.flush()
        automation_id = automation.id
        run_id = run.id
        pending_run_id = pending_run.id
        await session.commit()

    disabled = await disable_automation(
        sqlite_session_factory,
        automation_id,
        "Tarball not found",
        disabled_detail={"kind": "config"},
        run_id=run_id,
        source="permanent_dispatch_failure",
    )

    assert disabled is True

    async with sqlite_session_factory() as session:
        events = (
            (
                await session.execute(
                    select(AutomationDisableEvent).where(
                        AutomationDisableEvent.automation_id == automation_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].run_id == run_id
        assert events[0].reason == "Tarball not found"
        assert events[0].detail == {"kind": "config"}
        assert events[0].source == "permanent_dispatch_failure"

        pending_run = await session.get(AutomationRun, pending_run_id)
        assert pending_run is not None
        assert pending_run.status == AutomationRunStatus.SKIPPED
        assert pending_run.completed_at is not None
        assert pending_run.status_detail is not None
        assert pending_run.status_detail["operation"] == "automation_disabled"


async def test_transient_failures_do_not_count_toward_disable(
    sqlite_session_factory,
):
    async with sqlite_session_factory() as session:
        automation = await _create_automation(session)
        await _add_terminal_run(
            session, automation, status_detail=_permanent_detail(), index=1
        )
        await _add_terminal_run(
            session, automation, status_detail=_permanent_detail(), index=2
        )
        await _add_terminal_run(
            session, automation, status_detail=_transient_detail(), index=3
        )

        disabled = await maybe_disable_unhealthy_automation(
            session,
            automation.id,
            threshold=3,
        )
        await session.refresh(automation)

        assert disabled is False
        assert automation.enabled is True
        assert automation.disabled_reason is None


async def test_sdk_callback_error_runs_count_as_permanent_failures(
    sqlite_session_factory,
):
    async with sqlite_session_factory() as session:
        automation = await _create_automation(session)
        callback_error_detail = {
            "phase": "callback",
            "kind": "auth",
            "detail": "Missing MCP token",
            "transient": False,
            "source": "environment",
            "code": "MissingSecret",
            "user_action": "settings",
        }
        await _add_terminal_run(
            session,
            automation,
            status=AutomationRunStatus.FAILED,
            status_detail=callback_error_detail,
            index=1,
        )
        await _add_terminal_run(
            session,
            automation,
            status=AutomationRunStatus.FAILED,
            status_detail=callback_error_detail,
            index=2,
        )

        disabled = await maybe_disable_unhealthy_automation(
            session,
            automation.id,
            threshold=2,
        )
        await session.refresh(automation)

        assert disabled is True
        assert automation.enabled is False
        assert automation.disabled_detail is not None
        assert automation.disabled_detail["status_detail"]["source"] == "environment"
        assert automation.disabled_detail["status_detail"]["code"] == "MissingSecret"


# --- Consecutive-failure rule ---


@pytest.fixture
def auto_disable_config(monkeypatch):
    """Turn the consecutive rule on, with overridable knobs."""

    def _configure(threshold: str | None = "10", window_hours: str = "24"):
        monkeypatch.delenv(
            "AUTOMATION_CONSECUTIVE_FAILURE_DISABLE_THRESHOLD", raising=False
        )
        if threshold is not None:
            monkeypatch.setenv(
                "AUTOMATION_CONSECUTIVE_FAILURE_DISABLE_THRESHOLD", threshold
            )
        monkeypatch.setenv(
            "AUTOMATION_CONSECUTIVE_FAILURE_DISABLE_WINDOW_HOURS", window_hours
        )
        clear_config_cache()

    yield _configure
    monkeypatch.undo()
    clear_config_cache()


async def _add_run_at(
    session: AsyncSession,
    automation: Automation,
    *,
    status: AutomationRunStatus,
    at,
    status_detail: dict | None = None,
) -> AutomationRun:
    run = AutomationRun(
        automation_id=automation.id,
        status=status,
        status_detail=status_detail,
        created_at=at,
        completed_at=at,
    )
    session.add(run)
    await session.flush()
    return run


async def _add_failures(
    session: AsyncSession,
    automation: Automation,
    *,
    count: int,
    every: timedelta,
    ending: timedelta = timedelta(0),
    status_detail: dict | None = None,
) -> None:
    """`count` failures spaced `every` apart, the last one `ending` ago."""
    last = utcnow() - ending
    for i in range(count):
        await _add_run_at(
            session,
            automation,
            status=AutomationRunStatus.FAILED,
            at=last - every * (count - 1 - i),
            status_detail=status_detail,
        )


async def test_execution_errors_disable_the_automation(
    sqlite_session_factory, auto_disable_config
):
    """The case the permanent-failure rule misses: an agent that just fails."""
    auto_disable_config()
    async with sqlite_session_factory() as session:
        automation = await _create_automation(session)
        await _add_failures(
            session,
            automation,
            count=10,
            every=timedelta(hours=12),
            status_detail={"kind": "execution_error", "transient": False},
        )

        assert await maybe_disable_unhealthy_automation(session, automation.id) is True
        await session.refresh(automation)
        assert automation.enabled is False
        assert automation.disabled_detail["rule"] == "consecutive_failures"
        assert automation.disabled_detail["consecutive_failures"] == 10
        assert "the last 10 runs all failed" in automation.disabled_reason
        assert "24 hours" in automation.disabled_reason


async def test_failures_with_no_status_detail_disable_the_automation(
    sqlite_session_factory, auto_disable_config
):
    """A bare FAILED run with no classification still counts."""
    auto_disable_config()
    async with sqlite_session_factory() as session:
        automation = await _create_automation(session)
        await _add_failures(session, automation, count=10, every=timedelta(hours=12))

        assert await maybe_disable_unhealthy_automation(session, automation.id) is True


async def test_high_frequency_automation_broken_for_days_is_disabled(
    sqlite_session_factory, auto_disable_config
):
    """A 5-minute cron's last 10 failures span under an hour -- still disabled."""
    auto_disable_config()
    async with sqlite_session_factory() as session:
        automation = await _create_automation(session)
        await _add_failures(session, automation, count=200, every=timedelta(minutes=5))

        assert await maybe_disable_unhealthy_automation(session, automation.id) is True


async def test_outage_burst_does_not_disable_the_automation(
    sqlite_session_factory, auto_disable_config
):
    """8h outage on a 5-minute cron: it succeeded inside the window."""
    auto_disable_config()
    async with sqlite_session_factory() as session:
        automation = await _create_automation(session)
        await _add_run_at(
            session,
            automation,
            status=AutomationRunStatus.COMPLETED,
            at=utcnow() - timedelta(hours=9),
        )
        await _add_failures(session, automation, count=96, every=timedelta(minutes=5))

        assert await maybe_disable_unhealthy_automation(session, automation.id) is False
        await session.refresh(automation)
        assert automation.enabled is True


async def test_a_longer_window_rides_out_a_longer_outage(
    sqlite_session_factory, auto_disable_config
):
    """The same 30h outage disables at a 24h window but not at 72h."""
    async with sqlite_session_factory() as session:
        automation = await _create_automation(session)
        await _add_run_at(
            session,
            automation,
            status=AutomationRunStatus.COMPLETED,
            at=utcnow() - timedelta(hours=31),
        )
        await _add_failures(session, automation, count=60, every=timedelta(minutes=30))

        auto_disable_config(window_hours="24")
        assert await maybe_disable_unhealthy_automation(session, automation.id) is True

        automation.enabled = True
        await session.flush()

        auto_disable_config(window_hours="72")
        assert await maybe_disable_unhealthy_automation(session, automation.id) is False


async def test_fewer_failures_than_the_threshold_does_nothing(
    sqlite_session_factory, auto_disable_config
):
    auto_disable_config()
    async with sqlite_session_factory() as session:
        automation = await _create_automation(session)
        await _add_failures(session, automation, count=9, every=timedelta(hours=12))

        assert await maybe_disable_unhealthy_automation(session, automation.id) is False


async def test_a_recent_success_breaks_the_streak(
    sqlite_session_factory, auto_disable_config
):
    auto_disable_config()
    async with sqlite_session_factory() as session:
        automation = await _create_automation(session)
        await _add_failures(
            session,
            automation,
            count=10,
            every=timedelta(hours=12),
            ending=timedelta(hours=2),
        )
        await _add_run_at(
            session,
            automation,
            status=AutomationRunStatus.COMPLETED,
            at=utcnow() - timedelta(hours=1),
        )

        assert await maybe_disable_unhealthy_automation(session, automation.id) is False


# --- Re-enable boundary ---


async def test_re_enabling_clears_prior_failure_history(
    sqlite_session_factory, auto_disable_config
):
    """Old failures must not re-disable it on the first new failure."""
    auto_disable_config()
    async with sqlite_session_factory() as session:
        automation = await _create_automation(session)
        await _add_failures(
            session,
            automation,
            count=10,
            every=timedelta(hours=12),
            ending=timedelta(minutes=5),
        )

        assert await maybe_disable_unhealthy_automation(session, automation.id) is True

        # User fixes the automation and re-enables it.
        automation.enabled = True
        automation.disabled_reason = None
        automation.disabled_detail = None
        automation.disabled_at = None
        await session.flush()

        await _add_run_at(
            session,
            automation,
            status=AutomationRunStatus.FAILED,
            at=utcnow(),
        )
        assert await maybe_disable_unhealthy_automation(session, automation.id) is False
        await session.refresh(automation)
        assert automation.enabled is True


# --- Opt-in behavior ---


async def test_the_rule_is_off_by_default(sqlite_session_factory, auto_disable_config):
    """History that would fire the rule does nothing until it's enabled."""
    auto_disable_config(threshold=None)
    async with sqlite_session_factory() as session:
        automation = await _create_automation(session)
        await _add_failures(session, automation, count=30, every=timedelta(hours=4))

        assert await maybe_disable_unhealthy_automation(session, automation.id) is False
        await session.refresh(automation)
        assert automation.enabled is True


async def test_setting_the_count_turns_the_rule_on(
    sqlite_session_factory, auto_disable_config
):
    auto_disable_config(threshold="10")
    async with sqlite_session_factory() as session:
        automation = await _create_automation(session)
        await _add_failures(session, automation, count=30, every=timedelta(hours=4))

        assert await maybe_disable_unhealthy_automation(session, automation.id) is True
        await session.refresh(automation)
        assert automation.disabled_detail["rule"] == "consecutive_failures"


def test_window_hours_is_configurable(monkeypatch):
    from openhands.automation.config import ServiceSettings

    monkeypatch.setenv("AUTOMATION_CONSECUTIVE_FAILURE_DISABLE_WINDOW_HOURS", "72")
    settings = ServiceSettings()

    assert settings.consecutive_failure_disable_window_hours == 72
    assert settings.consecutive_failure_disable_threshold is None
