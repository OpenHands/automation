"""Supervises one task per stream source for the life of the process.

A source holds a connection open for the life of the process, in the same
process as everything else. The supervisor is what keeps that from mattering:
every source is a child task whose exceptions are caught here, so one bad
provider can never take down the others, the scheduler, the dispatcher, or
HTTP webhook handling.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from contextlib import suppress
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.config import StreamSettings, get_config
from openhands.automation.ingest import AcceptedEvent, accept_event
from openhands.automation.streams.base import (
    Emit,
    StreamConfigError,
    StreamProvider,
    health_for,
    record_health,
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

# Restart backoff: doubled per consecutive failure, up to the ceiling. Not
# configurable -- these bound how fast a dead source is retried, which is a
# property of the transport rather than of a deployment.
_BACKOFF_SECONDS: Final[float] = 5.0
_MAX_BACKOFF_SECONDS: Final[float] = 300.0
# The ceiling caps the delay long before this, but an unbounded shift on a
# long-running failure would build an integer instead of a delay.
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
            _supervise(provider, session_factory, shutdown_event)
            for provider in providers
        )
    )


async def _supervise(
    provider: StreamProvider,
    session_factory: async_sessionmaker[AsyncSession],
    shutdown_event: asyncio.Event,
) -> None:
    """Run one source, restarting it with backoff for as long as it can work."""
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
            failures = health_for(provider.name).consecutive_failures + 1
            record_health(provider.name, consecutive_failures=failures)
            logger.exception(
                "Stream source %s failed (%d consecutive)", provider.name, failures
            )
        else:
            failures = 0
            record_health(provider.name, consecutive_failures=failures)

        if shutdown_event.is_set():
            return

        delay = min(
            _BACKOFF_SECONDS * 2 ** min(max(failures - 1, 0), _MAX_BACKOFF_DOUBLINGS),
            _MAX_BACKOFF_SECONDS,
        )
        logger.info("Restarting stream source %s in %.0fs", provider.name, delay)
        # The loop condition re-reads the event, so a shutdown landing during
        # the wait ends the source instead of restarting it.
        with suppress(TimeoutError):
            await asyncio.wait_for(shutdown_event.wait(), timeout=delay)


def _make_emit(
    provider: StreamProvider,
    session_factory: async_sessionmaker[AsyncSession],
) -> Emit:
    """Bind a provider's org to `accept_event()`.

    Each event gets its own session: the connection is long-lived and holding
    one open across it would pin a pooled connection for the process lifetime.
    """

    async def emit(event: AcceptedEvent) -> None:
        record_health(provider.name, last_event_at=utcnow())
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
