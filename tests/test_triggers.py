"""Tests for the trigger schemas and their ``create_pending_run`` methods.

These tests exercise the real :class:`CronTrigger`/:class:`EventTrigger`/
:class:`GithubTrigger`/:class:`SlackTrigger` code paths against a real
(SQLite in-memory) session, so
:func:`openhands.automation.utils.run.create_pending_run` runs against a
real database. Only the GitHub/Slack HTTP transports are replaced via
``httpx.MockTransport`` (see the ``patch_github_transport`` /
``patch_slack_transport`` fixtures); the trigger code — including
header/auth construction, response parsing, timestamp filtering, and
parallel resource fan-out — is untouched.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.models import Automation, AutomationRun
from openhands.automation.schemas import (
    EventTrigger,
    GithubTrigger,
    SlackTrigger,
    TriggerAdapter,
)


# --- helpers ----------------------------------------------------------------


TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")


async def _make_automation(
    session: AsyncSession,
    *,
    trigger: dict[str, Any],
    enabled: bool = True,
    deleted_at: datetime | None = None,
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
        deleted_at=deleted_at,
        created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
        last_triggered_at=last_triggered_at,
    )
    session.add(automation)
    await session.flush()
    return automation


def _gh_event(
    event_id: int, created_at: datetime, event_type: str = "PushEvent"
) -> dict[str, Any]:
    return {
        "id": str(event_id),
        "type": event_type,
        "created_at": created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ---------------------------------------------------------------------------
# CronTrigger.create_pending_run
# ---------------------------------------------------------------------------


class TestCronTriggerCreatesRun:
    async def test_creates_run_when_due(self, sqlite_session):
        trigger_cfg = {
            "type": "cron",
            "schedule": "0,30 * * * *",
            "timezone": "UTC",
        }
        # Every 30 min; created 10 min before the 10:30 fire window.
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger_cfg,
            created_at=datetime(2026, 3, 15, 10, 25, tzinfo=UTC),
        )
        trigger = TriggerAdapter.validate_python(trigger_cfg)
        now = datetime(2026, 3, 15, 10, 35, tzinfo=UTC)

        run = await trigger.create_pending_run(sqlite_session, automation, now)
        await sqlite_session.commit()

        assert run is not None
        assert run.automation_id == automation.id
        # Round-trip via DB to confirm the row was persisted.
        from_db = (
            (
                await sqlite_session.execute(
                    select(AutomationRun).where(AutomationRun.id == run.id)
                )
            )
            .scalars()
            .first()
        )
        assert from_db is not None
        # Cron triggers don't attach an event payload.
        assert from_db.event_payload is None
        # The util bumped last_triggered_at on the automation.
        assert automation.last_triggered_at is not None

    async def test_returns_none_when_not_due(self, sqlite_session):
        trigger_cfg = {
            "type": "cron",
            "schedule": "0,30 * * * *",
            "timezone": "UTC",
        }
        # Automation was JUST created — the most recent fire (10:30) is BEFORE
        # creation (10:34), so it shouldn't fire yet.
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger_cfg,
            created_at=datetime(2026, 3, 15, 10, 34, tzinfo=UTC),
        )
        trigger = TriggerAdapter.validate_python(trigger_cfg)
        now = datetime(2026, 3, 15, 10, 35, tzinfo=UTC)

        assert await trigger.create_pending_run(sqlite_session, automation, now) is None

    async def test_returns_none_when_disabled(self, sqlite_session):
        trigger_cfg = {"type": "cron", "schedule": "* * * * *", "timezone": "UTC"}
        automation = await _make_automation(
            sqlite_session, trigger=trigger_cfg, enabled=False
        )
        trigger = TriggerAdapter.validate_python(trigger_cfg)
        assert await trigger.create_pending_run(sqlite_session, automation) is None


# ---------------------------------------------------------------------------
# EventTrigger.create_pending_run
# ---------------------------------------------------------------------------


class TestEventTriggerNeverFires:
    async def test_polling_never_creates_a_run(self, sqlite_session):
        trigger_cfg = {
            "type": "event",
            "source": "github",
            "on": "pull_request.opened",
        }
        automation = await _make_automation(sqlite_session, trigger=trigger_cfg)
        trigger = EventTrigger.model_validate(trigger_cfg)
        assert await trigger.create_pending_run(sqlite_session, automation) is None


# ---------------------------------------------------------------------------
# GithubTrigger — validation
# ---------------------------------------------------------------------------


class TestGithubTriggerValidation:
    def test_minimum_valid_config(self):
        trigger = GithubTrigger(
            github_access_token=SecretStr("ghp_xxx"),
            repositories=["All-Hands-AI/OpenHands"],
        )
        assert trigger.type == "github"
        assert trigger.repositories == ["All-Hands-AI/OpenHands"]
        # Secret never leaks via repr.
        assert "ghp_xxx" not in repr(trigger)
        assert trigger.github_access_token.get_secret_value() == "ghp_xxx"

    def test_invalid_repository_format_rejected(self):
        with pytest.raises(ValidationError, match="Invalid repository"):
            GithubTrigger(
                github_access_token=SecretStr("ghp_xxx"),
                repositories=["not-a-repo"],
            )

    def test_empty_repositories_rejected(self):
        with pytest.raises(ValidationError):
            GithubTrigger(
                github_access_token=SecretStr("ghp_xxx"),
                repositories=[],
            )

    def test_valid_jmespath_event_filter_is_accepted(self):
        trigger = GithubTrigger(
            github_access_token=SecretStr("ghp_xxx"),
            repositories=["foo/bar"],
            event_filter="type == 'PushEvent' && payload.ref == 'refs/heads/main'",
        )
        assert (
            trigger.event_filter
            == "type == 'PushEvent' && payload.ref == 'refs/heads/main'"
        )

    def test_invalid_jmespath_event_filter_rejected(self):
        with pytest.raises(ValidationError, match="Invalid JMESPath expression"):
            GithubTrigger(
                github_access_token=SecretStr("ghp_xxx"),
                repositories=["foo/bar"],
                event_filter="this is not valid jmespath @@@",
            )

    def test_empty_event_filter_becomes_none(self):
        # Whitespace-only filter shouldn't error and shouldn't be retained.
        trigger = GithubTrigger(
            github_access_token=SecretStr("ghp_xxx"),
            repositories=["foo/bar"],
            event_filter="   ",
        )
        assert trigger.event_filter is None

    def test_discriminated_union_dispatches_to_github(self):
        parsed = TriggerAdapter.validate_python(
            {
                "type": "github",
                "github_access_token": "ghp_yyy",
                "repositories": ["foo/bar"],
            }
        )
        assert isinstance(parsed, GithubTrigger)


# ---------------------------------------------------------------------------
# GithubTrigger.create_pending_run
# ---------------------------------------------------------------------------


class TestGithubTriggerCreatesRun:
    async def test_fires_and_attaches_events_to_payload(
        self, sqlite_session, patch_github_transport
    ):
        cutoff = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        fresh = _gh_event(2, cutoff + timedelta(minutes=5))
        seen = patch_github_transport(
            lambda req: __import__("httpx").Response(200, json=[fresh])
        )

        trigger = GithubTrigger(
            github_access_token=SecretStr("ghp_xxx"),
            repositories=["foo/bar"],
        )
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger.model_dump(mode="python"),
            last_triggered_at=cutoff,
        )

        run = await trigger.create_pending_run(sqlite_session, automation)
        await sqlite_session.commit()

        assert run is not None
        assert run.automation_id == automation.id
        assert run.event_payload is not None
        assert run.event_payload["source"] == "github_trigger"
        assert len(run.event_payload["events"]) == 1
        event = run.event_payload["events"][0]
        assert event["id"] == "2"
        assert event["type"] == "PushEvent"
        # The trigger tags each event with the source repo.
        assert event["_repository"] == "foo/bar"

        # Sanity-check the outgoing HTTP request.
        assert len(seen) == 1
        assert seen[0].url.path == "/repos/foo/bar/events"
        assert seen[0].headers["Authorization"] == "Bearer ghp_xxx"

    async def test_returns_none_when_no_new_events(
        self, sqlite_session, patch_github_transport
    ):
        cutoff = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        # All events are older than cutoff.
        stale = _gh_event(1, cutoff - timedelta(minutes=10))
        patch_github_transport(
            lambda req: __import__("httpx").Response(200, json=[stale])
        )

        trigger = GithubTrigger(
            github_access_token=SecretStr("ghp_xxx"),
            repositories=["foo/bar"],
        )
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger.model_dump(mode="python"),
            last_triggered_at=cutoff,
        )

        assert await trigger.create_pending_run(sqlite_session, automation) is None
        # No run was persisted.
        runs = (await sqlite_session.execute(select(AutomationRun))).scalars().all()
        assert runs == []

    async def test_jmespath_filter_excludes_non_matching(
        self, sqlite_session, patch_github_transport
    ):
        """``event_filter`` drops events whose JMESPath expression is falsy."""
        cutoff = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        events = [_gh_event(2, cutoff + timedelta(minutes=5), event_type="IssuesEvent")]
        patch_github_transport(
            lambda req: __import__("httpx").Response(200, json=events)
        )

        trigger = GithubTrigger(
            github_access_token=SecretStr("ghp_xxx"),
            repositories=["foo/bar"],
            event_filter="type == 'PushEvent'",
        )
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger.model_dump(mode="python"),
            last_triggered_at=cutoff,
        )
        assert await trigger.create_pending_run(sqlite_session, automation) is None

    async def test_jmespath_filter_keeps_matching(
        self, sqlite_session, patch_github_transport
    ):
        """A truthy JMESPath result keeps the event and fires the trigger."""
        cutoff = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        events = [
            _gh_event(7, cutoff + timedelta(minutes=1), event_type="IssuesEvent"),
            _gh_event(8, cutoff + timedelta(minutes=2), event_type="PushEvent"),
        ]
        patch_github_transport(
            lambda req: __import__("httpx").Response(200, json=events)
        )

        trigger = GithubTrigger(
            github_access_token=SecretStr("ghp_xxx"),
            repositories=["foo/bar"],
            event_filter="type == 'PushEvent'",
        )
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger.model_dump(mode="python"),
            last_triggered_at=cutoff,
        )
        run = await trigger.create_pending_run(sqlite_session, automation)
        assert run is not None
        # Only the PushEvent survived the filter.
        kept = run.event_payload["events"]
        assert [e["id"] for e in kept] == ["8"]

    async def test_jmespath_filter_supports_nested_payload(
        self, sqlite_session, patch_github_transport
    ):
        """JMESPath can match against the nested ``payload`` object."""
        cutoff = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        events = [
            {
                "id": "9",
                "type": "PullRequestEvent",
                "created_at": (cutoff + timedelta(minutes=1)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "payload": {"action": "closed"},
            },
            {
                "id": "10",
                "type": "PullRequestEvent",
                "created_at": (cutoff + timedelta(minutes=2)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "payload": {"action": "opened"},
            },
        ]
        patch_github_transport(
            lambda req: __import__("httpx").Response(200, json=events)
        )

        trigger = GithubTrigger(
            github_access_token=SecretStr("ghp_xxx"),
            repositories=["foo/bar"],
            event_filter=("type == 'PullRequestEvent' && payload.action == 'opened'"),
        )
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger.model_dump(mode="python"),
            last_triggered_at=cutoff,
        )
        run = await trigger.create_pending_run(sqlite_session, automation)
        assert run is not None
        kept = run.event_payload["events"]
        assert [e["id"] for e in kept] == ["10"]

    async def test_uses_created_at_when_never_triggered(
        self, sqlite_session, patch_github_transport
    ):
        created = datetime(2026, 3, 15, 9, 0, 0, tzinfo=UTC)
        events = [_gh_event(3, created + timedelta(hours=1))]
        patch_github_transport(
            lambda req: __import__("httpx").Response(200, json=events)
        )

        trigger = GithubTrigger(
            github_access_token=SecretStr("ghp_xxx"),
            repositories=["foo/bar"],
        )
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger.model_dump(mode="python"),
            created_at=created,
            last_triggered_at=None,
        )

        run = await trigger.create_pending_run(sqlite_session, automation)
        assert run is not None
        assert len(run.event_payload["events"]) == 1

    async def test_disabled_short_circuits_before_http(
        self, sqlite_session, patch_github_transport
    ):
        seen = patch_github_transport(
            lambda req: __import__("httpx").Response(200, json=[])
        )
        trigger = GithubTrigger(
            github_access_token=SecretStr("ghp_xxx"),
            repositories=["foo/bar"],
        )
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger.model_dump(mode="python"),
            enabled=False,
        )
        assert await trigger.create_pending_run(sqlite_session, automation) is None
        assert seen == []  # Must short-circuit BEFORE hitting the network.

    async def test_non_200_response_is_not_due(
        self, sqlite_session, patch_github_transport
    ):
        cutoff = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        patch_github_transport(
            lambda req: __import__("httpx").Response(
                403, json={"message": "rate limit"}
            )
        )
        trigger = GithubTrigger(
            github_access_token=SecretStr("ghp_xxx"),
            repositories=["foo/bar"],
        )
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger.model_dump(mode="python"),
            last_triggered_at=cutoff,
        )
        assert await trigger.create_pending_run(sqlite_session, automation) is None

    async def test_collects_events_across_repos(
        self, sqlite_session, patch_github_transport
    ):
        cutoff = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        import httpx

        def responder(req: httpx.Request) -> httpx.Response:
            if "foo/bar" in req.url.path:
                return httpx.Response(
                    200,
                    json=[_gh_event(1, cutoff + timedelta(minutes=1))],
                )
            return httpx.Response(
                200,
                json=[_gh_event(2, cutoff + timedelta(minutes=2))],
            )

        seen = patch_github_transport(responder)
        trigger = GithubTrigger(
            github_access_token=SecretStr("ghp_xxx"),
            repositories=["foo/bar", "foo/baz"],
        )
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger.model_dump(mode="python"),
            last_triggered_at=cutoff,
        )

        run = await trigger.create_pending_run(sqlite_session, automation)
        assert run is not None
        # Both repos contributed an event.
        events = run.event_payload["events"]
        assert len(events) == 2
        repos = {ev["_repository"] for ev in events}
        assert repos == {"foo/bar", "foo/baz"}
        # Both repos were polled (parallel fan-out).
        polled = {r.url.path for r in seen}
        assert polled == {"/repos/foo/bar/events", "/repos/foo/baz/events"}


# ---------------------------------------------------------------------------
# SlackTrigger
# ---------------------------------------------------------------------------


def _slack_message(
    msg_id: str,
    ts_dt: datetime,
    *,
    text: str = "hello",
    user: str = "U0123ABC",
    msg_type: str = "message",
    subtype: str | None = None,
) -> dict[str, Any]:
    """Build a fake Slack message dict.

    Slack ``ts`` is a string of unix seconds with microsecond precision.
    """
    msg: dict[str, Any] = {
        "type": msg_type,
        "user": user,
        "text": text,
        "ts": f"{ts_dt.timestamp():.6f}",
        "client_msg_id": msg_id,
    }
    if subtype is not None:
        msg["subtype"] = subtype
    return msg


def _slack_ok(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"ok": True, "messages": messages, "has_more": False}


class TestSlackTriggerValidation:
    def test_valid_channel_ids_are_accepted(self):
        trigger = SlackTrigger(
            slack_token=SecretStr("xoxb-xxx"),
            channels=["C0123ABC", "D0SOMEDM"],
        )
        assert trigger.channels == ["C0123ABC", "D0SOMEDM"]

    def test_lowercase_channel_id_rejected(self):
        with pytest.raises(ValidationError, match="Invalid Slack channel id"):
            SlackTrigger(
                slack_token=SecretStr("xoxb-xxx"),
                channels=["c0123abc"],
            )

    def test_channel_name_rejected(self):
        # '#general' isn't a stable id; ensure we reject names early.
        with pytest.raises(ValidationError, match="Invalid Slack channel id"):
            SlackTrigger(
                slack_token=SecretStr("xoxb-xxx"),
                channels=["#general"],
            )

    def test_empty_channels_rejected(self):
        with pytest.raises(ValidationError):
            SlackTrigger(slack_token=SecretStr("xoxb-xxx"), channels=[])

    def test_invalid_jmespath_event_filter_rejected(self):
        # The JMESPath validation lives on the shared polling base, so this
        # also exercises that SlackTrigger inherits it correctly.
        with pytest.raises(ValidationError, match="Invalid JMESPath expression"):
            SlackTrigger(
                slack_token=SecretStr("xoxb-xxx"),
                channels=["C0123ABC"],
                event_filter="not valid @@@",
            )

    def test_token_round_trips_through_model_dump(self):
        """``model_dump`` must emit the raw string so the trigger can be
        persisted in the AutomationsTrigger JSON column and rehydrated."""
        trigger = SlackTrigger(
            slack_token=SecretStr("xoxb-supersecret"),
            channels=["C0123ABC"],
        )
        dumped = trigger.model_dump(mode="python")
        assert dumped["slack_token"] == "xoxb-supersecret"
        # Round-trip through the discriminated union too.
        roundtripped = TriggerAdapter.validate_python(dumped)
        assert isinstance(roundtripped, SlackTrigger)
        assert roundtripped.slack_token.get_secret_value() == "xoxb-supersecret"

    def test_discriminated_union_dispatches_to_slack(self):
        parsed = TriggerAdapter.validate_python(
            {
                "type": "slack",
                "slack_token": "xoxb-xxx",
                "channels": ["C0123ABC"],
            }
        )
        assert isinstance(parsed, SlackTrigger)


class TestSlackTriggerCreatesRun:
    async def test_new_message_creates_run_with_payload(
        self, sqlite_session, patch_slack_transport
    ):
        import httpx

        cutoff = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        msg = _slack_message("m1", cutoff + timedelta(seconds=30))
        seen = patch_slack_transport(
            lambda req: httpx.Response(200, json=_slack_ok([msg]))
        )

        trigger = SlackTrigger(
            slack_token=SecretStr("xoxb-xxx"),
            channels=["C0123ABC"],
        )
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger.model_dump(mode="python"),
            last_triggered_at=cutoff,
        )

        run = await trigger.create_pending_run(sqlite_session, automation)
        assert run is not None
        assert run.event_payload["source"] == "slack_trigger"
        kept = run.event_payload["messages"]
        assert [m["client_msg_id"] for m in kept] == ["m1"]
        # Channel was tagged onto the message.
        assert kept[0]["_channel"] == "C0123ABC"
        # The Slack `oldest` param uses unix seconds derived from the cutoff.
        assert len(seen) == 1
        assert seen[0].url.path == "/api/conversations.history"
        assert seen[0].url.params["channel"] == "C0123ABC"
        assert float(seen[0].url.params["oldest"]) == pytest.approx(cutoff.timestamp())
        # And the run was persisted.
        rows = (await sqlite_session.execute(select(AutomationRun))).scalars().all()
        assert len(rows) == 1

    async def test_no_new_messages_returns_none(
        self, sqlite_session, patch_slack_transport
    ):
        import httpx

        cutoff = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        patch_slack_transport(lambda req: httpx.Response(200, json=_slack_ok([])))

        trigger = SlackTrigger(
            slack_token=SecretStr("xoxb-xxx"),
            channels=["C0123ABC"],
        )
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger.model_dump(mode="python"),
            last_triggered_at=cutoff,
        )
        assert await trigger.create_pending_run(sqlite_session, automation) is None
        rows = (await sqlite_session.execute(select(AutomationRun))).scalars().all()
        assert rows == []

    async def test_slack_ok_false_treated_as_no_messages(
        self, sqlite_session, patch_slack_transport
    ):
        """Slack returns HTTP 200 for app errors; ``ok=false`` must not fire."""
        import httpx

        cutoff = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        patch_slack_transport(
            lambda req: httpx.Response(
                200, json={"ok": False, "error": "channel_not_found"}
            )
        )

        trigger = SlackTrigger(
            slack_token=SecretStr("xoxb-xxx"),
            channels=["C0123ABC"],
        )
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger.model_dump(mode="python"),
            last_triggered_at=cutoff,
        )
        assert await trigger.create_pending_run(sqlite_session, automation) is None

    async def test_rate_limit_treated_as_no_messages(
        self, sqlite_session, patch_slack_transport
    ):
        """HTTP 429 (rate limit) is logged and treated as no new messages."""
        import httpx

        cutoff = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        patch_slack_transport(
            lambda req: httpx.Response(429, headers={"Retry-After": "5"}, json={})
        )

        trigger = SlackTrigger(
            slack_token=SecretStr("xoxb-xxx"),
            channels=["C0123ABC"],
        )
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger.model_dump(mode="python"),
            last_triggered_at=cutoff,
        )
        assert await trigger.create_pending_run(sqlite_session, automation) is None

    async def test_message_at_or_before_cutoff_excluded(
        self, sqlite_session, patch_slack_transport
    ):
        """A message exactly at the cutoff or older must not fire the trigger."""
        import httpx

        cutoff = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        # Slack's `oldest` is server-side exclusive — but it's perfectly
        # reasonable for the API to still hand us a message at the cutoff
        # (e.g. if the float rounds). Our client-side check must reject it.
        stale = _slack_message("m_old", cutoff)
        patch_slack_transport(lambda req: httpx.Response(200, json=_slack_ok([stale])))

        trigger = SlackTrigger(
            slack_token=SecretStr("xoxb-xxx"),
            channels=["C0123ABC"],
        )
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger.model_dump(mode="python"),
            last_triggered_at=cutoff,
        )
        assert await trigger.create_pending_run(sqlite_session, automation) is None

    async def test_jmespath_filter_keeps_matching_messages(
        self, sqlite_session, patch_slack_transport
    ):
        """A truthy JMESPath result keeps the message and fires the trigger."""
        import httpx

        cutoff = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        msgs = [
            _slack_message(
                "join",
                cutoff + timedelta(seconds=10),
                subtype="channel_join",
                text="<@U1> has joined",
            ),
            _slack_message(
                "real",
                cutoff + timedelta(seconds=20),
                text="real user message",
            ),
        ]
        patch_slack_transport(lambda req: httpx.Response(200, json=_slack_ok(msgs)))

        # Filter out join/leave noise — only regular user messages.
        trigger = SlackTrigger(
            slack_token=SecretStr("xoxb-xxx"),
            channels=["C0123ABC"],
            event_filter="subtype == null",
        )
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger.model_dump(mode="python"),
            last_triggered_at=cutoff,
        )
        run = await trigger.create_pending_run(sqlite_session, automation)
        assert run is not None
        kept = run.event_payload["messages"]
        assert [m["client_msg_id"] for m in kept] == ["real"]

    async def test_jmespath_filter_excludes_all(
        self, sqlite_session, patch_slack_transport
    ):
        """A filter that matches nothing must not fire the trigger."""
        import httpx

        cutoff = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        msgs = [
            _slack_message("a", cutoff + timedelta(seconds=10), user="U1"),
            _slack_message("b", cutoff + timedelta(seconds=20), user="U2"),
        ]
        patch_slack_transport(lambda req: httpx.Response(200, json=_slack_ok(msgs)))

        trigger = SlackTrigger(
            slack_token=SecretStr("xoxb-xxx"),
            channels=["C0123ABC"],
            event_filter="user == 'UNOBODY'",
        )
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger.model_dump(mode="python"),
            last_triggered_at=cutoff,
        )
        assert await trigger.create_pending_run(sqlite_session, automation) is None

    async def test_multiple_channels_polled_in_parallel(
        self, sqlite_session, patch_slack_transport
    ):
        """Two configured channels are each polled; both contribute messages."""
        import httpx

        cutoff = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)

        def responder(request: httpx.Request) -> httpx.Response:
            ch = request.url.params["channel"]
            msg = _slack_message(f"msg_{ch}", cutoff + timedelta(seconds=5))
            return httpx.Response(200, json=_slack_ok([msg]))

        seen = patch_slack_transport(responder)

        trigger = SlackTrigger(
            slack_token=SecretStr("xoxb-xxx"),
            channels=["C0123ABC", "C0456DEF"],
        )
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger.model_dump(mode="python"),
            last_triggered_at=cutoff,
        )

        run = await trigger.create_pending_run(sqlite_session, automation)
        assert run is not None
        kept = run.event_payload["messages"]
        assert {m["_channel"] for m in kept} == {"C0123ABC", "C0456DEF"}
        polled = {r.url.params["channel"] for r in seen}
        assert polled == {"C0123ABC", "C0456DEF"}

    async def test_disabled_short_circuits_without_calling_slack(
        self, sqlite_session, patch_slack_transport
    ):
        """A disabled automation must not call Slack at all."""
        import httpx

        seen = patch_slack_transport(
            lambda req: httpx.Response(200, json=_slack_ok([]))
        )

        trigger = SlackTrigger(
            slack_token=SecretStr("xoxb-xxx"),
            channels=["C0123ABC"],
        )
        automation = await _make_automation(
            sqlite_session,
            trigger=trigger.model_dump(mode="python"),
            enabled=False,
        )
        assert await trigger.create_pending_run(sqlite_session, automation) is None
        assert seen == []
