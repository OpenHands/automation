"""Tests for the watchdog module.

The watchdog processes stale runs (RUNNING but past timeout_at) and marks them
with appropriate status based on sandbox verification results.
"""

import asyncio
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from openhands.automation.config import Settings, clear_config_cache
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
    IntegrationEvent,
)
from openhands.automation.utils import utcnow
from openhands.automation.utils.agent_server import VerificationResult
from openhands.automation.watchdog import (
    PRUNE_BATCH_SIZE,
    _should_cleanup_sandbox_after_terminal,
    _verify_and_mark_run,
    mark_stale_runs,
    prune_integration_events,
    watchdog_loop,
)


# Test UUIDs
TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")


def _create_mock_backend(verification_result: VerificationResult) -> MagicMock:
    """Create a mock backend with configured verification result."""
    mock_backend = MagicMock()
    mock_backend.verify_run = AsyncMock(return_value=verification_result)
    mock_backend.cleanup_after_verification = AsyncMock()
    mock_backend.get_api_key = AsyncMock(return_value="test-api-key")
    return mock_backend


@pytest.fixture
async def automation_with_run(async_session_factory):
    """Create an automation with a RUNNING run that is past timeout."""
    async with async_session_factory() as session:
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test Automation",
            trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run main.py",
            enabled=True,
            timeout=60,
        )
        session.add(automation)
        await session.commit()

        now = utcnow()
        run = AutomationRun(
            automation_id=automation.id,
            status=AutomationRunStatus.RUNNING,
            sandbox_id="test-sandbox-123",
            started_at=now - timedelta(minutes=5),
            timeout_at=now - timedelta(minutes=1),  # Already past timeout
        )
        session.add(run)
        await session.commit()

        yield {"automation": automation, "run": run, "run_id": run.id}


