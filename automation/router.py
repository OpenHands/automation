"""FastAPI router for the automations CRUD API."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from automation.auth import AuthenticatedUser, authenticate_request
from automation.db import get_session
from automation.models import Automation, AutomationRun, AutomationRunStatus
from automation.schemas import (
    AutomationListResponse,
    AutomationResponse,
    AutomationRunListResponse,
    AutomationRunResponse,
    CreateAutomationRequest,
    RunCompleteRequest,
    UpdateAutomationRequest,
)
from automation.utils import utcnow
from automation.utils.run import create_pending_run
from automation.utils.tarball_validation import validate_tarball_path


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/automations", tags=["Automations"])


# --- CRUD ---


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_automation(
    body: CreateAutomationRequest,
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> AutomationResponse:
    """Create a new automation.

    The tarball_path can be either:
    - Internal upload: oh-internal://uploads/{uuid} (from /api/v1/uploads)
    - External public URL: https://, s3://, or gs:// URLs
    """
    # Validate tarball_path (checks ownership for internal uploads)
    await validate_tarball_path(
        tarball_path=body.tarball_path,
        user_id=user.user_id,
        org_id=user.org_id,
        session=session,
    )

    auto = Automation(
        user_id=user.user_id,
        org_id=user.org_id,
        name=body.name,
        triggers=body.trigger.model_dump(),
        tarball_path=body.tarball_path,
        setup_script_path=body.setup_script_path,
        entrypoint=body.entrypoint,
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
    """List automations for the authenticated user (excludes soft-deleted)."""
    base_query = select(Automation).where(
        Automation.user_id == user.user_id,
        Automation.org_id == user.org_id,
        Automation.deleted_at.is_(None),
    )

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
    auto = await _get_user_automation(session, automation_id, user.user_id, user.org_id)
    return AutomationResponse.model_validate(auto)


@router.patch("/{automation_id}")
async def update_automation(
    automation_id: uuid.UUID,
    body: UpdateAutomationRequest,
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> AutomationResponse:
    """Partially update an automation."""
    auto = await _get_user_automation(session, automation_id, user.user_id, user.org_id)

    update_data = body.model_dump(exclude_unset=True)
    # Handle trigger -> triggers field mapping (only if trigger has a real value)
    if body.trigger is not None:
        update_data["triggers"] = body.trigger.model_dump()
    update_data.pop("trigger", None)

    for field, value in update_data.items():
        setattr(auto, field, value)

    # Note: updated_at is handled automatically by the model's onupdate=utcnow
    await session.flush()
    await session.refresh(auto)
    return AutomationResponse.model_validate(auto)


@router.delete("/{automation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_automation(
    automation_id: uuid.UUID,
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Soft delete an automation."""
    auto = await _get_user_automation(session, automation_id, user.user_id, user.org_id)
    auto.enabled = False
    auto.deleted_at = utcnow()
    await session.flush()


# --- Runs ---


@router.post("/{automation_id}/dispatch", status_code=status.HTTP_201_CREATED)
async def dispatch_automation(
    automation_id: uuid.UUID,
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> AutomationRunResponse:
    """Manually dispatch an automation run.

    Creates a PENDING run for the specified automation, which will be
    picked up by the dispatcher and executed.
    """
    auto = await _get_user_automation(session, automation_id, user.user_id, user.org_id)
    run = await create_pending_run(session, auto)
    await session.flush()
    await session.refresh(run)
    return AutomationRunResponse.model_validate(run)


@router.get("/{automation_id}/runs")
async def list_automation_runs(
    automation_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> AutomationRunListResponse:
    """List runs for a specific automation.

    Returns runs ordered by creation time (latest first), with pagination.
    """
    # Verify the automation exists and belongs to the user
    await _get_user_automation(session, automation_id, user.user_id, user.org_id)

    # Count total runs for this automation
    count_result = await session.execute(
        select(func.count()).where(AutomationRun.automation_id == automation_id)
    )
    total = count_result.scalar() or 0

    # Fetch paginated runs ordered by latest first
    result = await session.execute(
        select(AutomationRun)
        .where(AutomationRun.automation_id == automation_id)
        .order_by(AutomationRun.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    runs = result.scalars().all()

    return AutomationRunListResponse(
        runs=[AutomationRunResponse.model_validate(r) for r in runs],
        total=total,
    )


# --- Run completion callback ---


@router.post("/runs/{run_id}/complete")
async def complete_run(
    run_id: uuid.UUID,
    body: RunCompleteRequest,
    session: AsyncSession = Depends(get_session),
) -> AutomationRunResponse:
    """Receive completion callback from the SDK running inside a sandbox.

    Called by ``OpenHandsCloudWorkspace.__exit__`` when the automation
    entry-point finishes (success or failure).  Transitions the run from
    RUNNING → COMPLETED or RUNNING → FAILED.

    This endpoint is unauthenticated — it is only reachable from inside a
    sandbox that already has the run ID.
    """
    result = await session.execute(
        select(AutomationRun).where(AutomationRun.id == run_id)
    )
    run = result.scalars().first()
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Run not found")

    if run.status != AutomationRunStatus.RUNNING:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Run is {run.status.value}, expected RUNNING",
        )

    now = utcnow()
    if body.status == "COMPLETED":
        run.status = AutomationRunStatus.COMPLETED
        run.completed_at = now
        if body.conversation_id:
            run.conversation_id = body.conversation_id
    else:
        run.status = AutomationRunStatus.FAILED
        run.error_detail = body.error
        run.completed_at = now

    await session.flush()
    await session.refresh(run)
    logger.info("Run %s → %s", run_id, run.status.value)
    return AutomationRunResponse.model_validate(run)


# --- Helpers ---


async def _get_user_automation(
    session: AsyncSession,
    automation_id: uuid.UUID,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
) -> Automation:
    """Fetch a non-deleted automation, ensuring it belongs to the given user and org."""
    result = await session.execute(
        select(Automation).where(
            Automation.id == automation_id,
            Automation.user_id == user_id,
            Automation.org_id == org_id,
            Automation.deleted_at.is_(None),
        )
    )
    auto = result.scalars().first()
    if auto is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Automation not found",
        )
    return auto
