"""The rule mapping an external subject to a conversation id."""

import uuid
from typing import Final


# Pinned permanently: changing it makes every live thread lose its memory.
CONVERSATION_NAMESPACE: Final[uuid.UUID] = uuid.UUID(
    "d7f3a2b1-5c48-4e9a-9b6d-2f1e8c3a7d40"
)


def conversation_id_for(
    org_id: uuid.UUID,
    automation_id: uuid.UUID,
    source: str,
    subject_key: str,
) -> str:
    """The conversation a subject's events belong to.

    Keyed on `automation_id` too, so editing an automation re-keys its threads
    -- attaching with a different agent kind raises on the server.
    """
    return str(
        uuid.uuid5(
            CONVERSATION_NAMESPACE,
            f"{org_id}/{automation_id}/{source}/{subject_key}",
        )
    )
