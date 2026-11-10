"""Routes another service calls, authenticated by the service key.

Endpoint: ``GET /v1/service/factories``

The OpenHands Enterprise server stores a factory's worklogs but not the
factory, so it asks here which factory ids exist before it creates a factory's
worklog, and in its maintenance task that removes the worklogs of factories
that are gone. The caller is the server, not a user: like ``usage_router`` it
sends the service key in ``X-Service-API-Key`` and no user credential is read.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.db import get_session
from openhands.automation.models import Factory
from openhands.automation.schemas import ServiceFactoriesResponse, ServiceFactory
from openhands.automation.usage_router import require_service_key


MAX_FACTORY_IDS = 100

router = APIRouter(prefix="/v1/service", tags=["Service"])


@router.get("/factories", dependencies=[Depends(require_service_key)])
async def list_factories_by_id(
    org_id: uuid.UUID,
    id: Annotated[
        list[uuid.UUID],
        Query(
            max_length=MAX_FACTORY_IDS,
            description=f"Factory ids to look up, at most {MAX_FACTORY_IDS}.",
        ),
    ] = [],  # noqa: B006
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ServiceFactoriesResponse:
    """The requested factories that exist in ``org_id``.

    An id that does not exist, or belongs to another org, is absent from the
    answer.
    """
    if not id:
        return ServiceFactoriesResponse(factories=[])
    rows = await session.execute(
        select(Factory.id, Factory.org_id).where(
            Factory.org_id == org_id, Factory.id.in_(id)
        )
    )
    return ServiceFactoriesResponse(
        factories=[ServiceFactory(id=i, org_id=o) for i, o in rows.all()]
    )
