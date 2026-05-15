"""Session management utilities for event routing.

Helpers for creating, looking up, and updating automation sessions used
in session-based event routing (see schemas.SessionConfig).
"""

import logging
import uuid
from datetime import timedelta
from typing import Any

import jmespath
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.models import (
    AutomationRun,
    AutomationSession,
    PendingSessionEvent,
    SessionStatus,
)
from openhands.automation.utils.time import utcnow


logger = logging.getLogger("automation.utils.session")


def extract_session_key(key_expr: str, payload: dict[str, Any]) -> str | None:
    """Extract a session key from an event payload using a JMESPath expression.

    Args:
        key_expr: JMESPath expression, e.g. ``'pull_request.number'`` or
                  ``'thread_ts || ts'``.
        payload: The event payload dict.

    Returns:
        The extracted value as a string, or ``None`` if the expression
        evaluates to ``None`` or raises an error.
    """
    try:
        result = jmespath.search(key_expr, payload)
    except Exception as e:
        logger.warning("Failed to extract session key using expr=%r: %s", key_expr, e)
        return None

    if result is None:
        return None
    return str(result)


async def get_active_session(
    automation_id: uuid.UUID,
    session_key: str,
    db_session: AsyncSession,
) -> AutomationSession | None:
    """Look up an ACTIVE, non-expired session for an automation + session key.

    Returns the most-recently-started active session, or ``None`` if none exists.
    """
    now = utcnow()
    result = await db_session.execute(
        select(AutomationSession)
        .where(
            AutomationSession.automation_id == automation_id,
            AutomationSession.session_key == session_key,
            AutomationSession.status == SessionStatus.ACTIVE,
            AutomationSession.expires_at > now,
        )
        .order_by(AutomationSession.started_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create_session(
    automation_id: uuid.UUID,
    session_key: str,
    run_id: uuid.UUID,
    session_timeout_seconds: int,
    db_session: AsyncSession,
) -> AutomationSession:
    """Create a new ACTIVE session record.

    Args:
        automation_id: The parent automation.
        session_key: The key extracted from the triggering event payload.
        run_id: The ``AutomationRun`` that owns this session.
        session_timeout_seconds: Maximum session lifetime.
        db_session: Async SQLAlchemy session.

    Returns:
        The newly created ``AutomationSession`` (not yet committed).
    """
    now = utcnow()
    session = AutomationSession(
        id=uuid.uuid4(),
        automation_id=automation_id,
        session_key=session_key,
        run_id=run_id,
        status=SessionStatus.ACTIVE,
        started_at=now,
        expires_at=now + timedelta(seconds=session_timeout_seconds),
        last_event_at=now,
    )
    db_session.add(session)
    return session


async def queue_pending_event(
    automation_id: uuid.UUID,
    session_key: str,
    event_payload: dict[str, Any],
    db_session: AsyncSession,
) -> PendingSessionEvent:
    """Queue an event for delivery to the sandbox owning the given session.

    Updates the session's ``last_event_at`` if an active session exists.

    Args:
        automation_id: The parent automation.
        session_key: The session key.
        event_payload: The event payload to deliver.
        db_session: Async SQLAlchemy session.

    Returns:
        The created ``PendingSessionEvent`` (not yet committed).
    """
    pending_event = PendingSessionEvent(
        id=uuid.uuid4(),
        automation_id=automation_id,
        session_key=session_key,
        event_payload=event_payload,
    )
    db_session.add(pending_event)

    # Bump last_event_at on the active session so idle-timeout tracking is accurate
    now = utcnow()
    await db_session.execute(
        update(AutomationSession)
        .where(
            AutomationSession.automation_id == automation_id,
            AutomationSession.session_key == session_key,
            AutomationSession.status == SessionStatus.ACTIVE,
        )
        .values(last_event_at=now)
    )

    return pending_event


async def mark_session_dead(
    automation_run: AutomationRun,
    db_session: AsyncSession,
) -> bool:
    """Mark the session for a given run as DEAD.

    Called by the watchdog when a run's sandbox is found to be dead.  Uses
    optimistic locking (``WHERE status = 'ACTIVE'``) so concurrent callbacks
    don't clobber a legitimate EXPIRED transition.

    Args:
        automation_run: The run whose session should be marked dead.
        db_session: Async SQLAlchemy session.

    Returns:
        ``True`` if a session was found and updated, ``False`` otherwise.
    """
    from sqlalchemy.engine import CursorResult

    stmt = (
        update(AutomationSession)
        .where(
            AutomationSession.run_id == automation_run.id,
            AutomationSession.status == SessionStatus.ACTIVE,
        )
        .values(status=SessionStatus.DEAD)
    )
    result: CursorResult = await db_session.execute(stmt)  # type: ignore[assignment]
    return result.rowcount > 0
