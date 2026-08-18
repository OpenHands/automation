"""Lookup for automations created from an extension catalog template.

Template provenance is stored under ``preset_metadata["template"]`` by every
creation path — both preset endpoints and the raw ``POST /v1`` a catalog entry
shipping its own tarball uses — so the lookup that makes creation idempotent
lives here rather than in any one router.
"""

import uuid
from typing import Any

from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.db import using_sqlite
from openhands.automation.models import Automation
from openhands.automation.schemas import AutomationResponse


# The 200 every creation path returns when the template is already enabled,
# documented alongside its 201 in the OpenAPI schema.
TEMPLATE_EXISTS_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {
        "model": AutomationResponse,
        "description": (
            "An automation created from this template already exists for this "
            "user; it is returned unchanged."
        ),
    },
}


async def find_existing_template_automation(
    session: AsyncSession,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    template_id: str,
) -> Automation | None:
    """Find the caller's live automation created from the given template.

    Cross-database JSON extraction mirrors ``get_event_automations``: SQLite
    uses ``json_extract``, PostgreSQL uses the ``->`` / ``->>`` operators. Rows
    without template provenance yield NULL and are excluded on both databases.

    Two concurrent creates can both miss the existing row (there is no
    cross-database unique index on a JSON path); the earliest-created row wins
    subsequent lookups.
    """
    if using_sqlite():
        template_filter = func.json_extract(
            Automation.preset_metadata, "$.template.id"
        ) == literal(template_id)
    else:
        template_filter = Automation.preset_metadata.op("->")("template").op("->>")(
            "id"
        ) == literal(template_id)

    result = await session.execute(
        select(Automation)
        .where(
            Automation.user_id == user_id,
            Automation.org_id == org_id,
            Automation.deleted_at.is_(None),
            template_filter,
        )
        .order_by(Automation.created_at.asc())
        .limit(1)
    )
    return result.scalars().first()
