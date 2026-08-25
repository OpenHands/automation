"""The stream provider protocol and the health the supervisor records.

A provider owns its own connection loop and pushes events out through `emit`
rather than being polled: a callback-driven SDK already reconnects correctly,
and forcing it into a `receive()` shape means reimplementing that badly.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from openhands.automation.ingest import AcceptedEvent


logger = logging.getLogger("automation.streams")

Emit = Callable[[AcceptedEvent], Awaitable[None]]


class StreamConfigError(Exception):
    """A source is configured wrongly and will never work as it stands.

    Restarting cannot help, so the supervisor stops supervising that source
    rather than backing off and retrying forever.
    """


class StreamProvider(Protocol):
    """One supervised long-lived connection to an event source."""

    # The event source name, matching what automations trigger on.
    source: str
    # The organization this connection's events are routed to. `emit` takes
    # only the event, so the supervisor binds the org from here.
    org_id: uuid.UUID

    @property
    def name(self) -> str:
        """Identifies this connection in logs and health; a source may have many."""
        ...

    async def run(self, emit: Emit, shutdown: asyncio.Event) -> None:
        """Hold the connection until `shutdown` is set, emitting as events land."""
        ...


@dataclass
class SourceHealth:
    """Whether a source is working, in the terms an operator would ask in."""

    last_connected_at: datetime | None = None
    last_event_at: datetime | None = None
    consecutive_failures: int = 0


# Process-local, like git sync's in-flight state: a crash cannot strand it the
# way a persisted flag would, and it describes this replica's connections only.
_HEALTH: dict[str, SourceHealth] = {}


def health_for(name: str) -> SourceHealth:
    """The health record for one connection, created on first use."""
    return _HEALTH.setdefault(name, SourceHealth())


def stream_health() -> dict[str, SourceHealth]:
    """Health for every stream source this replica has supervised."""
    return dict(_HEALTH)


def reset_stream_health() -> None:
    """Drop all recorded health. For tests."""
    _HEALTH.clear()
