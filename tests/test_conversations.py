"""Tests for routing events to an existing conversation.

Sending the turn is stubbed here; `test_conversation_turn.py` covers it.
"""

import asyncio
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from openhands.automation.auth import AuthenticatedUser
from openhands.automation.conversations import (
    attach_run_conversation,
    resolve_subject_key,
)
from openhands.automation.ingest import AcceptedEvent, accept_event
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
    ExternalConversation,
)
from openhands.automation.schemas import EventTrigger
from openhands.automation.subjects import (
    EventSubject,
    github_subject,
    slack_subject,
)
from openhands.automation.utils.conversation_turn import compose_turn


TEAM = "T06P212QSEA"


@pytest.fixture
def org_id(mock_authenticated_user: AuthenticatedUser) -> uuid.UUID:
    return mock_authenticated_user.org_id


def slack_envelope(
    *,
    channel: str = "C123",
    ts: str = "1755000000.000100",
    thread_ts: str | None = None,
    text: str = "<@U999> take a look",
) -> dict[str, Any]:
    """A Socket Mode envelope, as the stream provider passes it on."""
    event: dict[str, Any] = {
        "type": "app_mention",
        "channel": channel,
        "user": "U456",
        "text": text,
        "ts": ts,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return {"type": "event_callback", "team_id": TEAM, "event": event}


def make_automation(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    trigger: dict,
    name: str = "Test Automation",
) -> Automation:
    return Automation(
        user_id=user_id,
        org_id=org_id,
        name=name,
        trigger=trigger,
        tarball_path="s3://bucket/code.tar.gz",
        entrypoint="uv run script.py",
    )


def continuing_trigger(**overrides: Any) -> dict:
    trigger = {
        "type": "event",
        "source": "slack",
        "on": "app_mention",
        "destination": "continue_conversation",
    }
    trigger.update(overrides)
    return trigger


async def fetch_runs(session) -> list[AutomationRun]:
    result = await session.execute(select(AutomationRun))
    return list(result.scalars().all())


async def fetch_mappings(session) -> list[ExternalConversation]:
    result = await session.execute(select(ExternalConversation))
    return list(result.scalars().all())


@pytest.fixture
def delivered_turns(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Accept every turn, recording (conversation_id, text)."""
    sent: list[tuple[str, str]] = []

    async def fake_send(run, conversation_id, text):
        sent.append((conversation_id, text))
        return True

    monkeypatch.setattr(
        "openhands.automation.conversations.send_conversation_turn", fake_send
    )
    return sent


@pytest.fixture
def unreachable_conversations(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Refuse every turn, as a reaped sandbox would."""
    attempted: list[str] = []

    async def fake_send(run, conversation_id, text):
        attempted.append(conversation_id)
        return False

    monkeypatch.setattr(
        "openhands.automation.conversations.send_conversation_turn", fake_send
    )
    return attempted


# ---------------------------------------------------------------------------
# Subject extraction
# ---------------------------------------------------------------------------


class TestSlackSubject:
    def test_a_threaded_reply_keys_on_the_thread(self):
        subject = slack_subject(
            slack_envelope(ts="1755000009.000900", thread_ts="1755000000.000100")
        )
        assert subject == EventSubject(key=f"{TEAM}/C123/1755000000.000100")

    def test_an_opening_mention_keys_on_its_own_ts(self):
        """The mention that starts a thread is the same subject as its replies."""
        opener = slack_subject(slack_envelope(ts="1755000000.000100"))
        reply = slack_subject(
            slack_envelope(ts="1755000009.000900", thread_ts="1755000000.000100")
        )
        assert opener == reply

    def test_different_channels_are_different_subjects(self):
        assert slack_subject(slack_envelope(channel="C1")) != slack_subject(
            slack_envelope(channel="C2")
        )

    @pytest.mark.parametrize(
        "envelope",
        [
            {"event": {"channel": "C1", "ts": "1.1"}},  # no team
            {"team_id": TEAM, "event": {"ts": "1.1"}},  # no channel
            {"team_id": TEAM, "event": {"channel": "C1"}},  # no ts
            {"team_id": TEAM},  # no event
            {"team_id": TEAM, "event": "not-a-dict"},
        ],
    )
    def test_an_incomplete_envelope_has_no_subject(self, envelope):
        assert slack_subject(envelope) is None


class TestGithubSubject:
    def test_a_pull_request_keys_on_its_number(self):
        subject = github_subject(
            {"repository": {"full_name": "org/repo"}, "pull_request": {"number": 12}}
        )
        assert subject == EventSubject(key="org/repo#12")

    def test_an_issue_comment_on_a_pr_is_the_same_subject(self):
        """GitHub numbers issues and pull requests from one sequence."""
        review = github_subject(
            {"repository": {"full_name": "org/repo"}, "pull_request": {"number": 12}}
        )
        comment = github_subject(
            {"repository": {"full_name": "org/repo"}, "issue": {"number": 12}}
        )
        assert review == comment

    def test_a_top_level_number_is_read(self):
        subject = github_subject(
            {"repository": {"full_name": "org/repo"}, "number": 12}
        )
        assert subject == EventSubject(key="org/repo#12")

    def test_a_push_has_no_subject(self):
        """Nothing numbered, so nothing to continue."""
        assert (
            github_subject(
                {"repository": {"full_name": "org/repo"}, "ref": "refs/heads/main"}
            )
            is None
        )

    def test_without_a_repository_there_is_no_subject(self):
        assert github_subject({"issue": {"number": 12}}) is None


class TestResolveSubjectKey:
    def test_the_provider_subject_is_the_default(self):
        trigger = EventTrigger.model_validate(continuing_trigger())
        subject = EventSubject(key=f"{TEAM}/C123/1.1")
        assert resolve_subject_key(trigger, {}, subject) == f"{TEAM}/C123/1.1"

    def test_a_trigger_expression_overrides_the_provider(self):
        """The trigger's expression wins over the provider's extractor."""
        trigger = EventTrigger.model_validate(
            continuing_trigger(subject_key_expr="event.channel")
        )
        key = resolve_subject_key(
            trigger, slack_envelope(), EventSubject(key="ignored")
        )
        assert key == "C123"

    def test_no_expression_and_no_provider_subject_is_no_key(self):
        trigger = EventTrigger.model_validate(continuing_trigger())
        assert resolve_subject_key(trigger, {}, None) is None

    def test_a_number_is_an_acceptable_key(self):
        trigger = EventTrigger.model_validate(
            continuing_trigger(source="linear", subject_key_expr="data.number")
        )
        assert resolve_subject_key(trigger, {"data": {"number": 42}}, None) == "42"

    @pytest.mark.parametrize(
        "payload",
        [
            {"data": {"issue": {"id": "abc"}}},  # a dict is not an identity
            {"data": {"ids": ["a", "b"]}},
            {"data": {"flag": True}},  # would collapse every event onto one key
        ],
    )
    def test_a_non_scalar_result_is_rejected(self, payload):
        trigger = EventTrigger.model_validate(
            continuing_trigger(source="linear", subject_key_expr="data.*|[0]")
        )
        assert resolve_subject_key(trigger, payload, None) is None

    def test_an_oversized_key_is_rejected(self):
        """Longer than the column: a broken extractor, not a long thread."""
        trigger = EventTrigger.model_validate(
            continuing_trigger(source="linear", subject_key_expr="id")
        )
        assert resolve_subject_key(trigger, {"id": "x" * 501}, None) is None

    def test_an_expression_matching_nothing_is_no_key(self):
        trigger = EventTrigger.model_validate(
            continuing_trigger(source="linear", subject_key_expr="nope.missing")
        )
        assert resolve_subject_key(trigger, {"id": "x"}, None) is None


class TestTriggerValidation:
    def test_dispatch_run_is_the_default(self):
        trigger = EventTrigger.model_validate(
            {"type": "event", "source": "slack", "on": "app_mention"}
        )
        assert trigger.destination == "dispatch_run"
        assert trigger.subject_key_expr is None

    def test_an_unknown_destination_is_refused(self):
        with pytest.raises(ValueError):
            EventTrigger.model_validate(continuing_trigger(destination="whatever"))

    def test_a_broken_subject_expression_is_refused_at_creation(self):
        """A typo would otherwise look like the feature being switched off."""
        with pytest.raises(ValueError, match="subject_key_expr"):
            EventTrigger.model_validate(continuing_trigger(subject_key_expr="((("))


class TestComposeTurn:
    def test_the_payload_travels_verbatim(self):
        """The payload goes over verbatim."""
        text = compose_turn("slack", "app_mention", slack_envelope(text="ping"))
        assert "app_mention" in text
        assert "ping" in text
        body = text.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
        assert json.loads(body)["team_id"] == TEAM

    def test_an_empty_payload_is_still_valid_json(self):
        text = compose_turn("slack", "app_mention", None)
        body = text.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
        assert json.loads(body) == {}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_event_creates_a_run_and_claims_the_subject(
    org_id, async_session, mock_authenticated_user, delivered_turns
):
    """Nothing to continue yet: today's behaviour, plus a mapping."""
    automation = make_automation(
        org_id, mock_authenticated_user.user_id, continuing_trigger()
    )
    async_session.add(automation)
    await async_session.commit()

    envelope = slack_envelope()
    result = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=envelope,
            provider_event_id="Ev1",
            subject=slack_subject(envelope),
        ),
        async_session,
    )

    assert result.matched == 1
    assert len(result.run_ids) == 1
    assert result.conversation_ids == []
    assert delivered_turns == []

    mappings = await fetch_mappings(async_session)
    assert len(mappings) == 1
    assert mappings[0].subject_key == f"{TEAM}/C123/1755000000.000100"
    assert mappings[0].automation_id == automation.id
    assert str(mappings[0].run_id) == result.run_ids[0]
    # Not known until the run completes and reports one.
    assert mappings[0].conversation_id is None


