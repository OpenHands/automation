"""Normalization rules for automation tags.

A tag is a lowercase slug (``software-factory``), so a filter is an exact match
and "Software Factory" and "software factory" cannot become two tags.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final


MAX_TAGS: Final[int] = 10
MAX_TAG_LENGTH: Final[int] = 50
_TAG_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


def normalize_tag(tag: str) -> str:
    """Trim, lowercase, and hyphenate whitespace; reject anything not a slug."""
    if not isinstance(tag, str):
        raise ValueError("tags must be strings")
    normalized = _WHITESPACE_RE.sub("-", tag.strip().lower())
    if not normalized:
        raise ValueError("tags must not be empty")
    if len(normalized) > MAX_TAG_LENGTH:
        raise ValueError(f"tags must be at most {MAX_TAG_LENGTH} characters")
    if not _TAG_RE.fullmatch(normalized):
        raise ValueError(
            "tags may only contain lowercase letters, numbers, and single hyphens "
            "between them"
        )
    return normalized


def normalize_tags(tags: Iterable[str]) -> list[str]:
    """Normalize each tag and drop duplicates, keeping first-seen order."""
    normalized = list(dict.fromkeys(normalize_tag(tag) for tag in tags))
    if len(normalized) > MAX_TAGS:
        raise ValueError(f"an automation supports at most {MAX_TAGS} tags")
    return normalized
