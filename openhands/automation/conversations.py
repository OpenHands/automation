"""Routing an event to the conversation its subject already has.

No mapping table: the id is derived (`subjects.conversation_id_for`). Only the
*sandbox* has to be looked up, via `AutomationRun.subject_key` -- a turn sent
there costs no run at all.
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from openhands.automation.db import using_sqlite
from openhands.automation.filter_eval import (
    FilterEvaluationError,
    evaluate_expression,
)
from openhands.automation.models import AutomationRun, AutomationRunStatus
from openhands.automation.providers import get_provider
from openhands.automation.schemas import EventTrigger
from openhands.automation.subjects import EventSubject, conversation_id_for
from openhands.automation.utils import utcnow
from openhands.automation.utils.conversation_turn import (
    compose_turn,
    send_conversation_turn,
)


logger = logging.getLogger("automation.conversations")

CONTINUE_CONVERSATION: Final[str] = "continue_conversation"

_FINISHED = (
    AutomationRunStatus.COMPLETED,
    AutomationRunStatus.FAILED,
    AutomationRunStatus.CANCELLED,
    AutomationRunStatus.SKIPPED,
)

# Matches AutomationRun.subject_key. Truncating would merge two subjects.
MAX_SUBJECT_KEY_LENGTH: Final[int] = 500

# Where turns for a run that has not started yet are parked, on that run's
# `event_payload`. Underscored and lifted back out by the dispatcher, so the
# provider's own payload reaches the script exactly as it arrived.
COALESCED_TURNS_KEY: Final[str] = "_automation_follow_up_turns"

# A burst is a handful of messages, not a mailbox. The cap bounds one JSON
# column against a subject that keeps producing events while its run queues.
MAX_COALESCED_TURNS: Final[int] = 20


@dataclass(frozen=True, slots=True)
class ContinueResult:
    """What became of an event routed at a subject's conversation."""

    # The conversation the event reached. None means nothing holds this
    # subject yet, and the caller has to create the run that opens it.
    conversation_id: str | None = None
    # True when the turn was folded into a run that has not started, rather
    # than delivered to a live agent server. Reporting only.
    coalesced: bool = False

    @property
    def needs_run(self) -> bool:
        return self.conversation_id is None


def event_subject(source: str, payload: dict[str, Any]) -> EventSubject | None:
    """The subject a provider derives from a payload, if it has an extractor."""
    provider = get_provider(source)
    if provider is None or provider.subject is None:
        return None
    try:
        return provider.subject(payload)
    except Exception as exc:
        logger.warning("Subject extractor for %s failed: %s", source, exc)
        return None


def resolve_subject_key(
    trigger: EventTrigger,
    payload: dict[str, Any],
    subject: EventSubject | None,
) -> str | None:
    """The key this event's siblings are grouped by, or None for a plain run.

    A trigger's `subject_key_expr` wins over the provider's extractor.
    """
    if trigger.subject_key_expr:
        return _key_from_expression(trigger.subject_key_expr, payload)
    return _clean_key(subject, "provider") if subject is not None else None


def resolve_turn_text(
    trigger: EventTrigger,
    payload: dict[str, Any],
) -> str | None:
    """The trigger's own rendering of this event, if it declares one.

    None means fall back to `compose_turn`'s built-in rendering. Evaluated
    against the raw payload, like `filter` and `subject_key_expr`.
    """
    if not trigger.turn_text_expr:
        return None
    try:
        value = evaluate_expression(trigger.turn_text_expr, payload)
    except FilterEvaluationError as exc:
        logger.warning("turn_text_expr %r failed: %s", trigger.turn_text_expr, exc)
        return None
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        # `true` is what a comparison expression yields; sending "True" as the
        # whole turn is worse than falling back to the built-in rendering.
        logger.warning(
            "turn_text_expr %r yielded a non-scalar (%s); ignoring it",
            trigger.turn_text_expr,
            type(value).__name__,
        )
        return None
    return str(value).strip() or None


def _key_from_expression(expression: str, payload: dict[str, Any]) -> str | None:
    try:
        value = evaluate_expression(expression, payload)
    except FilterEvaluationError as exc:
        logger.warning("subject_key_expr %r failed: %s", expression, exc)
        return None

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        # A dict is not an identity; `true` would collapse every event onto one.
        logger.warning(
            "subject_key_expr %r yielded a non-scalar (%s); ignoring it",
            expression,
            type(value).__name__,
        )
        return None

    return _clean_key(str(value), f"subject_key_expr {expression!r}")


def _clean_key(value: str, origin: str) -> str | None:
    """A key the column can actually hold, or None.

    `AutomationRun.subject_key` is String(500): an over-long key is a failed
    INSERT that rolls back the whole delivery, and truncating it would merge
    two subjects instead. Provider extractors go through here too -- they read
    a payload we do not control.
    """
    key = value.strip()
    if not key or len(key) > MAX_SUBJECT_KEY_LENGTH:
        logger.warning("%s yielded an unusable key of length %d", origin, len(key))
        return None
    return key


