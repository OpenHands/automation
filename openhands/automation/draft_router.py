"""Server-backed automation drafts.

Draft rows hold partial setup UI state. A draft is materialized into a disabled
Automation only when it validates and the user manually dispatches it for a test
run.
"""

import logging
import uuid
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.auth import AuthenticatedUser, require_permission
from openhands.automation.capabilities_router import (
    _DRAFT_MODELS,
    _cron_errors,
    _event_type_errors,
    _schema_errors,
)
from openhands.automation.db import get_session
from openhands.automation.git_sync import mark_git_sync_dirty
from openhands.automation.models import (
    Automation,
    AutomationDraft,
    AutomationLifecycleStatus,
    TarballUpload,
    UploadStatus,
)
from openhands.automation.preset_router import (
    CreatePluginAutomationRequest,
    CreatePromptAutomationRequest,
    _bytes_to_async_iter,
    _generate_plugin_tarball,
    _generate_tarball,
    _get_preset_entrypoint,
    _resolve_experiment_variant_models,
    _safe_truncate,
)
from openhands.automation.schemas import (
    AutomationDraftListResponse,
    AutomationDraftResponse,
    AutomationRunResponse,
    CreateAutomationDraftRequest,
    CreateAutomationRequest,
    CronTrigger,
    DraftEndpoint,
    DraftValidationError,
    EventTrigger,
    UpdateAutomationDraftRequest,
)
from openhands.automation.storage import FileStore, get_file_store
from openhands.automation.telemetry import (
    capture_automation_event,
    get_request_telemetry_context,
)
from openhands.automation.utils import utcnow
from openhands.automation.utils.model_profiles import (
    resolve_model_profile_for_user,
    validate_model_profile_for_user,
)
from openhands.automation.utils.run import create_pending_run
from openhands.automation.utils.tarball_validation import (
    build_internal_url,
    build_upload_storage_path,
    validate_tarball_path,
)
from openhands.automation.utils.timeout import default_automation_timeout
from openhands.automation.utils.webhook import get_webhook_config


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/drafts", tags=["Automation Drafts"])

_require_view_automations = require_permission("view_automations")
_require_manage_automations = require_permission("manage_automations")