@pytest.mark.asyncio
async def test_two_mentions_in_one_thread_reach_the_same_conversation(
    org_id, async_session, mock_authenticated_user, delivered_turns
):
    """A follow-up lands on the conversation the first event created."""
    automation = make_automation(
        org_id, mock_authenticated_user.user_id, continuing_trigger()
    )
    async_session.add(automation)
    await async_session.commit()

    opener = slack_envelope(ts="1755000000.000100", text="<@U999> what broke?")
    first = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=opener,
            provider_event_id="Ev1",
            subject=slack_subject(opener),
        ),
        async_session,
    )
    assert len(first.run_ids) == 1

    # The run finishes and reports the conversation it created.
    attached = await attach_run_conversation(
        async_session, uuid.UUID(first.run_ids[0]), "conv-thread-1"
    )
    assert attached is True
    await async_session.commit()

    reply = slack_envelope(
        ts="1755000009.000900",
        thread_ts="1755000000.000100",
        text="<@U999> and now?",
    )
    second = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=reply,
            provider_event_id="Ev2",
            subject=slack_subject(reply),
        ),
        async_session,
    )

    assert second.matched == 1
    assert second.run_ids == []
    assert second.conversation_ids == ["conv-thread-1"]

    assert len(delivered_turns) == 1
    conversation_id, text = delivered_turns[0]
    assert conversation_id == "conv-thread-1"
    # The turn carries the new event, with the first exchange still in history.
    assert "and now?" in text

    # Still one run in total: the second event started none.
    assert len(await fetch_runs(async_session)) == 1


