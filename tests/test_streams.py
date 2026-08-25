"""Tests for the supervised stream transport and its Slack provider."""

import asyncio
import uuid
from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from sqlalchemy import select

from openhands.automation.config import (
    StreamSettings,
    clear_config_cache,
    get_config,
)
from openhands.automation.ingest import AcceptedEvent
from openhands.automation.models import Automation, AutomationRun
from openhands.automation.streams import (
    SourceHealth,
    StreamConfigError,
    build_stream_providers,
    stream_health,
    stream_supervisor_loop,
)
from openhands.automation.streams.base import record_health, reset_stream_health
from openhands.automation.streams.slack import SlackStreamProvider
from openhands.automation.utils.time import utcnow


TEAM_ID = "T06P212QSEA"
BOT_USER_ID = "U0BOT"


@pytest.fixture(autouse=True)
def clean_health():
    reset_stream_health()
    yield
    reset_stream_health()


@pytest.fixture
def provider() -> SlackStreamProvider:
    return SlackStreamProvider(
        org_id=uuid.uuid4(),
        app_token="xapp-test",
        bot_token="xoxb-test",
        team_id=TEAM_ID,
        bot_user_id=BOT_USER_ID,
    )


def envelope(**event_overrides: Any) -> dict:
    """A Slack `event_callback` envelope, as Socket Mode delivers it."""
    event = {
        "type": "app_mention",
        "user": "U456",
        "text": f"<@{BOT_USER_ID}> please take a look",
        "ts": "1755000000.000100",
        "channel": "C123",
    }
    event.update(event_overrides)
    return {
        "type": "event_callback",
        "team_id": TEAM_ID,
        "api_app_id": "A123",
        "event_id": "Ev0001",
        "event_time": 1755000000,
        "event": event,
    }


class FakeSocketClient:
    """Records what the provider did, and in what order."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def send_socket_mode_response(self, response) -> None:
        self.calls.append(f"ack:{response.envelope_id}")


class FakeWebClient:
    """Stands in for `AsyncWebClient.auth_test()`."""

    def __init__(self, **identity: str) -> None:
        self.identity = identity

    async def auth_test(self) -> dict:
        return self.identity


def request(payload: dict, request_type: str = "events_api"):
    from slack_sdk.socket_mode.request import SocketModeRequest

    return SocketModeRequest(type=request_type, envelope_id="env-1", payload=payload)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health_records_are_frozen_and_slotted():
    health = SourceHealth()
    with pytest.raises(FrozenInstanceError):
        health.consecutive_failures = 1  # type: ignore[misc]
    assert not hasattr(health, "__dict__")


def test_recording_health_replaces_the_record():
    """An update leaves the fields it does not name alone."""
    stamped = record_health("fake:one", last_event_at=utcnow())
    updated = record_health("fake:one", consecutive_failures=2)

    assert updated is not stamped
    assert updated.last_event_at == stamped.last_event_at
    assert updated.consecutive_failures == 2
    assert stream_health()["fake:one"] == updated


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_streams_are_off_by_default():
    """The default deployment behaves exactly as it does today."""
    settings = StreamSettings()
    assert settings.streams_enabled is False
    assert settings.enabled is False
    assert build_stream_providers(settings) == []


def test_slack_apps_come_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    """One provider per configured app, no table involved."""
    org_id = uuid.uuid4()
    monkeypatch.setenv("AUTOMATION_STREAMS_ENABLED", "true")
    monkeypatch.setenv(
        "AUTOMATION_SLACK_APPS",
        f'[{{"org_id": "{org_id}", "app_token": "xapp-1", '
        f'"bot_token": "xoxb-1", "team_id": "{TEAM_ID}", '
        f'"bot_user_id": "{BOT_USER_ID}"}}]',
    )
    clear_config_cache()
    try:
        settings = get_config().streams
        assert settings.enabled is True

        providers = build_stream_providers(settings)
        assert len(providers) == 1
        assert providers[0].source == "slack"
        assert providers[0].org_id == org_id
        assert providers[0].name == f"slack:{TEAM_ID}"
    finally:
        clear_config_cache()


def test_enabled_needs_a_configured_source(monkeypatch: pytest.MonkeyPatch):
    """The switch alone starts nothing."""
    monkeypatch.setenv("AUTOMATION_STREAMS_ENABLED", "true")
    clear_config_cache()
    try:
        assert get_config().streams.enabled is False
    finally:
        clear_config_cache()


# ---------------------------------------------------------------------------
# Slack provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_envelope_is_acked_before_it_is_routed(provider):
    """Slack redelivers anything unacked, so the ack cannot wait on routing."""
    client = FakeSocketClient()

    async def emit(event: AcceptedEvent) -> None:
        client.calls.append(f"emit:{event.event_key}")

    await provider.handle(client, request(envelope()), emit)

    assert client.calls == ["ack:env-1", "emit:app_mention"]


@pytest.mark.asyncio
async def test_ignored_envelope_is_still_acked(provider):
    """A message we drop is a message Slack must not redeliver."""
    client = FakeSocketClient()
    emitted: list[AcceptedEvent] = []

    await provider.handle(
        client,
        request(envelope(type="message")),
        lambda event: emitted.append(event),  # never awaited
    )

    assert client.calls == ["ack:env-1"]
    assert emitted == []


@pytest.mark.asyncio
async def test_non_events_api_request_is_acked_only(provider):
    client = FakeSocketClient()
    emitted: list[AcceptedEvent] = []

    await provider.handle(
        client,
        request({}, request_type="slash_commands"),
        lambda event: emitted.append(event),
    )

    assert client.calls == ["ack:env-1"]
    assert emitted == []


def test_app_mention_becomes_an_accepted_event(provider):
    """The whole envelope is carried, so existing triggers filter unchanged."""
    event = provider.accepted_event(envelope())

    assert event is not None
    assert event.source == "slack"
    assert event.event_key == "app_mention"
    assert event.payload["team_id"] == TEAM_ID
    assert event.provider_event_id == "Ev0001"


@pytest.mark.parametrize(
    "overrides",
    [
        {"bot_id": "B123"},
        {"subtype": "bot_message"},
    ],
)
def test_bot_messages_are_dropped(provider, overrides: dict):
    """Otherwise the bot's own mention re-triggers the automation that wrote it."""
    assert provider.accepted_event(envelope(**overrides)) is None


