"""Slug rules for factories.

A factory slug is the identifier in URLs. It is lowercase and URL-safe, derived
from the name when the caller omits it, and never changes after creation.
"""

from __future__ import annotations

import re
from typing import Final


MAX_FACTORY_SLUG_LENGTH: Final[int] = 63
_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_NON_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")


def slugify_factory_name(name: str) -> str:
    """Derive a slug from a name: lowercase, with runs of other characters
    collapsed to one hyphen. Raises ``ValueError`` when nothing usable is left.
    """
    slug = _NON_SLUG_RE.sub("-", name.strip().lower()).strip("-")
    slug = slug[:MAX_FACTORY_SLUG_LENGTH].rstrip("-")
    if not slug:
        raise ValueError(
            "The name has no letters or digits to derive a slug from; provide a slug"
        )
    return slug


def normalize_factory_slug(slug: str) -> str:
    """Validate a caller-supplied slug. Case and surrounding space are
    normalized; anything else that is not a slug is rejected."""
    normalized = slug.strip().lower()
    if len(normalized) > MAX_FACTORY_SLUG_LENGTH:
        raise ValueError(f"slug must be at most {MAX_FACTORY_SLUG_LENGTH} characters")
    if not _SLUG_RE.fullmatch(normalized):
        raise ValueError(
            "slug may only contain lowercase letters, numbers, and single "
            "hyphens between them"
        )
    return normalized
