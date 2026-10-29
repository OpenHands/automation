"""Audit trail of changes to an automation.

Every ORM insert, update and soft delete of an ``Automation`` writes one
``AutomationChangeEvent`` per transaction, through the ``before_flush`` hook
registered by ``register_audit_hook``. A transaction that flushes several times
keeps one row: later flushes add their fields to it. Call sites do not record
changes; they only say who acts, with ``set_audit_actor`` on the session, or,
for an API request, through ``bind_request`` (see ``db.get_session``).

The hook sees only ORM writes. A Core ``update(Automation)`` statement is
invisible to it, so the two that change audited columns (the auto-disables in
``utils/run.py`` and ``utils/unhealthy.py``) call ``record_core_disable``.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from openhands.automation.auth import AuthenticatedUser
from openhands.automation.models import (
    Automation,
    AutomationChangeActorKind,
    AutomationChangeEvent,
)
from openhands.automation.utils.owner import SERVICE_ACCOUNT_ROLE


logger = logging.getLogger(__name__)

# Tests turn this on, so a write path that names no actor fails instead of
# being recorded as SYSTEM.
STRICT = False

_ACTOR_KEY = "audit_actor"
_ACTION_KEY = "audit_action"
_REQUEST_KEY = "audit_request"
# automation id -> (row, base action) written earlier in this transaction.
_PENDING_KEY = "audit_pending"

# When one transaction flushes several times, its row keeps the strongest action.
_ACTION_RANK = {"update": 0, "enable": 1, "disable": 1, "transfer": 2, "delete": 3}

# Columns the service updates for its own bookkeeping; a change to only these
# is not a change anyone made to the automation.
_UNAUDITED_COLUMNS = frozenset(
    {"updated_at", "last_polled_at", "last_triggered_at", "created_at"}
)
_OWNER_COLUMNS = frozenset({"user_id", "owner_kind"})
_LIFECYCLE_COLUMNS = frozenset(
    {"enabled", "state", "disabled_reason", "disabled_detail", "disabled_at"}
)


class MissingAuditActorError(RuntimeError):
    """A flush changed an automation, but no actor was set (strict mode only)."""


@dataclass(frozen=True)
class AuditActor:
    """Who makes the changes a session flushes. ``user_id`` is None for
    Git sync and the service itself."""

    kind: AutomationChangeActorKind
    user_id: uuid.UUID | None = None

    @classmethod
    def from_user(cls, user: AuthenticatedUser) -> "AuditActor":
        kind = (
            AutomationChangeActorKind.SERVICE
            if user.role == SERVICE_ACCOUNT_ROLE
            else AutomationChangeActorKind.USER
        )
        return cls(kind, user.user_id)


SYSTEM_ACTOR = AuditActor(AutomationChangeActorKind.SYSTEM)
GIT_SYNC_ACTOR = AuditActor(AutomationChangeActorKind.GIT_SYNC)


def set_audit_actor(
    session: AsyncSession | Session, actor: AuditActor, action: str | None = None
) -> None:
    """Name the actor of every automation change ``session`` flushes from now on.

    ``action`` replaces the action derived from the changed columns, for a
    change the columns alone do not name.
    """
    info = session.info
    info[_ACTOR_KEY] = actor
    if action is None:
        info.pop(_ACTION_KEY, None)
    else:
        info[_ACTION_KEY] = action


def bind_request(session: AsyncSession | Session, request: Any) -> None:
    """Take the actor of ``session``'s flushes from the request's authenticated
    user, read at flush time (authentication may run after the session opens).
    """
    session.info[_REQUEST_KEY] = request


def _resolve_actor(session: Session) -> AuditActor:
    actor = session.info.get(_ACTOR_KEY)
    if actor is not None:
        return actor
    request = session.info.get(_REQUEST_KEY)
    user = getattr(getattr(request, "state", None), "authenticated_user", None)
    if user is not None:
        return AuditActor.from_user(user)
    if STRICT:
        raise MissingAuditActorError("An automation changed without an audit actor")
    logger.error(
        "An automation changed without an audit actor; recording it as system",
        stack_info=True,
    )
    return SYSTEM_ACTOR


def _changed_columns(automation: Automation, *, is_new: bool) -> list[str]:
    state = inspect(automation)
    columns = []
    for attr in state.mapper.column_attrs:
        name = attr.key
        if name == "id" or name in _UNAUDITED_COLUMNS:
            continue
        if is_new:
            if getattr(automation, name) is not None:
                columns.append(name)
            continue
        history = state.attrs[name].history
        # Setting a loaded column to its current value is not a change. An
        # unloaded column has no old value here, so it counts as changed.
        if history.has_changes() and list(history.added) != list(history.deleted):
            columns.append(name)
    return sorted(columns)


def _derive_action(changed: set[str], *, is_new: bool, enabled: bool) -> str:
    if is_new:
        return "create"
    if "deleted_at" in changed:
        return "delete"
    if changed & _OWNER_COLUMNS:
        return "transfer"
    if "enabled" in changed and changed <= _LIFECYCLE_COLUMNS:
        return "enable" if enabled else "disable"
    return "update"


def _audit_row(
    automation_id: uuid.UUID,
    org_id: uuid.UUID,
    actor: AuditActor,
    action: str,
    fields: list[str],
) -> AutomationChangeEvent:
    return AutomationChangeEvent(
        automation_id=automation_id,
        org_id=org_id,
        actor_user_id=actor.user_id,
        actor_kind=actor.kind,
        action=action,
        changed_fields=fields,
    )


def _before_flush(session: Session, _flush_context: Any, _instances: Any) -> None:
    changes = [(obj, True) for obj in session.new if isinstance(obj, Automation)]
    changes += [
        (obj, False)
        for obj in session.dirty
        if isinstance(obj, Automation) and session.is_modified(obj)
    ]
    pending = session.info.setdefault(_PENDING_KEY, {})
    label = session.info.get(_ACTION_KEY)
    for automation, is_new in changes:
        fields = _changed_columns(automation, is_new=is_new)
        if not fields:
            continue
        if is_new and automation.id is None:
            # The column default runs at INSERT time, after this hook; the audit
            # row needs the id now. Same generator, called earlier.
            automation.id = uuid.uuid4()
        actor = _resolve_actor(session)
        base = label or _derive_action(
            set(fields), is_new=is_new, enabled=automation.enabled
        )
        earlier = pending.get(automation.id)
        if earlier is not None:
            # A handler may flush more than once (autoflush before a query, or
            # an explicit flush). Fold this flush into the transaction's row.
            row, earlier_base = earlier
            base = _stronger(earlier_base, base)
            row.changed_fields = sorted(set(row.changed_fields) | set(fields))
        else:
            row = _audit_row(automation.id, automation.org_id, actor, base, fields)
            session.add(row)
        row.action = (
            f"git_sync_{base}"
            if actor.kind == AutomationChangeActorKind.GIT_SYNC and label is None
            else base
        )
        pending[automation.id] = (row, base)


def _stronger(earlier: str, later: str) -> str:
    if earlier == "create":
        return earlier
    if _ACTION_RANK.get(later, 0) >= _ACTION_RANK.get(earlier, 0):
        return later
    return earlier


def _end_transaction(session: Session, *_args: Any) -> None:
    session.info.pop(_PENDING_KEY, None)


def record_core_disable(session: AsyncSession, automation: Automation) -> None:
    """Record a disable that a Core ``update(Automation)`` made, which the hook
    cannot see. Call it only when the statement changed a row."""
    session.add(
        _audit_row(
            automation.id,
            automation.org_id,
            SYSTEM_ACTOR,
            "disable",
            sorted(_LIFECYCLE_COLUMNS),
        )
    )


def register_audit_hook() -> None:
    """Attach the hook to every ORM session. Idempotent."""
    if event.contains(Session, "before_flush", _before_flush):
        return
    event.listen(Session, "before_flush", _before_flush)
    for name in ("after_commit", "after_rollback"):
        event.listen(Session, name, _end_transaction)
