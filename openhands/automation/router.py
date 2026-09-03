"""FastAPI router for the automations CRUD API."""

import asyncio
import logging
import re
import uuid
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from openhands.automation.auth import AuthenticatedUser, require_permission
from openhands.automation.db import get_session
from openhands.automation.git_sync import mark_git_sync_dirty
from openhands.automation.models import (
    Automation,
    AutomationDisableEvent,
    AutomationLifecycleStatus as ModelAutomationLifecycleStatus,
    AutomationRun,
    AutomationRunStatus,
    TarballUpload,
)
from openhands.automation.preset_router import regenerate_preset_prompt_tarball
from openhands.automation.schemas import (
    AutomationLifecycleStatus,
    AutomationListResponse,
    AutomationResponse,
    AutomationRunListResponse,
    AutomationRunResponse,
    CreateAutomationRequest,
    RunCompleteRequest,
    RunPhaseRequest,
    UpdateAutomationRequest,
)
from openhands.automation.storage import FileStore, get_file_store
from openhands.automation.telemetry import (
    capture_automation_event,
    get_request_telemetry_context,
)
from openhands.automation.utils import utcnow
from openhands.automation.utils.api_key import (
    APIKeyError,
    get_api_key_for_automation_run,
)
from openhands.automation.utils.callback_error import format_callback_error
from openhands.automation.utils.conversation_outcome import (
    fetch_latest_finish_tool_response_for_run,
)
from openhands.automation.utils.model_profiles import resolve_model_profile_for_user
from openhands.automation.utils.run import (
    create_pending_run,
    record_first_run_outcome,
    skip_pending_runs_for_disabled_automation,
)
from openhands.automation.utils.run_status_detail import (
    run_status_detail_from_callback_error,
)
from openhands.automation.utils.sandbox import cleanup_sandbox
from openhands.automation.utils.tarball_validation import (
    is_http_url,
    parse_internal_upload_id,
    validate_tarball_path,
)
from openhands.automation.utils.templates import (
    TEMPLATE_EXISTS_RESPONSE,
    find_existing_template_automation,
)
from openhands.automation.utils.timeout import default_automation_timeout
from openhands.automation.utils.unhealthy import maybe_disable_unhealthy_automation


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Automations"])


def _model_lifecycle_status(
    lifecycle_status: AutomationLifecycleStatus | str | None, enabled: bool
) -> ModelAutomationLifecycleStatus:
    if lifecycle_status is not None:
        return ModelAutomationLifecycleStatus(str(lifecycle_status))
    return (
        ModelAutomationLifecycleStatus.ACTIVE
        if enabled
        else ModelAutomationLifecycleStatus.INACTIVE
    )


def _lifecycle_enabled(
    lifecycle_status: ModelAutomationLifecycleStatus,
) -> bool:
    return lifecycle_status == ModelAutomationLifecycleStatus.ACTIVE


_require_view_automations = require_permission("view_automations")
_require_manage_automations = require_permission("manage_automations")


async def _assert_can_manage(automation: Automation, user: AuthenticatedUser) -> None:
    """Authorize a write operation on a specific automation.

    Admins and owners carry ``manage_automations`` and are always allowed.
    A member (view-only) is allowed only when they are the creator of the
    automation being modified — the ticket calls this the "creator escape
    hatch."  ``require_permission`` can't express this because it has no
    access to the automation row, so the check lives here in the handler
    path instead of in the dependency.

    Callers must have already passed a ``view_automations`` dependency so
    the user is at least a member of the org.
    """
    if "manage_automations" in user.permissions:
        return
    if automation.user_id == user.user_id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only admins, owners, or the automation creator can modify it",
    )


# --- CRUD ---


