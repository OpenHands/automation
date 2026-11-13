"""Service-to-service routes for the OpenHands Enterprise server.

Callers authenticate with the service key in ``X-Service-API-Key``, exactly like
``usage_router``: this service's ``AUTOMATION_SERVICE_KEY`` is the value
Enterprise holds as ``AUTOMATIONS_SERVICE_KEY``.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.db import get_session
from openhands.automation.models import Factory
from openhands.automation.schemas import FactoryLookupItem, FactoryLookupResponse
from openhands.automation.usage_router import require_service_key


MAX_LOOKUP_FACTORY_IDS = 100

router = APIRouter(prefix="/v1/service", tags=["Service"])


@router.get("/factories", dependencies=[Depends(require_service_key)])
async def lookup_factories(
    org_id: uuid.UUID,
    ids: Annotated[
        list[uuid.UUID],
        Query(alias="id", min_length=1, max_length=MAX_LOOKUP_FACTORY_IDS),
    ],
    session: AsyncSession = Depends(get_session, scope="function"),
) -> FactoryLookupResponse:
    """The requested factory ids that exist in ``org_id``.

    An id that does not exist, or belongs to another org, is absent from the
    answer. Enterprise confirms a factory exists before it creates the
    factory's default worklog, and uses this to remove the worklogs of factories
    that are gone.
    """
    rows = await session.execute(
        select(Factory.id, Factory.org_id).where(
            Factory.org_id == org_id, Factory.id.in_(ids)
        )
    )
    return FactoryLookupResponse(
        factories=[FactoryLookupItem(id=row.id, org_id=row.org_id) for row in rows]
    )
