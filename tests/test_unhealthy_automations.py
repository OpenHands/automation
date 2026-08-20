"""Tests for unhealthy automation classification and auto-disable behavior."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
        automation_id = automation.id
        run_id = run.id
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


async def test_completed_blocking_runs_count_as_permanent_failures(
    sqlite_session_factory,
):
    async with sqlite_session_factory() as session:
        automation = await _create_automation(session)
        blocking_detail = {
            "phase": "callback",
            "kind": "blocked",
            "detail": "MCP server credentials missing",
            "transient": False,
            "user_action": "settings",
            "blocking_factor": {"kind": "config", "reason": "Missing MCP token"},
        }
        await _add_terminal_run(
            session,
            automation,
            status=AutomationRunStatus.COMPLETED,
            status_detail=blocking_detail,
            index=1,
        )
        await _add_terminal_run(
            session,
            automation,
            status=AutomationRunStatus.COMPLETED,
            status_detail=blocking_detail,
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
        assert automation.disabled_detail["status_detail"]["blocking_factor"]
