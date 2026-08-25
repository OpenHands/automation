"""Subject keys: the identity of the external thing an event is about.

A leaf module, so `providers`, `streams.slack` and `ingest` can all import
`EventSubject` without a cycle.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class EventSubject:
    """The external thing an event is about."""

    key: str


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
