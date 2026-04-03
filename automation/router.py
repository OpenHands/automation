"""FastAPI router for the automations CRUD API.

Uses Temporal for workflow execution:
- Creating/updating automations creates/updates Temporal Schedules
- Manual dispatch starts a Temporal Workflow
- Run completion updates database records
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client

from automation.auth import AuthenticatedUser, authenticate_request
from automation.config import get_settings
from automation.constants import MAX_RUN_DURATION_SECONDS
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
from automation.temporal.client import get_temporal_client
from automation.temporal.schedules import (
    create_schedule,
    delete_schedule,
    update_schedule,
)
from automation.temporal.types import (
    AutomationConfig,
    TriggerContext,
    WorkflowInput,
)
from automation.temporal.workflows import AutomationWorkflow
from automation.utils import utcnow
from automation.utils.tarball_validation import validate_tarball_path


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Automations"])


# --- Dependencies ---


async def get_client() -> Client:
    """Dependency to get the Temporal client."""
    return await get_temporal_client()


# --- CRUD ---


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_automation(
    body: CreateAutomationRequest,
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(get_client),
) -> AutomationResponse:
    """Create a new automation with Temporal scheduling.

    Creates the automation in the database and a corresponding Temporal
    Schedule if the trigger is cron-based.
    """
    # Validate tarball_path
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
        trigger=body.trigger.model_dump(),
        tarball_path=body.tarball_path,
        setup_script_path=body.setup_script_path,
        entrypoint=body.entrypoint,
        timeout=body.timeout,
    )
    session.add(auto)
    await session.flush()
    await session.refresh(auto)

    # Create Temporal Schedule for cron triggers
    if body.trigger.type == "cron":
        try:
            await create_schedule(client, auto)
        except Exception as e:
            logger.error("Failed to create Temporal schedule: %s", e)
            # Rollback the automation creation
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create schedule: {e}",
            )

    return AutomationResponse.model_validate(auto)


@router.get("")
async def list_automations(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> AutomationListResponse:
    """List automations for the authenticated user."""
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
async def update_automation_endpoint(
    automation_id: uuid.UUID,
    body: UpdateAutomationRequest,
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(get_client),
) -> AutomationResponse:
    """Update an automation and its Temporal Schedule."""
    auto = await _get_user_automation(session, automation_id, user.user_id, user.org_id)

    update_data = body.model_dump(exclude_unset=True)
    if body.trigger is not None:
        update_data["trigger"] = body.trigger.model_dump()

    for field, value in update_data.items():
        setattr(auto, field, value)

    await session.flush()
    await session.refresh(auto)

    # Update Temporal Schedule
    if auto.trigger.get("type") == "cron":
        try:
            await update_schedule(client, auto)
        except Exception as e:
            logger.warning("Failed to update Temporal schedule: %s", e)
            # Try to create it if it doesn't exist
            try:
                await create_schedule(client, auto)
            except Exception:
                pass

    return AutomationResponse.model_validate(auto)


@router.delete("/{automation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_automation_endpoint(
    automation_id: uuid.UUID,
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(get_client),
) -> None:
    """Soft delete an automation and its Temporal Schedule."""
    auto = await _get_user_automation(session, automation_id, user.user_id, user.org_id)
    auto.enabled = False
    auto.deleted_at = utcnow()
    await session.flush()

    # Delete Temporal Schedule
    await delete_schedule(client, automation_id)


# --- Runs ---


@router.post("/{automation_id}/dispatch", status_code=status.HTTP_201_CREATED)
async def dispatch_automation(
    automation_id: uuid.UUID,
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(get_client),
) -> AutomationRunResponse:
    """Manually dispatch an automation run using Temporal.

    Starts a Temporal Workflow for immediate execution instead of
    creating a PENDING database record.
    """
    auto = await _get_user_automation(session, automation_id, user.user_id, user.org_id)
    settings = get_settings()

    # Create a database record for tracking
    run = AutomationRun(
        automation_id=automation_id,
        status=AutomationRunStatus.RUNNING,
        started_at=utcnow(),
    )
    session.add(run)
    await session.flush()
    await session.refresh(run)

    # Build workflow input
    automation_config = AutomationConfig(
        automation_id=str(auto.id),
        user_id=str(auto.user_id),
        org_id=str(auto.org_id),
        name=auto.name,
        tarball_path=auto.tarball_path,
        entrypoint=auto.entrypoint,
        timeout_seconds=auto.timeout or MAX_RUN_DURATION_SECONDS,
        trigger=auto.trigger,
        setup_script_path=auto.setup_script_path,
    )

    trigger_context = TriggerContext(
        trigger_type="manual",
        triggered_by=str(user.user_id),
    )

    workflow_input = WorkflowInput(
        automation=automation_config,
        trigger_context=trigger_context,
        run_id=str(run.id),
        callback_url=f"{settings.resolved_base_url}/v1/runs/{run.id}/complete",
    )

    # Start the workflow
    workflow_id = f"automation-run-{run.id}"
    try:
        await client.start_workflow(
            AutomationWorkflow.run,
            workflow_input,
            id=workflow_id,
            task_queue=settings.temporal_task_queue,
        )
        logger.info(
            "Started workflow: workflow_id=%s run_id=%s automation_id=%s",
            workflow_id,
            run.id,
            automation_id,
        )
    except Exception as e:
        # Mark run as failed if workflow couldn't start
        run.status = AutomationRunStatus.FAILED
        run.error_detail = f"Failed to start workflow: {e}"
        run.completed_at = utcnow()
        await session.flush()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start workflow: {e}",
        )

    return AutomationRunResponse.model_validate(run)


@router.get("/{automation_id}/runs")
async def list_automation_runs(
    automation_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> AutomationRunListResponse:
    """List runs for a specific automation."""
    await _get_user_automation(session, automation_id, user.user_id, user.org_id)

    count_result = await session.execute(
        select(func.count()).where(AutomationRun.automation_id == automation_id)
    )
    total = count_result.scalar() or 0

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
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> AutomationRunResponse:
    """Receive completion callback from the SDK.

    This endpoint is called by the SDK running inside a sandbox when
    the automation entrypoint finishes. It updates the database record.

    Note: With Temporal, workflow completion is also tracked in Temporal's
    event history, but this callback updates our database for API queries.
    """
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.orm import selectinload

    result = await session.execute(
        select(AutomationRun)
        .where(AutomationRun.id == run_id)
        .options(selectinload(AutomationRun.automation))
    )
    run = result.scalars().first()
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Run not found")

    automation = run.automation
    if automation.user_id != user.user_id or automation.org_id != user.org_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Not your automation")

    now = utcnow()
    new_status = (
        AutomationRunStatus.COMPLETED
        if body.status == "COMPLETED"
        else AutomationRunStatus.FAILED
    )

    values: dict = {
        "status": new_status,
        "completed_at": now,
    }
    if body.status == "COMPLETED" and body.conversation_id:
        values["conversation_id"] = body.conversation_id
    if body.status == "FAILED" and body.error:
        values["error_detail"] = body.error

    # Optimistic update
    stmt = (
        update(AutomationRun)
        .where(
            AutomationRun.id == run_id,
            AutomationRun.status == AutomationRunStatus.RUNNING,
        )
        .values(**values)
    )
    db_result: CursorResult = await session.execute(stmt)  # type: ignore[assignment]

    if db_result.rowcount == 0:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Run is {run.status.value}, expected RUNNING",
        )

    await session.refresh(run)
    logger.info("Run completed: run_id=%s status=%s", run_id, new_status.value)

    return AutomationRunResponse.model_validate(run)


# --- Helpers ---


async def _get_user_automation(
    session: AsyncSession,
    automation_id: uuid.UUID,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
) -> Automation:
    """Fetch a non-deleted automation belonging to the user."""
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
