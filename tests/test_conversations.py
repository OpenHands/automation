"""Tests for routing events to an existing conversation.

The conversation id is derived, so these tests never invent one: they assert
against `conversation_id_for`. What makes a subject continuable is that some
earlier run for it is on a sandbox.

Sending the turn is stubbed here; `test_conversation_turn.py` covers it.
"""

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from openhands.automation.auth import AuthenticatedUser
from openhands.automation.conversations import (
    continue_conversation,
    resolve_subject_key,
)
from openhands.automation.ingest import AcceptedEvent, accept_event
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
)
from openhands.automation.schemas import EventTrigger
from openhands.automation.subjects import (
    EventSubject,
    conversation_id_for,
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


async def subject_runs(session) -> list[AutomationRun]:
    """Runs that own a subject -- the only thing this feature stores."""
    result = await session.execute(
        select(AutomationRun).where(AutomationRun.subject_key.isnot(None))
    )
    return list(result.scalars().all())


async def give_sandbox(session, run_id, sandbox_id: str = "sandbox-1") -> None:
    """Put a run on a sandbox, which is what makes its subject continuable."""
    run = await session.get(AutomationRun, run_id)
    assert run is not None
    run.sandbox_id = sandbox_id
    await session.commit()


def expected_conversation(org_id, automation_id, subject_key, source="slack") -> str:
    return conversation_id_for(org_id, automation_id, source, subject_key)


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


async def _mention(session, org_id, envelope, event_id: str):
    return await accept_event(
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


@pytest.mark.asyncio
async def test_first_event_creates_a_run_and_claims_the_subject(
    org_id, async_session, mock_authenticated_user, delivered_turns
):
    """Nothing to continue yet: today's behaviour, plus the subject on the run."""
    automation = make_automation(
        org_id, mock_authenticated_user.user_id, continuing_trigger()
    )
    async_session.add(automation)
    await async_session.commit()

    result = await _mention(async_session, org_id, slack_envelope(), "Ev1")

    assert result.matched == 1
    assert len(result.run_ids) == 1
    assert result.conversation_ids == []
    assert delivered_turns == []

    owners = await subject_runs(async_session)
    assert len(owners) == 1
    assert owners[0].subject_key == f"{TEAM}/C123/1755000000.000100"
    assert str(owners[0].id) == result.run_ids[0]
    # Nothing records a conversation id: it is derived when needed.
    assert owners[0].conversation_id is None


@pytest.mark.asyncio
async def test_two_mentions_in_one_thread_reach_the_same_conversation(
    org_id, async_session, mock_authenticated_user, delivered_turns
):
    """A follow-up lands on the conversation the first event's run created."""
    automation = make_automation(
        org_id, mock_authenticated_user.user_id, continuing_trigger()
    )
    async_session.add(automation)
    await async_session.commit()

    opener = slack_envelope(ts="1755000000.000100", text="<@U999> what broke?")
    first = await _mention(async_session, org_id, opener, "Ev1")
    assert len(first.run_ids) == 1
    await give_sandbox(async_session, uuid.UUID(first.run_ids[0]))

    reply = slack_envelope(
        ts="1755000009.000900",
        thread_ts="1755000000.000100",
        text="<@U999> and now?",
    )
    second = await _mention(async_session, org_id, reply, "Ev2")

    derived = expected_conversation(
        org_id, automation.id, f"{TEAM}/C123/1755000000.000100"
    )
    assert second.matched == 1
    assert second.run_ids == []
    assert second.conversation_ids == [derived]

    assert len(delivered_turns) == 1
    conversation_id, text = delivered_turns[0]
    assert conversation_id == derived
    assert "and now?" in text

    # Still one run in total: the second event started none.
    assert len(await fetch_runs(async_session)) == 1


@pytest.mark.asyncio
async def test_an_event_arriving_mid_run_continues_that_conversation(
    org_id, async_session, mock_authenticated_user, delivered_turns
):
    """A derived id is known before the first run reports anything.

    A stored mapping could not answer here -- the run has not completed, so it
    has no conversation id to record -- and the event forked a second run
    against the same thread.
    """
    automation = make_automation(
        org_id, mock_authenticated_user.user_id, continuing_trigger()
    )
    async_session.add(automation)
    await async_session.commit()

    first = await _mention(async_session, org_id, slack_envelope(), "Ev1")
    run = await async_session.get(AutomationRun, uuid.UUID(first.run_ids[0]))
    assert run is not None
    run.status = AutomationRunStatus.RUNNING
    run.sandbox_id = "sandbox-live"
    await async_session.commit()

    follow_up = slack_envelope(
        ts="1755000002.000200", thread_ts="1755000000.000100", text="<@U999> also"
    )
    second = await _mention(async_session, org_id, follow_up, "Ev2")

    assert second.run_ids == []
    assert second.conversation_ids == [
        expected_conversation(org_id, automation.id, f"{TEAM}/C123/1755000000.000100")
    ]
    assert len(await fetch_runs(async_session)) == 1


@pytest.mark.asyncio
async def test_a_subject_with_no_sandbox_yet_falls_back_to_a_run(
    org_id, async_session, mock_authenticated_user, delivered_turns
):
    """A PENDING run has nothing to talk to, so the event starts its own."""
    automation = make_automation(
        org_id, mock_authenticated_user.user_id, continuing_trigger()
    )
    async_session.add(automation)
    await async_session.commit()

    await _mention(async_session, org_id, slack_envelope(), "Ev1")

    follow_up = slack_envelope(
        ts="1755000002.000200", thread_ts="1755000000.000100", text="<@U999> also"
    )
    second = await _mention(async_session, org_id, follow_up, "Ev2")

    assert len(second.run_ids) == 1
    assert second.conversation_ids == []
    assert delivered_turns == []


@pytest.mark.asyncio
async def test_a_mention_in_another_thread_starts_its_own_run(
    org_id, async_session, mock_authenticated_user, delivered_turns
):
    """Two threads share no context."""
    automation = make_automation(
        org_id, mock_authenticated_user.user_id, continuing_trigger()
    )
    async_session.add(automation)
    await async_session.commit()

    first = await _mention(async_session, org_id, slack_envelope(ts="1.1"), "Ev1")
    await give_sandbox(async_session, uuid.UUID(first.run_ids[0]))

    other = await _mention(async_session, org_id, slack_envelope(ts="2.2"), "Ev2")

    assert len(other.run_ids) == 1
    assert other.conversation_ids == []
    assert delivered_turns == []

    keys = {run.subject_key for run in await subject_runs(async_session)}
    assert keys == {f"{TEAM}/C123/1.1", f"{TEAM}/C123/2.2"}


@pytest.mark.asyncio
async def test_an_automation_that_does_not_opt_in_is_untouched(
    org_id, async_session, mock_authenticated_user, delivered_turns
):
    """Every event starts a run, and nothing records a subject."""
    automation = make_automation(
        org_id,
        mock_authenticated_user.user_id,
        {"type": "event", "source": "slack", "on": "app_mention"},
    )
    async_session.add(automation)
    await async_session.commit()

    first = await _mention(async_session, org_id, slack_envelope(), "Ev1")
    await give_sandbox(async_session, uuid.UUID(first.run_ids[0]))
    second = await _mention(async_session, org_id, slack_envelope(), "Ev2")

    assert len(first.run_ids) == 1
    assert len(second.run_ids) == 1
    assert second.conversation_ids == []
    assert delivered_turns == []
    assert await subject_runs(async_session) == []


@pytest.mark.asyncio
async def test_an_unreachable_conversation_degrades_to_a_run(
    org_id, async_session, mock_authenticated_user, unreachable_conversations
):
    """A reaped sandbox is ordinary; the event must not error or vanish."""
    automation = make_automation(
        org_id, mock_authenticated_user.user_id, continuing_trigger()
    )
    async_session.add(automation)
    await async_session.commit()

    first = await _mention(async_session, org_id, slack_envelope(), "Ev1")
    dead_run_id = uuid.UUID(first.run_ids[0])
    await give_sandbox(async_session, dead_run_id, "sandbox-reaped")

    reply = slack_envelope(ts="1755000009.000900", thread_ts="1755000000.000100")
    second = await _mention(async_session, org_id, reply, "Ev2")

    assert unreachable_conversations == [
        expected_conversation(org_id, automation.id, f"{TEAM}/C123/1755000000.000100")
    ]
    assert len(second.run_ids) == 1
    assert second.conversation_ids == []

    # The dead run no longer answers for the subject, so later events do not
    # pay the timeout to rediscover it. The new run owns it instead.
    dead_run = await async_session.get(AutomationRun, dead_run_id)
    assert dead_run is not None and dead_run.subject_key is None
    owners = await subject_runs(async_session)
    assert [str(run.id) for run in owners] == second.run_ids


@pytest.mark.asyncio
async def test_an_event_without_a_subject_falls_back_to_a_run(
    org_id, async_session, mock_authenticated_user, delivered_turns
):
    """No subject, nothing to group by."""
    automation = make_automation(
        org_id, mock_authenticated_user.user_id, continuing_trigger()
    )
    async_session.add(automation)
    await async_session.commit()

    result = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload={"type": "event_callback", "team_id": TEAM},
            provider_event_id="Ev1",
        ),
        async_session,
    )

    assert len(result.run_ids) == 1
    assert result.conversation_ids == []
    assert await subject_runs(async_session) == []