async def _take_subject_lock(
    session: AsyncSession,
    automation_id: uuid.UUID,
    subject_key: str,
) -> None:
    """Serialise every event for one subject, including those finding no run.

    `SELECT ... FOR UPDATE` only locks rows it returns, so it cannot order two
    events that both find nothing -- the burst that leaves one subject with
    two runs, and so two sandboxes claiming one derived conversation id.

    Transaction-scoped: the caller's commit releases it, so it also covers the
    run this event goes on to create. Taken in automation order, which is why
    `get_event_automations` orders by id -- otherwise two events on one
    subject deadlock. SQLite runs single-process and skips it.
    """
    if using_sqlite():
        return
    # Hashed here, not with `hashtextextended`, so the key does not depend on
    # a server-side hash staying stable across versions.
    digest = hashlib.blake2b(
        f"{automation_id}/{subject_key}".encode(), digest_size=8
    ).digest()
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)").bindparams(
            key=int.from_bytes(digest, "big", signed=True)
        )
    )


async def _lock_subject_run(
    session: AsyncSession,
    automation_id: uuid.UUID,
    subject_key: str,
) -> AutomationRun | None:
    """The most recent run holding this subject, started or not, locked.

    A run that has not started is included on purpose: it already owns the
    subject and will open the derived conversation, so a second run here
    would be the duplicate. A released run is excluded: it keeps its
    `subject_key` for the record but no longer routes.

    Eager-loads the automation because minting a cloud API key reads
    `run.automation`, and a lazy load raises MissingGreenlet -- which
    `send_conversation_turn` swallows into a silent fallback.
    """
    stmt = (
        select(AutomationRun)
        .where(
            AutomationRun.automation_id == automation_id,
            AutomationRun.subject_key == subject_key,
            AutomationRun.subject_released_at.is_(None),
        )
        .order_by(AutomationRun.created_at.desc())
        .limit(1)
        .options(selectinload(AutomationRun.automation))
    )
    if not using_sqlite():
        stmt = stmt.with_for_update(of=AutomationRun)
    result = await session.execute(stmt)
    return result.scalars().first()


def _coalesce_turn(run: AutomationRun, turn: str) -> None:
    """Park a turn on a run that has not been dispatched yet.

    Reassigns `event_payload` instead of mutating it in place: the column is a
    plain JSON type, so SQLAlchemy only notices a whole-value set and an
    in-place edit would be dropped on commit.
    """
    payload = dict(run.event_payload or {})
    turns = list(payload.get(COALESCED_TURNS_KEY) or [])
    if len(turns) >= MAX_COALESCED_TURNS:
        logger.warning(
            "Run %s already carries %d follow-up turns; dropping this one",
            run.id,
            len(turns),
        )
        return
    turns.append(turn)
    payload[COALESCED_TURNS_KEY] = turns
    run.event_payload = payload


async def continue_conversation(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    source: str,
    subject_key: str,
    automation_id: uuid.UUID,
    event_key: str,
    event_payload: dict[str, Any] | None,
    turn_text: str | None = None,
    wake_agent: bool = True,
) -> ContinueResult:
    """Deliver this event as another turn on the subject's conversation.

    Returns the conversation id when the turn reached it -- posted to a live
    agent server, or folded into the run that is about to open it. A result
    with `needs_run` means the caller should create the run: nothing holds
    this subject yet, or its sandbox is gone.

    Holds the subject's lock until the caller commits, so the run it may go on
    to create is covered by the same lock that decided one was needed.

    The id is known before the first run finishes, so an event arriving
    mid-run continues that conversation instead of racing a second run.
    """
    await _take_subject_lock(session, automation_id, subject_key)

    run = await _lock_subject_run(session, automation_id, subject_key)
    if run is None:
        return ContinueResult()

    conversation_id = conversation_id_for(org_id, automation_id, source, subject_key)
    turn = compose_turn(source, event_key, event_payload, override=turn_text)

    if run.started_at is None:
        # No agent server yet, but this run opens the same derived
        # conversation and has not read its payload, so the turn rides along.
        _coalesce_turn(run, turn)
        logger.info(
            "Folded a turn into queued run %s for conversation %s",
            run.id,
            conversation_id,
        )
        return ContinueResult(conversation_id=conversation_id, coalesced=True)

    delivered = await send_conversation_turn(
        run, conversation_id, turn, wake_agent=wake_agent
    )
    if not delivered:
        # Only release a finished run: one still going may not have recorded
        # its sandbox yet, and releasing it would orphan the subject.
        if run.status in _FINISHED:
            run.subject_released_at = utcnow()
        return ContinueResult()

    return ContinueResult(conversation_id=conversation_id)
