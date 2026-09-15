"""Scoped API for work selected by a running automation."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from openhands.automation.config import get_config
from openhands.automation.conversations import submit_subject_turn
from openhands.automation.db import get_session
from openhands.automation.models import AutomationRun, AutomationRunStatus
from openhands.automation.schemas import SubjectTurnRequest, SubjectTurnResponse
from openhands.automation.utils.run_token import (
    SUBMIT_SUBJECT_TURN,
    RunTokenError,
    signing_secret,
    verify_run_token,
)


router = APIRouter(prefix="/v1/runs", tags=["Automation subject turns"])


@router.post(
    "/{run_id}/subject-turns",
    response_model=SubjectTurnResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_subject_turn(
    run_id: uuid.UUID,
    body: SubjectTurnRequest,
    authorization: Annotated[str | None, Header()] = None,
    session: AsyncSession = Depends(get_session),
) -> SubjectTurnResponse:
    """Create or continue a subject conversation for the caller's automation."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Run token required")
    try:
        claims = verify_run_token(
            signing_secret(get_config().service),
            authorization.removeprefix("Bearer ").strip(),
            SUBMIT_SUBJECT_TURN,
        )
    except RunTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    if claims.run_id != run_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Token belongs to another run")

    requester = (
        (
            await session.execute(
                select(AutomationRun)
                .where(AutomationRun.id == run_id)
                .options(selectinload(AutomationRun.automation))
            )
        )
        .scalars()
        .first()
    )
    if requester is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if requester.automation_id != claims.automation_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Token belongs to another automation"
        )
    if requester.status != AutomationRunStatus.RUNNING:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only a running automation can submit work"
        )

    try:
        result = await submit_subject_turn(
            session,
            requester=requester,
            source=body.source,
            subject_key=body.subject_key,
            turn=body.turn,
            idempotency_key=body.idempotency_key,
            wake_agent=body.wake_agent,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    await session.commit()
    return SubjectTurnResponse(
        disposition=result.disposition,
        run_id=result.run_id,
        conversation_id=uuid.UUID(result.conversation_id),
    )
