"""FastAPI router for the automations CRUD API."""

import asyncio
import json
import logging
import re
import unicodedata
import uuid
from datetime import timedelta
from typing import Any, Literal
from urllib.parse import quote

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

from openhands.automation.auth import (
    AuthenticatedUser,
    get_http_client,
    require_permission,
)
from openhands.automation.config import get_config
from openhands.automation.db import get_session
from openhands.automation.git_sync import mark_git_sync_dirty
from openhands.automation.models import (
    Automation,
    AutomationChangeEvent,
    AutomationDisableEvent,
    AutomationDraft,
    AutomationOwnerKind,
    AutomationRun,
    AutomationRunStatus,
    AutomationState as ModelAutomationState,
)
from openhands.automation.observability import (
    add_event,
    automation_attributes,
    current_span_context,
    span,
)
from openhands.automation.plugin_specs import (
    AutomationPluginSource,
    CreatePluginSpecAutomationRequest,
    PluginSpecError,
    entrypoint_for_manifest,
    preflight_manifest,
    resolve_suggested_trigger,
    setup_script_for_manifest,
    validate_plugin_config,
    validate_required_plugins,
)
from openhands.automation.preset_router import (
    _COMMIT_SHA_RE,
    _require_pinned_plugin_refs,
    dump_repos,
    regenerate_preset_tarball,
    repos_config_json,
)
from openhands.automation.schemas import (
    AutomationChangeListResponse,
    AutomationChangeResponse,
    AutomationListResponse,
    AutomationResponse,
    AutomationRunListResponse,
    AutomationRunResponse,
    AutomationTagCount,
    AutomationTagsResponse,
    CreateAutomationRequest,
    EventTrigger,
    RunCompleteRequest,
    RunPhaseRequest,
    TransferAutomationRequest,
    UpdateAutomationRequest,
)
from openhands.automation.storage import FileStore, get_file_store
from openhands.automation.tags import normalize_tag
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
from openhands.automation.utils.change_events import record_change
from openhands.automation.utils.conversation_outcome import (
    fetch_latest_finish_tool_response_for_run,
)
from openhands.automation.utils.event_sources import is_event_source_configured
from openhands.automation.utils.model_profiles import (
    ensure_agent_profile_exists,
    resolve_model_profile_for_user,
    validate_agent_profile_allowlists,
    validate_agent_profile_combination,
)
from openhands.automation.utils.owner import (
    ResolvedOwner,
    assign_upload_to_owner,
    require_service_owner_permission,
    resolve_owner,
    verify_service_account,
)
from openhands.automation.utils.run import (
    create_pending_run,
    record_first_run_outcome,
    skip_pending_runs_for_disabled_automation,
)
from openhands.automation.utils.run_status_detail import (
    run_status_detail_from_callback_error,
)
from openhands.automation.utils.sandbox import cleanup_sandbox, pause_sandbox
from openhands.automation.utils.state import (
    automation_state_enabled,
    model_automation_state,
)
from openhands.automation.utils.tags import count_org_tags, has_tag
from openhands.automation.utils.tarball_validation import (
    get_org_upload,
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
from openhands.automation.utils.webhook import is_builtin_source
from openhands.sdk.plugin import PluginSource


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Automations"])


_require_view_automations = require_permission("view_automations")


async def _assert_can_manage(automation: Automation, user: AuthenticatedUser) -> None:
    """Authorize a write operation on a specific automation.

    Admins and owners carry ``manage_automations`` and are always allowed.
    A member (view-only) is allowed only when they are the creator of the
    automation being modified — the ticket calls this the "creator escape
    hatch."  ``require_permission`` can't express this because it has no
    access to the automation row, so the check lives here in the handler
    path instead of in the dependency.

    A service-account-owned automation has no creator escape: only holders of
    ``manage_automations`` pass, so the service account's own run key cannot
    edit, delete, dispatch or cancel the automation it runs.

    Callers must have already passed a ``view_automations`` dependency so
    the user is at least a member of the org.
    """
    if "manage_automations" in user.permissions:
        return
    if (
        automation.owner_kind != AutomationOwnerKind.SERVICE
        and automation.user_id == user.user_id
    ):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only admins, owners, or the automation creator can modify it",
    )


def _is_disable_only_update(update_data: dict[str, Any]) -> bool:
    fields = set(update_data)
    if not fields or not fields <= {"enabled", "state"}:
        return False
    if "state" in update_data:
        state = model_automation_state(update_data["state"], update_data.get("enabled"))
        return (
            state == ModelAutomationState.INACTIVE
            and update_data.get("enabled", False) is False
        )
    return update_data.get("enabled") is False


def _update_action(update_data: dict[str, Any]) -> str:
    """Audit action of a PATCH: enable or disable when it only flips the switch."""
    lifecycle_fields = {
        "enabled",
        "state",
        "disabled_reason",
        "disabled_detail",
        "disabled_at",
    }
    if "enabled" in update_data and set(update_data) <= lifecycle_fields:
        return "enable" if update_data["enabled"] else "disable"
    return "update"


def _assert_can_update_fields(
    automation: Automation, user: AuthenticatedUser, update_data: dict[str, Any]
) -> None:
    """Allow a full edit by the creator, or by a manager of a service-owned one.

    ``_assert_can_manage`` has already run: a service-owned automation reaches
    here only for a ``manage_automations`` holder, so no permission recheck.
    """
    if automation.owner_kind == AutomationOwnerKind.SERVICE:
        return
    if automation.user_id == user.user_id:
        return
    if _is_disable_only_update(update_data):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "Only the automation creator can edit it; admins and owners can only "
            "turn it off or delete it"
        ),
    )


