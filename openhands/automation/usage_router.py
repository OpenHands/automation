"""Instance-wide totals for the OpenHands server's usage report.

Endpoint: GET /v1/usage

The OpenHands server reports an install's usage to Customer Center. Automations
live in this service's database, so it asks here how many there are. The caller
is the server itself, not a user, so instead of the user credentials that
``authenticate_request`` checks, the request carries the service key this
service uses to call the server (``AUTOMATION_SERVICE_KEY`` here,
``AUTOMATIONS_SERVICE_KEY`` on the server), in the same ``X-Service-API-Key``
header.
"""

import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.config import get_config
from openhands.automation.db import get_session
from openhands.automation.models import Automation
from openhands.automation.schemas import UsageResponse


logger = logging.getLogger("automation.usage_router")

router = APIRouter(prefix="/v1/usage", tags=["Usage"])


def require_service_key(
    x_service_api_key: Annotated[str | None, Header()] = None,
) -> None:
    """Reject the request unless it carries the configured service key."""
    service_key = get_config().service.service_key
    if not service_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service authentication not configured",
        )
    # Compared as UTF-8 bytes: compare_digest raises on a non-ASCII str, and a
    # header value can be non-ASCII.
    if not x_service_api_key or not hmac.compare_digest(
        x_service_api_key.encode("utf-8"), service_key.encode("utf-8")
    ):
        logger.warning("Invalid service API key attempted")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service API key",
        )


@router.get("", dependencies=[Depends(require_service_key)])
async def get_usage(
    session: AsyncSession = Depends(get_session, scope="function"),
) -> UsageResponse:
    """Totals across every organization."""
    total = await session.scalar(
        select(func.count())
        .select_from(Automation)
        .where(Automation.deleted_at.is_(None))
    )
    return UsageResponse(automations=total or 0)
