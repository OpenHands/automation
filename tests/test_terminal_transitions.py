"""Guarded terminal run transitions.

Each terminal writer must change a run only when the run is still in the
status the transition starts from, learn from the UPDATE itself whether it
did, and skip the side effects of a transition another writer won.
"""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Update

from openhands.automation.dispatcher import _execute_run_safe, _skip_blocked_run
from openhands.automation.models import Automation, AutomationRun, AutomationRunStatus
from openhands.automation.utils import utcnow
from openhands.automation.utils.agent_server import VerificationResult
from openhands.automation.utils.run import (
    guarded_run_update,
    mark_run_terminal,
    skip_pending_runs_for_disabled_automation,
)
from openhands.automation.utils.run_status_detail import RunStatusDetailKind
from openhands.automation.watchdog import _verify_and_mark_run


TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")


def _automation(**overrides) -> Automation:
    fields = {
        "user_id": TEST_USER_ID,
        "org_id": TEST_ORG_ID,
        "name": "Terminal Transitions",
        "trigger": {"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        "tarball_path": "s3://bucket/code.tar.gz",
        "entrypoint": "uv run main.py",
        "enabled": True,
        "timeout": 60,
    }
    fields.update(overrides)
    return Automation(**fields)


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *statuses: AutomationRunStatus,
    automation: Automation | None = None,
    **run_fields,
) -> tuple[Automation, list[AutomationRun]]:
    async with factory() as session:
        automation = automation or _automation()
        session.add(automation)
        await session.commit()
        runs = [
            AutomationRun(automation_id=automation.id, status=status, **run_fields)
            for status in statuses
        ]
        session.add_all(runs)
        await session.commit()
        return automation, runs


