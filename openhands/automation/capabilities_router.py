"""Capability discovery and preflight validation for automation setup UIs.

A setup UI needs two answers before it creates anything: what this deployment
supports, so it never offers an option that cannot work, and whether a draft is
acceptable, so a bad configuration is caught before an automation exists.
Neither endpoint writes.
"""

import fnmatch
import logging
import uuid
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.auth import AuthenticatedUser, authenticate_request
from openhands.automation.config import get_config
from openhands.automation.db import get_session
from openhands.automation.event_schemas import parse_event
from openhands.automation.event_schemas.github import (
    get_supported_event_patterns,
    get_supported_event_types,
)
from openhands.automation.filter_eval import FilterFunctions
from openhands.automation.models import CustomWebhook
from openhands.automation.preset_router import (
    CreatePluginAutomationRequest,
    CreatePromptAutomationRequest,
)
from openhands.automation.providers import builtin_sources
from openhands.automation.scheduler import POLL_INTERVAL_SECONDS
from openhands.automation.schemas import (
    CapabilitiesResponse,
    CreateAutomationRequest,
    CronCapabilities,
    CronTrigger,
    DraftValidationError,
    EventCapabilities,
    EventTrigger,
    TriggerCapabilities,
    ValidateDraftRequest,
    ValidateDraftResponse,
)
from openhands.automation.trigger_matcher import matches_trigger
from openhands.automation.utils.cron import min_interval_seconds
from openhands.automation.utils.model_profiles import validate_model_profile_for_user
from openhands.automation.utils.webhook import get_webhook_config


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Capabilities"])

DraftModel = (
    CreateAutomationRequest
    | CreatePromptAutomationRequest
    | CreatePluginAutomationRequest
)

# Draft models keyed by the endpoint they would be posted to. Validating with
# the model creation itself uses is what keeps preflight from drifting.
# "/v1" is the raw create path, used by an entry shipping its own tarball. Its
# tarball_path is checked for scheme here and for ownership only at creation:
# preflight validates the body, not the upload behind it.
_DRAFT_MODELS: dict[str, type[DraftModel]] = {
    "/v1": CreateAutomationRequest,
    "/v1/preset/prompt": CreatePromptAutomationRequest,
    "/v1/preset/plugin": CreatePluginAutomationRequest,
}

# Features every deployment has: they come from the SDK code the service
# packages into a run, not from configuration.
_STATIC_FEATURES = (
    "conversationDispatch",
    # Can run a client-supplied tarball, so an entry may ship a script bundle.
    "customTarball",
    "mcpTools",
    "presetPlugin",
    "presetPrompt",
    "repoClone",
)

# Tags Pydantic inserts into an error location for the trigger union.
_TRIGGER_TAGS = frozenset({"cron", "event"})