@pytest.mark.asyncio
async def test_two_automations_do_not_share_one_subject(
    org_id, async_session, mock_authenticated_user, delivered_turns
):
    """`automation_id` is in the key, so each gets its own conversation."""
    alpha = make_automation(
        org_id, mock_authenticated_user.user_id, continuing_trigger(), "Alpha"
    )
    bravo = make_automation(
        org_id, mock_authenticated_user.user_id, continuing_trigger(), "Bravo"
    )
    async_session.add_all([alpha, bravo])
    await async_session.commit()

    first = await _mention(async_session, org_id, slack_envelope(), "Ev1")
    assert len(first.run_ids) == 2
    for run_id in first.run_ids:
        await give_sandbox(async_session, uuid.UUID(run_id), f"sandbox-{run_id}")

    reply = slack_envelope(ts="1755000009.000900", thread_ts="1755000000.000100")
    second = await _mention(async_session, org_id, reply, "Ev2")

    key = f"{TEAM}/C123/1755000000.000100"
    assert sorted(second.conversation_ids) == sorted(
        [
            expected_conversation(org_id, alpha.id, key),
            expected_conversation(org_id, bravo.id, key),
        ]
    )
    assert len(set(second.conversation_ids)) == 2


@pytest.mark.asyncio
async def test_github_derives_its_subject_without_the_transport_naming_one(
    org_id, async_session, mock_authenticated_user, delivered_turns
):
    """The provider's extractor runs when the transport supplied no subject."""
    automation = make_automation(
        org_id,
        mock_authenticated_user.user_id,
        {
            "type": "event",
            "source": "github",
            "on": "issue_comment",
            "destination": "continue_conversation",
        },
    )
    async_session.add(automation)
    await async_session.commit()

    payload = {
        "repository": {"full_name": "OpenHands/automation"},
        "issue": {"number": 362},
    }

    async def deliver(event_id: str):
        return await accept_event(
            org_id,
            AcceptedEvent(
                source="github",
                event_key="issue_comment",
                payload=payload,
                provider_event_id=event_id,
            ),
            async_session,
        )

    first = await deliver("Ev1")
    await give_sandbox(async_session, uuid.UUID(first.run_ids[0]))
    second = await deliver("Ev2")

    assert second.run_ids == []
    assert second.conversation_ids == [
        conversation_id_for(org_id, automation.id, "github", "OpenHands/automation#362")
    ]


