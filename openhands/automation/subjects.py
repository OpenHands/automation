"""What an event is about, and how each provider names it.

A *subject key* is the stable identity of the external thing an event concerns:
a Slack thread, a GitHub pull request. `ExternalConversation` maps that key to a
conversation, so a follow-up event can be routed back to where the first one
went instead of starting a conversation with no memory of it.

A leaf module on purpose. `providers`, `streams.slack` and `ingest` all need
`EventSubject`, and every one of them already sits on an import path to the
others; keeping the type and its extractors here is what stops that from
becoming a cycle.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class EventSubject:
    """The external thing an event is about."""

    key: str


def slack_subject(envelope: dict[str, Any]) -> EventSubject | None:
    """`team/channel/thread_ts` for a Slack event envelope.

    Reads the envelope Socket Mode and the Events API both deliver: `team_id`
    at the top level, the event itself nested under `event`.

    A message that is not yet in a thread carries no `thread_ts`, and its own
    `ts` is what becomes the thread id the moment somebody replies. Falling
    back to `ts` is therefore not a guess -- it is what puts the opening
    mention and its replies under one key.
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
    """`owner/repo#number` for a GitHub event about one issue or pull request.

    A pull request is an issue as far as GitHub's numbering goes, so both read
    to the same key: a review comment on PR 12 and an `issue_comment` on PR 12
    are the same subject, which is the point.

    Events that are not about a numbered thing -- `push`, `create` -- have no
    subject. That is a real answer, not a failure: there is no conversation to
    continue for a branch.
    """
    repository = payload.get("repository")
    repo = repository.get("full_name") if isinstance(repository, dict) else None
    if not repo:
        return None

    for field in ("pull_request", "issue"):
        node = payload.get(field)
        if isinstance(node, dict) and node.get("number") is not None:
            return EventSubject(key=f"{repo}#{node['number']}")

    # `pull_request.number` is absent on some payloads that still carry the
    # number at the top level (a bare `number` accompanies the object on
    # several action variants).
    number = payload.get("number")
    if number is not None:
        return EventSubject(key=f"{repo}#{number}")
    return None