@pytest.mark.asyncio
async def test_a_mention_in_another_thread_starts_its_own_run(
    org_id, async_session, mock_authenticated_user, delivered_turns
):
    automation = make_automation(
        org_id, mock_authenticated_user.user_id, continuing_trigger()
    )
    async_session.add(automation)
    await async_session.commit()

    first_thread = slack_envelope(ts="1755000000.000100")
    first = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=first_thread,
            provider_event_id="Ev1",
            subject=slack_subject(first_thread),
        ),
        async_session,
    )
    await attach_run_conversation(
        async_session, uuid.UUID(first.run_ids[0]), "conv-thread-1"
    )
    await async_session.commit()

    other_thread = slack_envelope(ts="1755000100.000200")
    second = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=other_thread,
            provider_event_id="Ev2",
            subject=slack_subject(other_thread),
        ),
        async_session,
    )

    assert second.conversation_ids == []
    assert len(second.run_ids) == 1
    assert delivered_turns == []
    assert len(await fetch_mappings(async_session)) == 2


@pytest.mark.asyncio
async def test_an_automation_that_does_not_opt_in_is_untouched(
    org_id, async_session, mock_authenticated_user, delivered_turns
):
    """No `destination`: no mapping, no lookup, no turn."""
    automation = make_automation(
        org_id,
        mock_authenticated_user.user_id,
        {"type": "event", "source": "slack", "on": "app_mention"},
    )
    async_session.add(automation)
    await async_session.commit()

    for index, ts in enumerate(("1755000000.000100", "1755000009.000900")):
        envelope = slack_envelope(ts=ts, thread_ts="1755000000.000100")
        result = await accept_event(
            org_id,
            AcceptedEvent(
                source="slack",
                event_key="app_mention",
                payload=envelope,
                provider_event_id=f"Ev{index}",
                subject=slack_subject(envelope),
            ),
            async_session,
        )
        assert len(result.run_ids) == 1
        assert result.conversation_ids == []

    assert len(await fetch_runs(async_session)) == 2
    assert await fetch_mappings(async_session) == []
    assert delivered_turns == []