# ---------------------------------------------------------------------------
# The completion callback
# ---------------------------------------------------------------------------


async def _complete(async_client, run_id, conversation_id: str = "conv-x"):
    with patch(
        "openhands.automation.router.cleanup_sandbox", new_callable=AsyncMock
    ) as mock_cleanup:
        with patch(
            "openhands.automation.router.fetch_latest_finish_tool_response_for_run",
            new_callable=AsyncMock,
            return_value=None,
        ):
            response = await async_client.post(
                f"/api/automation/v1/runs/{run_id}/complete",
                json={"status": "COMPLETED", "conversation_id": conversation_id},
            )
        await asyncio.sleep(0)
    return response, mock_cleanup


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
        subject_key=f"{TEAM}/C123/1.1",
    )
    async_session.add(run)
    await async_session.commit()

    response, mock_cleanup = await _complete(async_client, run.id)

    assert response.status_code == 200
    mock_cleanup.assert_not_awaited()


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

    response, mock_cleanup = await _complete(async_client, run.id)

    assert response.status_code == 200
    mock_cleanup.assert_awaited_once()


# ---------------------------------------------------------------------------
# Reaching the sandbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continue_conversation_loads_the_run_s_automation(
    org_id, async_session_factory, mock_authenticated_user, monkeypatch
):
    """Minting a cloud API key reads `run.automation`.

    A lazy load raises MissingGreenlet, which `send_conversation_turn` catches
    and turns into a silent fallback to a run -- the feature would look
    switched off in cloud mode. Stubs sit at the HTTP boundary so the ORM path
    is the real one.
    """
    key_urls: list[str] = []

    class KeyResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"key": "minted-key"}

    class KeyClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            key_urls.append(url)
            return KeyResponse()

    monkeypatch.setattr(
        "openhands.automation.utils.api_key.httpx",
        SimpleNamespace(AsyncClient=KeyClient, HTTPStatusError=httpx.HTTPStatusError),
    )

    async def fake_sandbox_url(client, api_url, api_key, sandbox_id):
        return "https://sandbox.example.com", "sandbox-key"

    monkeypatch.setattr(
        "openhands.automation.utils.conversation_turn.get_sandbox_agent_url",
        fake_sandbox_url,
    )

    posted: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request.url.path)
        return httpx.Response(200, json={"success": True})

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(
        "openhands.automation.utils.conversation_turn.httpx",
        SimpleNamespace(AsyncClient=client_factory),
    )

    subject_key = f"{TEAM}/C123/1.1"
    async with async_session_factory() as setup:
        automation = make_automation(
            org_id, mock_authenticated_user.user_id, continuing_trigger()
        )
        setup.add(automation)
        await setup.commit()
        setup.add(
            AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.COMPLETED,
                sandbox_id="sbx-1",
                subject_key=subject_key,
            )
        )
        await setup.commit()
        automation_id = automation.id

    # A session with nothing preloaded, so no identity-map hit can mask it.
    async with async_session_factory() as session:
        result = await continue_conversation(
            session,
            org_id=org_id,
            source="slack",
            subject_key=subject_key,
            automation_id=automation_id,
            event_key="app_mention",
            event_payload={"hello": "world"},
        )
        await session.commit()

    derived = conversation_id_for(org_id, automation_id, "slack", subject_key)
    assert result == derived
    assert key_urls, "the API key was never minted, so run.automation failed"
    assert posted == [f"/api/conversations/{derived}/events"]


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_follow_ups_all_continue_one_conversation(
    org_id, async_session_factory, mock_authenticated_user, monkeypatch
):
    """The row lock serialises them; none forks off a run of its own."""
    sent: list[str] = []

    async def fake_send(run, conversation_id, text):
        sent.append(conversation_id)
        await asyncio.sleep(0.01)  # hold the lock across an await
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
        automation_id = automation.id

    envelope = slack_envelope()

    async def deliver(event_id: str):
        async with async_session_factory() as session:
            return await _mention(session, org_id, envelope, event_id)

    first = await deliver("Ev0")
    async with async_session_factory() as session:
        await give_sandbox(session, uuid.UUID(first.run_ids[0]))

    results = await asyncio.gather(*(deliver(f"Later{n}") for n in range(5)))

    derived = conversation_id_for(
        org_id, automation_id, "slack", f"{TEAM}/C123/1755000000.000100"
    )
    assert sent == [derived] * 5
    assert all(r.conversation_ids == [derived] for r in results)
    assert all(r.run_ids == [] for r in results)

    async with async_session_factory() as session:
        assert len(await fetch_runs(session)) == 1


@pytest.mark.asyncio
async def test_no_deadlock_across_several_automations_on_one_subject(
    org_id, async_session_factory, mock_authenticated_user, monkeypatch
):
    """Why `get_event_automations` orders by id.

    Without a stable lock order, two events on one subject can take the same
    run rows in opposite orders and deadlock.
    """

    async def fake_send(run, conversation_id, text):
        return True

    monkeypatch.setattr(
        "openhands.automation.conversations.send_conversation_turn", fake_send
    )

    async with async_session_factory() as setup:
        for name in ("Alpha", "Bravo", "Charlie"):
            setup.add(
                make_automation(
                    org_id,
                    mock_authenticated_user.user_id,
                    continuing_trigger(),
                    name,
                )
            )
        await setup.commit()

    envelope = slack_envelope()
    errors: list[str] = []

    async def deliver(event_id: str) -> None:
        try:
            async with async_session_factory() as session:
                await _mention(session, org_id, envelope, event_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    await asyncio.gather(*(deliver(f"Ev{n}") for n in range(8)))

    assert errors == []
    async with async_session_factory() as session:
        owners = await subject_runs(session)
    assert len({run.automation_id for run in owners}) == 3