async def _assert_org_automation_exists(
    session: AsyncSession, automation_id: uuid.UUID, org_id: uuid.UUID
) -> None:
    result = await session.execute(
        select(Automation.id).where(
            Automation.id == automation_id,
            Automation.org_id == org_id,
            Automation.deleted_at.is_(None),
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Automation not found")


async def _get_org_draft(
    session: AsyncSession, draft_id: uuid.UUID, org_id: uuid.UUID
) -> AutomationDraft:
    result = await session.execute(
        select(AutomationDraft).where(
            AutomationDraft.id == draft_id,
            AutomationDraft.org_id == org_id,
            AutomationDraft.deleted_at.is_(None),
        )
    )
    draft = result.scalars().first()
    if draft is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="Automation draft not found"
        )
    return draft


def _draft_name(name: str | None, draft_body: dict[str, Any]) -> str | None:
    if name:
        return name
    body_name = draft_body.get("name")
    return body_name if isinstance(body_name, str) and body_name.strip() else None


def _errors_to_json(errors: list[DraftValidationError]) -> list[dict[str, Any]]:
    return [error.model_dump() for error in errors]


async def _validate_draft_body(
    endpoint: DraftEndpoint,
    draft_body: dict[str, Any],
    user: AuthenticatedUser,
    session: AsyncSession,
) -> tuple[BaseModel | None, list[DraftValidationError]]:
    try:
        draft = _DRAFT_MODELS[endpoint].model_validate(draft_body)
    except ValidationError as e:
        return None, _schema_errors(e)

    errors: list[DraftValidationError] = []
    try:
        validate_model_profile_for_user(draft.model, user)
    except HTTPException as e:
        errors.append(
            DraftValidationError(
                field="model",
                code="model_profile_not_found",
                message=str(e.detail),
            )
        )

    if isinstance(draft, CreatePluginAutomationRequest):
        try:
            default_model = resolve_model_profile_for_user(draft.model, user)
            _resolve_experiment_variant_models(
                draft.variants, user, default_model=default_model
            )
        except HTTPException as e:
            errors.append(
                DraftValidationError(
                    field="variants",
                    code="variant_model_profile_not_found",
                    message=str(e.detail),
                )
            )

    if isinstance(draft, CreateAutomationRequest):
        try:
            await validate_tarball_path(
                tarball_path=draft.tarball_path,
                user_id=user.user_id,
                org_id=user.org_id,
                session=session,
            )
        except HTTPException as e:
            errors.append(
                DraftValidationError(
                    field="tarball_path",
                    code=f"tarball_path_{e.status_code}",
                    message=str(e.detail),
                )
            )

    trigger = draft.trigger
    if isinstance(trigger, CronTrigger):
        errors.extend(_cron_errors(trigger))
    elif isinstance(trigger, EventTrigger):
        webhook = await get_webhook_config(trigger.source, user.org_id, session)
        if webhook is None:
            errors.append(
                DraftValidationError(
                    field="trigger.source",
                    code="event_source_not_configured",
                    message=(
                        f"No webhook is configured to deliver '{trigger.source}' "
                        "events to this deployment."
                    ),
                )
            )
        else:
            errors.extend(_event_type_errors(trigger))

    return draft, errors


async def _refresh_validation(
    draft: AutomationDraft,
    user: AuthenticatedUser,
    session: AsyncSession,
) -> BaseModel | None:
    parsed, errors = await _validate_draft_body(
        draft.endpoint, draft.draft_body, user, session
    )
    draft.validation_errors = _errors_to_json(errors) if errors else None
    draft.dispatchable = not errors
    return parsed if not errors else None


async def _write_generated_tarball(
    *,
    session: AsyncSession,
    file_store: FileStore,
    user: AuthenticatedUser,
    name: str,
    prefix: str,
    description: str,
    tarball_content: bytes,
) -> str:
    upload_id = uuid.uuid4()
    storage_path = build_upload_storage_path(user.org_id, user.user_id, upload_id)
    upload = TarballUpload(
        id=upload_id,
        user_id=user.user_id,
        org_id=user.org_id,
        name=f"{prefix}-{_safe_truncate(name, 50)}",
        description=_safe_truncate(description, 200),
        status=UploadStatus.UPLOADING,
        storage_path=storage_path,
    )
    session.add(upload)
    await session.flush()

    try:
        size_bytes = await file_store.write_stream(
            path=storage_path,
            stream=_bytes_to_async_iter(tarball_content),
            content_type="application/x-tar",
        )
        upload.status = UploadStatus.COMPLETED
        upload.size_bytes = size_bytes
    except Exception as e:
        logger.exception("Failed to upload generated draft tarball: %s", e)
        upload.status = UploadStatus.FAILED
        upload.error_message = f"Upload failed: {e!s}"
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to upload tarball: {e!s}",
        )

    return build_internal_url(upload_id)


async def _materialize_raw_draft(
    body: CreateAutomationRequest,
    user: AuthenticatedUser,
    session: AsyncSession,
) -> dict[str, Any]:
    await validate_tarball_path(
        tarball_path=body.tarball_path,
        user_id=user.user_id,
        org_id=user.org_id,
        session=session,
    )
    preset_metadata = None
    if body.template is not None:
        preset_metadata = {"template": body.template.model_dump(exclude_none=True)}
    return {
        "name": body.name,
        "prompt": None,
        "preset_metadata": preset_metadata,
        "model": resolve_model_profile_for_user(body.model, user),
        "trigger": body.trigger.model_dump(),
        "tarball_path": body.tarball_path,
        "setup_script_path": body.setup_script_path,
        "entrypoint": body.entrypoint,
        "timeout": default_automation_timeout(body.timeout),
        "keep_alive": body.keep_alive,
    }