@pytest.mark.asyncio
async def test_an_unreachable_conversation_degrades_to_a_run(
    org_id, async_session, mock_authenticated_user, unreachable_conversations
):
    """A reaped sandbox degrades to a run rather than erroring."""
    automation = make_automation(
        org_id, mock_authenticated_user.user_id, continuing_trigger()
    )
    async_session.add(automation)
    await async_session.commit()

    opener = slack_envelope(ts="1755000000.000100")
    first = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=opener,
            provider_event_id="Ev1",
            subject=slack_subject(opener),
        ),
        async_session,
    )
    await attach_run_conversation(
        async_session, uuid.UUID(first.run_ids[0]), "conv-gone"
    )
    await async_session.commit()

    reply = slack_envelope(ts="1755000009.000900", thread_ts="1755000000.000100")
    second = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=reply,
            provider_event_id="Ev2",
            subject=slack_subject(reply),
        ),
        async_session,
    )

    assert unreachable_conversations == ["conv-gone"]
    assert second.conversation_ids == []
    assert len(second.run_ids) == 1

    # The dead conversation is forgotten; the new run owns the subject.
    mappings = await fetch_mappings(async_session)
    assert len(mappings) == 1
    assert mappings[0].conversation_id is None
    assert str(mappings[0].run_id) == second.run_ids[0]


@pytest.mark.asyncio
async def test_a_run_still_in_flight_does_not_hold_up_the_next_event(
    org_id, async_session, mock_authenticated_user, delivered_turns
):
    """No conversation reported yet, and no queue: the event gets its own run."""
    automation = make_automation(
        org_id, mock_authenticated_user.user_id, continuing_trigger()
    )
    async_session.add(automation)
    await async_session.commit()

    for index, (ts, thread) in enumerate(
        ((("1755000000.000100"), None), ("1755000009.000900", "1755000000.000100"))
    ):
        envelope = slack_envelope(ts=ts, thread_ts=thread)
        result = await accept_event(
            org_id,
            AcceptedEvent(
                source="slack",
                event_key="app_mention",
                payload=envelope,
                provider_event_id=f"Ev{index}",
                subject=slack_subject(envelope),
            ),
            async_session,
        )
        assert len(result.run_ids) == 1

    assert delivered_turns == []
    assert len(await fetch_runs(async_session)) == 2
    # One subject, one mapping, now pointing at the newer run.
    assert len(await fetch_mappings(async_session)) == 1