class TestVerifyAndMarkRunExitCodes:
    """Tests for _verify_and_mark_run handling different exit codes."""

    @pytest.mark.asyncio
    async def test_exit_code_0_marks_completed(
        self, async_session_factory, automation_with_run, mock_settings
    ):
        """Exit code 0 means command succeeded - mark as COMPLETED."""
        run_id = automation_with_run["run_id"]

        verification = VerificationResult(
            verified=True,
            success=True,
            exit_code=0,
            stdout="Success output",
            stderr="",
        )

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True
        mock_backend.cleanup_after_verification.assert_called_once()

        # Verify the run was marked as COMPLETED
        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.status == AutomationRunStatus.COMPLETED
            assert run.completed_at is not None
            assert run.error_detail is None

    @pytest.mark.asyncio
    async def test_exit_code_0_keep_alive_true_skips_cleanup(
        self, async_session_factory, automation_with_run, mock_settings
    ):
        """keep_alive=true skips cleanup after verified terminal exit."""
        run_id = automation_with_run["run_id"]

        async with async_session_factory() as session:
            automation = await session.get(
                Automation, automation_with_run["automation"].id
            )
            automation.keep_alive = True
            await session.commit()

        verification = VerificationResult(
            verified=True,
            success=True,
            exit_code=0,
            stdout="Success output",
            stderr="",
        )

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True
        mock_backend.cleanup_after_verification.assert_not_called()

        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.status == AutomationRunStatus.COMPLETED
            assert run.completed_at is not None
            assert run.error_detail is None

    @pytest.mark.asyncio
    async def test_exit_code_minus_1_marks_timed_out(
        self, async_session_factory, automation_with_run, mock_settings
    ):
        """Exit code -1 means command was killed/timed out."""
        run_id = automation_with_run["run_id"]

        verification = VerificationResult(
            verified=True,
            success=False,
            exit_code=-1,
            stdout="",
            stderr="Command timed out after 60 seconds",
        )

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True
        mock_backend.cleanup_after_verification.assert_called_once()

        # Verify the run was marked as FAILED with timeout message
        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.status == AutomationRunStatus.FAILED
            assert run.completed_at is not None
            assert "Timed out" in run.error_detail
            assert "timed out" in run.error_detail.lower()

    @pytest.mark.asyncio
    async def test_exit_code_none_marks_timed_out(
        self, async_session_factory, automation_with_run, mock_settings
    ):
        """Exit code None means command was killed - mark as FAILED with timeout."""
        run_id = automation_with_run["run_id"]

        verification = VerificationResult(
            verified=True,
            success=False,
            exit_code=None,
            stdout="",
            stderr="",
        )

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True
        mock_backend.cleanup_after_verification.assert_called_once()

        # Verify the run was marked as FAILED with timeout message
        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.status == AutomationRunStatus.FAILED
            assert run.completed_at is not None
            assert "Timed out" in run.error_detail

    @pytest.mark.asyncio
    async def test_nonzero_exit_code_marks_failed_without_timeout(
        self, async_session_factory, automation_with_run, mock_settings
    ):
        """Non-zero exit code (not -1) means command failed."""
        run_id = automation_with_run["run_id"]

        verification = VerificationResult(
            verified=True,
            success=False,
            exit_code=1,
            stdout="Some output",
            stderr="Error: something went wrong",
        )

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True
        mock_backend.cleanup_after_verification.assert_called_once()

        # Verify the run was marked as FAILED with exit code (not timeout)
        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.status == AutomationRunStatus.FAILED
            assert run.completed_at is not None
            assert "exit_code=1" in run.error_detail
            assert "Timed out" not in run.error_detail
            assert "stderr: Error: something went wrong" in run.error_detail

    @pytest.mark.asyncio
    async def test_exit_code_127_marks_failed_without_timeout(
        self, async_session_factory, automation_with_run, mock_settings
    ):
        """Exit code 127 (command not found) - mark as FAILED without timeout."""
        run_id = automation_with_run["run_id"]

        verification = VerificationResult(
            verified=True,
            success=False,
            exit_code=127,
            stdout="",
            stderr="bash: command not found",
        )

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True
        mock_backend.cleanup_after_verification.assert_called_once()

        # Verify the run was marked as FAILED with exit code (not timeout)
        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.status == AutomationRunStatus.FAILED
            assert "exit_code=127" in run.error_detail
            assert "Timed out" not in run.error_detail