def test_other_event_types_are_dropped(provider):
    assert provider.accepted_event(envelope(type="message")) is None


def test_other_teams_are_dropped(provider):
    payload = envelope()
    payload["team_id"] = "T-OTHER"
    assert provider.accepted_event(payload) is None


@pytest.mark.asyncio
async def test_identity_assertion_accepts_the_configured_app(provider):
    await provider.assert_identity(
        FakeWebClient(team_id=TEAM_ID, bot_user_id=BOT_USER_ID)
    )


@pytest.mark.parametrize(
    "identity",
    [
        {"team_id": "T-OTHER", "bot_user_id": BOT_USER_ID},
        {"team_id": TEAM_ID, "bot_user_id": "U-OTHER"},
    ],
)
@pytest.mark.asyncio
async def test_identity_assertion_refuses_a_mismatched_token(provider, identity: dict):
    """A mis-pasted token fails loudly instead of bridging the wrong workspace."""
    with pytest.raises(StreamConfigError):
        await provider.assert_identity(FakeWebClient(**identity))


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class FakeProvider:
    """A provider whose `run()` does whatever the test needs."""

    def __init__(self, name: str, behaviour) -> None:
        self.source = "fake"
        self.org_id = uuid.uuid4()
        self.name = name
        self.behaviour = behaviour
        self.runs = 0

    async def run(self, emit, shutdown: asyncio.Event) -> None:
        self.runs += 1
        await self.behaviour(self, emit, shutdown)


def fast_settings() -> StreamSettings:
    return StreamSettings(stream_backoff_seconds=0.01, stream_max_backoff_seconds=0.01)


async def hold(_provider, _emit, shutdown: asyncio.Event) -> None:
    await shutdown.wait()