async def _materialize_prompt_draft(
    body: CreatePromptAutomationRequest,
    user: AuthenticatedUser,
    session: AsyncSession,
    file_store: FileStore,
) -> dict[str, Any]:
    tarball_content = _generate_tarball(body.prompt, repos=body.repos)
    tarball_path = await _write_generated_tarball(
        session=session,
        file_store=file_store,
        user=user,
        name=body.name,
        prefix="draft-prompt-automation",
        description=(
            f"Draft test generated from prompt: {_safe_truncate(body.prompt, 100)}"
        ),
        tarball_content=tarball_content,
    )
    preset_metadata: dict[str, Any] = {"preset_type": "prompt", "prompt": body.prompt}
    if body.repos:
        preset_metadata["repos"] = [r.model_dump(exclude_none=True) for r in body.repos]
    if body.template is not None:
        preset_metadata["template"] = body.template.model_dump(exclude_none=True)
    return {
        "name": body.name,
        "prompt": body.prompt,
        "preset_metadata": preset_metadata,
        "model": resolve_model_profile_for_user(body.model, user),
        "trigger": body.trigger.model_dump(),
        "tarball_path": tarball_path,
        "setup_script_path": "setup.sh",
        "entrypoint": _get_preset_entrypoint(),
        "timeout": default_automation_timeout(body.timeout),
        "keep_alive": body.keep_alive,
    }


async def _materialize_plugin_draft(
    body: CreatePluginAutomationRequest,
    user: AuthenticatedUser,
    session: AsyncSession,
    file_store: FileStore,
) -> dict[str, Any]:
    model = resolve_model_profile_for_user(body.model, user)
    variants = _resolve_experiment_variant_models(
        body.variants, user, default_model=model
    )
    tarball_content = _generate_plugin_tarball(
        body.plugins,
        body.prompt,
        repos=body.repos,
        experiment_id=body.experiment_id,
        variants=variants,
    )
    tarball_path = await _write_generated_tarball(
        session=session,
        file_store=file_store,
        user=user,
        name=body.name,
        prefix="draft-plugin-automation",
        description=f"Draft plugin automation test: {_safe_truncate(body.name, 100)}",
        tarball_content=tarball_content,
    )
    preset_metadata: dict[str, Any] = {"preset_type": "plugin", "prompt": body.prompt}
    if body.plugins:
        preset_metadata["plugins"] = [
            p.model_dump(exclude_none=True) for p in body.plugins
        ]
    if body.repos:
        preset_metadata["repos"] = [r.model_dump(exclude_none=True) for r in body.repos]
    if body.template is not None:
        preset_metadata["template"] = body.template.model_dump(exclude_none=True)
    return {
        "name": body.name,
        "prompt": body.prompt,
        "preset_metadata": preset_metadata,
        "model": model,
        "trigger": body.trigger.model_dump(),
        "tarball_path": tarball_path,
        "setup_script_path": "setup.sh",
        "entrypoint": _get_preset_entrypoint(),
        "timeout": default_automation_timeout(body.timeout),
        "keep_alive": body.keep_alive,
    }