class TestVerifyAndMarkRunFirstRunOutcome:
    """First-run outcome recording when the watchdog terminates a run."""

    @pytest.mark.asyncio
    async def test_watchdog_failure_records_watchdog_stage(
        self, async_session_factory, mock_settings
    ):
        """A stale template run failed by the watchdog records its stage."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Template Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                timeout=60,
                preset_metadata={
                    "preset_type": "prompt",
                    "prompt": "p",
                    "template": {"id": "tpl", "version": "1.0.0"},
                },
            )
            session.add(automation)
            await session.commit()
            automation_id = automation.id

            now = utcnow()
            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.RUNNING,
                sandbox_id="test-sandbox-123",
                started_at=now - timedelta(minutes=5),
                timeout_at=now - timedelta(minutes=1),
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        verification = VerificationResult(
            verified=True,
            success=False,
            exit_code=3,
            stdout="",
            stderr="boom",
        )
        mock_backend = _create_mock_backend(verification)

        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True
        async with async_session_factory() as session:
            automation = await session.get(Automation, automation_id)
            first_run = automation.preset_metadata["first_run"]
            assert first_run["status"] == "failure"
            assert first_run["failure_stage"] == "watchdog"


class TestVerifyAndMarkRunVerificationFailed:
    """Tests for _verify_and_mark_run when verification fails."""

    @pytest.mark.asyncio
    async def test_verification_failed_with_null_keep_alive_cleans_up(
        self, async_session_factory, automation_with_run, mock_settings
    ):
        """Null keep_alive marks timed out and performs explicit cleanup."""
        run_id = automation_with_run["run_id"]

        verification = VerificationResult(
            verified=False,
            error="Sandbox not available",
        )

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True
        mock_backend.cleanup_after_verification.assert_called_once()

        # Verify the run was marked as FAILED with timeout message
        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.status == AutomationRunStatus.FAILED
            assert run.completed_at is not None
            assert "Timed out" in run.error_detail
            assert "Sandbox not available" in run.error_detail

    @pytest.mark.asyncio
    async def test_verification_failed_keep_alive_true_does_not_cleanup(
        self, async_session_factory, automation_with_run, mock_settings
    ):
        """keep_alive=true leaves cleanup to the runtime TTL reaper."""
        run_id = automation_with_run["run_id"]

        async with async_session_factory() as session:
            automation_id = automation_with_run["automation"].id
            automation = await session.get(Automation, automation_id)
            automation.keep_alive = True
            await session.commit()

        verification = VerificationResult(
            verified=False,
            error="Sandbox not available",
        )

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True
        mock_backend.cleanup_after_verification.assert_not_called()

    @pytest.mark.asyncio
    async def test_verification_failed_keep_alive_false_cleans_up(
        self, async_session_factory, automation_with_run, mock_settings
    ):
        """keep_alive=false preserves explicit cleanup on verification failure."""
        run_id = automation_with_run["run_id"]

        async with async_session_factory() as session:
            automation_id = automation_with_run["automation"].id
            automation = await session.get(Automation, automation_id)
            automation.keep_alive = False
            await session.commit()

        verification = VerificationResult(
            verified=False,
            error="Sandbox not available",
        )

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True
        mock_backend.cleanup_after_verification.assert_called_once()

    @pytest.mark.asyncio
    async def test_transient_verification_error_leaves_run_running(self, mock_settings):
        """Transient verification errors do not fail or clean up the run."""
        verification = VerificationResult(
            verified=False,
            error=(
                "Sandbox API temporarily unavailable while checking "
                "sandbox-123: HTTP 429"
            ),
            transient=True,
        )
        run = MagicMock()
        run.id = uuid.uuid4()
        run.sandbox_id = "sandbox-123"
        run.status_detail = None
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock(rowcount=1))

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
        ):
            result = await _verify_and_mark_run(session, run, mock_settings)

        assert result is False
        session.execute.assert_awaited_once()
        stmt = session.execute.await_args.args[0]
        params = stmt.compile().params
        assert params["status_detail"]["phase"] == "verification"
        assert params["status_detail"]["transient"] is True
        assert params["status_detail"]["detail"] == verification.detail
        mock_backend.cleanup_after_verification.assert_not_called()


class TestVerifyAndMarkRunStillRunning:
    """Tests for the bounded deferral when the bash command may still be running."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "verification_error",
        ["Command still running", "No bash output found"],
    )
    async def test_still_running_defers_instead_of_failing(
        self,
        async_session_factory,
        automation_with_run,
        mock_settings,
        verification_error,
    ):
        """A still-running command defers timeout_at; no FAILED, no cleanup."""
        run_id = automation_with_run["run_id"]

        verification = VerificationResult(
            verified=False,
            error=verification_error,
        )

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is False
        mock_backend.cleanup_after_verification.assert_not_called()

        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.status == AutomationRunStatus.RUNNING
            assert run.completed_at is None
            # Deadline pushed up to one watchdog interval into the future.
            deferred_for = (run.timeout_at - utcnow()).total_seconds()
            assert 0 < deferred_for <= mock_settings.watchdog_interval_seconds

    @pytest.mark.asyncio
    async def test_still_running_past_hard_cap_marks_timed_out(
        self, async_session_factory, automation_with_run, mock_settings
    ):
        """Exhausting the hard grace makes still-running a terminal timeout."""
        run_id = automation_with_run["run_id"]

        # Push the run's start far beyond ready-timeout + budget + hard grace.
        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            run.started_at = utcnow() - timedelta(hours=2)
            await session.commit()

        verification = VerificationResult(
            verified=False,
            error="Command still running",
        )

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True
        mock_backend.cleanup_after_verification.assert_called_once()

        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.status == AutomationRunStatus.FAILED
            assert run.completed_at is not None
            assert "Timed out" in run.error_detail
            assert "Command still running" in run.error_detail

    @pytest.mark.asyncio
    async def test_still_running_concurrent_callback_wins(
        self, async_session_factory, automation_with_run, mock_settings
    ):
        """A callback that lands mid-scan is preserved; the deferral is a no-op."""
        run_id = automation_with_run["run_id"]

        verification = VerificationResult(
            verified=False,
            error="Command still running",
        )

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                stale_timeout_at = run.timeout_at
                # Callback commits COMPLETED after the watchdog loaded the row.
                async with async_session_factory() as other_session:
                    other_run = await other_session.get(AutomationRun, run_id)
                    other_run.status = AutomationRunStatus.COMPLETED
                    await other_session.commit()
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is False

        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.status == AutomationRunStatus.COMPLETED
            assert run.timeout_at == stale_timeout_at