@pytest.mark.asyncio
async def test_a_failing_source_does_not_affect_the_others(async_session_factory):
    """The isolation a separate systemd unit used to provide."""
    shutdown = asyncio.Event()
    failures = 0

    async def fail_twice(_provider, _emit, _shutdown) -> None:
        nonlocal failures
        failures += 1
        if failures <= 2:
            raise RuntimeError("socket died")
        await _shutdown.wait()

    flaky = FakeProvider("fake:flaky", fail_twice)
    healthy = FakeProvider("fake:healthy", hold)

    task = asyncio.create_task(
        stream_supervisor_loop(
            async_session_factory,
            shutdown_event=shutdown,
            providers=[flaky, healthy],
            settings=fast_settings(),
        )
    )
    while flaky.runs < 3:
        await asyncio.sleep(0.01)
    shutdown.set()
    await asyncio.wait_for(task, timeout=5)

    assert healthy.runs == 1
    assert stream_health()["fake:flaky"].consecutive_failures == 0


@pytest.mark.asyncio
async def test_a_misconfigured_source_is_not_retried(async_session_factory):
    """Nothing a restart can fix, so it stops rather than spinning."""
    shutdown = asyncio.Event()

    async def refuse(_provider, _emit, _shutdown) -> None:
        raise StreamConfigError("team_id does not match")

    broken = FakeProvider("fake:broken", refuse)
    healthy = FakeProvider("fake:healthy", hold)

    task = asyncio.create_task(
        stream_supervisor_loop(
            async_session_factory,
            shutdown_event=shutdown,
            providers=[broken, healthy],
            settings=fast_settings(),
        )
    )
    await asyncio.sleep(0.05)
    shutdown.set()
    await asyncio.wait_for(task, timeout=5)

    assert broken.runs == 1
    assert healthy.runs == 1


@pytest.mark.asyncio
async def test_shutdown_ends_every_source(async_session_factory):
    shutdown = asyncio.Event()
    providers = [FakeProvider(f"fake:{i}", hold) for i in range(3)]

    task = asyncio.create_task(
        stream_supervisor_loop(
            async_session_factory,
            shutdown_event=shutdown,
            providers=providers,
            settings=fast_settings(),
        )
    )
    await asyncio.sleep(0.05)
    shutdown.set()
    await asyncio.wait_for(task, timeout=5)

    assert all(provider.runs == 1 for provider in providers)


@pytest.mark.asyncio
async def test_emitted_event_creates_a_run(
    async_session_factory,
    mock_authenticated_user,
):
    """End to end: a mention creates the run an unmodified automation asks for.

    The second emit is the same envelope again -- a redelivery -- and creates
    nothing, because `accept_event()` deduplicates on `event_id`.
    """
    org_id = mock_authenticated_user.org_id
    async with async_session_factory() as session:
        session.add(
            Automation(
                id=uuid.uuid4(),
                user_id=mock_authenticated_user.user_id,
                org_id=org_id,
                name="Slack mention",
                tarball_path="oh-internal://uploads/test.tar.gz",
                entrypoint="python main.py",
                trigger={
                    "type": "event",
                    "source": "slack",
                    "on": "app_mention",
                    "filter": f"team_id == '{TEAM_ID}'",
                },
            )
        )
        await session.commit()

    shutdown = asyncio.Event()
    delivered = envelope()

    async def emit_twice(provider, emit, _shutdown) -> None:
        event = SlackStreamProvider(
            org_id=org_id,
            app_token="xapp",
            bot_token="xoxb",
            team_id=TEAM_ID,
            bot_user_id=BOT_USER_ID,
        ).accepted_event(delivered)
        assert event is not None
        await emit(event)
        await emit(event)
        _shutdown.set()

    source = FakeProvider("fake:slack", emit_twice)
    source.org_id = org_id

    await asyncio.wait_for(
        stream_supervisor_loop(
            async_session_factory,
            shutdown_event=shutdown,
            providers=[source],
            settings=fast_settings(),
        ),
        timeout=10,
    )

    async with async_session_factory() as session:
        runs = list((await session.execute(select(AutomationRun))).scalars().all())
    assert len(runs) == 1
    assert stream_health()["fake:slack"].last_event_at is not None
