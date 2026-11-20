"""Event catalogs: the event types each built-in source can deliver.

A setup UI offers these, preflight checks a trigger's ``on`` against them, and
the OpenHands server uses the detection rules to forward only events an
automation could match. A source without a catalog (a custom webhook) accepts
any event key.
"""

import fnmatch

from openhands.automation.event_schemas import github
from openhands.automation.event_schemas.azure_devops import AZURE_DEVOPS_EVENT_TYPES
from openhands.automation.event_schemas.bitbucket_data_center import (
    BITBUCKET_DATA_CENTER_EVENT_TYPES,
)
from openhands.automation.event_schemas.jira_dc import JIRA_DC_EVENT_TYPES


# Sources whose payload names its own event type in one top-level field, so the
# type is the whole event key and needs no structural detection.
_KEYED_CATALOGS: dict[str, tuple[str, tuple[str, ...]]] = {
    "azure_devops": ("eventType", AZURE_DEVOPS_EVENT_TYPES),
    "bitbucket_data_center": ("eventKey", BITBUCKET_DATA_CENTER_EVENT_TYPES),
    "jira_dc": ("webhookEvent", JIRA_DC_EVENT_TYPES),
}

SOURCE_LABELS: dict[str, str] = {
    "azure_devops": "Azure DevOps",
    "bitbucket_data_center": "Bitbucket Data Center",
    "github": "GitHub",
    "jira_dc": "Jira Data Center",
}


def catalog_sources() -> list[str]:
    """Sources that publish an event catalog."""
    return sorted(["github", *_KEYED_CATALOGS])


def supported_event_types(source: str) -> list[str] | None:
    """Event types a source delivers, or None when it has no catalog."""
    if source == "github":
        return github.get_supported_event_types()
    keyed = _KEYED_CATALOGS.get(source)
    return list(keyed[1]) if keyed else None


def supported_event_patterns(source: str) -> list[str] | None:
    """The ``on`` patterns a setup UI can offer for a source."""
    if source == "github":
        return github.get_supported_event_patterns()
    return supported_event_types(source)


def event_detection_rules(source: str) -> list[dict[str, str]]:
    """Ordered rules detecting a payload's event type, empty without a catalog."""
    if source == "github":
        return github.get_event_detection_rules()
    keyed = _KEYED_CATALOGS.get(source)
    if keyed is None:
        return []
    field, event_types = keyed
    return [
        {"event_type": event_type, "jmespath": f"{field} == '{event_type}'"}
        for event_type in event_types
    ]


# Sources whose catalog holds event types (``pull_request``) while delivered keys
# add an action after a dot (``pull_request.opened``). A keyed source's whole
# key is its type, dots included (``git.pullrequest.created``).
_ACTION_SUFFIX_SOURCES: frozenset[str] = frozenset({"github"})


def pattern_can_match(source: str, pattern: str) -> bool | None:
    """Whether a trigger's ``on`` pattern can match an event this source delivers.

    None when the source has no catalog. The pattern is compared the way the
    trigger matcher compares a delivered key, except that an action suffix is
    dropped first for sources whose catalog types carry none.
    """
    supported = supported_event_types(source)
    if supported is None:
        return None
    if source in _ACTION_SUFFIX_SOURCES:
        pattern = pattern.split(".", 1)[0]
    return any(fnmatch.fnmatch(event_type, pattern) for event_type in supported)


_EVENT_LABELS: dict[str, dict[str, tuple[str, str]]] = {
    "github": {
        "issue_comment.*": (
            "Issue and pull request comments — all actions",
            "Comments",
        ),
        "issue_comment.created": ("Issue or pull request comment added", "Comments"),
        "issue_comment.edited": ("Issue or pull request comment edited", "Comments"),
        "issues.*": ("Issues — all actions", "Issues"),
        "issues.opened": ("Issue opened", "Issues"),
        "issues.closed": ("Issue closed", "Issues"),
        "issues.labeled": ("Issue labeled", "Issues"),
        "pull_request.*": ("Pull requests — all actions", "Pull requests"),
        "pull_request.opened": ("Pull request opened", "Pull requests"),
        "pull_request.synchronize": ("Pull request commits updated", "Pull requests"),
        "pull_request.closed": ("Pull request closed", "Pull requests"),
        "pull_request_review.*": ("Pull request reviews — all actions", "Reviews"),
        "pull_request_review.submitted": ("Pull request review submitted", "Reviews"),
        "push": ("Commits pushed", "Repositories"),
        "release.*": ("Releases — all actions", "Releases"),
        "release.published": ("Release published", "Releases"),
    },
    "jira_dc": {
        "comment_created": ("Issue comment added", "Comments"),
        "comment_deleted": ("Issue comment deleted", "Comments"),
        "comment_updated": ("Issue comment updated", "Comments"),
        "jira:issue_created": ("Issue created", "Issues"),
        "jira:issue_deleted": ("Issue deleted", "Issues"),
        "jira:issue_updated": ("Issue updated", "Issues"),
    },
    "azure_devops": {
        "git.pullrequest.created": ("Pull request created", "Pull requests"),
        "git.pullrequest.merged": ("Pull request merge attempted", "Pull requests"),
        "git.pullrequest.updated": ("Pull request updated", "Pull requests"),
        "ms.vss-code.git-pullrequest-comment-event": (
            "Pull request comment changed",
            "Comments",
        ),
        "workitem.commented": ("Work item comment added", "Work items"),
    },
    "bitbucket_data_center": {
        "pr:comment:added": ("Pull request comment added", "Comments"),
        "pr:comment:deleted": ("Pull request comment deleted", "Comments"),
        "pr:comment:edited": ("Pull request comment edited", "Comments"),
        "pr:declined": ("Pull request declined", "Pull requests"),
        "pr:deleted": ("Pull request deleted", "Pull requests"),
        "pr:from_ref_updated": ("Pull request source branch updated", "Pull requests"),
        "pr:merged": ("Pull request merged", "Pull requests"),
        "pr:modified": ("Pull request modified", "Pull requests"),
        "pr:opened": ("Pull request opened", "Pull requests"),
        "pr:reviewer:approved": ("Pull request approved", "Reviews"),
        "pr:reviewer:needs_work": ("Pull request changes requested", "Reviews"),
        "pr:reviewer:unapproved": ("Pull request approval removed", "Reviews"),
        "repo:comment:added": ("Commit comment added", "Comments"),
        "repo:comment:deleted": ("Commit comment deleted", "Comments"),
        "repo:comment:edited": ("Commit comment edited", "Comments"),
        "repo:refs_changed": ("Repository branches or tags changed", "Repositories"),
    },
}


def event_options(source: str) -> list[dict[str, str]] | None:
    """Display metadata for known keys; it never changes matching or delivery."""
    patterns = supported_event_patterns(source)
    if patterns is None:
        return None
    labels = _EVENT_LABELS.get(source, {})
    keys = dict.fromkeys([*patterns, *labels])
    return [
        {
            "key": key,
            "label": labels.get(key, (key, SOURCE_LABELS[source]))[0],
            "group": labels.get(key, (key, SOURCE_LABELS[source]))[1],
            "kind": "pattern" if any(char in key for char in "*?[") else "event",
        }
        for key in sorted(keys, key=lambda key: any(char in key for char in "*?["))
        if pattern_can_match(source, key)
    ]
