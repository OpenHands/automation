"""Automation outcome submission API."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.db import get_session
from openhands.automation.kv_router import KVAuthContext, get_kv_auth_context
from openhands.automation.models import (
    Automation,
    AutomationOutcomeValue,
    AutomationRun,
)
from openhands.automation.outcome_schemas import (
    SubmitOutcomeRequest,
    SubmitOutcomeResponse,
)
from openhands.automation.outcome_values import extract_outcome_value_columns
from openhands.automation.utils import utcnow


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/outcomes", tags=["Outcomes"])


async def _get_automation(
    session: AsyncSession, automation_id: uuid.UUID
) -> Automation:
    result = await session.execute(
        select(Automation).where(Automation.id == automation_id)
    )
    automation = result.scalars().first()
    if automation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Automation not found",
        )
    return automation


async def _validated_run_id(
    session: AsyncSession,
    automation_id: uuid.UUID,
    run_id: uuid.UUID | None,
) -> uuid.UUID | None:
    if run_id is None:
        return None
    result = await session.execute(
        select(AutomationRun.id).where(
            AutomationRun.id == run_id,
            AutomationRun.automation_id == automation_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="run_id does not belong to the authenticated automation",
        )
    return run_id


@router.post("", status_code=status.HTTP_201_CREATED)
async def submit_outcome(
    body: SubmitOutcomeRequest,
    auth: KVAuthContext = Depends(get_kv_auth_context),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> SubmitOutcomeResponse:
    automation = await _get_automation(session, auth.automation_id)
    run_id = auth.run_id if auth.auth_method == "kv_token" else body.run_id
    run_id = await _validated_run_id(session, automation.id, run_id)
    try:
        value_columns = extract_outcome_value_columns(
            body.payload, automation.outcome_parsers
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    now = utcnow()
    for columns in value_columns:
        session.add(
            AutomationOutcomeValue(
                org_id=automation.org_id,
                automation_id=automation.id,
                run_id=run_id,
                created_at=now,
                **columns,
            )
        )
    await session.flush()
    logger.info(
        "Stored %s outcome values for automation %s", len(value_columns), automation.id
    )
    return SubmitOutcomeResponse(stored=len(value_columns))
