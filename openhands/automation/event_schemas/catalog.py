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


def pattern_can_match(source: str, pattern: str) -> bool | None:
    """Whether a trigger's ``on`` pattern can match an event this source delivers.

    None when the source has no catalog. GitHub's catalog holds event types
    (``pull_request``) while its keys add the action (``pull_request.opened``),
    so only the part before the first dot is compared; a keyed source's whole
    key is its type, dots included (``git.pullrequest.created``), and the
    pattern is compared the way the trigger matcher compares a delivered key.
    """
    supported = supported_event_types(source)
    if supported is None:
        return None
    if source == "github":
        pattern = pattern.split(".", 1)[0]
    return any(fnmatch.fnmatch(event_type, pattern) for event_type in supported)
