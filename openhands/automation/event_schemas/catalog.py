"""Event catalogs: the event types each built-in source can deliver.

A setup UI offers these, preflight checks a trigger's ``on`` against them, and
the OpenHands server uses the detection rules to forward only events an
automation could match. A source without a catalog (Azure DevOps, a custom
webhook) accepts any event key.
"""

from openhands.automation.event_schemas import github
from openhands.automation.event_schemas.bitbucket_data_center import (
    BITBUCKET_DATA_CENTER_EVENT_TYPES,
)
from openhands.automation.event_schemas.jira_dc import JIRA_DC_EVENT_TYPES


# Sources whose payload names its own event type in one top-level field, so the
# type is the whole event key and needs no structural detection.
_KEYED_CATALOGS: dict[str, tuple[str, tuple[str, ...]]] = {
    "bitbucket_data_center": ("eventKey", BITBUCKET_DATA_CENTER_EVENT_TYPES),
    "jira_dc": ("webhookEvent", JIRA_DC_EVENT_TYPES),
}

SOURCE_LABELS: dict[str, str] = {
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
