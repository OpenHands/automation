"""Supervises one task per stream source for the life of the process.

Moving a source out of its own systemd unit and into the service process gives
up that unit's failure isolation. The supervisor buys it back: every source is
a child task whose exceptions are caught here, so one bad provider can never
take down the others, the scheduler, the dispatcher, or HTTP webhook handling.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.config import StreamSettings, get_config
from openhands.automation.ingest import AcceptedEvent, accept_event
from openhands.automation.streams.base import (
    Emit,
    StreamConfigError,
    StreamProvider,
    health_for,
)
from openhands.automation.streams.slack import build_slack_providers
from openhands.automation.utils.time import utcnow


logger = logging.getLogger("automation.streams.supervisor")

# Registry of the sources this build can stream, each built from settings --
# the same shape as the webhook provider registry, without a table behind it.
# Per-org socket configuration is a multi-tenant requirement; add it when
# someone has it.
BUILTIN_STREAM_SOURCES: Final[
    dict[str, Callable[[StreamSettings], Sequence[StreamProvider]]]
] = {
    "slack": build_slack_providers,
}

# Doubling is capped well below this, but an unbounded shift on a long-running
# failure would build an integer instead of a delay.
_MAX_BACKOFF_DOUBLINGS: Final[int] = 20


def build_stream_providers(
    settings: StreamSettings | None = None,
) -> list[StreamProvider]:
    """Build every provider this deployment has configured."""
    settings = settings if settings is not None else get_config().streams
    providers: list[StreamProvider] = []
    for build in BUILTIN_STREAM_SOURCES.values():
        providers.extend(build(settings))
    return providers


async def stream_supervisor_loop(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    shutdown_event: asyncio.Event,
    providers: Sequence[StreamProvider] | None = None,
    settings: StreamSettings | None = None,
) -> None:
    """Run every configured stream source until shutdown.

    Started by app.py only when `config.streams.enabled`.
    """
    settings = settings if settings is not None else get_config().streams
    providers = build_stream_providers(settings) if providers is None else providers
    if not providers:
        logger.info("No stream sources configured; supervisor idle")
        return

    logger.info(
        "Supervising %d stream source(s): %s",
        len(providers),
        ", ".join(provider.name for provider in providers),
    )
    await asyncio.gather(
        *(
            _supervise(provider, session_factory, shutdown_event, settings)
            for provider in providers
        )
    )


async def _supervise(
    provider: StreamProvider,
    session_factory: async_sessionmaker[AsyncSession],
    shutdown_event: asyncio.Event,
    settings: StreamSettings,
) -> None:
    """Run one source, restarting it with backoff for as long as it can work."""
    health = health_for(provider.name)
    emit = _make_emit(provider, session_factory)

    while not shutdown_event.is_set():
        try:
            await provider.run(emit, shutdown_event)
        except StreamConfigError as e:
            # Nothing a restart can fix, and retrying would keep presenting a
            # bad token to Slack. Stop this source; the others are unaffected.
            logger.error("Stream source %s will not start: %s", provider.name, e)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            health.consecutive_failures += 1
            logger.exception(
                "Stream source %s failed (%d consecutive)",
                provider.name,
                health.consecutive_failures,
            )
        else:
            health.consecutive_failures = 0

        if shutdown_event.is_set():
            return

        delay = min(
            settings.stream_backoff_seconds
            * 2 ** min(max(health.consecutive_failures - 1, 0), _MAX_BACKOFF_DOUBLINGS),
            settings.stream_max_backoff_seconds,
        )
        logger.info("Restarting stream source %s in %.0fs", provider.name, delay)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
        except TimeoutError:
            continue
        return


def _make_emit(
    provider: StreamProvider,
    session_factory: async_sessionmaker[AsyncSession],
) -> Emit:
    """Bind a provider's org to `accept_event()`.

    Each event gets its own session: the connection is long-lived and holding
    one open across it would pin a pooled connection for the process lifetime.
    """
    health = health_for(provider.name)

    async def emit(event: AcceptedEvent) -> None:
        health.last_event_at = utcnow()
        async with session_factory() as session:
            result = await accept_event(
                provider.org_id,
                event,
                session,
                session_factory=session_factory,
            )
        logger.info(
            "Stream source %s: %s matched %d automation(s)",
            provider.name,
            event.event_key,
            result.matched,
        )

    return emit
