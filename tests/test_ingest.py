"""Direct unit tests for `accept_event()`.

Every test builds an `AcceptedEvent` by hand — no request, no signature, no
FastAPI app — which is the point of the seam.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from openhands.automation import telemetry
from openhands.automation.auth import AuthenticatedUser
from openhands.automation.config import clear_config_cache
from openhands.automation.event_schemas import parse_event
from openhands.automation.ingest import (
    AcceptedEvent,
    AcceptResult,
    EventSubject,
    accept_event,
)
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
    IntegrationEvent,
)


@pytest.fixture
def org_id(mock_authenticated_user: AuthenticatedUser) -> uuid.UUID:
    """Get org_id from authenticated user fixture."""
    return mock_authenticated_user.org_id


@pytest.fixture
def slack_payload() -> dict:
    """A Slack app_mention envelope, as a socket transport would receive it."""
    return {
        "type": "app_mention",
        "team_id": "T06P212QSEA",
        "channel": "C123",
        "user": "U456",
        "text": "<@U999> please take a look",
        "ts": "1755000000.000100",
    }


@pytest.fixture
def github_push_payload() -> dict:
    """Sample GitHub push payload, already unwrapped."""
    return {
        "ref": "refs/heads/main",
        "before": "abc123",
        "after": "def456",
        "commits": [
            {
                "id": "def456",
                "message": "Test commit",
                "author": {"name": "Test", "email": "test@example.com"},
            }
        ],
        "repository": {
            "id": 123,
            "name": "test-repo",
            "full_name": "org/test-repo",
            "private": False,
        },
        "sender": {"id": 1, "login": "testuser"},
    }


def make_automation(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    trigger: dict,
    name: str = "Test Automation",
) -> Automation:
    """Build an event-triggered automation."""
    return Automation(
        id=uuid.uuid4(),
        user_id=user_id,
        org_id=org_id,
        name=name,
        tarball_path="oh-internal://uploads/test.tar.gz",
        entrypoint="python main.py",
        trigger=trigger,
    )


async def fetch_runs(session) -> list[AutomationRun]:
    """Return every run in the database."""
    result = await session.execute(select(AutomationRun))
    return list(result.scalars().all())


async def fetch_events(session) -> list[IntegrationEvent]:
    """Return every recorded event, oldest first."""
    result = await session.execute(
        select(IntegrationEvent).order_by(IntegrationEvent.received_at)
    )
    return list(result.scalars().all())


@pytest.fixture
def captured_telemetry(monkeypatch: pytest.MonkeyPatch):
    """Record the events telemetry would POST instead of sending them."""
    monkeypatch.setenv("AUTOMATION_POSTHOG_API_KEY", "phc-test-key")
    clear_config_cache()

    events: list[str] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args) -> bool:
            return False

        async def post(self, url: str, *, json: dict[str, Any]) -> FakeResponse:
            events.append(str(json["event"]))
            return FakeResponse()

    monkeypatch.setattr(telemetry.httpx, "AsyncClient", FakeClient)
    yield events
    clear_config_cache()


@pytest.mark.asyncio
async def test_accept_event_no_automations(
    org_id: uuid.UUID,
    async_session,
    slack_payload: dict,
):
    """No automations configured: nothing matches, nothing is created."""
    result = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=slack_payload,
        ),
        async_session,
    )

    assert isinstance(result, AcceptResult)
    assert result.matched == 0
    assert result.run_ids == []
    assert result.duplicate is False
    assert await fetch_runs(async_session) == []


@pytest.mark.asyncio
async def test_accept_event_matching_automation_creates_run(
    org_id: uuid.UUID,
    async_session,
    slack_payload: dict,
    mock_authenticated_user,
):
    """A matching trigger produces one PENDING run, with no HTTP involved."""
    automation = make_automation(
        org_id,
        mock_authenticated_user.user_id,
        {"type": "event", "source": "slack", "on": "app_mention"},
    )
    async_session.add(automation)
    await async_session.commit()

    result = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=slack_payload,
        ),
        async_session,
    )

    assert result.matched == 1
    assert len(result.run_ids) == 1

    runs = await fetch_runs(async_session)
    assert len(runs) == 1
    run = runs[0]
    assert str(run.id) == result.run_ids[0]
    assert run.automation_id == automation.id
    assert run.status == AutomationRunStatus.PENDING


@pytest.mark.asyncio
async def test_accept_event_without_parsed_event_stores_raw_payload(
    org_id: uuid.UUID,
    async_session,
    slack_payload: dict,
    mock_authenticated_user,
):
    """A transport with no typed model gets its raw payload on the run."""
    async_session.add(
        make_automation(
            org_id,
            mock_authenticated_user.user_id,
            {"type": "event", "source": "slack", "on": "app_mention"},
        )
    )
    await async_session.commit()

    await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=slack_payload,
        ),
        async_session,
    )

    runs = await fetch_runs(async_session)
    assert runs[0].event_payload == slack_payload


@pytest.mark.asyncio
async def test_accept_event_with_parsed_event_stores_model_dump(
    org_id: uuid.UUID,
    async_session,
    github_push_payload: dict,
    mock_authenticated_user,
):
    """A typed event persists its model_dump, not the raw provider payload."""
    async_session.add(
        make_automation(
            org_id,
            mock_authenticated_user.user_id,
            {"type": "event", "source": "github", "on": "push"},
        )
    )
    await async_session.commit()

    parsed = parse_event("github", github_push_payload)

    await accept_event(
        org_id,
        AcceptedEvent(
            source="github",
            event_key=parsed.event_key,
            payload=github_push_payload,
            parsed_event=parsed,
        ),
        async_session,
    )

    runs = await fetch_runs(async_session)
    assert runs[0].event_payload == parsed.model_dump(mode="json")
    assert runs[0].event_payload != github_push_payload


@pytest.mark.asyncio
async def test_accept_event_filter_runs_against_raw_payload(
    org_id: uuid.UUID,
    async_session,
    slack_payload: dict,
    mock_authenticated_user,
):
    """JMESPath filters are evaluated against `AcceptedEvent.payload`."""
    async_session.add(
        make_automation(
            org_id,
            mock_authenticated_user.user_id,
            {
                "type": "event",
                "source": "slack",
                "on": "app_mention",
                "filter": "team_id == 'T06P212QSEA'",
            },
            name="Matching filter",
        )
    )
    async_session.add(
        make_automation(
            org_id,
            mock_authenticated_user.user_id,
            {
                "type": "event",
                "source": "slack",
                "on": "app_mention",
                "filter": "team_id == 'T_OTHER'",
            },
            name="Non-matching filter",
        )
    )
    await async_session.commit()

    result = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=slack_payload,
        ),
        async_session,
    )

    assert result.matched == 1
    assert len(result.run_ids) == 1


@pytest.mark.asyncio
async def test_accept_event_event_key_mismatch(
    org_id: uuid.UUID,
    async_session,
    slack_payload: dict,
    mock_authenticated_user,
):
    """An automation listening for a different event key does not match."""
    async_session.add(
        make_automation(
            org_id,
            mock_authenticated_user.user_id,
            {"type": "event", "source": "slack", "on": "message"},
        )
    )
    await async_session.commit()

    result = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=slack_payload,
        ),
        async_session,
    )

    assert result.matched == 0
    assert result.run_ids == []
    assert await fetch_runs(async_session) == []


@pytest.mark.asyncio
async def test_accept_event_other_source_not_matched(
    org_id: uuid.UUID,
    async_session,
    slack_payload: dict,
    mock_authenticated_user,
):
    """Automations for a different source are never candidates."""
    async_session.add(
        make_automation(
            org_id,
            mock_authenticated_user.user_id,
            {"type": "event", "source": "github", "on": "push"},
        )
    )
    await async_session.commit()

    result = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=slack_payload,
        ),
        async_session,
    )

    assert result.matched == 0
    assert result.run_ids == []


@pytest.mark.asyncio
async def test_accept_event_other_org_not_matched(
    org_id: uuid.UUID,
    async_session,
    slack_payload: dict,
    mock_authenticated_user,
):
    """Automations belonging to another org are never candidates."""
    async_session.add(
        make_automation(
            uuid.uuid4(),
            mock_authenticated_user.user_id,
            {"type": "event", "source": "slack", "on": "app_mention"},
        )
    )
    await async_session.commit()

    result = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=slack_payload,
        ),
        async_session,
    )

    assert result.matched == 0
    assert result.run_ids == []


@pytest.mark.asyncio
async def test_accept_event_creates_a_run_per_matched_automation(
    org_id: uuid.UUID,
    async_session,
    slack_payload: dict,
    mock_authenticated_user,
):
    """Every matching automation gets its own run."""
    for i in range(3):
        async_session.add(
            make_automation(
                org_id,
                mock_authenticated_user.user_id,
                {"type": "event", "source": "slack", "on": "app_mention"},
                name=f"Automation {i}",
            )
        )
    await async_session.commit()

    result = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=slack_payload,
        ),
        async_session,
    )

    assert result.matched == 3
    assert len(set(result.run_ids)) == 3
    assert len(await fetch_runs(async_session)) == 3


@pytest.mark.asyncio
async def test_accept_event_commits_runs(
    org_id: uuid.UUID,
    async_session_factory,
    async_session,
    slack_payload: dict,
    mock_authenticated_user,
):
    """Runs are committed, so a separate session can see them."""
    async_session.add(
        make_automation(
            org_id,
            mock_authenticated_user.user_id,
            {"type": "event", "source": "slack", "on": "app_mention"},
        )
    )
    await async_session.commit()

    result = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=slack_payload,
        ),
        async_session,
    )

    async with async_session_factory() as other_session:
        runs = await fetch_runs(other_session)

    assert [str(run.id) for run in runs] == result.run_ids


@pytest.mark.asyncio
async def test_accept_event_reserved_fields_are_ignored(
    org_id: uuid.UUID,
    async_session,
    slack_payload: dict,
    mock_authenticated_user,
):
    """`subject` and `occurred_at` are read by nobody and change nothing."""
    async_session.add(
        make_automation(
            org_id,
            mock_authenticated_user.user_id,
            {"type": "event", "source": "slack", "on": "app_mention"},
        )
    )
    await async_session.commit()

    result = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=slack_payload,
            provider_event_id="Ev123456",
            subject=EventSubject(key="T06P212QSEA/C123/1755000000.000100"),
            occurred_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
        ),
        async_session,
    )

    assert result.matched == 1
    assert result.duplicate is False

    runs = await fetch_runs(async_session)
    assert len(runs) == 1
    assert runs[0].event_payload == slack_payload


@pytest.mark.asyncio
async def test_accept_event_emits_telemetry_without_a_request(
    org_id: uuid.UUID,
    async_session,
    async_session_factory,
    slack_payload: dict,
    mock_authenticated_user,
    captured_telemetry: list[str],
):
    """A non-HTTP caller gets the full telemetry sequence via session_factory."""
    async_session.add(
        make_automation(
            org_id,
            mock_authenticated_user.user_id,
            {"type": "event", "source": "slack", "on": "app_mention"},
        )
    )
    await async_session.commit()

    result = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=slack_payload,
        ),
        async_session,
        session_factory=async_session_factory,
    )

    assert result.matched == 1
    assert captured_telemetry == [
        "automation_event_matched",
        "automation_run_scheduled",
        "automation_run_created",
    ]


@pytest.mark.asyncio
async def test_accept_event_telemetry_needs_a_session_source(
    org_id: uuid.UUID,
    async_session,
    slack_payload: dict,
    mock_authenticated_user,
    captured_telemetry: list[str],
):
    """Neither a request nor a session_factory: telemetry is silently dropped.

    Routing still works, which is why this is easy to miss. Pins the reason
    `session_factory` exists on the signature.
    """
    async_session.add(
        make_automation(
            org_id,
            mock_authenticated_user.user_id,
            {"type": "event", "source": "slack", "on": "app_mention"},
        )
    )
    await async_session.commit()

    result = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=slack_payload,
        ),
        async_session,
    )

    assert result.matched == 1
    assert captured_telemetry == []


@pytest.mark.asyncio
async def test_accepted_event_defaults():
    """Only `source` and `event_key` are required."""
    event = AcceptedEvent(source="slack", event_key="app_mention")

    assert event.payload == {}
    assert event.provider_event_id is None
    assert event.subject is None
    assert event.occurred_at is None
    assert event.parsed_event is None


def test_dataclasses_are_frozen():
    """The ingest dataclasses are immutable."""
    event = AcceptedEvent(source="slack", event_key="app_mention")
    result = AcceptResult(matched=0, run_ids=[])
    subject = EventSubject(key="T1/C1/1.0")

    for obj, attr, value in (
        (event, "source", "github"),
        (result, "matched", 1),
        (subject, "key", "other"),
    ):
        with pytest.raises(AttributeError):
            setattr(obj, attr, value)


def test_dataclasses_use_slots():
    """The ingest dataclasses use slots, so instances carry no __dict__."""
    for obj in (
        AcceptedEvent(source="slack", event_key="app_mention"),
        AcceptResult(matched=0, run_ids=[]),
        EventSubject(key="T1/C1/1.0"),
    ):
        assert hasattr(type(obj), "__slots__")
        assert not hasattr(obj, "__dict__")


# ---------------------------------------------------------------------------
# Recording and deduplication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_event_records_what_it_matched(
    org_id: uuid.UUID,
    async_session,
    slack_payload: dict,
    mock_authenticated_user,
):
    """A routed event leaves a row describing itself and its match count."""
    async_session.add(
        make_automation(
            org_id,
            mock_authenticated_user.user_id,
            {"type": "event", "source": "slack", "on": "app_mention"},
        )
    )
    await async_session.commit()

    await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=slack_payload,
            provider_event_id="Ev123456",
        ),
        async_session,
    )

    events = await fetch_events(async_session)
    assert len(events) == 1
    recorded = events[0]
    assert recorded.org_id == org_id
    assert recorded.source == "slack"
    assert recorded.event_key == "app_mention"
    assert recorded.provider_event_id == "Ev123456"
    assert recorded.payload == slack_payload
    assert recorded.matched_count == 1
    assert recorded.received_at is not None


@pytest.mark.asyncio
async def test_accept_event_records_events_that_match_nothing(
    org_id: uuid.UUID,
    async_session,
    slack_payload: dict,
):
    """Nothing matches, so the row is the only evidence the event arrived."""
    result = await accept_event(
        org_id,
        AcceptedEvent(
            source="slack",
            event_key="app_mention",
            payload=slack_payload,
        ),
        async_session,
    )

    assert result.matched == 0
    assert await fetch_runs(async_session) == []

    events = await fetch_events(async_session)
    assert len(events) == 1
    assert events[0].matched_count == 0
    assert events[0].payload == slack_payload


@pytest.mark.asyncio
async def test_accept_event_drops_a_redelivered_provider_event_id(
    org_id: uuid.UUID,
    async_session,
    github_push_payload: dict,
    mock_authenticated_user,
):
    """The same delivery id twice: runs once, and says so."""
    async_session.add(
        make_automation(
            org_id,
            mock_authenticated_user.user_id,
            {"type": "event", "source": "github", "on": "push"},
        )
    )
    await async_session.commit()

    delivery = AcceptedEvent(
        source="github",
        event_key="push",
        payload=github_push_payload,
        provider_event_id="72d3162e-cc78-11e3-81ab-4c9367dc0958",
    )

    first = await accept_event(org_id, delivery, async_session)
    second = await accept_event(org_id, delivery, async_session)

    assert first.matched == 1
    assert first.duplicate is False

    assert second.duplicate is True
    assert second.matched == 0
    assert second.run_ids == []

    assert len(await fetch_runs(async_session)) == 1
    assert len(await fetch_events(async_session)) == 1


@pytest.mark.asyncio
async def test_accept_event_deduplicates_through_the_database(
    org_id: uuid.UUID,
    async_session_factory,
    slack_payload: dict,
    mock_authenticated_user,
):
    """Two sessions, no shared memory: the index is what does the work."""
    async with async_session_factory() as setup:
        setup.add(
            make_automation(
                org_id,
                mock_authenticated_user.user_id,
                {"type": "event", "source": "slack", "on": "app_mention"},
            )
        )
        await setup.commit()

    delivery = AcceptedEvent(
        source="slack",
        event_key="app_mention",
        payload=slack_payload,
        provider_event_id="Ev08MFMKH6",
    )

    async with async_session_factory() as replica_a:
        first = await accept_event(org_id, delivery, replica_a)
    async with async_session_factory() as replica_b:
        second = await accept_event(org_id, delivery, replica_b)

    assert first.matched == 1
    assert second.duplicate is True

    async with async_session_factory() as session:
        assert len(await fetch_runs(session)) == 1


@pytest.mark.asyncio
async def test_accept_event_does_not_deduplicate_without_an_id(
    org_id: uuid.UUID,
    async_session,
    slack_payload: dict,
    mock_authenticated_user,
):
    """No id means every event is distinct, so the index must let them coexist."""
    async_session.add(
        make_automation(
            org_id,
            mock_authenticated_user.user_id,
            {"type": "event", "source": "slack", "on": "app_mention"},
        )
    )
    await async_session.commit()

    delivery = AcceptedEvent(
        source="slack",
        event_key="app_mention",
        payload=slack_payload,
    )

    first = await accept_event(org_id, delivery, async_session)
    second = await accept_event(org_id, delivery, async_session)

    assert first.duplicate is False
    assert second.duplicate is False
    assert len(await fetch_runs(async_session)) == 2
    assert len(await fetch_events(async_session)) == 2


@pytest.mark.asyncio
async def test_accept_event_deduplicates_within_one_org_only(
    org_id: uuid.UUID,
    async_session,
    slack_payload: dict,
):
    """A custom webhook's source name is only unique per org, so the key is too."""
    other_org = uuid.uuid4()
    delivery = AcceptedEvent(
        source="ci",
        event_key="build",
        payload=slack_payload,
        provider_event_id="1",
    )

    first = await accept_event(org_id, delivery, async_session)
    second = await accept_event(other_org, delivery, async_session)

    assert first.duplicate is False
    assert second.duplicate is False
    assert {event.org_id for event in await fetch_events(async_session)} == {
        org_id,
        other_org,
    }


