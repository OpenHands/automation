"""Tag queries over ``automations.tags``, a JSON array of normalized tags.

JSON expansion mirrors ``find_existing_template_automation``: ``json_each`` on
SQLite, the ``json``/``jsonb`` operators on PostgreSQL. The column is NOT NULL
with a ``[]`` default, so expanding it never meets a JSON null.
"""

import uuid

from sqlalchemy import ColumnElement, cast, exists, func, literal, select, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.db import using_sqlite
from openhands.automation.models import Automation


def has_tag(tag: str) -> ColumnElement[bool]:
    """Whether an automation carries ``tag`` (already normalized)."""
    if using_sqlite():
        each = func.json_each(Automation.tags).table_valued("value")
        return exists(select(literal(1)).select_from(each).where(each.c.value == tag))
    return cast(Automation.tags, JSONB).contains(func.jsonb_build_array(tag))


async def count_org_tags(
    session: AsyncSession, org_id: uuid.UUID
) -> list[tuple[str, int]]:
    """Each tag on the org's live automations with how many carry it.

    Most-used first, then alphabetical, so the order is stable across requests.
    """
    if using_sqlite():
        elements = func.json_each(Automation.tags).table_valued("value")
    else:
        elements = func.json_array_elements_text(Automation.tags).table_valued("value")
    tag = elements.c.value
    count = func.count()
    result = await session.execute(
        select(tag, count)
        .select_from(Automation)
        .join(elements, true())
        .where(Automation.org_id == org_id, Automation.deleted_at.is_(None))
        .group_by(tag)
        .order_by(count.desc(), tag.asc())
    )
    return [(row[0], row[1]) for row in result.all()]
