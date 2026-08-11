"""Tests for classified automation failures and auto-disablement."""

import uuid

from sqlalchemy import select

from openhands.automation.exceptions import PermanentDispatchError
from openhands.automation.health import classify_exception
from openhands.automation.models import Automation, AutomationRun, AutomationRunStatus
from openhands.automation.utils.run import mark_run_terminal
from openhands.sdk.event.error_classification import FailureKind
from openhands.sdk.llm.exceptions import (
    LLMAuthenticationError,
    LLMRateLimitError,
    LLMTimeoutError,
)


TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")


class TestFailureClassification:
    """Permanent vs transient classification and disablement eligibility."""

    def test_permanent_config_fault_counts(self):
        assert classify_exception(PermanentDispatchError("tarball gone")).kind is (
            FailureKind.CONFIG
        )

    def test_service_owned_value_error_is_config(self):
        failure = classify_exception(ValueError("unsupported tarball path"))
        assert failure.kind is FailureKind.CONFIG

    def test_auth_failure_counts(self):
        failure = classify_exception(LLMAuthenticationError("bad key"))
        assert failure.kind is FailureKind.AUTH
        assert failure.counts_toward_disablement

    def test_rate_limit_is_transient(self):
        failure = classify_exception(LLMRateLimitError("429"))
        assert failure.kind is FailureKind.RATE_LIMIT
        assert not failure.counts_toward_disablement

    def test_timeout_is_transient(self):
        failure = classify_exception(LLMTimeoutError("timed out"))
        assert failure.kind is FailureKind.TRANSIENT
        assert not failure.counts_toward_disablement


async def _new_automation(session_factory) -> uuid.UUID:
    async with session_factory() as session:
        auto = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Health Test",
            trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
            tarball_path="https://example.com/a.tar.gz",
            entrypoint="uv run main.py",
            enabled=True,
        )
        session.add(auto)
        await session.commit()
        return auto.id


async def _run_config_failure(session_factory, automation_id, threshold=3):
    """Create a RUNNING run and mark it FAILED as a config fault."""
    async with session_factory() as session:
        run = AutomationRun(
            automation_id=automation_id,
            status=AutomationRunStatus.RUNNING,
        )
        session.add(run)
        await session.commit()
        run_id = run.id
    async with session_factory() as session:
        run = await session.get(AutomationRun, run_id)
        await mark_run_terminal(
            session_factory,
            run,
            AutomationRunStatus.FAILED,
            "config error",
            failure_kind=FailureKind.CONFIG,
            threshold=threshold,
        )


async def _mark_terminal(session_factory, automation_id, *, error, failure_kind):
    async with session_factory() as session:
        run = AutomationRun(
            automation_id=automation_id,
            status=AutomationRunStatus.RUNNING,
        )
        session.add(run)
        await session.commit()
        run_id = run.id
    async with session_factory() as session:
        run = await session.get(AutomationRun, run_id)
        await mark_run_terminal(
            session_factory,
            run,
            AutomationRunStatus.FAILED,
            error,
            failure_kind=failure_kind,
        )


async def _get_automation(session_factory, automation_id) -> Automation:
    async with session_factory() as session:
        return await session.get(Automation, automation_id)


class TestAutoDisableThreshold:
    """Disabled only after the configured consecutive-failure threshold."""

    async def test_third_config_failure_disables(self, async_session_factory):
        automation_id = await _new_automation(async_session_factory)
        for _ in range(2):
            await _run_config_failure(async_session_factory, automation_id)
        auto = await _get_automation(async_session_factory, automation_id)
        assert auto.enabled is True
        assert auto.consecutive_unhealthy_runs == 2

        await _run_config_failure(async_session_factory, automation_id)
        auto = await _get_automation(async_session_factory, automation_id)
        assert auto.enabled is False
        assert auto.consecutive_unhealthy_runs == 3
        assert auto.disabled_reason is not None

    async def test_transient_failure_resets_streak(self, async_session_factory):
        automation_id = await _new_automation(async_session_factory)
        await _run_config_failure(async_session_factory, automation_id)
        await _run_config_failure(async_session_factory, automation_id)
        await _mark_terminal(
            async_session_factory,
            automation_id,
            error="rate limited",
            failure_kind=FailureKind.RATE_LIMIT,
        )
        auto = await _get_automation(async_session_factory, automation_id)
        assert auto.consecutive_unhealthy_runs == 0
        assert auto.enabled is True

    async def test_disabling_skips_pending_runs(self, async_session_factory):
        automation_id = await _new_automation(async_session_factory)
        async with async_session_factory() as session:
            for _ in range(2):
                session.add(
                    AutomationRun(
                        automation_id=automation_id,
                        status=AutomationRunStatus.PENDING,
                    )
                )
            await session.commit()

        for _ in range(3):
            await _run_config_failure(async_session_factory, automation_id)

        async with async_session_factory() as session:
            runs = (
                (
                    await session.execute(
                        select(AutomationRun).where(
                            AutomationRun.automation_id == automation_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        pending = [r for r in runs if r.status is AutomationRunStatus.PENDING]
        skipped = [r for r in runs if r.status is AutomationRunStatus.SKIPPED]
        assert pending == []
        assert len(skipped) == 2


class TestDisabledDispatchApi:
    """Disabled dispatch is rejected and callback classifications persist."""

    async def test_dispatch_disabled_automation_returns_409(
        self, async_client, async_session
    ):
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Disabled Test",
            trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
            enabled=False,
            disabled_reason="3 consecutive config failures",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.post(
            f"/api/automation/v1/{automation.id}/dispatch"
        )
        assert response.status_code == 409
        assert "3 consecutive config failures" in response.json()["detail"]

    async def test_failed_callback_persists_classification(
        self, async_client, async_session
    ):
        from openhands.sdk.event.conversation_error import ConversationErrorEvent

        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Failed Test",
            trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        run = AutomationRun(
            automation_id=automation.id,
            status=AutomationRunStatus.RUNNING,
        )
        async_session.add(run)
        await async_session.commit()

        error = ConversationErrorEvent(
            source="environment",
            code="LLMAuthenticationError",
            detail="invalid api key",
        )
        response = await async_client.post(
            f"/api/automation/v1/runs/{run.id}/complete",
            json={
                "status": "FAILED",
                "error": error.model_dump(mode="json"),
            },
        )
        assert response.status_code == 200
        assert response.json()["failure_kind"] == "auth"
        assert response.json()["error_detail"] == "invalid api key"

        await async_session.refresh(run)
        assert run.failure_kind == "auth"
        assert run.error_detail == "invalid api key"
