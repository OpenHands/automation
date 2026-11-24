"""Check parser definitions against a factory's registered outcome keys."""

import uuid
from collections.abc import Sequence
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.factory_router import get_org_factory
from openhands.automation.outcome_parsers import OutcomeParserDefinition


async def validate_parser_registry(
    session: AsyncSession,
    org_id: uuid.UUID,
    factory_id: uuid.UUID | None,
    parsers: Sequence[OutcomeParserDefinition | dict[str, Any]] | None,
) -> None:
    if not parsers:
        return
    if factory_id is None:
        raise HTTPException(409, "Outcome parsers require a factory")
    factory = await get_org_factory(session, org_id, factory_id)
    for raw in parsers:
        parser = OutcomeParserDefinition.model_validate(raw)
        if factory.outcome_keys.get(parser.key) != parser.value_type:
            raise HTTPException(
                422,
                f"Outcome parser key {parser.key!r} must match the factory registry",
            )
