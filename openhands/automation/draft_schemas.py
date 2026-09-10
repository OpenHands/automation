"""Endpoint-specific schemas for saved automation draft bodies."""

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationInfo,
    field_validator,
    model_validator,
)

from openhands.automation.constants import MODEL_PROFILE_PATTERN
from openhands.automation.preset_router import (
    MAX_VARIANTS,
    CreatePluginAutomationRequest,
    CreatePromptAutomationRequest,
)
from openhands.automation.schemas import (
    AutomationState,
    CreateAutomationRequest,
    DraftEndpoint,
    TemplateProvenance,
    normalize_automation_state_enabled,
    validate_command_string,
)
from openhands.automation.utils.timeout import validate_automation_timeout
from openhands.sdk.plugin import PluginSource
from openhands.workspace import RepoSource


type DraftModel = (
    CreateAutomationRequest
    | CreatePromptAutomationRequest
    | CreatePluginAutomationRequest
)


def _get_draft_trigger_discriminator(v: dict | BaseModel) -> str:
    if isinstance(v, dict):
        trigger_type = v.get("type")
        if not trigger_type:
            return "__missing_trigger_type__"
        return trigger_type
    return getattr(v, "type")


class CronTriggerDraft(BaseModel):
    """Partial cron trigger shape accepted while editing a draft."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["cron"] = "cron"
    schedule: str | None = Field(default=None, min_length=1)
    timezone: str | None = Field(default=None, min_length=1)


class EventTriggerDraft(BaseModel):
    """Partial event trigger shape accepted while editing a draft."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["event"] = "event"
    source: str | None = Field(default=None, min_length=1)
    on: str | list[str] | None = None
    filter: str | None = None
    destination: Literal["dispatch_run", "continue_conversation"] = "dispatch_run"
    subject_key_expr: str | None = None
    turn_text_expr: str | None = None
    wake_agent: bool = True

    @field_validator("filter", "subject_key_expr", "turn_text_expr")
    @classmethod
    def validate_jmespath_expression(
        cls, v: str | None, info: ValidationInfo
    ) -> str | None:
        if v:
            from openhands.automation.filter_eval import validate_filter

            is_valid, error = validate_filter(v)
            if not is_valid:
                raise ValueError(f"Invalid {info.field_name} expression: {error}")
        return v


DraftTrigger = Annotated[
    Annotated[CronTriggerDraft, Tag("cron")]
    | Annotated[EventTriggerDraft, Tag("event")],
    Discriminator(_get_draft_trigger_discriminator),
]


class _BaseDraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=500)
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=MODEL_PROFILE_PATTERN,
    )
    trigger: DraftTrigger | None = None
    timeout: int | None = None
    keep_alive: bool | None = None
    enabled: bool | None = None
    lifecycle_status: AutomationState | None = None
    template: TemplateProvenance | None = None

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, v: int | None) -> int | None:
        return validate_automation_timeout(v)

    @model_validator(mode="before")
    @classmethod
    def validate_automation_state_enabled(cls, data: Any) -> Any:
        return normalize_automation_state_enabled(data)


def _normalize_automation_state(data: Any) -> Any:
    return normalize_automation_state_enabled(data)


def _normalize_list_field(data: dict[str, Any], field: str) -> None:
    if (
        field in data
        and data[field] is not None
        and isinstance(data[field], (str, dict))
    ):
        data[field] = [data[field]]


class RawAutomationDraftBody(_BaseDraftBody):
    """Partial body for the raw `/v1` automation creation endpoint."""

    tarball_path: str | None = None
    setup_script_path: str | None = None
    entrypoint: str | None = None

    @field_validator("tarball_path")
    @classmethod
    def validate_tarball_path(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(
            ("s3://", "gs://", "http://", "https://", "oh-internal://")
        ):
            raise ValueError(
                "tarball_path must start with s3://, gs://, http://, https://, "
                "or oh-internal://"
            )
        return v

    @field_validator("setup_script_path")
    @classmethod
    def validate_setup_script_path(cls, v: str | None) -> str | None:
        return validate_command_string(v, "setup_script_path")

    @field_validator("entrypoint")
    @classmethod
    def validate_entrypoint(cls, v: str | None) -> str | None:
        return validate_command_string(v, "entrypoint")


class PromptAutomationDraftBody(_BaseDraftBody):
    """Partial body for the prompt preset creation endpoint."""

    prompt: str | None = Field(default=None, min_length=1, max_length=50000)
    repos: list[RepoSource] | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_repos(cls, data: Any) -> Any:
        data = _normalize_automation_state(data)
        if isinstance(data, dict):
            _normalize_list_field(data, "repos")
        return data


class ExperimentVariantDraft(BaseModel):
    """Partial plugin experiment variant shape for draft editing."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    weight: int | None = Field(default=None, gt=0)
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=MODEL_PROFILE_PATTERN,
    )
    plugins: list[PluginSource] | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_plugins(cls, data: Any) -> Any:
        if isinstance(data, dict):
            _normalize_list_field(data, "plugins")
        return data


class PluginAutomationDraftBody(_BaseDraftBody):
    """Partial body for the plugin preset creation endpoint."""

    plugins: list[PluginSource] | None = None
    variants: list[ExperimentVariantDraft] | None = None
    experiment_id: str | None = Field(default=None, min_length=1, max_length=200)
    prompt: str | None = Field(default=None, min_length=1, max_length=50000)
    repos: list[RepoSource] | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_plugins_and_repos(cls, data: Any) -> Any:
        data = _normalize_automation_state(data)
        if isinstance(data, dict):
            _normalize_list_field(data, "plugins")
            _normalize_list_field(data, "repos")
        return data

    @model_validator(mode="after")
    def validate_plugin_draft_shape(self) -> "PluginAutomationDraftBody":
        if self.plugins is not None and self.variants is not None:
            raise ValueError("Only one of 'plugins' or 'variants' may be provided.")
        if self.variants is not None and len(self.variants) > MAX_VARIANTS:
            raise ValueError(f"At most {MAX_VARIANTS} variants are allowed.")
        return self


type DraftBodyModel = (
    RawAutomationDraftBody | PromptAutomationDraftBody | PluginAutomationDraftBody
)

DRAFT_BODY_MODELS: dict[str, type[DraftBodyModel]] = {
    "/v1": RawAutomationDraftBody,
    "/v1/preset/prompt": PromptAutomationDraftBody,
    "/v1/preset/plugin": PluginAutomationDraftBody,
}

FINAL_DRAFT_MODELS: dict[str, type[DraftModel]] = {
    "/v1": CreateAutomationRequest,
    "/v1/preset/prompt": CreatePromptAutomationRequest,
    "/v1/preset/plugin": CreatePluginAutomationRequest,
}


def parse_draft_body_shape(
    endpoint: DraftEndpoint, draft_body: dict[str, Any]
) -> DraftBodyModel:
    """Validate and normalize a saved draft body for its target endpoint."""
    return DRAFT_BODY_MODELS[endpoint].model_validate(draft_body)


def normalize_draft_body(
    endpoint: DraftEndpoint, draft_body: dict[str, Any]
) -> dict[str, Any]:
    """Return the canonical JSON body stored for a structurally valid draft."""
    return parse_draft_body_shape(endpoint, draft_body).model_dump(exclude_none=True)
