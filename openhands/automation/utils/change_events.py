"""Audit trail of changes to an automation."""

from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.auth import AuthenticatedUser
from openhands.automation.models import (
    Automation,
    AutomationChangeActorKind,
    AutomationChangeEvent,
)
from openhands.automation.utils.owner import SERVICE_ACCOUNT_ROLE


def record_change(
    session: AsyncSession,
    automation: Automation,
    actor: AuthenticatedUser | None,
    action: str,
    fields: Iterable[str],
) -> None:
    """Add an audit row to ``session``; the caller's commit writes it.

    Call it in the transaction that makes the change, so a rolled-back change
    leaves no row. ``actor`` is the authenticated caller, or ``None`` for Git
    sync. ``fields`` are field NAMES only: never pass values.

    Lives beside ``utils/run.py`` rather than in it because Git sync imports
    this module, and ``utils/run.py`` imports Git sync.
    """
    if actor is None:
        actor_kind = AutomationChangeActorKind.GIT_SYNC
    elif actor.role == SERVICE_ACCOUNT_ROLE:
        actor_kind = AutomationChangeActorKind.SERVICE
    else:
        actor_kind = AutomationChangeActorKind.USER
    session.add(
        AutomationChangeEvent(
            automation_id=automation.id,
            actor_user_id=actor.user_id if actor is not None else None,
            actor_kind=actor_kind,
            action=action,
            changed_fields=sorted(set(fields)),
        )
    )
