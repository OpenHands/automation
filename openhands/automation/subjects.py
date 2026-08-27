"""Subject keys: the identity of the external thing an event is about.

A leaf module, so `providers`, `streams.slack` and `ingest` can all import
`EventSubject` without a cycle.

Also holds the rule that turns a subject into a conversation id. The id is
derived, never stored: the agent server accepts a caller-supplied
`conversation_id` and attaches to an existing conversation rather than
erroring, so a documented function of the subject is all the correspondence
this service needs.
"""

import uuid
from dataclasses import dataclass
from typing import Any


# Pinned permanently. Changing it re-keys every live thread at once, which
# reads to users as every conversation losing its memory on deploy.
CONVERSATION_NAMESPACE = uuid.UUID("d7f3a2b1-5c48-4e9a-9b6d-2f1e8c3a7d40")


@dataclass(frozen=True, slots=True)
class EventSubject:
    """The external thing an event is about."""

    key: str


def conversation_id_for(
    org_id: uuid.UUID,
    automation_id: uuid.UUID,
    source: str,
    subject_key: str,
) -> str:
    """The conversation a subject's events belong to.

    `automation_id` is part of the key so that editing an automation re-keys
    its threads. Without it a thread stays pinned to whatever agent the first
    event saw, and attaching with a different agent kind raises on the server.
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
    return EventSubject(key=f"{team}/{channel}/{thread}")


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
            return EventSubject(key=f"{repo}#{node['number']}")

    number = payload.get("number")
    if number is not None:
        return EventSubject(key=f"{repo}#{number}")
    return None