async def _get_draft_for_materialized_automation(
    session: AsyncSession, automation: Automation
) -> AutomationDraft | None:
    result = await session.execute(
        select(AutomationDraft).where(
            AutomationDraft.materialized_automation_id == automation.id,
            AutomationDraft.org_id == automation.org_id,
            AutomationDraft.deleted_at.is_(None),
        )
    )
    return result.scalars().first()


def _draft_not_dispatchable_error(draft: AutomationDraft) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "message": "Draft is not dispatchable",
            "errors": draft.validation_errors or [],
        },
    )


async def _assert_normal_api_can_use_draft_artifact(
    session: AsyncSession, automation: Automation
) -> None:
    draft = await _get_draft_for_materialized_automation(session, automation)
    if automation.state != ModelAutomationState.DRAFT and draft is None:
        return

    if draft is not None and not draft.dispatchable:
        raise _draft_not_dispatchable_error(draft)

    raise HTTPException(
        status.HTTP_409_CONFLICT,
        detail=(
            "Draft automation artifacts must be dispatched or activated through "
            "the draft API so the current draft is validated and materialized"
        ),
    )


# --- CRUD ---


@router.post(
    "", status_code=status.HTTP_201_CREATED, responses=TEMPLATE_EXISTS_RESPONSE
)
async def create_automation(
    body: CreateAutomationRequest,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> AutomationResponse:
    """Create a new automation.

    The tarball_path can be either:
    - Internal upload: oh-internal://uploads/{uuid} (from /v1/uploads)
    - External public URL: https://, s3://, or gs:// URLs

    An entry shipping its own tarball creates here rather than through a
    preset, so it may carry the same ``template`` provenance those accept.

    ``owner_user_id`` creates it as a service account of the org (managers only).
    """
    validate_agent_profile_combination(body.agent_profile_id, body.model)

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

    # After the template lookup, so a repeat enable stays one query and does
    # not depend on the OpenHands API, and before the rest of the transaction.
    await ensure_agent_profile_exists(body.agent_profile_id, request, user)

    owner = await resolve_owner(body.owner_user_id, user, get_http_client(request))

    # Validate tarball_path (checks ownership for internal uploads). Always
    # against the caller: a service account owns an upload only after creation.
    await validate_tarball_path(
        tarball_path=body.tarball_path,
        user_id=user.user_id,
        org_id=user.org_id,
        session=session,
    )
    model = (
        None
        if body.agent_profile_id
        else resolve_model_profile_for_user(body.model, user)
    )

    preset_metadata: dict[str, Any] | None = None
    if body.template is not None:
        preset_metadata = {"template": body.template.model_dump(exclude_none=True)}

    state = model_automation_state(body.state, body.enabled)

    auto = Automation(
        user_id=owner.user_id,
        owner_kind=owner.kind,
        org_id=user.org_id,
        name=body.name,
        description=body.description,
        model=model,
        agent_profile_id=body.agent_profile_id,
        preset_metadata=preset_metadata,
        trigger=body.trigger.model_dump(),
        observability_associations=body.observability_associations,
        tags=body.tags,
        tarball_path=body.tarball_path,
        setup_script_path=body.setup_script_path,
        entrypoint=body.entrypoint,
        timeout=default_automation_timeout(body.timeout),
        keep_alive=body.keep_alive,
        enabled=automation_state_enabled(state),
        state=state,
        telemetry_distinct_id=get_request_telemetry_context(
            request
        ).frontend_distinct_id,
    )
    session.add(auto)
    await assign_upload_to_owner(session, body.tarball_path, user.org_id, owner)
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


@router.post(
    "/plugin-spec",
    status_code=status.HTTP_201_CREATED,
    responses=TEMPLATE_EXISTS_RESPONSE,
)
async def create_automation_from_plugin_spec(
    body: CreatePluginSpecAutomationRequest,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> AutomationResponse:
    """Create an automation from an executable plugin specification."""
    if body.template is not None:
        existing = await find_existing_template_automation(
            session, user.org_id, body.template.id
        )
        if existing is not None:
            response.status_code = status.HTTP_200_OK
            return AutomationResponse.model_validate(existing)

    owner = await resolve_owner(body.owner_user_id, user, get_http_client(request))

    runtime_fields = await _resolve_plugin_spec_runtime_fields(
        plugin=body.plugin,
        plugin_config=body.config,
        request=request,
        user=user,
        pin_ref=owner.kind == AutomationOwnerKind.SERVICE,
    )
    trigger = await _resolve_plugin_spec_trigger(
        requested_trigger=body.trigger,
        manifest=runtime_fields["manifest"],
        plugin_config=runtime_fields["plugin_spec"]["config"],
        user=user,
        session=session,
    )

    keep_alive = body.keep_alive
    if (
        isinstance(trigger, EventTrigger)
        and trigger.destination == "continue_conversation"
    ):
        if keep_alive is False:
            raise HTTPException(
                status_code=400,
                detail=(
                    "keep_alive cannot be false when destination is "
                    "'continue_conversation': deleting the sandbox would destroy "
                    "the conversation the next event continues"
                ),
            )
        keep_alive = True

    state = model_automation_state(body.state, body.enabled)
    preset_metadata: dict[str, Any] = {"preset_type": "plugin_spec"}
    if body.template is not None:
        preset_metadata["template"] = body.template.model_dump(exclude_none=True)

    automation = Automation(
        user_id=owner.user_id,
        owner_kind=owner.kind,
        org_id=user.org_id,
        name=body.name,
        prompt=None,
        preset_metadata=preset_metadata,
        plugin_spec=runtime_fields["plugin_spec"],
        model=runtime_fields["model"],
        agent_profile_id=runtime_fields["agent_profile_id"],
        trigger=trigger.model_dump(),
        observability_associations=body.observability_associations,
        tarball_path=runtime_fields["tarball_path"],
        setup_script_path=runtime_fields["setup_script_path"],
        entrypoint=runtime_fields["entrypoint"],
        timeout=default_automation_timeout(body.timeout),
        keep_alive=keep_alive,
        enabled=automation_state_enabled(state),
        state=state,
        telemetry_distinct_id=get_request_telemetry_context(
            request
        ).frontend_distinct_id,
    )
    session.add(automation)
    await session.flush()
    await session.refresh(automation)
    await mark_git_sync_dirty(session, automation)
    await capture_automation_event(
        "automation_created",
        request=request,
        user=user,
        automation=automation,
        properties={"creation_path": "plugin_spec"},
    )
    return AutomationResponse.model_validate(automation)


def _allow_local_plugin_sources() -> bool:
    return get_config().service.is_local_mode


def _fetch_validated_plugin_manifest(
    plugin: AutomationPluginSource,
    plugin_config: dict[str, Any] | None,
) -> tuple[dict[str, Any], Any, str | None, list[dict[str, Any]]]:
    """Preflight a plugin.

    Returns its config, manifest, resolved commit SHA and the preflighted
    secondary plugins (each with its own ``resolved_ref``).
    """
    allow_local_sources = _allow_local_plugin_sources()
    try:
        resolved_ref, manifest = preflight_manifest(
            plugin, allow_local_sources=allow_local_sources
        )
        required_plugins = validate_required_plugins(
            manifest.runtime.plugins,
            allow_local_sources=allow_local_sources,
        )
        resolved_config = validate_plugin_config(manifest, plugin_config)
    except PluginSpecError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return resolved_config, manifest, resolved_ref, required_plugins


def _profile_requirements_signature(manifest: Any) -> dict[str, Any]:
    agent_profile = manifest.runtime.agent_profile
    llm_profile = manifest.runtime.llm_profile
    return {
        "agent_profile": agent_profile.model_dump(exclude_none=True)
        if agent_profile is not None
        else None,
        "llm_profile": llm_profile.model_dump(exclude_none=True)
        if llm_profile is not None
        else None,
    }


def _plugin_runtime_fields(
    *,
    plugin: AutomationPluginSource,
    plugin_config: dict[str, Any],
    manifest: Any,
    model: str | None,
    agent_profile_id: uuid.UUID | None,
    pinned_ref: str | None = None,
    pinned_required_plugins: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Automation fields derived from a plugin spec.

    ``pinned_ref`` (the resolved commit SHA) replaces the plugin's ref in the
    stored spec, so every run fetches exactly that commit.
    ``pinned_required_plugins`` (the preflighted secondary plugins) is stored
    with each ref replaced by its resolved SHA; runs fetch those instead of
    re-resolving the manifest's secondary plugins.
    """
    if pinned_ref:
        plugin = plugin.model_copy(update={"ref": pinned_ref})
    pins: dict[str, Any] = {}
    if pinned_required_plugins is not None:
        pins["required_plugins"] = [
            {
                **{k: v for k, v in item.items() if k != "resolved_ref"},
                **({"ref": item["resolved_ref"]} if item.get("resolved_ref") else {}),
            }
            for item in pinned_required_plugins
        ]
    return {
        "plugin_spec": {
            "plugin": plugin.model_dump(exclude_none=True),
            "config": plugin_config,
            **pins,
        },
        "manifest": manifest,
        "model": model,
        "agent_profile_id": agent_profile_id,
        "tarball_path": "plugin-spec://runtime",
        "setup_script_path": setup_script_for_manifest(manifest),
        "entrypoint": entrypoint_for_manifest(manifest),
    }


async def _resolve_plugin_spec_trigger(
    *,
    requested_trigger: Any,
    manifest: Any,
    plugin_config: dict[str, Any],
    user: AuthenticatedUser,
    session: AsyncSession,
) -> Any:
    try:
        trigger = requested_trigger or resolve_suggested_trigger(
            manifest, plugin_config
        )
    except PluginSpecError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if trigger is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Plugin automation requires a trigger or manifest suggested_trigger",
        )
    if (
        isinstance(trigger, EventTrigger)
        and not is_builtin_source(trigger.source)
        and not await is_event_source_configured(trigger.source, user.org_id, session)
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Event source `{trigger.source}` is not configured",
        )
    return trigger


async def _resolve_plugin_spec_runtime_fields(
    *,
    plugin: AutomationPluginSource,
    plugin_config: dict[str, Any] | None,
    request: Request,
    user: AuthenticatedUser,
    pin_ref: bool = False,
) -> dict[str, Any]:
    """Validate a plugin spec and return automation fields derived from it.

    ``pin_ref`` stores the resolved commit SHA as the plugin ref, for a
    service-account-owned automation: a moving branch or tag would otherwise
    let anyone who can push to the plugin repo change code that runs as the
    service account, with no edit visible here. The manifest's secondary
    plugins are pinned the same way.
    """
    plugin_config, manifest, resolved_ref, required_plugins = (
        _fetch_validated_plugin_manifest(plugin, plugin_config)
    )

    agent_profile_name = (
        manifest.runtime.agent_profile.default_name
        if manifest.runtime.agent_profile is not None
        else None
    )
    has_agent_profile = bool(agent_profile_name)
    if (
        manifest.runtime.agent_profile is not None
        and manifest.runtime.agent_profile.required
        and not agent_profile_name
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Plugin automation requires an agent profile selection",
        )

    if has_agent_profile:
        model = None
    else:
        llm_default = (
            manifest.runtime.llm_profile.default_name
            if manifest.runtime.llm_profile is not None
            else None
        )
        if manifest.runtime.llm_profile is not None and (
            manifest.runtime.llm_profile.required and not llm_default
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Plugin automation requires an LLM profile",
            )
        model = (
            resolve_model_profile_for_user(llm_default, user) if llm_default else None
        )

    agent_profile_requirement = manifest.runtime.agent_profile
    agent_profile_id = await validate_agent_profile_allowlists(
        None,
        agent_profile_name,
        agent_profile_requirement.allowed_tools
        if agent_profile_requirement is not None
        else None,
        request,
        user,
        allowed_mcp_server_refs=agent_profile_requirement.allowed_mcp_server_refs
        if agent_profile_requirement is not None
        else None,
        allowed_secret_refs=agent_profile_requirement.allowed_secret_refs
        if agent_profile_requirement is not None
        else None,
    )

    return _plugin_runtime_fields(
        plugin=plugin,
        plugin_config=plugin_config,
        manifest=manifest,
        model=model,
        agent_profile_id=agent_profile_id,
        pinned_ref=resolved_ref if pin_ref else None,
        pinned_required_plugins=required_plugins if pin_ref else None,
    )


def _resolve_plugin_spec_update_fields_reusing_existing_profiles(
    *,
    plugin: AutomationPluginSource,
    automation: Automation,
) -> dict[str, Any]:
    """Resolve a plugin update without rereading agent profile details.

    This fallback supports Bearer API-key clients when the OpenHands app server
    accepts the key for `/users/me` but refuses the agent-profile API. It is safe
    only when the new manifest keeps the same profile requirements as the
    existing plugin spec; otherwise the selected profile must be revalidated.
    """
    existing_plugin_spec = automation.plugin_spec
    old_plugin_payload = (
        existing_plugin_spec.get("plugin")
        if isinstance(existing_plugin_spec, dict)
        else None
    )
    if not isinstance(existing_plugin_spec, dict) or not isinstance(
        old_plugin_payload, dict
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Existing plugin-spec automation is missing plugin source metadata",
        )
    requested_config = existing_plugin_spec.get("config")
    old_plugin = AutomationPluginSource.model_validate(old_plugin_payload)
    _old_config, old_manifest, _old_ref, _old_required = (
        _fetch_validated_plugin_manifest(old_plugin, requested_config)
    )
    plugin_config, manifest, resolved_ref, required_plugins = (
        _fetch_validated_plugin_manifest(plugin, requested_config)
    )
    if _profile_requirements_signature(manifest) != _profile_requirements_signature(
        old_manifest
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            (
                "OpenHands API refused the request for agent profiles, and the "
                "new plugin manifest changes profile requirements"
            ),
        )
    is_service_owned = automation.owner_kind == AutomationOwnerKind.SERVICE
    return _plugin_runtime_fields(
        plugin=plugin,
        plugin_config=plugin_config,
        manifest=manifest,
        model=automation.model,
        agent_profile_id=automation.agent_profile_id,
        pinned_ref=resolved_ref if is_service_owned else None,
        pinned_required_plugins=required_plugins if is_service_owned else None,
    )


@router.get("")
async def list_automations(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    created_by: Literal["me", "others"] | None = Query(default=None),
    tag: str | None = Query(
        default=None, description="Only automations carrying this tag"
    ),
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> AutomationListResponse:
    """List automations for the caller's org (excludes soft-deleted)."""
    base_query = select(Automation).where(
        Automation.org_id == user.org_id,
        Automation.deleted_at.is_(None),
    )
    if created_by == "me":
        base_query = base_query.where(Automation.user_id == user.user_id)
    elif created_by == "others":
        base_query = base_query.where(Automation.user_id != user.user_id)
    if tag is not None:
        try:
            base_query = base_query.where(has_tag(normalize_tag(tag)))
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc

    count_result = await session.execute(
        select(func.count()).select_from(base_query.subquery())
    )
    total = count_result.scalar() or 0

    # id breaks created_at ties (e.g. one Git Sync import), so offset pages
    # keep one order across requests.
    result = await session.execute(
        base_query.order_by(Automation.created_at.desc(), Automation.id.desc())
        .offset(offset)
        .limit(limit)
    )
    automations = result.scalars().all()

    return AutomationListResponse(
        automations=[AutomationResponse.model_validate(a) for a in automations],
        total=total,
    )


# Before /{automation_id}, which would otherwise take "tags" as an id.
@router.get("/tags")
async def list_automation_tags(
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> AutomationTagsResponse:
    """Tags on the caller's org's live automations, most-used first."""
    counts = await count_org_tags(session, user.org_id)
    return AutomationTagsResponse(
        tags=[AutomationTagCount(tag=tag, count=count) for tag, count in counts]
    )


@router.get("/{automation_id}")
async def get_automation(
    automation_id: uuid.UUID,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
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
    session: AsyncSession = Depends(get_session, scope="function"),
) -> AutomationResponse:
    """Partially update an automation.

    Only the creator may edit the definition. Admins and owners may turn an
    automation off but cannot reactivate it or change what it runs, except for
    an automation owned by a service account, which they may edit and enable.
    """
    auto = await _get_org_automation(session, automation_id, user.org_id)
    await _assert_can_manage(auto, user)

    update_data = body.model_dump(exclude_unset=True)
    _assert_can_update_fields(auto, user, update_data)
    # Same ownership check as create. Only a new value is checked: resending the
    # stored path cannot grant anything the row does not already have, and that
    # upload need not be owned by the editor or still be live.
    requested_tarball_path = update_data.get("tarball_path")
    if (
        requested_tarball_path is not None
        and requested_tarball_path != auto.tarball_path
    ):
        await validate_tarball_path(
            tarball_path=requested_tarball_path,
            user_id=user.user_id,
            org_id=user.org_id,
            session=session,
        )
        if auto.owner_kind == AutomationOwnerKind.SERVICE:
            # Validated against the caller above; the service account owns it
            # from now on, as on create.
            await assign_upload_to_owner(
                session,
                requested_tarball_path,
                user.org_id,
                ResolvedOwner(auto.user_id, auto.owner_kind),
            )
    # Repositories are not a column: a preset bakes them into its tarball and
    # records them in preset_metadata, so they are applied with the prompt below.
    repos_requested = "repos" in update_data
    update_data.pop("repos", None)
    if repos_requested and (auto.preset_metadata or {}).get("preset_type") not in (
        "prompt",
        "plugin",
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Only prompt and plugin preset automations can update repos",
        )
    plugin_update_requested = "plugin" in update_data
    if plugin_update_requested:
        if update_data["plugin"] is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "plugin cannot be null",
            )
        if not auto.plugin_spec or auto.tarball_path != "plugin-spec://runtime":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Only plugin-spec automations can update plugin source",
            )
        runtime_overrides = sorted(
            set(update_data)
            & {
                "agent_profile_id",
                "model",
                "prompt",
                "tarball_path",
                "setup_script_path",
                "entrypoint",
            }
        )
        if runtime_overrides:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                (
                    "Plugin-spec automations derive runtime fields from the "
                    "plugin manifest; remove: " + ", ".join(runtime_overrides)
                ),
            )
        plugin_source = AutomationPluginSource.model_validate(update_data.pop("plugin"))
        try:
            plugin_runtime_fields = await _resolve_plugin_spec_runtime_fields(
                plugin=plugin_source,
                plugin_config=auto.plugin_spec.get("config")
                if auto.plugin_spec
                else None,
                request=request,
                user=user,
                pin_ref=auto.owner_kind == AutomationOwnerKind.SERVICE,
            )
        except HTTPException as exc:
            if (
                exc.status_code
                not in (
                    status.HTTP_401_UNAUTHORIZED,
                    status.HTTP_403_FORBIDDEN,
                )
                or exc.detail != "OpenHands API refused the request for agent profiles"
            ):
                raise
            plugin_runtime_fields = (
                _resolve_plugin_spec_update_fields_reusing_existing_profiles(
                    plugin=plugin_source,
                    automation=auto,
                )
            )
        plugin_runtime_fields.pop("manifest", None)
        update_data.update(plugin_runtime_fields)

    # Handle trigger field mapping (only if trigger has a real value)
    if body.trigger is not None:
        update_data["trigger"] = body.trigger.model_dump()

    requested_state = update_data.pop("state", None)
    if requested_state is not None:
        state = model_automation_state(
            requested_state, update_data.get("enabled", auto.enabled)
        )
        if (
            state == ModelAutomationState.DRAFT
            and auto.state != ModelAutomationState.DRAFT
        ):
            raise HTTPException(
                status_code=400,
                detail="Existing automations cannot be moved to draft state",
            )
        update_data["state"] = state
        update_data["enabled"] = automation_state_enabled(state)
    elif "enabled" in update_data:
        update_data["state"] = model_automation_state(None, update_data["enabled"])

    if auto.state == ModelAutomationState.DRAFT and (
        "state" in update_data or "enabled" in update_data
    ):
        await _assert_normal_api_can_use_draft_artifact(session, auto)
    elif update_data.get("enabled") is True:
        await _assert_normal_api_can_use_draft_artifact(session, auto)

    # Same rule CreateAutomationRequest enforces, applied to the merged view:
    # either half of the pair can arrive alone in a partial update.
    trigger = update_data.get("trigger") or auto.trigger or {}
    if trigger.get("destination") == "continue_conversation":
        keep_alive = update_data.get("keep_alive", auto.keep_alive)
        if keep_alive is False:
            raise HTTPException(
                status_code=400,
                detail=(
                    "keep_alive cannot be false when destination is "
                    "'continue_conversation': deleting the sandbox would "
                    "destroy the conversation the next event continues"
                ),
            )
        if keep_alive is not True:
            update_data["keep_alive"] = True

    disable_event: AutomationDisableEvent | None = None
    skip_pending_reason: str | None = None
    if update_data.get("enabled") is True:
        update_data["disabled_reason"] = None
        update_data["disabled_detail"] = None
        update_data["disabled_at"] = None
    elif update_data.get("enabled") is False:
        state = update_data.get("state")
        is_manual_inactive = state == ModelAutomationState.INACTIVE or (
            state is None and auto.state != ModelAutomationState.DRAFT
        )
        skip_pending_reason = (
            "Automation moved to draft by user"
            if state == ModelAutomationState.DRAFT
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

    if not plugin_update_requested and (
        "agent_profile_id" in update_data or "model" in update_data
    ):
        selected_profile = update_data.get("agent_profile_id", auto.agent_profile_id)
        validate_agent_profile_combination(selected_profile, body.model)
        if selected_profile != auto.agent_profile_id:
            # Only a newly selected profile is looked up: an update that sends
            # the current one back must not start failing because the profile
            # was deleted since, or the OpenHands API is down.
            await ensure_agent_profile_exists(selected_profile, request, user)
        if selected_profile:
            update_data["model"] = None
        elif "model" in update_data:
            update_data["model"] = resolve_model_profile_for_user(body.model, user)

    original_prompt = auto.prompt
    for field, value in update_data.items():
        setattr(auto, field, value)

    # A preset automation bakes its prompt and repositories into the tarball the
    # dispatcher executes; the `prompt` column and preset metadata are a record
    # only. When either actually changes, rebuild the tarball once so the next
    # dispatch runs the edit instead of the original baked files. Skipped when
    # the values are unchanged (a no-op edit), or for non-preset automations.
    prompt_changed = (
        "prompt" in update_data
        and isinstance(auto.prompt, str)
        and auto.prompt != original_prompt
    )
    new_repos = dump_repos(body.repos)
    repos_changed = repos_requested and new_repos != (
        (auto.preset_metadata or {}).get("repos") or []
    )
    files: dict[str, str | None] = {}
    if prompt_changed:
        files["prompt.txt"] = auto.prompt
    if repos_changed:
        files["repos_config.json"] = repos_config_json(new_repos) if new_repos else None
    if files:
        new_tarball_path = await regenerate_preset_tarball(
            auto, files, session, background_tasks
        )
        if new_tarball_path is not None:
            auto.tarball_path = new_tarball_path
        elif repos_changed:
            # Unlike the prompt column, recorded repositories that the tarball
            # does not clone would claim a checkout the next run never makes.
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "This automation's repositories cannot be changed: its preset "
                "tarball could not be rebuilt",
            )
        # Keep preset metadata in sync with the edit. Reassign the whole dict:
        # in-place mutation of a JSON column is not change-tracked.
        if auto.preset_metadata is not None:
            metadata = dict(auto.preset_metadata)
            if prompt_changed:
                metadata["prompt"] = auto.prompt
            if repos_changed:
                if new_repos:
                    metadata["repos"] = new_repos
                else:
                    metadata.pop("repos", None)
            auto.preset_metadata = metadata

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
    updated_fields = sorted(
        update_data.keys() | ({"repos"} if repos_requested else set())
    )
    record_change(session, auto, user, _update_action(update_data), updated_fields)
    await capture_automation_event(
        "automation_updated",
        request=request,
        user=user,
        automation=auto,
        properties={"updated_fields": updated_fields},
    )
    return AutomationResponse.model_validate(auto)


@router.delete("/{automation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_automation(
    automation_id: uuid.UUID,
    request: Request,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> None:
    """Soft delete an automation."""
    auto = await _get_org_automation(session, automation_id, user.org_id)
    await _assert_can_manage(auto, user)
    was_enabled = auto.enabled
    auto.enabled = False
    auto.state = ModelAutomationState.INACTIVE
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
    record_change(session, auto, user, "delete", [])
    await capture_automation_event(
        "automation_deleted",
        request=request,
        user=user,
        automation=auto,
    )


async def _pin_plugins_for_transfer(
    auto: Automation,
    session: AsyncSession,
    background_tasks: BackgroundTasks,
) -> None:
    """Pin the plugin refs of ``auto`` to commit SHAs for a service account.

    An automation runs the plugin code its ref points at. Under a service
    account that code would change, with no edit visible here, whenever anyone
    pushes to the plugin repo. Create and update pin the refs for a
    service-account-owned automation; a human-owned one may hold branch refs,
    so the transfer pins them the same way:

    - plugin spec: the manifest is resolved as on create, and the resolved SHAs
      of the plugin and its secondary plugins replace the stored refs.
    - plugin preset: each ref in ``preset_metadata`` is resolved, and the
      tarball's ``plugins_config.json`` is rewritten with the SHAs.

    Raises:
        HTTPException: 422 when a plugin cannot be fetched or validated, or
            when its refs cannot be checked (experiment variants keep their
            plugins only inside the tarball).
    """
    if auto.plugin_spec:
        plugin = AutomationPluginSource.model_validate(auto.plugin_spec.get("plugin"))
        config, manifest, resolved_ref, required_plugins = (
            _fetch_validated_plugin_manifest(plugin, auto.plugin_spec.get("config"))
        )
        auto.plugin_spec = _plugin_runtime_fields(
            plugin=plugin,
            plugin_config=config,
            manifest=manifest,
            model=None,
            agent_profile_id=None,
            pinned_ref=resolved_ref,
            pinned_required_plugins=required_plugins,
        )["plugin_spec"]
    elif (auto.preset_metadata or {}).get("preset_type") == "plugin":
        plugins = (auto.preset_metadata or {}).get("plugins")
        if plugins is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Plugin refs cannot be checked for this automation, so it "
                "cannot be transferred to a service account",
            )
        # A full SHA is already pinned; any other ref is resolved by lookup.
        pinned = []
        for item in plugins:
            if _COMMIT_SHA_RE.fullmatch(item.get("ref") or ""):
                pinned.append(item)
                continue
            try:
                [resolved] = validate_required_plugins(
                    [AutomationPluginSource.model_validate(item)],
                    allow_local_sources=_allow_local_plugin_sources(),
                )
            except PluginSpecError as exc:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)
                ) from exc
            ref = resolved.pop("resolved_ref", None)
            pinned.append({**resolved, **({"ref": ref} if ref else {})})
        _require_pinned_plugin_refs(
            [PluginSource.model_validate(item) for item in pinned], None
        )
        if pinned == plugins:
            return
        new_tarball_path = await regenerate_preset_tarball(
            auto,
            {"plugins_config.json": json.dumps(pinned, indent=2)},
            session,
            background_tasks,
        )
        if new_tarball_path is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="The plugin refs of this automation cannot be pinned, so it "
                "cannot be transferred to a service account",
            )
        auto.tarball_path = new_tarball_path
        auto.preset_metadata = {**(auto.preset_metadata or {}), "plugins": pinned}