@pytest.mark.asyncio
async def test_an_event_without_a_subject_falls_back_to_a_run(
    org_id, async_session, mock_authenticated_user, delivered_turns
):
    """A GitHub push has no numbered thing to be about."""
    automation = make_automation(
        org_id,
        mock_authenticated_user.user_id,
        {
            "type": "event",
            "source": "github",
            "on": "push",
            "destination": "continue_conversation",
        },
    )
    async_session.add(automation)
    await async_session.commit()

    result = await accept_event(
        org_id,
        AcceptedEvent(
            source="github",
            event_key="push",
            payload={
                "ref": "refs/heads/main",
                "repository": {"full_name": "org/repo"},
            },
            provider_event_id="d-1",
        ),
        async_session,
    )

    assert len(result.run_ids) == 1
    assert await fetch_mappings(async_session) == []
    assert delivered_turns == []


@pytest.mark.asyncio
async def test_github_derives_its_subject_without_the_transport_naming_one(
    org_id, async_session, mock_authenticated_user, delivered_turns
):
    """The webhook path passes no subject; the descriptor supplies it."""
    automation = make_automation(
        org_id,
        mock_authenticated_user.user_id,
        {
            "type": "event",
            "source": "github",
            "on": "issue_comment.created",
            "destination": "continue_conversation",
        },
    )
    async_session.add(automation)
    await async_session.commit()

    await accept_event(
        org_id,
        AcceptedEvent(
            source="github",
            event_key="issue_comment.created",
            payload={
                "repository": {"full_name": "org/repo"},
                "issue": {"number": 12},
            },
            provider_event_id="d-1",
        ),
        async_session,
    )

    mappings = await fetch_mappings(async_session)
    assert [m.subject_key for m in mappings] == ["org/repo#12"]


@pytest.mark.asyncio
async def test_two_automations_do_not_share_one_subject(
    org_id, async_session, mock_authenticated_user, delivered_turns
):
    """A conversation belongs to the automation whose script created it."""
    for name in ("Triage", "Summarise"):
        async_session.add(
            make_automation(
                org_id, mock_authenticated_user.user_id, continuing_trigger(), name
            )
        )
    await async_session.commit()

    envelope = slack_envelope()
    result = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=envelope,
            provider_event_id="Ev1",
            subject=slack_subject(envelope),
        ),
        async_session,
    )

    assert result.matched == 2
    assert len(result.run_ids) == 2

    mappings = await fetch_mappings(async_session)
    assert len(mappings) == 2
    assert {m.subject_key for m in mappings} == {f"{TEAM}/C123/1755000000.000100"}
    assert len({m.automation_id for m in mappings}) == 2


@pytest.mark.asyncio
async def test_concurrent_events_for_one_subject_yield_one_mapping(
    org_id, async_session_factory, mock_authenticated_user, monkeypatch
):
    """Two workers, one brand-new subject: the unique index picks a winner.

    Both events still run -- there was nothing to continue -- but only one owns
    the subject, so later events do not split across two conversations.
    """

    async def fake_send(run, conversation_id, text):
        return True

    monkeypatch.setattr(
        "openhands.automation.conversations.send_conversation_turn", fake_send
    )

    async with async_session_factory() as setup:
        automation = make_automation(
            org_id, mock_authenticated_user.user_id, continuing_trigger()
        )
        setup.add(automation)
        await setup.commit()

    envelope = slack_envelope()

    async def deliver(event_id: str) -> None:
        async with async_session_factory() as session:
            await accept_event(
                org_id,
                AcceptedEvent(
                    source="slack",
                    event_key="app_mention",
                    payload=envelope,
                    provider_event_id=event_id,
                    subject=slack_subject(envelope),
                ),
                session,
            )

    await asyncio.gather(deliver("Ev1"), deliver("Ev2"))

    async with async_session_factory() as session:
        mappings = await fetch_mappings(session)
        runs = await fetch_runs(session)

    assert len(mappings) == 1
    assert len(runs) == 2
    # Whichever won, the mapping points at a real run.
    assert str(mappings[0].run_id) in {str(run.id) for run in runs}