async def _status(
    factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> AutomationRun:
    async with factory() as session:
        run = await session.get(AutomationRun, run_id)
        assert run is not None
        return run


async def _set_status(
    factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    status: AutomationRunStatus,
    **values,
) -> None:
    async with factory() as session:
        await session.execute(
            update(AutomationRun)
            .where(AutomationRun.id == run_id)
            .values(status=status, **values)
        )
        await session.commit()


@pytest.fixture
def dual_factory(
    dual_db_production_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        dual_db_production_engine, class_=AsyncSession, expire_on_commit=False
    )


class _RacingSession:
    """A session that lets another writer commit right before its first UPDATE.

    Models the window between a writer reading a run and writing it: the
    concurrent transition commits through its own session, so the racing
    writer's UPDATE runs against the already-changed row.
    """

    def __init__(self, inner: AsyncSession, before_update) -> None:
        self._inner = inner
        self._before_update = before_update

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def __aenter__(self):
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._inner.__aexit__(*exc)

    async def execute(self, statement, *args, **kwargs):
        if isinstance(statement, Update) and self._before_update is not None:
            before_update, self._before_update = self._before_update, None
            await before_update()
        return await self._inner.execute(statement, *args, **kwargs)


def _racing_factory(factory: async_sessionmaker[AsyncSession], before_update):
    return lambda: _RacingSession(factory(), before_update)


class TestGuardedRunUpdate:
    async def test_reports_only_rows_it_changed(self, dual_factory):
        _, (running, cancelled) = await _seed(
            dual_factory, AutomationRunStatus.RUNNING, AutomationRunStatus.CANCELLED
        )

        async with dual_factory() as session:
            changed = await guarded_run_update(
                session,
                AutomationRun.id.in_([running.id, cancelled.id]),
                expected=(AutomationRunStatus.RUNNING,),
                values={"status": AutomationRunStatus.FAILED, "completed_at": utcnow()},
            )
            await session.commit()

        assert changed == {running.id}
        assert (await _status(dual_factory, running.id)).status == (
            AutomationRunStatus.FAILED
        )
        assert (await _status(dual_factory, cancelled.id)).status == (
            AutomationRunStatus.CANCELLED
        )

    async def test_loser_leaves_its_stale_in_memory_run_alone(self, dual_factory):
        """A stale loaded run is not shown a status the database did not take."""
        _, (run,) = await _seed(dual_factory, AutomationRunStatus.RUNNING)

        async with dual_factory() as session:
            stale = await session.get(AutomationRun, run.id)
            assert stale is not None
            await _set_status(dual_factory, run.id, AutomationRunStatus.CANCELLED)

            changed = await guarded_run_update(
                session,
                AutomationRun.id == run.id,
                expected=(AutomationRunStatus.RUNNING,),
                values={"status": AutomationRunStatus.FAILED},
            )

            assert changed == set()
            assert stale.status == AutomationRunStatus.RUNNING
            await session.commit()

        assert (await _status(dual_factory, run.id)).status == (
            AutomationRunStatus.CANCELLED
        )

    async def test_winner_synchronizes_its_loaded_run(self, dual_factory):
        _, (run,) = await _seed(dual_factory, AutomationRunStatus.RUNNING)

        async with dual_factory() as session:
            loaded = await session.get(AutomationRun, run.id)
            assert loaded is not None
            await guarded_run_update(
                session,
                AutomationRun.id == run.id,
                expected=(AutomationRunStatus.RUNNING,),
                values={"status": AutomationRunStatus.COMPLETED},
            )
            assert loaded.status == AutomationRunStatus.COMPLETED
            await session.rollback()

    async def test_does_not_commit_for_the_caller(self, dual_factory):
        """The caller's rollback undoes the transition."""
        _, (run,) = await _seed(dual_factory, AutomationRunStatus.RUNNING)

        async with dual_factory() as session:
            changed = await guarded_run_update(
                session,
                AutomationRun.id == run.id,
                expected=(AutomationRunStatus.RUNNING,),
                values={"status": AutomationRunStatus.FAILED},
            )
            assert changed == {run.id}
            await session.rollback()

        assert (await _status(dual_factory, run.id)).status == (
            AutomationRunStatus.RUNNING
        )


class TestMarkRunTerminalRace:
    """The dispatcher's terminal write must not overwrite a concurrent one."""

    @pytest.mark.parametrize(
        "status",
        [AutomationRunStatus.FAILED, AutomationRunStatus.SKIPPED],
    )
    async def test_write_committed_before_its_update_wins(self, dual_factory, status):
        """A cancel committing between the read and the write is kept."""
        _, (run,) = await _seed(dual_factory, AutomationRunStatus.RUNNING)

        async def cancel() -> None:
            await _set_status(
                dual_factory,
                run.id,
                AutomationRunStatus.CANCELLED,
                error_detail="Cancelled by user",
            )

        transitioned = await mark_run_terminal(
            _racing_factory(dual_factory, cancel),  # type: ignore[arg-type]
            run,
            status,
            "sandbox creation failed",
        )

        persisted = await _status(dual_factory, run.id)
        assert persisted.status == AutomationRunStatus.CANCELLED
        assert persisted.error_detail == "Cancelled by user"
        assert transitioned is False
        assert run.status == AutomationRunStatus.RUNNING

    async def test_winner_reports_and_records_the_transition(self, dual_factory):
        _, (run,) = await _seed(dual_factory, AutomationRunStatus.RUNNING)

        transitioned = await mark_run_terminal(
            dual_factory, run, AutomationRunStatus.FAILED, "sandbox creation failed"
        )

        assert transitioned is True
        persisted = await _status(dual_factory, run.id)
        assert persisted.status == AutomationRunStatus.FAILED
        assert persisted.error_detail == "sandbox creation failed"
        assert persisted.completed_at is not None
        assert run.status == AutomationRunStatus.FAILED
        assert run.error_detail == "sandbox creation failed"

    async def test_commit_failure_is_not_a_transition(self, dual_factory):
        """A write that never committed reports False and leaves RUNNING."""
        _, (run,) = await _seed(dual_factory, AutomationRunStatus.RUNNING)

        class _CommitFails(_RacingSession):
            async def commit(self):
                raise RuntimeError("connection lost")

        transitioned = await mark_run_terminal(
            lambda: _CommitFails(dual_factory(), None),  # type: ignore[arg-type]
            run,
            AutomationRunStatus.FAILED,
            "sandbox creation failed",
        )

        assert transitioned is False
        assert run.status == AutomationRunStatus.RUNNING
        assert (await _status(dual_factory, run.id)).status == (
            AutomationRunStatus.RUNNING
        )


class TestDispatcherSideEffects:
    """A dispatcher path that lost its terminal write reports nothing.

    ``capture_automation_event`` is patched as the product-telemetry sink: the
    assertion is on whether the dispatcher claims a transition, not on PostHog.
    """

    async def test_lost_skip_emits_no_skip_event(self, async_session_factory):
        automation, (run,) = await _seed(
            async_session_factory, AutomationRunStatus.RUNNING
        )
        await _set_status(async_session_factory, run.id, AutomationRunStatus.CANCELLED)

        with patch(
            "openhands.automation.dispatcher.capture_automation_event",
            new_callable=AsyncMock,
        ) as capture:
            await _skip_blocked_run(
                run,
                automation,
                async_session_factory,
                kind=RunStatusDetailKind.DAILY_SANDBOX_LIMIT,
                detail="Daily sandbox limit reached.",
            )

        capture.assert_not_awaited()
        persisted = await _status(async_session_factory, run.id)
        assert persisted.status == AutomationRunStatus.CANCELLED
        assert persisted.status_detail is None

    async def test_lost_internal_failure_emits_no_failed_event(
        self, async_session_factory, mock_settings
    ):
        async with async_session_factory() as session:
            automation = _automation()
            session.add(automation)
            await session.commit()
            run = AutomationRun(
                automation_id=automation.id, status=AutomationRunStatus.RUNNING
            )
            session.add(run)
            await session.commit()
            run.automation = automation
        await _set_status(async_session_factory, run.id, AutomationRunStatus.CANCELLED)

        with (
            patch(
                "openhands.automation.dispatcher._execute_run",
                new_callable=AsyncMock,
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "openhands.automation.dispatcher.capture_automation_event",
                new_callable=AsyncMock,
            ) as capture,
        ):
            await _execute_run_safe(run, mock_settings, async_session_factory, None)  # type: ignore[arg-type]

        capture.assert_not_awaited()
        assert run.status == AutomationRunStatus.RUNNING
        assert (await _status(async_session_factory, run.id)).status == (
            AutomationRunStatus.CANCELLED
        )

    async def test_won_internal_failure_still_reports(
        self, async_session_factory, mock_settings
    ):
        async with async_session_factory() as session:
            automation = _automation()
            session.add(automation)
            await session.commit()
            run = AutomationRun(
                automation_id=automation.id, status=AutomationRunStatus.RUNNING
            )
            session.add(run)
            await session.commit()
            run.automation = automation

        with (
            patch(
                "openhands.automation.dispatcher._execute_run",
                new_callable=AsyncMock,
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "openhands.automation.dispatcher.capture_automation_event",
                new_callable=AsyncMock,
            ) as capture,
        ):
            await _execute_run_safe(run, mock_settings, async_session_factory, None)  # type: ignore[arg-type]

        capture.assert_awaited_once()
        assert capture.await_args_list[0].args[0] == "automation_run_failed"
        assert run.status == AutomationRunStatus.FAILED
        persisted = await _status(async_session_factory, run.id)
        assert persisted.status == AutomationRunStatus.FAILED
        assert persisted.error_detail == "Internal error"


class TestWatchdogLoser:
    async def test_verified_exit_after_a_cancel_changes_nothing(
        self, async_session_factory, mock_settings
    ):
        """The watchdog's verified COMPLETED loses to a committed cancel."""
        now = utcnow()
        _, (seeded,) = await _seed(
            async_session_factory,
            AutomationRunStatus.RUNNING,
            sandbox_id="sandbox-1",
            started_at=now - timedelta(minutes=5),
            timeout_at=now - timedelta(minutes=1),
        )
        backend = MagicMock()
        backend.is_local_mode = False
        backend.verify_run = AsyncMock(
            return_value=VerificationResult(verified=True, success=True, exit_code=0)
        )
        backend.cleanup_after_verification = AsyncMock()

        with (
            patch("openhands.automation.watchdog.get_backend", return_value=backend),
            patch(
                "openhands.automation.watchdog.capture_automation_event",
                new_callable=AsyncMock,
            ) as capture,
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, seeded.id)
                assert run is not None
                await _set_status(
                    async_session_factory, run.id, AutomationRunStatus.CANCELLED
                )
                marked = await _verify_and_mark_run(session, run, mock_settings)
                assert run.status == AutomationRunStatus.RUNNING
                await session.commit()

        assert marked is False
        capture.assert_not_awaited()
        backend.cleanup_after_verification.assert_not_awaited()
        assert (await _status(async_session_factory, seeded.id)).status == (
            AutomationRunStatus.CANCELLED
        )


async def _snapshot(session: AsyncSession, run_id: uuid.UUID) -> tuple:
    """The run as the request session sees it (the API test client's requests
    share that session and leave their writes uncommitted)."""
    run = (
        await session.execute(
            select(AutomationRun)
            .where(AutomationRun.id == run_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return (run.status, run.error_detail, run.cost, run.completed_at)


class TestCallbackCorrection:
    """The authorized watchdog-timeout FAILED -> COMPLETED correction."""

    async def test_correction_applies_once_and_repeats_change_nothing(
        self, async_client, async_session, async_session_factory
    ):
        _, (run,) = await _seed(
            async_session_factory,
            AutomationRunStatus.FAILED,
            error_detail="Timed out: no completion callback received",
            completed_at=utcnow(),
        )

        with patch(
            "openhands.automation.router.capture_automation_event",
            new_callable=AsyncMock,
        ) as capture:
            first = await async_client.post(
                f"/api/automation/v1/runs/{run.id}/complete",
                json={"status": "COMPLETED", "cost": 0.25},
            )
            corrected = await _snapshot(async_session, run.id)
            repeats = [
                await async_client.post(
                    f"/api/automation/v1/runs/{run.id}/complete",
                    json=body,
                )
                for body in (
                    {"status": "COMPLETED", "cost": 9.0},
                    {"status": "FAILED", "error": "late wrapper crash"},
                )
            ]

        assert first.status_code == 200
        assert corrected[:3] == (AutomationRunStatus.COMPLETED, None, 0.25)
        assert [r.status_code for r in repeats] == [409, 409]
        assert all("COMPLETED" in r.json()["detail"] for r in repeats)

        assert await _snapshot(async_session, run.id) == corrected
        assert [c.args[0] for c in capture.await_args_list] == [
            "automation_run_completed"
        ]
        assert (
            capture.await_args_list[0].kwargs["properties"][
                "reconciled_watchdog_timeout"
            ]
            is True
        )

    async def test_cancel_after_terminal_is_a_conflict_without_side_effects(
        self, async_client, async_session, async_session_factory
    ):
        _, (run,) = await _seed(
            async_session_factory,
            AutomationRunStatus.COMPLETED,
            completed_at=utcnow(),
        )

        with patch(
            "openhands.automation.router.capture_automation_event",
            new_callable=AsyncMock,
        ) as capture:
            response = await async_client.post(
                f"/api/automation/v1/runs/{run.id}/cancel"
            )

        assert response.status_code == 409
        capture.assert_not_awaited()
        assert (await _snapshot(async_session, run.id))[0] == (
            AutomationRunStatus.COMPLETED
        )


class TestDisabledAutomationBulkSkip:
    async def test_counts_only_rows_it_skipped_and_leaves_the_rest(self, dual_factory):
        automation, (pending, running, cancelled) = await _seed(
            dual_factory,
            AutomationRunStatus.PENDING,
            AutomationRunStatus.RUNNING,
            AutomationRunStatus.CANCELLED,
        )
        _, (manual,) = await _seed(
            dual_factory,
            AutomationRunStatus.PENDING,
            automation=automation,
            trigger_source="manual",
        )
        _, (other,) = await _seed(dual_factory, AutomationRunStatus.PENDING)

        async with dual_factory() as session:
            skipped = await skip_pending_runs_for_disabled_automation(
                session, automation.id, reason="disabled"
            )
            await session.commit()

        assert skipped == 1
        statuses = {
            run.id: (await _status(dual_factory, run.id)).status
            for run in (pending, running, cancelled, manual, other)
        }
        assert statuses == {
            pending.id: AutomationRunStatus.SKIPPED,
            running.id: AutomationRunStatus.RUNNING,
            cancelled.id: AutomationRunStatus.CANCELLED,
            manual.id: AutomationRunStatus.PENDING,
            other.id: AutomationRunStatus.PENDING,
        }

    async def test_a_repeat_skips_nothing(self, dual_factory):
        automation, _ = await _seed(dual_factory, AutomationRunStatus.PENDING)

        async with dual_factory() as session:
            first = await skip_pending_runs_for_disabled_automation(
                session, automation.id, reason="disabled", include_manual=True
            )
            second = await skip_pending_runs_for_disabled_automation(
                session, automation.id, reason="disabled", include_manual=True
            )
            await session.commit()

        assert (first, second) == (1, 0)

    async def test_follows_the_callers_rollback(self, dual_factory):
        automation, (pending,) = await _seed(dual_factory, AutomationRunStatus.PENDING)

        async with dual_factory() as session:
            assert (
                await skip_pending_runs_for_disabled_automation(
                    session, automation.id, reason="disabled"
                )
                == 1
            )
            await session.rollback()

        assert (await _status(dual_factory, pending.id)).status == (
            AutomationRunStatus.PENDING
        )
