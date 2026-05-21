"""FastAPI router for the automations CRUD API."""

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from openhands.automation.auth import AuthenticatedUser, require_permission
from openhands.automation.db import get_session
from openhands.automation.models import Automation, AutomationRun, AutomationRunStatus, TarballUpload
from openhands.automation.schemas import (
    AutomationListResponse,
    AutomationResponse,
    AutomationRunListResponse,
    AutomationRunResponse,
    CreateAutomationRequest,
    RunCompleteRequest,
    UpdateAutomationRequest,
)
from openhands.automation.storage import FileStore, get_file_store
from openhands.automation.utils import utcnow
from openhands.automation.utils.api_key import (
    APIKeyError,
    get_api_key_for_automation_run,
)
from openhands.automation.utils.model_profiles import resolve_model_profile_for_user
from openhands.automation.utils.run import create_pending_run
from openhands.automation.utils.sandbox import cleanup_sandbox
from openhands.automation.utils.tarball_validation import (
    is_http_url,
    parse_internal_upload_id,
    validate_tarball_path,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Automations"])

_require_manage_automations = require_permission("manage_automations")


# --- CRUD ---


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_automation(
    body: CreateAutomationRequest,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> AutomationResponse:
    """Create a new automation.

    The tarball_path can be either:
    - Internal upload: oh-internal://uploads/{uuid} (from /v1/uploads)
    - External public URL: https://, s3://, or gs:// URLs
    """
    # Validate tarball_path (checks ownership for internal uploads)
    await validate_tarball_path(
        tarball_path=body.tarball_path,
        user_id=user.user_id,
        org_id=user.org_id,
        session=session,
    )
    model = resolve_model_profile_for_user(body.model, user)

    auto = Automation(
        user_id=user.user_id,
        org_id=user.org_id,
        name=body.name,
        model=model,
        trigger=body.trigger.model_dump(),
        tarball_path=body.tarball_path,
        setup_script_path=body.setup_script_path,
        entrypoint=body.entrypoint,
        timeout=body.timeout,
    )
    session.add(auto)
    await session.flush()
    await session.refresh(auto)
    return AutomationResponse.model_validate(auto)


@router.get("")
async def list_automations(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: AuthenticatedUser = Depends(_require_manage_automations),
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
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> AutomationResponse:
    """Get a single automation by ID."""
    auto = await _get_user_automation(session, automation_id, user.user_id, user.org_id)
    return AutomationResponse.model_validate(auto)


@router.patch("/{automation_id}")
async def update_automation(
    automation_id: uuid.UUID,
    body: UpdateAutomationRequest,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> AutomationResponse:
    """Partially update an automation."""
    auto = await _get_user_automation(session, automation_id, user.user_id, user.org_id)

    update_data = body.model_dump(exclude_unset=True)
    # Handle trigger field mapping (only if trigger has a real value)
    if body.trigger is not None:
        update_data["trigger"] = body.trigger.model_dump()

    if "model" in update_data:
        update_data["model"] = resolve_model_profile_for_user(body.model, user)

    for field, value in update_data.items():
        setattr(auto, field, value)

    # Note: updated_at is handled automatically by the model's onupdate=utcnow
    await session.flush()
    await session.refresh(auto)
    return AutomationResponse.model_validate(auto)


@router.delete("/{automation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_automation(
    automation_id: uuid.UUID,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Soft delete an automation."""
    auto = await _get_user_automation(session, automation_id, user.user_id, user.org_id)
    auto.enabled = False
    auto.deleted_at = utcnow()
    await session.flush()


@router.get("/{automation_id}/tarball")
async def download_automation_tarball(
    automation_id: uuid.UUID,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
    file_store: FileStore = Depends(get_file_store),
) -> Response:
    """Download the tarball for an automation.

    - Internal uploads (oh-internal://): returns the raw tarball bytes as an
      attachment.
    - https:// URLs: returns a 302 redirect to the external URL.
    - s3:// or gs:// URLs: returns 422 (cannot proxy cloud storage URLs).
    - 404 if the automation has no accessible tarball.
    """
    auto = await _get_user_automation(session, automation_id, user.user_id, user.org_id)

    upload_id = parse_internal_upload_id(auto.tarball_path)
    if upload_id is not None:
        result = await session.execute(
            select(TarballUpload).where(
                TarballUpload.id == upload_id,
                TarballUpload.deleted_at.is_(None),
            )
        )
        upload = result.scalars().first()
        if upload is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Tarball not found (the underlying upload may have been deleted)",
            )
        try:
            data = await asyncio.to_thread(file_store.read, upload.storage_path)
        except FileNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Tarball file not found in storage",
            )
        safe_name = auto.name.replace('"', "").replace("/", "-")
        return Response(
            content=data,
            media_type="application/x-tar",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}.tar"'},
        )

    if is_http_url(auto.tarball_path):
        return RedirectResponse(url=auto.tarball_path, status_code=302)

    scheme = auto.tarball_path.split("://")[0] if "://" in auto.tarball_path else "unknown"
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=(
            f"Cannot proxy {scheme}:// tarball URLs. "
            "Retrieve the tarball_path from GET /api/automation/v1/{automation_id} "
            "and access the file directly."
        ),
    )


# --- Runs ---


@router.post("/{automation_id}/dispatch", status_code=status.HTTP_201_CREATED)
async def dispatch_automation(
    automation_id: uuid.UUID,
    user: AuthenticatedUser = Depends(_require_manage_automations),
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
    user: AuthenticatedUser = Depends(_require_manage_automations),
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
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> AutomationRunResponse:
    """Receive completion callback from the SDK running inside a sandbox.

    Called by ``OpenHandsCloudWorkspace.__exit__`` when the automation
    entry-point finishes (success or failure).  Transitions the run from
    RUNNING → COMPLETED or RUNNING → FAILED.

    Authenticated via the same credentials that were passed into
    the sandbox.  The credentials are validated against ``/api/v1/users/me``
    (by ``authenticate_request``) and the resulting user must own the run's
    parent automation.

    If keep_alive is False, deletes the sandbox after updating the run status.
    """
    result = await session.execute(
        select(AutomationRun)
        .where(AutomationRun.id == run_id)
        .options(selectinload(AutomationRun.automation))
    )
    run = result.scalars().first()
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Run not found")

    # Verify the caller owns this automation
    automation = run.automation
    if automation.user_id != user.user_id or automation.org_id != user.org_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Not your automation")

    # Optimistic locking: only update if the run is still RUNNING.
    # This prevents races between the watchdog and the callback.
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
    if body.conversation_id:
        values["conversation_id"] = body.conversation_id
    if body.status == "FAILED" and body.error:
        values["error_detail"] = body.error

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
    logger.info("Run %s → %s", run_id, new_status.value)

    # Clean up sandbox if not keeping alive
    if not run.keep_alive and run.sandbox_id:
        # Fire-and-forget sandbox deletion in background
        from openhands.automation.config import get_settings

        settings = get_settings()
        api_key = user.api_key
        if api_key is None:
            # Cookie-authenticated users don't carry an API key;
            # mint a temporary per-user key for sandbox cleanup.
            try:
                api_key = await get_api_key_for_automation_run(run)
            except (APIKeyError, ValueError):
                logger.warning(
                    "Could not mint API key for sandbox cleanup (run %s), "
                    "skipping cleanup",
                    run_id,
                )
                api_key = None

        if api_key is not None:
            asyncio.create_task(
                cleanup_sandbox(
                    api_url=settings.openhands_api_base_url,
                    api_key=api_key,
                    sandbox_id=run.sandbox_id,
                    run_id=str(run_id),
                )
            )

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