def _make_event(age: timedelta, index: int = 0) -> IntegrationEvent:
    """An accepted event received `age` ago."""
    return IntegrationEvent(
        org_id=TEST_ORG_ID,
        source="github",
        provider_event_id=f"delivery-{index}",
        event_key="push",
        payload={"ref": "refs/heads/main"},
        received_at=utcnow() - age,
    )


class TestPruneIntegrationEvents:
    """Pruning keeps `integration_events` bounded without a loop of its own."""

    @pytest.mark.asyncio
    async def test_prunes_only_past_the_retention_window(self, async_session_factory):
        """Old rows go; anything inside the window stays deduplicable."""
        settings = Settings(integration_event_retention_days=14)

        async with async_session_factory() as session:
            session.add(_make_event(timedelta(days=15), index=1))
            session.add(_make_event(timedelta(days=13), index=2))
            await session.commit()

        assert await prune_integration_events(async_session_factory, settings) == 1

        async with async_session_factory() as session:
            surviving = (
                (await session.execute(select(IntegrationEvent.provider_event_id)))
                .scalars()
                .all()
            )
        assert list(surviving) == ["delivery-2"]

    @pytest.mark.asyncio
    async def test_is_a_no_op_when_nothing_has_expired(self, async_session_factory):
        """The common case costs one DELETE that matches nothing."""
        settings = Settings(integration_event_retention_days=14)

        async with async_session_factory() as session:
            session.add(_make_event(timedelta(hours=1)))
            await session.commit()

        assert await prune_integration_events(async_session_factory, settings) == 0

    @pytest.mark.asyncio
    async def test_deletes_at_most_one_batch_per_scan(
        self, async_session_factory, monkeypatch
    ):
        """A backlog drains over several scans rather than one long DELETE."""
        monkeypatch.setattr("openhands.automation.watchdog.PRUNE_BATCH_SIZE", 2)
        settings = Settings(integration_event_retention_days=1)

        async with async_session_factory() as session:
            for index in range(3):
                session.add(_make_event(timedelta(days=2), index=index))
            await session.commit()

        assert await prune_integration_events(async_session_factory, settings) == 2
        assert await prune_integration_events(async_session_factory, settings) == 1
        assert await prune_integration_events(async_session_factory, settings) == 0

    def test_batch_size_is_bounded(self):
        """An unbounded default is the bug this guards."""
        assert 0 < PRUNE_BATCH_SIZE <= 10_000


