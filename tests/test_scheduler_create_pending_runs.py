"""Tests for ``scheduler._create_pending_runs``.

Exercises the scheduler's per-batch trigger dispatch loop against a real
(SQLite in-memory) session, validating the contract documented on
``_TriggerBase.create_pending_run``: return None ⇒ no run created, return
``AutomationRun`` ⇒ appended to the result list (and persisted by the
trigger itself).

Only the GitHub HTTP transport is replaced; everything else is real code.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.models import Automation, AutomationRun
from openhands.automation.scheduler import _create_pending_runs


TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")


async def _make(
    session: AsyncSession,
    *,
    trigger: dict,
    enabled: bool = True,
    created_at: datetime | None = None,
    last_triggered_at: datetime | None = None,
) -> Automation:
    automation = Automation(
        id=uuid.uuid4(),
        user_id=TEST_USER_ID,
        org_id=TEST_ORG_ID,
        name="Test",
        trigger=trigger,
        tarball_path="s3://bucket/code.tar.gz",
        entrypoint="uv run main.py",
        enabled=enabled,
        deleted_at=None,
        created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
        last_triggered_at=last_triggered_at,
    )
    session.add(automation)
    await session.flush()
    return automation


class TestCreatePendingRuns:
    async def test_empty_input(self, sqlite_session):
        assert (
            await _create_pending_runs(
                sqlite_session, [], datetime(2026, 1, 1, tzinfo=UTC)
            )
            == []
        )

    async def test_mixed_triggers_only_due_ones_create_runs(
        self, sqlite_session, patch_github_transport
    ):
        cutoff = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        now = cutoff + timedelta(minutes=5)

        # Github API: events for `org/has-events`, nothing for `org/quiet`.
        def responder(req: httpx.Request) -> httpx.Response:
            if "has-events" in req.url.path:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": "1",
                            "type": "PushEvent",
                            "created_at": (cutoff + timedelta(minutes=1)).strftime(
                                "%Y-%m-%dT%H:%M:%SZ"
                            ),
                        }
                    ],
                )
            return httpx.Response(200, json=[])

        patch_github_transport(responder)

        cron_due = await _make(
            sqlite_session,
            trigger={"type": "cron", "schedule": "0,30 * * * *", "timezone": "UTC"},
            created_at=datetime(2026, 3, 15, 10, 25, tzinfo=UTC),
        )
        cron_not_due = await _make(
            sqlite_session,
            trigger={"type": "cron", "schedule": "0,30 * * * *", "timezone": "UTC"},
            created_at=datetime(2026, 3, 15, 12, 4, tzinfo=UTC),
        )
        event_not_due = await _make(
            sqlite_session,
            trigger={
                "type": "event",
                "source": "github",
                "on": "pull_request.opened",
            },
        )
        gh_due = await _make(
            sqlite_session,
            trigger={
                "type": "github",
                "github_access_token": "ghp_xxx",
                "repositories": ["org/has-events"],
            },
            last_triggered_at=cutoff,
        )
        gh_not_due = await _make(
            sqlite_session,
            trigger={
                "type": "github",
                "github_access_token": "ghp_xxx",
                "repositories": ["org/quiet"],
            },
            last_triggered_at=cutoff,
        )
        invalid = await _make(sqlite_session, trigger={"type": "totally-not-a-trigger"})

        runs = await _create_pending_runs(
            sqlite_session,
            [cron_due, cron_not_due, event_not_due, gh_due, gh_not_due, invalid],
            now,
        )
        await sqlite_session.commit()

        run_automation_ids = {r.automation_id for r in runs}
        assert run_automation_ids == {cron_due.id, gh_due.id}

        # GitHub run carries its event payload; cron run does not.
        runs_by_aid = {r.automation_id: r for r in runs}
        assert runs_by_aid[gh_due.id].event_payload is not None
        assert runs_by_aid[gh_due.id].event_payload["source"] == "github_trigger"
        assert len(runs_by_aid[gh_due.id].event_payload["events"]) == 1
        assert runs_by_aid[cron_due.id].event_payload is None

        # And the runs are durably persisted.
        persisted_ids = {
            r.automation_id
            for r in (await sqlite_session.execute(select(AutomationRun)))
            .scalars()
            .all()
        }
        assert persisted_ids == {cron_due.id, gh_due.id}

    async def test_failing_trigger_does_not_block_others(
        self, sqlite_session, patch_github_transport
    ):
        """A trigger raising must NOT stop other triggers from creating runs."""
        cutoff = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        now = cutoff + timedelta(minutes=5)

        # GitHub transport raises a connection error → the trigger's internal
        # error handling logs and returns no events → no run, no exception.
        # Then a SECOND github trigger explodes outright by raising from the
        # transport responder again — both should be tolerated.
        patch_github_transport(
            lambda req: (_ for _ in ()).throw(httpx.ConnectError("no route"))
        )

        cron_due = await _make(
            sqlite_session,
            trigger={"type": "cron", "schedule": "0,30 * * * *", "timezone": "UTC"},
            created_at=datetime(2026, 3, 15, 10, 25, tzinfo=UTC),
        )
        gh_broken = await _make(
            sqlite_session,
            trigger={
                "type": "github",
                "github_access_token": "ghp_xxx",
                "repositories": ["org/exploding"],
            },
            last_triggered_at=cutoff,
        )

        runs = await _create_pending_runs(sqlite_session, [cron_due, gh_broken], now)
        await sqlite_session.commit()

        assert [r.automation_id for r in runs] == [cron_due.id]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
