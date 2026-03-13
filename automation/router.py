"""FastAPI router for the automations CRUD API."""

import uuid
from datetime import UTC, datetime

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from automation.auth import AuthenticatedUser, authenticate_request
from automation.db import get_session
from automation.encryption import decrypt_api_key, encrypt_api_key
from automation.executor import execute_automation
from automation.models import Automation, AutomationRun, AutomationRunStatus
from automation.schemas import (
    AutomationListResponse,
    AutomationResponse,
    AutomationRunResponse,
    CreateAutomationRequest,
    RunListResponse,
    UpdateAutomationRequest,
)


router = APIRouter(prefix="/api/v1/automations", tags=["Automations"])


def _validate_cron(schedule: str) -> None:
    """Validate a cron expression."""
    if not croniter.is_valid(schedule):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid cron expression: {schedule}",
        )


# --- CRUD ---


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_automation(
    body: CreateAutomationRequest,
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> AutomationResponse:
    """Create a new automation."""
    if body.triggers.cron:
        _validate_cron(body.triggers.cron.schedule)

    auto = Automation(
        user_id=user.user_id,
        name=body.name,
        triggers=body.triggers.model_dump(exclude_none=True),
        sdk_code_tarball_path=body.sdk_code_tarball_path,
        encrypted_api_key=encrypt_api_key(body.api_key),
    )
    session.add(auto)
    await session.flush()
    await session.refresh(auto)
    return AutomationResponse.model_validate(auto)


@router.get("")
async def list_automations(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> AutomationListResponse:
    """List automations for the authenticated user."""
    base_query = select(Automation).where(Automation.user_id == user.user_id)

    count_result = await session.execute(
        select(func.count()).select_from(base_query.subquery())
    )
    total = count_result.scalar() or 0

    result = await session.execute(
        base_query.order_by(Automation.created_at.desc()).offset(offset).limit(limit)
    )
    automations = result.scalars().all()

    return AutomationListResponse(
        automations=[AutomationResponse.model_validate(a) for a in automations],
        total=total,
    )


@router.get("/{automation_id}")
async def get_automation(
    automation_id: uuid.UUID,
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> AutomationResponse:
    """Get a single automation by ID."""
    auto = await _get_user_automation(session, automation_id, user.user_id)
    return AutomationResponse.model_validate(auto)


@router.patch("/{automation_id}")
async def update_automation(
    automation_id: uuid.UUID,
    body: UpdateAutomationRequest,
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> AutomationResponse:
    """Update an existing automation."""
    auto = await _get_user_automation(session, automation_id, user.user_id)

    if body.name is not None:
        auto.name = body.name
    if body.triggers is not None:
        if body.triggers.cron:
            _validate_cron(body.triggers.cron.schedule)
        auto.triggers = body.triggers.model_dump(exclude_none=True)
    if body.sdk_code_tarball_path is not None:
        auto.sdk_code_tarball_path = body.sdk_code_tarball_path
    if body.api_key is not None:
        auto.encrypted_api_key = encrypt_api_key(body.api_key)
    if body.enabled is not None:
        auto.enabled = body.enabled

    await session.flush()
    await session.refresh(auto)
    return AutomationResponse.model_validate(auto)


@router.delete("/{automation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_automation(
    automation_id: uuid.UUID,
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete an automation."""
    auto = await _get_user_automation(session, automation_id, user.user_id)
    await session.delete(auto)


# --- Runs ---


@router.get("/{automation_id}/runs")
async def list_runs(
    automation_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> RunListResponse:
    """List runs for a specific automation."""
    # Verify ownership
    await _get_user_automation(session, automation_id, user.user_id)

    base_query = select(AutomationRun).where(
        AutomationRun.automation_id == automation_id
    )
    count_result = await session.execute(
        select(func.count()).select_from(base_query.subquery())
    )
    total = count_result.scalar() or 0

    result = await session.execute(
        base_query.order_by(AutomationRun.created_at.desc()).offset(offset).limit(limit)
    )
    runs = result.scalars().all()

    return RunListResponse(
        runs=[AutomationRunResponse.model_validate(r) for r in runs],
        total=total,
    )


@router.post("/{automation_id}/trigger", status_code=status.HTTP_202_ACCEPTED)
async def trigger_automation(
    automation_id: uuid.UUID,
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> AutomationRunResponse:
    """Manually trigger an automation run ("Run Now" button)."""
    auto = await _get_user_automation(session, automation_id, user.user_id)
    now = datetime.now(UTC)

    run = AutomationRun(
        automation_id=auto.id,
        status=AutomationRunStatus.RUNNING,
        trigger_type="manual",
        started_at=now,
    )
    session.add(run)
    await session.flush()

    # Execute in-band for manual triggers (keep it simple for MVP)
    try:
        api_key = decrypt_api_key(auto.encrypted_api_key)
    except ValueError:
        run.status = AutomationRunStatus.FAILED
        run.error_detail = "Failed to decrypt stored API key"
        run.completed_at = datetime.now(UTC)
        await session.flush()
        await session.refresh(run)
        return AutomationRunResponse.model_validate(run)

    result = await execute_automation(
        api_key=api_key,
        sdk_code_tarball_path=auto.sdk_code_tarball_path,
    )

    if result.success:
        run.status = AutomationRunStatus.COMPLETED
        run.conversation_id = result.conversation_id
    else:
        run.status = AutomationRunStatus.FAILED
        run.error_detail = result.error

    run.completed_at = datetime.now(UTC)
    await session.flush()
    await session.refresh(run)
    return AutomationRunResponse.model_validate(run)


# --- Helpers ---


async def _get_user_automation(
    session: AsyncSession, automation_id: uuid.UUID, user_id: str
) -> Automation:
    """Fetch an automation, ensuring it belongs to the given user."""
    result = await session.execute(
        select(Automation).where(
            Automation.id == automation_id,
            Automation.user_id == user_id,
        )
    )
    auto = result.scalars().first()
    if auto is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Automation not found",
        )
    return auto