@router.post(
    "", status_code=status.HTTP_201_CREATED, responses=TEMPLATE_EXISTS_RESPONSE
)
async def create_automation(
    body: CreateAutomationRequest,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> AutomationResponse:
    """Create a new automation.

    The tarball_path can be either:
    - Internal upload: oh-internal://uploads/{uuid} (from /v1/uploads)
    - External public URL: https://, s3://, or gs:// URLs

    An entry shipping its own tarball creates here rather than through a
    preset, so it may carry the same ``template`` provenance those accept.
    """
    # Enabling the same template twice returns the existing automation rather
    # than a duplicate. Before tarball validation, so a repeat enable costs one
    # query and leaves the new upload unreferenced rather than adopting it.
    if body.template is not None:
        existing = await find_existing_template_automation(
            session, user.org_id, body.template.id
        )
        if existing is not None:
            response.status_code = status.HTTP_200_OK
            return AutomationResponse.model_validate(existing)

    # Validate tarball_path (checks ownership for internal uploads)
    await validate_tarball_path(
        tarball_path=body.tarball_path,
        user_id=user.user_id,
        org_id=user.org_id,
        session=session,
    )
    model = resolve_model_profile_for_user(body.model, user)

    preset_metadata: dict[str, Any] | None = None
    if body.template is not None:
        preset_metadata = {"template": body.template.model_dump(exclude_none=True)}

    lifecycle_status = _model_lifecycle_status(body.lifecycle_status, body.enabled)

    auto = Automation(
        user_id=user.user_id,
        org_id=user.org_id,
        name=body.name,
        model=model,
        preset_metadata=preset_metadata,
        trigger=body.trigger.model_dump(),
        tarball_path=body.tarball_path,
        setup_script_path=body.setup_script_path,
        entrypoint=body.entrypoint,
        timeout=default_automation_timeout(body.timeout),
        keep_alive=body.keep_alive,
        enabled=_lifecycle_enabled(lifecycle_status),
        lifecycle_status=lifecycle_status,
        telemetry_distinct_id=get_request_telemetry_context(
            request
        ).frontend_distinct_id,
    )
    session.add(auto)
    await session.flush()
    await session.refresh(auto)
    await mark_git_sync_dirty(session, auto)
    creation_properties: dict[str, Any] = {"creation_path": "raw"}
    if body.template is not None:
        creation_properties["template_id"] = body.template.id
        creation_properties["template_version"] = body.template.version
    await capture_automation_event(
        "automation_created",
        request=request,
        user=user,
        automation=auto,
        properties=creation_properties,
    )
    return AutomationResponse.model_validate(auto)


@router.get("")
async def list_automations(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> AutomationListResponse:
    """List automations for the caller's org (excludes soft-deleted)."""
    base_query = select(Automation).where(
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
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> AutomationResponse:
    """Get a single automation by ID."""
    auto = await _get_org_automation(session, automation_id, user.org_id)
    return AutomationResponse.model_validate(auto)


@router.patch("/{automation_id}")
async def update_automation(
    automation_id: uuid.UUID,
    body: UpdateAutomationRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    user: AuthenticatedUser = Depends(_require_view_automations),
    # Function scope commits the session when the handler returns, BEFORE the
    # response is sent and its background tasks run. With the default request
    # scope the deferred tarball delete would run before the commit, and a
    # commit failure would strand a live upload record pointing at an
    # already-deleted object.
    session: AsyncSession = Depends(get_session, scope="function"),
) -> AutomationResponse:
    """Partially update an automation."""
    auto = await _get_org_automation(session, automation_id, user.org_id)
    await _assert_can_manage(auto, user)

    update_data = body.model_dump(exclude_unset=True)
    # Handle trigger field mapping (only if trigger has a real value)
    if body.trigger is not None:
        update_data["trigger"] = body.trigger.model_dump()

    requested_lifecycle = update_data.pop("lifecycle_status", None)
    if requested_lifecycle is not None:
        lifecycle_status = _model_lifecycle_status(
            requested_lifecycle, update_data.get("enabled", auto.enabled)
        )
        update_data["lifecycle_status"] = lifecycle_status
        update_data["enabled"] = _lifecycle_enabled(lifecycle_status)
    elif "enabled" in update_data:
        update_data["lifecycle_status"] = _model_lifecycle_status(
            None, update_data["enabled"]
        )

    disable_event: AutomationDisableEvent | None = None
    skip_pending_reason: str | None = None
    if update_data.get("enabled") is True:
        update_data["disabled_reason"] = None
        update_data["disabled_detail"] = None
        update_data["disabled_at"] = None
    elif update_data.get("enabled") is False:
        lifecycle_status = update_data.get("lifecycle_status")
        is_manual_inactive = (
            lifecycle_status == ModelAutomationLifecycleStatus.INACTIVE
            or (
                lifecycle_status is None
                and auto.lifecycle_status != ModelAutomationLifecycleStatus.DRAFT
            )
        )
        skip_pending_reason = (
            "Automation moved to draft by user"
            if lifecycle_status == ModelAutomationLifecycleStatus.DRAFT
            else "Automation disabled by user"
        )
        if auto.enabled and is_manual_inactive:
            disabled_at = utcnow()
            disabled_detail = {"reason": "manual", "source": "user"}
            update_data["disabled_reason"] = "manual"
            update_data["disabled_detail"] = disabled_detail
            update_data["disabled_at"] = disabled_at
            disable_event = AutomationDisableEvent(
                automation_id=auto.id,
                reason="manual",
                detail=disabled_detail,
                source="manual",
            )

    if "model" in update_data:
        update_data["model"] = resolve_model_profile_for_user(body.model, user)

    original_prompt = auto.prompt
    for field, value in update_data.items():
        setattr(auto, field, value)

    # A preset automation bakes its prompt into the tarball the dispatcher
    # executes; the `prompt` column is metadata only. When the prompt actually
    # changes, rebuild the tarball so the next dispatch runs the new prompt
    # instead of the original baked one. Skipped when the value is unchanged (a
    # no-op edit), or for non-preset automations.
    if (
        "prompt" in update_data
        and isinstance(auto.prompt, str)
        and auto.prompt != original_prompt
    ):
        new_tarball_path = await regenerate_preset_prompt_tarball(
            auto, auto.prompt, session, background_tasks
        )
        if new_tarball_path is not None:
            auto.tarball_path = new_tarball_path
        # Keep preset metadata in sync with the edited prompt. Reassign the
        # whole dict: in-place mutation of a JSON column is not change-tracked.
        if auto.preset_metadata is not None:
            auto.preset_metadata = {**auto.preset_metadata, "prompt": auto.prompt}

    if skip_pending_reason is not None:
        await skip_pending_runs_for_disabled_automation(
            session,
            auto.id,
            reason=skip_pending_reason,
            disabled_detail=auto.disabled_detail,
            completed_at=auto.disabled_at,
        )
    if disable_event is not None:
        session.add(disable_event)

    # Note: updated_at is handled automatically by the model's onupdate=utcnow
    await session.flush()
    await session.refresh(auto)
    await mark_git_sync_dirty(session, auto)
    await capture_automation_event(
        "automation_updated",
        request=request,
        user=user,
        automation=auto,
        properties={"updated_fields": sorted(update_data.keys())},
    )
    return AutomationResponse.model_validate(auto)


@router.delete("/{automation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_automation(
    automation_id: uuid.UUID,
    request: Request,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Soft delete an automation."""
    auto = await _get_org_automation(session, automation_id, user.org_id)
    await _assert_can_manage(auto, user)
    was_enabled = auto.enabled
    auto.enabled = False
    auto.lifecycle_status = ModelAutomationLifecycleStatus.INACTIVE
    deleted_at = utcnow()
    auto.deleted_at = deleted_at
    if was_enabled:
        disabled_detail = {"reason": "manual_delete", "source": "user"}
        auto.disabled_reason = "manual_delete"
        auto.disabled_detail = disabled_detail
        auto.disabled_at = deleted_at
        session.add(
            AutomationDisableEvent(
                automation_id=auto.id,
                reason="manual_delete",
                detail=disabled_detail,
                source="manual_delete",
            )
        )
    await skip_pending_runs_for_disabled_automation(
        session,
        auto.id,
        reason="Automation deleted by user",
        disabled_detail=auto.disabled_detail,
        completed_at=deleted_at,
        include_manual=True,
    )
    await session.flush()
    await mark_git_sync_dirty(session, auto)
    await capture_automation_event(
        "automation_deleted",
        request=request,
        user=user,
        automation=auto,
    )


@router.get("/{automation_id}/tarball")
async def download_automation_tarball(
    automation_id: uuid.UUID,
    user: AuthenticatedUser = Depends(_require_view_automations),
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
    auto = await _get_org_automation(session, automation_id, user.org_id)

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
                detail="Tarball not found (underlying upload may have been deleted)",
            )
        try:
            data = await asyncio.to_thread(file_store.read, upload.storage_path)
        except FileNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Tarball file not found in storage",
            )
        except Exception as e:
            logger.error("Failed to read tarball from storage: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve tarball from storage",
            )
        safe_name = re.sub(r'[\x00-\x1f\x7f"\\\/]', "", auto.name) or "automation"
        return Response(
            content=data,
            media_type="application/x-tar",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}.tar"'},
        )

    if is_http_url(auto.tarball_path):
        return RedirectResponse(url=auto.tarball_path, status_code=302)

    scheme = (
        auto.tarball_path.split("://")[0] if "://" in auto.tarball_path else "unknown"
    )
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
    request: Request,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> AutomationRunResponse:
    """Manually dispatch an automation run.

    Creates a PENDING run for the specified automation, which will be
    picked up by the dispatcher and executed.
    """
    auto = await _get_org_automation(session, automation_id, user.org_id)
    await _assert_can_manage(auto, user)
    run = await create_pending_run(
        session,
        auto,
        telemetry_distinct_id=get_request_telemetry_context(
            request
        ).frontend_distinct_id,
        trigger_source="manual",
    )
    await session.flush()
    await session.refresh(run)
    await capture_automation_event(
        "automation_run_created",
        request=request,
        user=user,
        automation=auto,
        run=run,
        properties={"trigger_source": "manual"},
    )
    return AutomationRunResponse.model_validate(run)


@router.get("/{automation_id}/runs")
async def list_automation_runs(
    automation_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> AutomationRunListResponse:
    """List runs for a specific automation.

    Returns runs ordered by creation time (latest first), with pagination.
    """
    # Verify the automation exists and belongs to the caller's org
    await _get_org_automation(session, automation_id, user.org_id)

    # Count lifetime runs by status for this automation
    count_result = await session.execute(
        select(AutomationRun.status, func.count())
        .where(AutomationRun.automation_id == automation_id)
        .group_by(AutomationRun.status)
    )
    status_counts = {run_status.value: count for run_status, count in count_result}
    total = sum(status_counts.values())

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
        status_counts=status_counts,
    )


# --- Run completion callback ---


@router.post("/runs/{run_id}/complete")
async def complete_run(
    run_id: uuid.UUID,
    body: RunCompleteRequest,
    request: Request,
    user: AuthenticatedUser = Depends(_require_view_automations),
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

    If keep_alive is not true, deletes the sandbox after updating the run
    status. When post-run callbacks are configured, cleanup will happen after
    callbacks instead. keep_alive=true leaves cleanup to the runtime TTL reaper.
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
    if body.cost is not None:
        values["cost"] = body.cost
    if body.status == "FAILED":
        error_detail = (
            format_callback_error(body.error)
            if body.error
            else "Completion callback reported failure"
        )
        values["error_detail"] = error_detail
        values["status_detail"] = run_status_detail_from_callback_error(
            body.error or error_detail,
            formatted_detail=error_detail,
            previous=run.status_detail,
        )
    elif body.status == "COMPLETED":
        # Task outcomes and blocking factors are agent/user-level result metadata;
        # only SDK callback errors and system dispatch errors feed auto-disablement.
        values["status_detail"] = None
    if body.conversation_id:
        finish_tool_response = await fetch_latest_finish_tool_response_for_run(
            run, body.conversation_id
        )
        if finish_tool_response is not None:
            values["run_metadata"] = {
                **(run.run_metadata or {}),
                "finish_tool_response": finish_tool_response,
            }

    stmt = (
        update(AutomationRun)
        .where(
            AutomationRun.id == run_id,
            AutomationRun.status == AutomationRunStatus.RUNNING,
        )
        .values(**values)
    )
    db_result: CursorResult = await session.execute(stmt)  # type: ignore[assignment]

    reconciled = False
    if db_result.rowcount == 0:
        # See the *current* terminal state, not the pre-UPDATE snapshot
        # (the watchdog may have committed FAILED while we held a stale row).
        await session.refresh(run)
        if (
            new_status == AutomationRunStatus.COMPLETED
            and run.status == AutomationRunStatus.FAILED
            and (run.error_detail or "").startswith("Timed out: ")
        ):
            # The watchdog guessed FAILED at its deadline; the callback is
            # direct proof the entrypoint finished successfully. Flip it.
            # The "Timed out: " prefix is written only by the watchdog, so
            # dispatcher-authored failures and cancellations still 409.
            values["error_detail"] = None
            reconcile_stmt = (
                update(AutomationRun)
                .where(
                    AutomationRun.id == run_id,
                    AutomationRun.status == AutomationRunStatus.FAILED,
                    AutomationRun.error_detail.startswith("Timed out: "),
                )
                .values(**values)
            )
            reconcile_result: CursorResult = await session.execute(  # type: ignore[assignment]
                reconcile_stmt
            )
            reconciled = reconcile_result.rowcount > 0
        if not reconciled:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"Run is {run.status.value}, expected RUNNING",
            )
    if new_status == AutomationRunStatus.FAILED:
        automation_disabled = await maybe_disable_unhealthy_automation(
            session,
            automation.id,
        )
    else:
        automation_disabled = False

    await session.refresh(run)
    logger.info("Run %s → %s", run_id, new_status.value)
    telemetry_properties: dict = {"trigger_source": "callback"}
    if reconciled:
        telemetry_properties["reconciled_watchdog_timeout"] = True
    if automation_disabled:
        telemetry_properties["automation_disabled"] = True

    await capture_automation_event(
        "automation_run_completed"
        if new_status == AutomationRunStatus.COMPLETED
        else "automation_run_failed",
        request=request,
        user=user,
        automation=automation,
        run=run,
        properties=telemetry_properties,
    )
    await record_first_run_outcome(run, new_status, "execution", session=session)

    # Clean up immediately when this automation owns explicit cleanup. Once
    # post-run callbacks exist, this path should run them before deleting.
    if run.sandbox_id and automation.keep_alive is not True:
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


# --- Run phase reporting ---


@router.post("/runs/{run_id}/phase")
async def report_run_phase(
    run_id: uuid.UUID,
    body: RunPhaseRequest,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> AutomationRunResponse:
    """Record a live progress phase reported from inside the sandbox.

    Best-effort telemetry for the dashboard: authenticated with the same
    credentials injected into the sandbox (see ``complete_run``), the caller
    must own the run's parent automation, and the write only lands while the
    run is still PENDING or RUNNING (409 once terminal).
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

    stmt = (
        update(AutomationRun)
        .where(
            AutomationRun.id == run_id,
            AutomationRun.status.in_(
                (AutomationRunStatus.PENDING, AutomationRunStatus.RUNNING)
            ),
        )
        .values(current_phase=body.phase)
    )
    db_result: CursorResult = await session.execute(stmt)  # type: ignore[assignment]
    if db_result.rowcount == 0:
        await session.refresh(run)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Run is {run.status.value}, expected PENDING or RUNNING",
        )

    await session.refresh(run)
    return AutomationRunResponse.model_validate(run)


# --- Run cancellation ---


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: uuid.UUID,
    request: Request,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> AutomationRunResponse:
    """Cancel a pending or running automation run.

    For PENDING runs, prevents future dispatch.
    For RUNNING runs, marks as cancelled and cleans up the sandbox.
    Returns 409 if the run is already in a terminal state.
    """
    result = await session.execute(
        select(AutomationRun)
        .where(AutomationRun.id == run_id)
        .options(selectinload(AutomationRun.automation))
    )
    run = result.scalars().first()
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Run not found")

    automation = run.automation
    if automation.org_id != user.org_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Not your automation")
    await _assert_can_manage(automation, user)

    # Only PENDING and RUNNING runs can be cancelled
    if run.status not in (AutomationRunStatus.PENDING, AutomationRunStatus.RUNNING):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                f"Run is {run.status.value}, only PENDING or"
                " RUNNING runs can be cancelled"
            ),
        )

    now = utcnow()
    stmt = (
        update(AutomationRun)
        .where(
            AutomationRun.id == run_id,
            AutomationRun.status.in_(
                [AutomationRunStatus.PENDING, AutomationRunStatus.RUNNING]
            ),
        )
        .values(
            status=AutomationRunStatus.CANCELLED,
            completed_at=now,
            error_detail="Cancelled by user",
            status_detail=None,
        )
    )
    db_result: CursorResult = await session.execute(stmt)  # type: ignore[assignment]

    if db_result.rowcount == 0:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Run state changed concurrently, cancellation failed",
        )

    await session.refresh(run)
    logger.info("Run %s cancelled by user", run_id)
    await capture_automation_event(
        "automation_run_cancelled",
        request=request,
        user=user,
        automation=automation,
        run=run,
        properties={"trigger_source": "manual"},
    )

    # Clean up sandbox for runs that were RUNNING
    if run.sandbox_id:
        from openhands.automation.config import get_settings

        settings = get_settings()
        api_key = user.api_key
        if api_key is None:
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


async def _get_org_automation(
    session: AsyncSession,
    automation_id: uuid.UUID,
    org_id: uuid.UUID,
) -> Automation:
    """Fetch a non-deleted automation in the caller's org."""
    result = await session.execute(
        select(Automation).where(
            Automation.id == automation_id,
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
