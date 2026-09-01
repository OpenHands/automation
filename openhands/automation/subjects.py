"""Subject keys, and the rule mapping one to a conversation id.

A leaf module so `providers`, `streams.slack` and `ingest` can all import it
without a cycle.
"""

import uuid
from typing import Any, Final


# Pinned permanently: changing it makes every live thread lose its memory.
CONVERSATION_NAMESPACE: Final[uuid.UUID] = uuid.UUID(
    "d7f3a2b1-5c48-4e9a-9b6d-2f1e8c3a7d40"
)

# The external thing an event is about, named by its key. Every other layer
# already passes the key as a bare string, so there is nothing to wrap.
type EventSubject = str


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


def slack_subject(envelope: dict[str, Any]) -> EventSubject | None:
    """`team/channel/thread_ts` for a Slack event envelope.

    Applied by the Socket Mode transport. Slack arriving as a custom webhook
    has no provider descriptor, so such a trigger needs `subject_key_expr`.

    Falls back to `ts`: the mention that opens a thread carries no `thread_ts`,
    and its own `ts` becomes the thread id once someone replies.
    """
    team = envelope.get("team_id")
    event = envelope.get("event")
    if not isinstance(event, dict):
        return None
    channel = event.get("channel")
    thread = event.get("thread_ts") or event.get("ts")
    if not team or not channel or not thread:
        return None
    return f"{team}/{channel}/{thread}"


def github_subject(payload: dict[str, Any]) -> EventSubject | None:
    """`owner/repo#number` for an event about an issue or pull request.

    Both read to the same key, since GitHub numbers them from one sequence.
    Events about nothing numbered (`push`) have no subject.
    """
    repository = payload.get("repository")
    repo = repository.get("full_name") if isinstance(repository, dict) else None
    if not repo:
        return None

    for field in ("pull_request", "issue"):
        node = payload.get(field)
        if isinstance(node, dict) and node.get("number") is not None:
            return f"{repo}#{node['number']}"

    number = payload.get("number")
    if number is not None:
        return f"{repo}#{number}"
    return None
