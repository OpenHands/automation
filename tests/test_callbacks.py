"""Tests for post-run callback scheduling and completion."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from openhands.automation.backends.base import ExecutionContext
from openhands.automation.callbacks import (
    cleanup_run_after_callbacks_if_ready,
    complete_callback_records,
    schedule_and_dispatch_callbacks_for_run,
)
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunCallback,
    AutomationRunCallbackStatus,
    AutomationRunStatus,
    Base,
)
from openhands.automation.schemas import CallbackCompleteItem
from openhands.automation.utils import utcnow


TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")


@pytest.fixture
async def async_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def async_session(async_session_factory):
    async with async_session_factory() as session:
        yield session


def _automation(callbacks, keep_alive=None) -> Automation:
    return Automation(
        user_id=TEST_USER_ID,
        org_id=TEST_ORG_ID,
        name="Callback Automation",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="s3://bucket/code.tar.gz",
        entrypoint="python main.py",
        keep_alive=keep_alive,
        callbacks=callbacks,
    )


def _run(automation: Automation, status: AutomationRunStatus) -> AutomationRun:
    return AutomationRun(
        automation_id=automation.id,
        status=status,
        sandbox_id="sandbox-123",
        completed_at=utcnow(),
    )


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.get_existing_execution_context = AsyncMock(
        return_value=ExecutionContext(agent_url="http://agent", session_key="key")
    )
    backend.get_work_dir.return_value = "/workspace/project"
    backend.cleanup_after_verification = AsyncMock()
    return backend


async def _callback_rows(session: AsyncSession, run_id: uuid.UUID):
    result = await session.execute(
        select(AutomationRunCallback)
        .where(AutomationRunCallback.run_id == run_id)
        .order_by(AutomationRunCallback.order)
    )
    return list(result.scalars().all())


async def test_schedule_and_dispatch_callbacks_matches_terminal_status(async_session):
    automation = _automation(
        [
            {
                "name": "on-success",
                "on": ["COMPLETED"],
                "entrypoint": "python callbacks/success.py",
                "timeout": 30,
            },
            {
                "name": "on-failure",
                "on": ["FAILED"],
                "entrypoint": "python callbacks/failure.py",
            },
        ]
    )
    async_session.add(automation)
    await async_session.flush()
    run = _run(automation, AutomationRunStatus.COMPLETED)
    async_session.add(run)
    await async_session.flush()

    backend = _backend()
    with (
        patch("openhands.automation.callbacks.get_backend", return_value=backend),
        patch(
            "openhands.automation.callbacks._start_bash", new_callable=AsyncMock
        ) as mock_start,
    ):
        mock_start.return_value = "cmd-123"
        created = await schedule_and_dispatch_callbacks_for_run(async_session, run)

    assert created == 1
    mock_start.assert_awaited_once()
    callbacks = await _callback_rows(async_session, run.id)
    assert len(callbacks) == 1
    assert callbacks[0].name == "on-success"
    assert callbacks[0].status == AutomationRunCallbackStatus.RUNNING
    assert callbacks[0].entrypoint == "python callbacks/success.py"
    assert callbacks[0].bash_command_id == "cmd-123"


async def test_schedule_inline_python_callback_is_in_wrapper_command(async_session):
    automation = _automation(
        [
            {
                "name": "inline",
                "on": ["FAILED"],
                "inline_python": "print('failed')\n",
            }
        ]
    )
    async_session.add(automation)
    await async_session.flush()
    run = _run(automation, AutomationRunStatus.FAILED)
    async_session.add(run)
    await async_session.flush()

    backend = _backend()
    with (
        patch("openhands.automation.callbacks.get_backend", return_value=backend),
        patch(
            "openhands.automation.callbacks._start_bash", new_callable=AsyncMock
        ) as mock_start,
    ):
        mock_start.return_value = "cmd-inline"
        created = await schedule_and_dispatch_callbacks_for_run(async_session, run)

    assert created == 1
    assert mock_start.await_args is not None
    command = mock_start.await_args.args[3]
    assert "failed" in command


async def test_schedule_and_dispatch_marks_callbacks_failed_when_start_fails(
    async_session,
):
    automation = _automation(
        [
            {
                "name": "notify",
                "on": ["COMPLETED"],
                "entrypoint": "python callbacks/notify.py",
            }
        ]
    )
    async_session.add(automation)
    await async_session.flush()
    run = _run(automation, AutomationRunStatus.COMPLETED)
    async_session.add(run)
    await async_session.flush()

    backend = _backend()
    with (
        patch("openhands.automation.callbacks.get_backend", return_value=backend),
        patch(
            "openhands.automation.callbacks._start_bash", new_callable=AsyncMock
        ) as mock_start,
    ):
        mock_start.side_effect = RuntimeError("agent unavailable")
        created = await schedule_and_dispatch_callbacks_for_run(async_session, run)

    assert created == 1
    callbacks = await _callback_rows(async_session, run.id)
    assert callbacks[0].status == AutomationRunCallbackStatus.FAILED
    assert callbacks[0].error_detail == "agent unavailable"


async def test_complete_callback_records_updates_status(async_session):
    automation = _automation(callbacks=[], keep_alive=True)
    async_session.add(automation)
    await async_session.flush()
    run = _run(automation, AutomationRunStatus.COMPLETED)
    async_session.add(run)
    await async_session.flush()
    callback = AutomationRunCallback(
        run_id=run.id,
        name="notify",
        trigger_status=AutomationRunStatus.COMPLETED,
        entrypoint="python callbacks/notify.py",
        status=AutomationRunCallbackStatus.RUNNING,
        bash_command_id="cmd-123",
        order=0,
    )
    async_session.add(callback)
    await async_session.flush()

    await complete_callback_records(
        async_session,
        run,
        [
            CallbackCompleteItem(
                id=callback.id,
                name="notify",
                status="COMPLETED",
                exit_code=0,
            )
        ],
    )
    await async_session.flush()

    refreshed = await async_session.get(AutomationRunCallback, callback.id)
    await async_session.refresh(refreshed)
    assert refreshed.status == AutomationRunCallbackStatus.COMPLETED
    assert refreshed.completed_at is not None
    assert refreshed.error_detail is None


async def test_cleanup_after_callbacks_waits_for_unfinished_callbacks(async_session):
    automation = _automation(callbacks=[], keep_alive=False)
    async_session.add(automation)
    await async_session.flush()
    run = _run(automation, AutomationRunStatus.COMPLETED)
    async_session.add(run)
    await async_session.flush()
    callback = AutomationRunCallback(
        run_id=run.id,
        name="notify",
        trigger_status=AutomationRunStatus.COMPLETED,
        entrypoint="python callbacks/notify.py",
        status=AutomationRunCallbackStatus.RUNNING,
        order=0,
    )
    async_session.add(callback)
    await async_session.flush()

    backend = _backend()
    with patch("openhands.automation.callbacks.get_backend", return_value=backend):
        cleaned = await cleanup_run_after_callbacks_if_ready(async_session, run)

    assert cleaned is False
    backend.cleanup_after_verification.assert_not_called()


async def test_cleanup_after_callbacks_runs_when_all_callbacks_terminal(async_session):
    automation = _automation(callbacks=[], keep_alive=False)
    async_session.add(automation)
    await async_session.flush()
    run = _run(automation, AutomationRunStatus.COMPLETED)
    async_session.add(run)
    await async_session.flush()
    callback = AutomationRunCallback(
        run_id=run.id,
        name="notify",
        trigger_status=AutomationRunStatus.COMPLETED,
        entrypoint="python callbacks/notify.py",
        status=AutomationRunCallbackStatus.COMPLETED,
        completed_at=utcnow(),
        order=0,
    )
    async_session.add(callback)
    await async_session.flush()

    backend = _backend()
    with patch("openhands.automation.callbacks.get_backend", return_value=backend):
        cleaned = await cleanup_run_after_callbacks_if_ready(async_session, run)

    assert cleaned is True
    backend.cleanup_after_verification.assert_awaited_once()