@pytest.mark.asyncio
async def test_accept_event_records_and_creates_runs_atomically(
    org_id: uuid.UUID,
    async_session,
    slack_payload: dict,
    mock_authenticated_user,
    monkeypatch: pytest.MonkeyPatch,
):
    """A failure creating runs leaves no row claiming the event was routed."""
    async_session.add(
        make_automation(
            org_id,
            mock_authenticated_user.user_id,
            {"type": "event", "source": "slack", "on": "app_mention"},
        )
    )
    await async_session.commit()

    async def boom(*args, **kwargs):
        raise RuntimeError("run creation failed")

    monkeypatch.setattr("openhands.automation.ingest.create_automation_run", boom)

    with pytest.raises(RuntimeError):
        await accept_event(
            org_id,
            AcceptedEvent(
                source="slack",
                event_key="app_mention",
                payload=slack_payload,
                provider_event_id="Ev123456",
            ),
            async_session,
        )

    await async_session.rollback()
    assert await fetch_events(async_session) == []
    assert await fetch_runs(async_session) == []


@pytest.mark.asyncio
async def test_accept_event_dedupe_survives_a_rolled_back_delivery(
    org_id: uuid.UUID,
    async_session_factory,
    slack_payload: dict,
    mock_authenticated_user,
    monkeypatch: pytest.MonkeyPatch,
):
    """The dedupe row rolls back too, so the provider's retry is not a duplicate."""
    async with async_session_factory() as setup:
        setup.add(
            make_automation(
                org_id,
                mock_authenticated_user.user_id,
                {"type": "event", "source": "slack", "on": "app_mention"},
            )
        )
        await setup.commit()

    delivery = AcceptedEvent(
        source="slack",
        event_key="app_mention",
        payload=slack_payload,
        provider_event_id="Ev08MFMKH6",
    )

    async def boom(*args, **kwargs):
        raise RuntimeError("run creation failed")

    monkeypatch.setattr("openhands.automation.ingest.create_automation_run", boom)
    async with async_session_factory() as failing:
        with pytest.raises(RuntimeError):
            await accept_event(org_id, delivery, failing)

    monkeypatch.undo()
    async with async_session_factory() as retry:
        result = await accept_event(org_id, delivery, retry)

    assert result.duplicate is False
    assert result.matched == 1