@pytest.mark.asyncio
async def test_attach_run_conversation_reports_whether_a_subject_was_waiting(
    org_id, async_session, mock_authenticated_user
):
    """The return value tells the callback whether to keep the sandbox."""
    automation = make_automation(
        org_id, mock_authenticated_user.user_id, continuing_trigger()
    )
    async_session.add(automation)
    await async_session.commit()

    run = AutomationRun(automation_id=automation.id, status=AutomationRunStatus.RUNNING)
    async_session.add(run)
    await async_session.commit()

    assert await attach_run_conversation(async_session, run.id, "conv-1") is False

    async_session.add(
        ExternalConversation(
            org_id=org_id,
            source="slack",
            subject_key=f"{TEAM}/C123/1.1",
            automation_id=automation.id,
            run_id=run.id,
        )
    )
    await async_session.commit()

    assert await attach_run_conversation(async_session, run.id, "conv-1") is True
    await async_session.commit()

    mappings = await fetch_mappings(async_session)
    assert mappings[0].conversation_id == "conv-1"


# ---------------------------------------------------------------------------
# The completion callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completing_a_subject_owning_run_keeps_its_sandbox(
    async_client, async_session, mock_authenticated_user
):
    """Deleting it would destroy the conversation the next event continues."""
    automation = make_automation(
        mock_authenticated_user.org_id,
        mock_authenticated_user.user_id,
        continuing_trigger(),
    )
    async_session.add(automation)
    await async_session.commit()

    run = AutomationRun(
        automation_id=automation.id,
        status=AutomationRunStatus.RUNNING,
        sandbox_id="sandbox-threaded",
    )
    async_session.add(run)
    await async_session.commit()

    async_session.add(
        ExternalConversation(
            org_id=mock_authenticated_user.org_id,
            source="slack",
            subject_key=f"{TEAM}/C123/1.1",
            automation_id=automation.id,
            run_id=run.id,
        )
    )
    await async_session.commit()

    with patch(
        "openhands.automation.router.cleanup_sandbox", new_callable=AsyncMock
    ) as mock_cleanup:
        with patch(
            "openhands.automation.router.fetch_latest_finish_tool_response_for_run",
            new_callable=AsyncMock,
            return_value=None,
        ):
            response = await async_client.post(
                f"/api/automation/v1/runs/{run.id}/complete",
                json={"status": "COMPLETED", "conversation_id": "conv-kept"},
            )
        await asyncio.sleep(0)

    assert response.status_code == 200
    mock_cleanup.assert_not_awaited()

    mappings = await fetch_mappings(async_session)
    assert mappings[0].conversation_id == "conv-kept"


@pytest.mark.asyncio
async def test_completing_an_ordinary_run_still_cleans_up(
    async_client, async_session, mock_authenticated_user
):
    """Retention is scoped to runs that own a subject."""
    automation = make_automation(
        mock_authenticated_user.org_id,
        mock_authenticated_user.user_id,
        {"type": "event", "source": "slack", "on": "app_mention"},
    )
    async_session.add(automation)
    await async_session.commit()

    run = AutomationRun(
        automation_id=automation.id,
        status=AutomationRunStatus.RUNNING,
        sandbox_id="sandbox-ordinary",
    )
    async_session.add(run)
    await async_session.commit()

    with patch(
        "openhands.automation.router.cleanup_sandbox", new_callable=AsyncMock
    ) as mock_cleanup:
        with patch(
            "openhands.automation.router.fetch_latest_finish_tool_response_for_run",
            new_callable=AsyncMock,
            return_value=None,
        ):
            response = await async_client.post(
                f"/api/automation/v1/runs/{run.id}/complete",
                json={"status": "COMPLETED", "conversation_id": "conv-ordinary"},
            )
        await asyncio.sleep(0)

    assert response.status_code == 200
    mock_cleanup.assert_awaited_once()
