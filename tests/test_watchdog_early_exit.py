import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from openhands.automation.config import Settings
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
    Base,
)
from openhands.automation.utils.agent_server import (
    VerificationOutcome,
    VerificationResult,
)
from openhands.automation.utils.time import ensure_utc, utcnow
from openhands.automation.watchdog import mark_stale_runs


@pytest.fixture
async def run_store(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/runs.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(
        "openhands.automation.watchdog.capture_automation_event", AsyncMock()
    )
    monkeypatch.setattr(
        "openhands.automation.watchdog.record_first_run_outcome", AsyncMock()
    )
    monkeypatch.setattr(
        "openhands.automation.watchdog.maybe_disable_unhealthy_automation_after_run",
        AsyncMock(),
    )
    try:
        yield factory
    finally:
        await engine.dispose()


async def create_run(
    factory, *, keep_alive=False, command_id: str | None = "command-1"
):
    async with factory() as session:
        automation = Automation(
            user_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            name="Early exit",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="oh-internal://uploads/test",
            entrypoint="python3 main.py",
            timeout=1800,
            keep_alive=keep_alive,
        )
        session.add(automation)
        await session.flush()
        run = AutomationRun(
            automation_id=automation.id,
            status=AutomationRunStatus.RUNNING,
            sandbox_id="sandbox-1",
            bash_command_id=command_id,
            started_at=utcnow(),
            timeout_at=utcnow() + timedelta(minutes=32),
        )
        session.add(run)
        await session.commit()
        assert run.timeout_at is not None
        return run.id, ensure_utc(run.timeout_at)


@pytest.mark.parametrize("exit_code", [0, 2])
@pytest.mark.parametrize("keep_alive", [False, True])
async def test_records_exit_before_deadline(
    run_store, monkeypatch, exit_code, keep_alive
):
    run_id, deadline = await create_run(run_store, keep_alive=keep_alive)
    backend = AsyncMock()
    backend.verify_run.return_value = VerificationResult(
        verified=True,
        success=exit_code == 0,
        exit_code=exit_code,
        stderr="invalid archive" if exit_code else "",
    )
    monkeypatch.setattr(
        "openhands.automation.watchdog.get_backend", lambda run: backend
    )

    assert await mark_stale_runs(run_store, Settings()) == 1
    async with run_store() as session:
        run = await session.get(AutomationRun, run_id)
        assert run is not None
        assert run.status == (
            AutomationRunStatus.COMPLETED
            if exit_code == 0
            else AutomationRunStatus.FAILED
        )
        assert run.completed_at is not None
        assert ensure_utc(run.completed_at) < deadline
        if exit_code:
            assert run.error_detail is not None
            assert "exit_code=2" in run.error_detail
            assert "invalid archive" in run.error_detail
    assert backend.cleanup_after_verification.await_count == int(not keep_alive)
    assert await mark_stale_runs(run_store, Settings()) == 0
    backend.verify_run.assert_awaited_once_with(str(run_id))


@pytest.mark.parametrize(
    "outcome",
    [
        VerificationOutcome.STILL_RUNNING,
        VerificationOutcome.TRANSIENT_ERROR,
        VerificationOutcome.VERIFICATION_ERROR,
        VerificationOutcome.ENVIRONMENT_UNAVAILABLE,
        None,
    ],
)
async def test_uncertain_verification_preserves_run_and_deadline(
    run_store, monkeypatch, outcome
):
    run_id, deadline = await create_run(run_store)
    backend = AsyncMock()
    if outcome is None:
        backend.verify_run.side_effect = RuntimeError("API unavailable")
    else:
        backend.verify_run.return_value = VerificationResult(
            outcome=outcome, detail="not an observed exit"
        )
    monkeypatch.setattr(
        "openhands.automation.watchdog.get_backend", lambda run: backend
    )

    assert await mark_stale_runs(run_store, Settings()) == 0
    async with run_store() as session:
        run = await session.get(AutomationRun, run_id)
        assert run is not None
        assert run.status == AutomationRunStatus.RUNNING
        assert run.completed_at is None
        assert run.error_detail is None
        assert run.timeout_at is not None
        assert ensure_utc(run.timeout_at) == deadline
    backend.cleanup_after_verification.assert_not_awaited()


async def test_provisioning_run_waits_for_deadline(run_store, monkeypatch):
    await create_run(run_store, command_id=None)
    get_backend = AsyncMock()
    monkeypatch.setattr("openhands.automation.watchdog.get_backend", get_backend)
    assert await mark_stale_runs(run_store, Settings()) == 0
    get_backend.assert_not_called()


async def test_concurrent_callback_wins_early_verification(run_store, monkeypatch):
    run_id, _ = await create_run(run_store)
    backend = AsyncMock()

    async def verify_run(_):
        async with run_store() as session:
            run = await session.get(AutomationRun, run_id)
            assert run is not None
            run.status = AutomationRunStatus.COMPLETED
            run.completed_at = utcnow()
            await session.commit()
        return VerificationResult(verified=True, success=False, exit_code=2)

    backend.verify_run.side_effect = verify_run
    monkeypatch.setattr(
        "openhands.automation.watchdog.get_backend", lambda run: backend
    )

    assert await mark_stale_runs(run_store, Settings()) == 0
    async with run_store() as session:
        run = await session.get(AutomationRun, run_id)
        assert run is not None
        assert run.status == AutomationRunStatus.COMPLETED
        assert run.error_detail is None
    backend.cleanup_after_verification.assert_not_awaited()