class TestSubjectOwningRunsKeepTheirSandbox:
    """A `continue_conversation` run's sandbox holds the live conversation.

    Creation forces `keep_alive` on such an automation, so the hold is the
    ordinary keep_alive one. The watchdog is the other way a run reaches a
    terminal state -- a lost completion callback is the ordinary reason -- and
    deleting the sandbox there loses the thread just as thoroughly.
    """

    def test_the_helper_holds_a_kept_sandbox(self):
        run = MagicMock(spec=AutomationRun)
        run.sandbox_id = "sbx-1"
        run.subject_key = "T06P212QSEA/C123/1755000000.000100"
        assert _should_cleanup_sandbox_after_terminal(run, keep_alive=True) is False

    def test_an_ordinary_run_is_still_cleaned_up(self):
        run = MagicMock(spec=AutomationRun)
        run.sandbox_id = "sbx-1"
        run.subject_key = None
        assert _should_cleanup_sandbox_after_terminal(run, keep_alive=False) is True

    @pytest.mark.asyncio
    async def test_watchdog_does_not_delete_the_conversations_sandbox(
        self, async_session_factory, automation_with_run, mock_settings
    ):
        """The whole feature depends on that sandbox outliving the run."""
        run_id = automation_with_run["run_id"]
        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            run.subject_key = "T06P212QSEA/C123/1755000000.000100"
            automation = await session.get(Automation, run.automation_id)
            automation.keep_alive = True
            await session.commit()

        mock_backend = _create_mock_backend(
            VerificationResult(
                verified=True, success=True, exit_code=0, stdout="ok", stderr=""
            )
        )
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        mock_backend.cleanup_after_verification.assert_not_called()

        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.status == AutomationRunStatus.COMPLETED
            # The key stays on the row: it is the record of what this run was
            # about, and nothing has released it.
            assert run.subject_key == "T06P212QSEA/C123/1755000000.000100"
            assert run.subject_released_at is None


@pytest.mark.asyncio
class TestMarkStaleRunsAutoDisable:
    """The watchdog must re-check auto-disable after it authors a failure.

    Automations that only ever time out never reach the callback or dispatcher
    paths, so this wiring is the only thing that pauses them.
    """

    async def test_watchdog_timeout_disables_a_chronically_failing_automation(
        self, async_session_factory, mock_settings, monkeypatch
    ):
        monkeypatch.setenv("AUTOMATION_CONSECUTIVE_FAILURE_DISABLE_THRESHOLD", "10")
        monkeypatch.setenv("AUTOMATION_CONSECUTIVE_FAILURE_DISABLE_WINDOW_HOURS", "24")
        clear_config_cache()

        now = utcnow()
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Only ever times out",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                timeout=60,
            )
            session.add(automation)
            await session.flush()
            automation_id = automation.id

            for i in range(9):
                session.add(
                    AutomationRun(
                        automation_id=automation_id,
                        status=AutomationRunStatus.FAILED,
                        created_at=now - timedelta(hours=i + 1),
                        completed_at=now - timedelta(hours=i + 1),
                    )
                )
            # The 10th run is stale and RUNNING; the watchdog marks it FAILED.
            session.add(
                AutomationRun(
                    automation_id=automation_id,
                    status=AutomationRunStatus.RUNNING,
                    sandbox_id="sb-timeout",
                    started_at=now - timedelta(minutes=5),
                    timeout_at=now - timedelta(minutes=1),
                )
            )
            await session.commit()

        mock_backend = _create_mock_backend(
            VerificationResult(verified=False, error="Sandbox not available")
        )
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
        ):
            marked = await mark_stale_runs(async_session_factory, mock_settings)

        assert marked == 1
        async with async_session_factory() as session:
            automation = await session.get(Automation, automation_id)
            assert automation is not None
            assert automation.enabled is False
            assert automation.disabled_detail is not None
            assert automation.disabled_detail["rule"] == "consecutive_failures"

        clear_config_cache()

    async def test_watchdog_leaves_a_healthy_automation_enabled(
        self, async_session_factory, mock_settings, monkeypatch, automation_with_run
    ):
        """One timeout is not grounds to pause; the rule needs the streak."""
        monkeypatch.setenv("AUTOMATION_CONSECUTIVE_FAILURE_DISABLE_THRESHOLD", "10")
        clear_config_cache()

        automation_id = automation_with_run["automation"].id
        mock_backend = _create_mock_backend(
            VerificationResult(verified=False, error="Sandbox not available")
        )
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
        ):
            marked = await mark_stale_runs(async_session_factory, mock_settings)

        assert marked == 1
        async with async_session_factory() as session:
            automation = await session.get(Automation, automation_id)
            assert automation is not None
            assert automation.enabled is True

        clear_config_cache()


