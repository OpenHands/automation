"""High-cardinality observability associations for automation traces."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any, Final

import jmespath
from jmespath import exceptions as jmespath_exceptions


logger = logging.getLogger("automation.observability_associations")

MAX_ASSOCIATIONS: Final[int] = 20
MAX_ASSOCIATION_KEY_LENGTH: Final[int] = 128
MAX_ASSOCIATION_EXPR_LENGTH: Final[int] = 1000
MAX_ASSOCIATION_STRING_VALUE_LENGTH: Final[int] = 1000
_ASSOCIATION_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.-]+$")
ScalarAssociationValue = str | bool | int | float


def validate_observability_associations(
    associations: Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Validate an association key -> JMESPath expression mapping."""
    if associations is None:
        return None
    if not isinstance(associations, Mapping):
        raise ValueError("observability_associations must be an object")
    if len(associations) > MAX_ASSOCIATIONS:
        raise ValueError(
            f"observability_associations supports at most {MAX_ASSOCIATIONS} entries"
        )

    validated: dict[str, str] = {}
    for key, expr in associations.items():
        if not isinstance(key, str) or not key:
            raise ValueError("observability association keys must be non-empty strings")
        if len(key) > MAX_ASSOCIATION_KEY_LENGTH:
            raise ValueError(
                "observability association keys must be at most "
                f"{MAX_ASSOCIATION_KEY_LENGTH} characters"
            )
        if not _ASSOCIATION_KEY_RE.fullmatch(key):
            raise ValueError(
                "observability association keys may only contain letters, numbers, "
                "underscore, hyphen, and dot"
            )
        if not isinstance(expr, str) or not expr:
            raise ValueError(
                "observability association expressions must be non-empty strings"
            )
        if len(expr) > MAX_ASSOCIATION_EXPR_LENGTH:
            raise ValueError(
                "observability association expressions must be at most "
                f"{MAX_ASSOCIATION_EXPR_LENGTH} characters"
            )
        try:
            jmespath.compile(expr)
        except jmespath_exceptions.JMESPathError as exc:
            raise ValueError(
                f"Invalid observability association JMESPath for {key}: {exc}"
            ) from exc
        validated[key] = expr
    return validated


def _clean_association_value(value: Any) -> ScalarAssociationValue | None:
    if value is None:
        return None
    if isinstance(value, str):
        if len(value) > MAX_ASSOCIATION_STRING_VALUE_LENGTH:
            return None
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value
    return None


def evaluate_observability_associations(
    associations: Mapping[str, str] | None,
    payload: Mapping[str, Any] | None,
) -> dict[str, ScalarAssociationValue]:
    """Evaluate configured associations against an event payload.

    Invalid or non-scalar outputs are ignored. Config is normally validated at
    write time, but evaluation is defensive so a malformed legacy row cannot
    prevent run creation.
    """
    if not associations or payload is None:
        return {}

    evaluated: dict[str, ScalarAssociationValue] = {}
    for key, expr in associations.items():
        try:
            value = jmespath.search(expr, payload)
        except Exception:
            logger.debug(
                "Failed to evaluate observability association %s", key, exc_info=True
            )
            continue
        clean_value = _clean_association_value(value)
        if clean_value is not None:
            evaluated[key] = clean_value
    return evaluated
