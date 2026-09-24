"""Lookup for automations created from an extension catalog template.

Every creation path stores provenance under ``preset_metadata["template"]``, so
the lookup that makes creation idempotent belongs to none of them in particular.
"""

import uuid
from typing import Any

from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.db import using_sqlite
from openhands.automation.models import Automation
from openhands.automation.schemas import AutomationResponse


# Documents the 200 a creation path returns when the template is already
# enabled, alongside its 201, in the OpenAPI schema.
TEMPLATE_EXISTS_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {
        "model": AutomationResponse,
        "description": (
            "An automation created from this template already exists in this "
            "organization; it is returned unchanged."
        ),
    },
}


async def find_existing_template_automation(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: str,
) -> Automation | None:
    """Find the org's live automation created from the given template.

    JSON extraction mirrors ``get_event_automations``: ``json_extract`` on
    SQLite, ``->``/``->>`` on PostgreSQL. Rows without provenance yield NULL and
    are excluded on both.

    Two concurrent creates can both miss the existing row, since no
    cross-database unique index on a JSON path is available; the earliest-created
    row wins subsequent lookups.
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
            Automation.org_id == org_id,
            Automation.deleted_at.is_(None),
            template_filter,
        )
        .order_by(Automation.created_at.asc())
        .limit(1)
    )
    return result.scalars().first()