@router.post("/{automation_id}/transfer")
async def transfer_automation(
    automation_id: uuid.UUID,
    body: TransferAutomationRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> AutomationResponse:
    """Move an automation to a service account of the org, or back to the caller.

    Only holders of ``manage_automations`` may transfer: to a service account
    (``to: service``) or to themselves (``to: self``). The automation keeps its
    id and KV store, but ends INACTIVE: a person must re-enable it under the new
    owner. Refused (409) while a run is PENDING or RUNNING or the automation is
    a draft.

    Conversation ids of subject-keyed runs include the owner for a
    service-owned automation (``subjects.conversation_id_for``): after a
    transfer to a service account, new events start new conversations, and a
    transfer back to a user restores the old ids.

    Plugin refs are pinned to commit SHAs on a transfer to a service account
    (see ``_pin_plugins_for_transfer``).

    Known limits:

    - ``keep_alive`` and deferred-cleanup sandboxes of runs that already
      finished are cleaned up with the NEW owner's key after a transfer. They
      can outlive the transfer until the runtime TTL.
    - The live-run check and the commit are not under a row lock. A run
      dispatched in between can finish under the old owner's key and get 403 on
      its callbacks, until the watchdog times it out.
    """
    auto = await _get_org_automation(session, automation_id, user.org_id)
    if body.to == "self":
        if "manage_automations" not in user.permissions:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail="Only admins and owners can take ownership of an automation",
            )
        new_owner = ResolvedOwner(user.user_id, AutomationOwnerKind.USER)
    else:
        require_service_owner_permission(
            user,
            "Only admins and owners can transfer an automation to a service account",
        )
        assert body.service_account_id is not None  # guaranteed by the schema
        new_owner = ResolvedOwner(body.service_account_id, AutomationOwnerKind.SERVICE)

    if (auto.user_id, auto.owner_kind) == (new_owner.user_id, new_owner.kind):
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="The automation already has this owner"
        )
    if auto.state == ModelAutomationState.DRAFT:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="A draft automation cannot be transferred"
        )
    live_run = await session.scalar(
        select(AutomationRun.id)
        .where(
            AutomationRun.automation_id == auto.id,
            AutomationRun.status.in_(
                (AutomationRunStatus.PENDING, AutomationRunStatus.RUNNING)
            ),
        )
        .limit(1)
    )
    if live_run is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="The automation has a pending or running run; wait for it to "
            "finish or cancel it",
        )

    if new_owner.kind == AutomationOwnerKind.SERVICE:
        await verify_service_account(
            new_owner.user_id,
            user.org_id,
            get_http_client(request),
            field="service_account_id",
        )
        await _pin_plugins_for_transfer(auto, session, background_tasks)

    now = utcnow()
    disabled_detail = {"reason": "owner_changed", "source": "transfer"}
    auto.user_id = new_owner.user_id
    auto.owner_kind = new_owner.kind
    auto.enabled = False
    auto.state = ModelAutomationState.INACTIVE
    auto.disabled_reason = "owner_changed"
    auto.disabled_detail = disabled_detail
    auto.disabled_at = now
    session.add(
        AutomationDisableEvent(
            automation_id=auto.id,
            reason="owner_changed",
            detail=disabled_detail,
            source="transfer",
        )
    )
    # No run is live (409 above); this covers one accepted since that check.
    await skip_pending_runs_for_disabled_automation(
        session,
        auto.id,
        reason="Automation transferred to a new owner",
        disabled_detail=disabled_detail,
        completed_at=now,
        include_manual=True,
    )
    # Conversations started under the old owner must not be continued as the new
    # one: stop routing events to them. The run rows keep their subject keys.
    await session.execute(
        update(AutomationRun)
        .where(
            AutomationRun.automation_id == auto.id,
            AutomationRun.subject_key.isnot(None),
            AutomationRun.subject_released_at.is_(None),
        )
        .values(subject_released_at=now)
    )
    await assign_upload_to_owner(session, auto.tarball_path, user.org_id, new_owner)
    await session.flush()
    await session.refresh(auto)
    await mark_git_sync_dirty(session, auto)
    record_change(
        session,
        auto,
        user,
        "transfer",
        [
            "user_id",
            "owner_kind",
            "enabled",
            "state",
            "disabled_reason",
            "disabled_detail",
            "disabled_at",
        ],
    )
    await capture_automation_event(
        "automation_transferred",
        request=request,
        user=user,
        automation=auto,
        properties={"to": body.to},
    )
    return AutomationResponse.model_validate(auto)