@pytest.mark.asyncio
async def test_watchdog_runs_local_workspace_purge_in_same_cycle(monkeypatch):
    """The watchdog owns workspace purge instead of starting another loop."""
    shutdown_event = asyncio.Event()
    calls = []

    async def mark_stale(*_args):
        calls.append("stale")
        return 0

    async def prune_events(*_args):
        calls.append("events")
        return 0

    async def purge_workspaces(*_args, **kwargs):
        calls.append("workspaces")
        assert kwargs["deferred_last_cycle"] == set()
        shutdown_event.set()
        return None

    monkeypatch.setattr("openhands.automation.watchdog.mark_stale_runs", mark_stale)
    monkeypatch.setattr(
        "openhands.automation.watchdog.prune_integration_events", prune_events
    )
    monkeypatch.setattr(
        "openhands.automation.watchdog.purge_terminal_workspaces", purge_workspaces
    )

    settings = Settings(
        agent_server_url="http://localhost:3000",
        watchdog_interval_seconds=60,
        workspace_retention_seconds=3600,
        workspace_base="/workspace",
    )
    await watchdog_loop(AsyncMock(), settings, shutdown_event)

    assert calls == ["stale", "events", "workspaces"]


@pytest.mark.asyncio
async def test_watchdog_forwards_shutdown_event_to_workspace_purge(monkeypatch):
    """Without this the purge's own shutdown checks can never fire."""
    shutdown_event = asyncio.Event()
    seen = []

    async def mark_stale(*_args):
        return 0

    async def prune_events(*_args):
        return 0

    async def purge_workspaces(*_args, **kwargs):
        seen.append(kwargs.get("shutdown_event"))
        shutdown_event.set()
        return None

    monkeypatch.setattr("openhands.automation.watchdog.mark_stale_runs", mark_stale)
    monkeypatch.setattr(
        "openhands.automation.watchdog.prune_integration_events", prune_events
    )
    monkeypatch.setattr(
        "openhands.automation.watchdog.purge_terminal_workspaces", purge_workspaces
    )

    settings = Settings(
        agent_server_url="http://localhost:3000",
        watchdog_interval_seconds=60,
        workspace_retention_seconds=3600,
        workspace_base="/workspace",
    )
    await watchdog_loop(AsyncMock(), settings, shutdown_event)

    assert seen == [shutdown_event]


@pytest.mark.asyncio
async def test_watchdog_carries_workspace_deferred_state_between_cycles(monkeypatch):
    """Workspace ordering state survives from one janitor cycle to the next."""
    shutdown_event = asyncio.Event()
    deferred_id = uuid.uuid4()
    seen_states = []

    async def mark_stale(*_args):
        return 0

    async def prune_events(*_args):
        return 0

    async def purge_workspaces(*_args, **kwargs):
        deferred_last_cycle = kwargs["deferred_last_cycle"]
        seen_states.append(set(deferred_last_cycle))
        if len(seen_states) == 1:
            deferred_last_cycle.add(deferred_id)
        else:
            shutdown_event.set()
        return None

    monkeypatch.setattr("openhands.automation.watchdog.mark_stale_runs", mark_stale)
    monkeypatch.setattr(
        "openhands.automation.watchdog.prune_integration_events", prune_events
    )
    monkeypatch.setattr(
        "openhands.automation.watchdog.purge_terminal_workspaces", purge_workspaces
    )

    settings = Settings(
        agent_server_url="http://localhost:3000",
        watchdog_interval_seconds=1,
        workspace_retention_seconds=3600,
        workspace_base="/workspace",
    )
    await watchdog_loop(AsyncMock(), settings, shutdown_event)

    assert seen_states == [set(), {deferred_id}]
