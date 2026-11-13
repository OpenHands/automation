"""The rule mapping an external subject to a conversation id."""

import uuid
from typing import Final

from openhands.automation.models import Automation, AutomationRunAsKind


# Pinned permanently: changing it makes every live thread lose its memory.
CONVERSATION_NAMESPACE: Final[uuid.UUID] = uuid.UUID(
    "d7f3a2b1-5c48-4e9a-9b6d-2f1e8c3a7d40"
)


def conversation_id_for(
    org_id: uuid.UUID,
    automation_id: uuid.UUID,
    source: str,
    subject_key: str,
    service_account_id: uuid.UUID | None,
) -> str:
    """The conversation a subject's events belong to.

    Keyed on `automation_id` too, so editing an automation re-keys its threads
    -- attaching with a different agent kind raises on the server.

    `service_account_id` is the service account an automation runs as (see
    `service_account_id_of`), else None. It is part of the key because
    enterprise rejects a webhook for a conversation created by another user:
    after a change to a service account, new runs need new conversations. An
    automation that runs as a person has no account in its key, so its ids do
    not change, and a change back to a person restores them.
    """
    account = f"service-account:{service_account_id}/" if service_account_id else ""
    return str(
        uuid.uuid5(
            CONVERSATION_NAMESPACE,
            f"{org_id}/{automation_id}/{account}{source}/{subject_key}",
        )
    )


def service_account_id_of(automation: Automation) -> uuid.UUID | None:
    """The `service_account_id` argument of `conversation_id_for` for ``automation``."""
    if automation.run_as_kind == AutomationRunAsKind.SERVICE:
        return automation.user_id
    return None