@router.get("/{automation_id}/tarball")
async def download_automation_tarball(
    automation_id: uuid.UUID,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
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
        upload = await get_org_upload(session, upload_id, auto.org_id)
        if upload is None or upload.deleted_at is not None:
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
        unsafe_chars = r'[\x00-\x1f\x7f"\\\/]'
        safe_name = re.sub(unsafe_chars, "", auto.name) or "automation"
        if safe_name.isascii():
            disposition = f'attachment; filename="{safe_name}.tar"'
        else:
            # NFKD keeps accented letters' base form ("Café" -> "Cafe"), but it
            # also maps compatibility characters onto removed ones (U+FF02 ->
            # '"'), so sanitize again before dropping what is still non-ASCII.
            normalized = re.sub(
                unsafe_chars, "", unicodedata.normalize("NFKD", safe_name)
            )
            ascii_name = normalized.encode("ascii", errors="ignore").decode()
            fallback = " ".join(ascii_name.split()) or "automation"
            disposition = (
                f'attachment; filename="{fallback}.tar"; '
                f"filename*=UTF-8''{quote(safe_name + '.tar', safe='')}"
            )
        return Response(
            content=data,
            media_type="application/x-tar",
            headers={"Content-Disposition": disposition},
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
    session: AsyncSession = Depends(get_session, scope="function"),
) -> AutomationRunResponse:
    """Manually dispatch an automation run.

    Creates a PENDING run for the specified automation, which will be
    picked up by the dispatcher and executed.
    """
    auto = await _get_org_automation(session, automation_id, user.org_id)
    await _assert_can_manage(auto, user)
    await _assert_normal_api_can_use_draft_artifact(session, auto)

    telemetry_context = get_request_telemetry_context(request)
    with span(
        "automation.manual_dispatch.receive",
        automation_attributes(
            auto,
            None,
            **{"automation.run.trigger_source": "manual"},
        ),
    ):
        run = await create_pending_run(
            session,
            auto,
            telemetry_distinct_id=telemetry_context.frontend_distinct_id,
            trigger_source="manual",
            observability_parent_span_context=current_span_context(),
        )
        run_created_attributes = automation_attributes(auto, run)
        with span("automation.route.run_created", run_created_attributes):
            add_event("automation.route.run_created", run_created_attributes)
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
    session: AsyncSession = Depends(get_session, scope="function"),
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


@router.get("/{automation_id}/changes")
async def list_automation_changes(
    automation_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> AutomationChangeListResponse:
    """List who changed an automation and which fields, latest first.

    Field names only, never values. A soft-deleted automation is a 404, so its
    history is not readable through the API.
    """
    await _get_org_automation(session, automation_id, user.org_id)

    total = await session.scalar(
        select(func.count())
        .select_from(AutomationChangeEvent)
        .where(AutomationChangeEvent.automation_id == automation_id)
    )
    result = await session.execute(
        select(AutomationChangeEvent)
        .where(AutomationChangeEvent.automation_id == automation_id)
        # id breaks ties: rows of one transaction share a timestamp, and SQLite
        # stores whole seconds, so paging would otherwise be unstable.
        .order_by(
            AutomationChangeEvent.created_at.desc(), AutomationChangeEvent.id.desc()
        )
        .offset(offset)
        .limit(limit)
    )
    return AutomationChangeListResponse(
        changes=[AutomationChangeResponse.model_validate(e) for e in result.scalars()],
        total=total or 0,
    )


# --- Run completion callback ---


@router.post("/runs/{run_id}/complete")
async def complete_run(
    run_id: uuid.UUID,
    body: RunCompleteRequest,
    request: Request,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
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
    status, or -- when ``sandbox_cleanup_delay_seconds`` is set -- pauses it
    and leaves deletion to the watchdog once the delay has passed. When
    post-run callbacks are configured, cleanup will happen after callbacks
    instead. keep_alive=true leaves cleanup to the runtime TTL reaper.
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
    from openhands.automation.config import get_settings

    settings = get_settings()
    cleanup_delay = settings.sandbox_cleanup_delay_seconds
    # Same gate as the cleanup branch below; a delay only replaces deletion
    # with a pause and stamps when the watchdog should delete instead.
    defer_cleanup = (
        cleanup_delay > 0 and bool(run.sandbox_id) and automation.keep_alive is not True
    )
    new_status = (
        AutomationRunStatus.COMPLETED
        if body.status == "COMPLETED"
        else AutomationRunStatus.FAILED
    )
    values: dict = {
        "status": new_status,
        "completed_at": now,
    }
    if defer_cleanup:
        values["sandbox_cleanup_due_at"] = now + timedelta(seconds=cleanup_delay)
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
    callback_attributes = automation_attributes(
        automation,
        run,
        **{
            "automation.conversation_id": body.conversation_id,
            "openhands.conversation_id": body.conversation_id,
            "automation.callback.reconciled_watchdog_timeout": reconciled,
        },
    )
    with span(
        "automation.callback.received",
        callback_attributes,
        parent_span_context=request.headers.get(
            "X-OpenHands-Observability-Parent-Span-Context"
        ),
    ):
        add_event(
            "automation.run.completed"
            if new_status == AutomationRunStatus.COMPLETED
            else "automation.run.failed",
            callback_attributes,
        )
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
    #
    # A `continue_conversation` automation is forced keep_alive at creation, so
    # deleting the sandbox out from under its conversation is already excluded
    # here without a second condition.
    if run.sandbox_id and automation.keep_alive is not True:
        # Fire-and-forget sandbox deletion in background
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
            if cleanup_delay > 0:
                # Deferred: pause now so the conversation stays resumable; the
                # watchdog deletes the sandbox at sandbox_cleanup_due_at.
                asyncio.create_task(
                    pause_sandbox(
                        api_url=settings.openhands_api_base_url,
                        api_key=api_key,
                        sandbox_id=run.sandbox_id,
                        run_id=str(run_id),
                    )
                )
            else:
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
    session: AsyncSession = Depends(get_session, scope="function"),
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
    session: AsyncSession = Depends(get_session, scope="function"),
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

    # Clean up sandbox for runs that were RUNNING. Cancelling is explicit, so
    # unlike `complete_run` the sandbox goes even when the run owns a subject
    # -- but the subject is released with it, or the next event would pick this
    # run and pay a lookup for a sandbox we just deleted. The key stays on the
    # row as the record of what this run was about.
    if run.sandbox_id:
        if run.subject_key and run.subject_released_at is None:
            run.subject_released_at = utcnow()
            await session.commit()

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