async def _materialize_draft(
    draft: AutomationDraft,
    parsed: BaseModel,
    user: AuthenticatedUser,
    request: Request,
    session: AsyncSession,
    file_store: FileStore,
) -> Automation:
    if isinstance(parsed, CreateAutomationRequest):
        values = await _materialize_raw_draft(parsed, user, session)
    elif isinstance(parsed, CreatePromptAutomationRequest):
        values = await _materialize_prompt_draft(parsed, user, session, file_store)
    elif isinstance(parsed, CreatePluginAutomationRequest):
        values = await _materialize_plugin_draft(parsed, user, session, file_store)
    else:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unsupported draft endpoint",
        )

    automation: Automation | None = None
    if draft.materialized_automation_id is not None:
        existing = await session.get(Automation, draft.materialized_automation_id)
        if existing is not None and existing.deleted_at is None:
            automation = existing

    values.update(
        {
            "enabled": False,
            "lifecycle_status": AutomationLifecycleStatus.DRAFT,
            "disabled_reason": None,
            "disabled_detail": None,
            "disabled_at": None,
        }
    )
    if automation is None:
        automation = Automation(
            id=uuid.uuid4(),
            user_id=user.user_id,
            org_id=user.org_id,
            telemetry_distinct_id=get_request_telemetry_context(
                request
            ).frontend_distinct_id,
            **values,
        )
        session.add(automation)
        draft.materialized_automation_id = automation.id
    else:
        for field, value in values.items():
            setattr(automation, field, value)
    await session.flush()
    await mark_git_sync_dirty(session, automation)
    return automation


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_draft(
    body: CreateAutomationDraftRequest,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> AutomationDraftResponse:
    if body.source_automation_id is not None:
        await _assert_org_automation_exists(
            session, body.source_automation_id, user.org_id
        )
    draft = AutomationDraft(
        user_id=user.user_id,
        org_id=user.org_id,
        endpoint=body.endpoint,
        name=_draft_name(body.name, body.draft),
        draft_body=body.draft,
        source_automation_id=body.source_automation_id,
    )
    session.add(draft)
    await session.flush()
    await _refresh_validation(draft, user, session)
    await session.flush()
    await session.refresh(draft)
    return AutomationDraftResponse.model_validate(draft)


@router.get("")
async def list_drafts(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> AutomationDraftListResponse:
    base_query = select(AutomationDraft).where(
        AutomationDraft.org_id == user.org_id,
        AutomationDraft.deleted_at.is_(None),
    )
    total = (
        await session.execute(select(func.count()).select_from(base_query.subquery()))
    ).scalar() or 0
    result = await session.execute(
        base_query.order_by(AutomationDraft.updated_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return AutomationDraftListResponse(
        drafts=[
            AutomationDraftResponse.model_validate(draft) for draft in result.scalars()
        ],
        total=total,
    )


@router.get("/{draft_id}")
async def get_draft(
    draft_id: uuid.UUID,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> AutomationDraftResponse:
    draft = await _get_org_draft(session, draft_id, user.org_id)
    return AutomationDraftResponse.model_validate(draft)


@router.patch("/{draft_id}")
async def update_draft(
    draft_id: uuid.UUID,
    body: UpdateAutomationDraftRequest,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> AutomationDraftResponse:
    draft = await _get_org_draft(session, draft_id, user.org_id)
    if body.endpoint is not None:
        draft.endpoint = body.endpoint
    if body.draft is not None:
        draft.draft_body = body.draft
    if body.name is not None or body.draft is not None:
        draft.name = _draft_name(body.name, draft.draft_body)
    await _refresh_validation(draft, user, session)
    await session.flush()
    await session.refresh(draft)
    return AutomationDraftResponse.model_validate(draft)


@router.delete("/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_draft(
    draft_id: uuid.UUID,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> None:
    draft = await _get_org_draft(session, draft_id, user.org_id)
    draft.deleted_at = utcnow()
    await session.flush()


@router.post("/{draft_id}/dispatch", status_code=status.HTTP_201_CREATED)
async def dispatch_draft(
    draft_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
    file_store: FileStore = Depends(get_file_store),
) -> AutomationRunResponse:
    del background_tasks
    draft = await _get_org_draft(session, draft_id, user.org_id)
    parsed = await _refresh_validation(draft, user, session)
    if parsed is None:
        await session.flush()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Draft is not dispatchable",
                "errors": draft.validation_errors or [],
            },
        )

    automation = await _materialize_draft(
        draft, parsed, user, request, session, file_store
    )
    run = await create_pending_run(
        session,
        automation,
        telemetry_distinct_id=get_request_telemetry_context(
            request
        ).frontend_distinct_id,
        trigger_source="manual",
    )
    draft.last_test_run_id = run.id
    await session.flush()
    await session.refresh(run)
    await capture_automation_event(
        "automation_run_created",
        request=request,
        user=user,
        automation=automation,
        run=run,
        properties={"trigger_source": "manual", "draft_id": str(draft.id)},
    )
    return AutomationRunResponse.model_validate(run)