@router.get("/capabilities", response_model_exclude_none=True)
async def get_capabilities(
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> CapabilitiesResponse:
    """Describe what this deployment supports, before anything is configured.

    Custom webhook sources are organization-scoped, so this response describes
    what the calling organization can do here.
    """
    config = get_config()

    # In cloud mode every run needs a minted API key, which needs the service
    # key. Without it the service accepts work it cannot execute, so it offers
    # nothing rather than letting a setup form proceed into certain failure.
    if not (config.service.is_local_mode or config.service.service_key):
        return CapabilitiesResponse(
            ready=False,
            max_automation_timeout_seconds=config.sandbox.max_run_duration,
            trigger_kinds=[],
            event_sources=[],
            event_types=[],
            triggers=TriggerCapabilities(),
            features=[],
        )

    builtin = builtin_sources() if config.service.webhook_secret else []
    event_sources = sorted({*builtin, *await _custom_sources(user.org_id, session)})

    features = [*_STATIC_FEATURES]
    if event_sources:
        features.append("webhookDelivery")
    if config.kv.enabled:
        features.append("kvStore")

    return CapabilitiesResponse(
        ready=True,
        max_automation_timeout_seconds=config.sandbox.max_run_duration,
        trigger_kinds=["cron", "event"] if event_sources else ["cron"],
        event_sources=event_sources,
        event_types=(get_supported_event_patterns() if "github" in builtin else []),
        triggers=TriggerCapabilities(
            cron=CronCapabilities(
                min_interval_seconds=_cron_interval_floor(),
                timezones=sorted(available_timezones()),
            ),
            event=(
                EventCapabilities(
                    filter_functions=sorted(FilterFunctions.FUNCTION_TABLE)
                )
                if event_sources
                else None
            ),
        ),
        features=sorted(features),
    )


@router.post("/validate")
async def validate_draft(
    body: ValidateDraftRequest,
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> ValidateDraftResponse:
    """Validate a draft automation without creating it.

    An invalid draft is still a successful validation: the response is 200 with
    `valid` false and one error per problem, each addressed to the field that
    caused it. Only a malformed request envelope is a 4xx.
    """
    logger.info(
        "Validating draft for %s (automation_id=%s)", body.endpoint, body.automation_id
    )

    try:
        draft = _DRAFT_MODELS[body.endpoint].model_validate(body.draft)
    except ValidationError as e:
        return ValidateDraftResponse(valid=False, errors=_schema_errors(e))

    errors: list[DraftValidationError] = []
    sample_event_matched: bool | None = None

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
            if body.sample_event is not None:
                try:
                    event = parse_event(
                        trigger.source,
                        body.sample_event,
                        event_key_expr=webhook.event_key_expr,
                    )
                except ValueError as e:
                    errors.append(
                        DraftValidationError(
                            field="sampleEvent",
                            code="unparseable_sample_event",
                            message=str(e),
                        )
                    )
                else:
                    sample_event_matched = matches_trigger(
                        trigger, trigger.source, event.event_key, body.sample_event
                    )

    return ValidateDraftResponse(
        valid=not errors,
        errors=errors,
        sample_event_matched=sample_event_matched,
    )


async def _custom_sources(org_id: uuid.UUID, session: AsyncSession) -> list[str]:
    """Custom webhook sources this organization has enabled.

    These carry their own per-webhook secret, so they work whether or not the
    service-level webhook secret for built-in sources is configured.
    """
    result = await session.execute(
        select(CustomWebhook.source)
        .where(CustomWebhook.org_id == org_id, CustomWebhook.enabled.is_(True))
        .distinct()
    )
    return list(result.scalars().all())


def _cron_interval_floor() -> int:
    """Shortest interval between fires the scheduler can actually honour."""
    return max(POLL_INTERVAL_SECONDS, get_config().service.scheduler_interval_seconds)


def _cron_errors(trigger: CronTrigger) -> list[DraftValidationError]:
    """Reject a schedule that asks to fire faster than the scheduler polls."""
    floor = _cron_interval_floor()
    if min_interval_seconds(trigger.schedule) >= floor:
        return []
    return [
        DraftValidationError(
            field="trigger.schedule",
            code="interval_too_short",
            message=(
                "This deployment fires an automation at most once every "
                f"{floor} seconds."
            ),
        )
    ]


def _event_type_errors(trigger: EventTrigger) -> list[DraftValidationError]:
    """Reject event keys this deployment cannot parse.

    Only sources publishing a known catalog can be checked. A custom webhook's
    event keys are whatever its own expression extracts, so anything matches.
    """
    if trigger.source != "github":
        return []

    supported = get_supported_event_types()
    return [
        DraftValidationError(
            field="trigger.on",
            code="event_type_not_delivered",
            message=f"This deployment cannot parse '{pattern}' events from GitHub.",
        )
        for pattern in trigger.event_patterns
        if not any(
            # Compare event types with the pattern's own wildcards, the way the
            # trigger matcher compares a delivered event key.
            fnmatch.fnmatch(event_type, pattern.split(".", 1)[0])
            for event_type in supported
        )
    ]


def _schema_errors(error: ValidationError) -> list[DraftValidationError]:
    """Translate Pydantic's report into field-addressed errors."""
    return [
        DraftValidationError(
            field=_error_field(item["loc"]),
            code=item["type"],
            message=item["msg"],
        )
        for item in error.errors()
    ]


def _error_field(loc: tuple[int | str, ...]) -> str | None:
    """Render a Pydantic error location as a dotted path into the draft.

    List positions become ``repos[0]``, and the tag Pydantic inserts for the
    discriminated trigger union is dropped so the path names only fields the
    caller actually sent. A whole-draft error has no path at all.
    """
    parts: list[str] = []
    for index, segment in enumerate(loc):
        if isinstance(segment, int):
            if parts:
                parts[-1] += f"[{segment}]"
            continue
        if index and loc[index - 1] == "trigger" and segment in _TRIGGER_TAGS:
            continue
        parts.append(segment)
    return ".".join(parts) or None
