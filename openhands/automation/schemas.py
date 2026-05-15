"""Pydantic request/response schemas for the API."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal
from zoneinfo import ZoneInfo

import httpx
from croniter import croniter
from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    SecretStr,
    Tag,
    TypeAdapter,
    field_serializer,
    field_validator,
)

from openhands.automation.config import get_config


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from openhands.automation.models import Automation, AutomationRun


logger = logging.getLogger(__name__)

_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")


# Allowed URI schemes for tarball_path (includes internal upload scheme)
_TARBALL_SCHEME_RE = re.compile(r"^(s3|gs|https?|oh-internal)://")

# Shell metacharacters that should not appear in entrypoints or script paths
_SHELL_META_RE = re.compile(r"[;&|`$(){}<>!\\\n\r]")

# Path traversal pattern
_PATH_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")


def _validate_timeout(v: int | None) -> int | None:
    """Validate timeout is positive and within max allowed duration.

    Shared validator used by CreateAutomationRequest and UpdateAutomationRequest.
    """
    if v is None:
        return v
    if v <= 0:
        raise ValueError("timeout must be a positive number")
    max_duration = get_config().sandbox.max_run_duration
    if v > max_duration:
        raise ValueError(f"timeout must not exceed {max_duration} seconds")
    return v


class _TriggerBase(BaseModel):
    """Common base for all trigger configurations.

    Subclasses implement :meth:`create_pending_run` to decide — once per
    scheduler poll cycle — whether the given automation should fire right now,
    and if so, what (optional) event payload to attach to the resulting
    ``AutomationRun``.

    Concurrency note: the scheduler invokes this method **sequentially** for
    each automation in a batch because they share a single
    :class:`~sqlalchemy.ext.asyncio.AsyncSession`, which is not safe for
    concurrent use. Triggers should still parallelize their *own* external
    I/O (e.g. by using :func:`openhands.automation.utils.async_utils.wait_all`
    to fan out HTTP calls across multiple resources).
    """

    model_config = ConfigDict(extra="forbid")

    type: str

    async def create_pending_run(
        self,
        session: AsyncSession,
        automation: Automation,
        now: datetime | None = None,
    ) -> AutomationRun | None:
        """Return a PENDING ``AutomationRun`` if the trigger is due, else None.

        Implementations that decide to fire MUST call
        :func:`openhands.automation.utils.run.create_pending_run` (which
        bumps ``last_triggered_at``/``last_polled_at`` and appends the run
        to ``session``) and may then mutate the returned run — for example
        to populate ``event_payload`` with the data that caused the fire.

        Exceptions raised here are logged and treated as "not due" by the
        scheduler so that one broken trigger cannot starve the rest of the
        batch.
        """
        raise NotImplementedError


class CronTrigger(_TriggerBase):
    """Cron-based trigger configuration."""

    type: Literal["cron"] = "cron"
    schedule: str = Field(..., description="Cron expression, e.g. '0 9 * * 5'")
    timezone: str = Field(default="UTC", description="IANA timezone name")

    @field_validator("schedule")
    @classmethod
    def validate_cron_schedule(cls, v: str) -> str:
        if not croniter.is_valid(v):
            raise ValueError(f"Invalid cron expression: {v}")
        return v

    async def create_pending_run(
        self,
        session: AsyncSession,
        automation: Automation,
        now: datetime | None = None,
    ) -> AutomationRun | None:
        """Fire if the cron's most recent slot has passed since last trigger."""
        from openhands.automation.utils.cron import (
            is_automation_due as _is_due_cron,
        )
        from openhands.automation.utils.run import (
            create_pending_run as _create_run_util,
        )

        if not _is_due_cron(automation, now):
            return None
        return await _create_run_util(session, automation)


class EventTrigger(_TriggerBase):
    """
    Event-based trigger configuration.

    Triggers automation when a matching event is received from the source.
    Uses pattern matching via the `on` field and optional JMESPath filter.

    ## Event Key Format

    Events are identified by "{event_type}.{action}" or just "{event_type}" for
    events without actions (like push).

    Examples:
    - `pull_request.opened` - PR opened
    - `pull_request.closed` - PR closed
    - `pull_request.*` - Any PR activity (wildcard)
    - `push` - Code pushed
    - `issue.created` - Linear issue created

    ## Filter Expressions (JMESPath DSL)

    The `filter` field accepts a JMESPath expression that is evaluated against
    the raw webhook payload. The expression must evaluate to a truthy value
    for the event to match.

    **Available functions:**
    - `contains(array, value)` - Check if array contains value
    - `glob(str, pattern)` - Wildcard matching (e.g., 'org/*')
    - `icontains(str, substr)` - Case-insensitive substring match
    - `regex(str, pattern)` - Regular expression match
    - `starts_with(str, prefix)` - Check if string starts with prefix
    - `ends_with(str, suffix)` - Check if string ends with suffix
    - `lower(str)` / `upper(str)` - Case conversion

    **Boolean operators:** `&&` (and), `||` (or), `!` (not)

    ## Examples

    ```json
    // GitHub: Match @openhands-resolver mentions in comments
    {
      "source": "github",
      "on": "issue_comment.created",
      "filter": "icontains(comment.body, '@openhands-resolver')"
    }

    // GitHub: PR opened in specific repo
    {
      "source": "github",
      "on": "pull_request.opened",
      "filter": "repository.full_name == 'myorg/myrepo'"
    }

    // GitHub: PR with 'bug' label in any org repo
    {
      "source": "github",
      "on": "pull_request.opened",
      "filter": "glob(repository.full_name, 'myorg/*')"
    }

    // GitHub: Push to main or release branches
    {
      "source": "github",
      "on": "push",
      "filter": "glob(ref, 'refs/heads/main') || glob(ref, 'refs/heads/release/*')"
    }

    // No filter - match any event of this type
    {"source": "github", "on": "push"}
    ```
    """

    type: Literal["event"] = "event"
    source: str = Field(
        ...,
        description="Event source: 'github' or custom webhook source name",
    )
    on: str | list[str] = Field(
        ...,
        description=(
            "Event key pattern(s) to match. "
            "Format: 'event_type.action' or 'event_type'. "
            "Supports wildcards: 'pull_request.*' matches any PR action. "
            "Can be a single pattern or list of patterns."
        ),
    )
    filter: str | None = Field(
        default=None,
        description=(
            "JMESPath expression evaluated against the raw payload. "
            "Must evaluate to truthy for the event to match. "
            "Functions: contains(), glob(), icontains(), regex(). "
            "Example: glob(repository.full_name, 'org/*') && "
            "icontains(comment.body, '@openhands-resolver')"
        ),
    )

    @field_validator("filter")
    @classmethod
    def validate_filter_expression(cls, v: str | None) -> str | None:
        """Validate JMESPath filter expression at creation time."""
        if v:
            from openhands.automation.filter_eval import validate_filter

            is_valid, error = validate_filter(v)
            if not is_valid:
                raise ValueError(f"Invalid filter expression: {error}")
        return v

    @property
    def event_patterns(self) -> list[str]:
        """Get the event patterns as a list."""
        if isinstance(self.on, str):
            return [self.on]
        return self.on

    async def create_pending_run(
        self,
        session: AsyncSession,  # noqa: ARG002
        automation: Automation,  # noqa: ARG002
        now: datetime | None = None,  # noqa: ARG002
    ) -> AutomationRun | None:
        """Event triggers are fired by the webhook router, never by polling."""
        return None


class GithubTrigger(_TriggerBase):
    """Poll-based trigger that fires when new events appear on GitHub repos.

    On each scheduler poll, the trigger queries the
    ``/repos/{owner}/{repo}/events`` endpoint for each configured repository
    **concurrently** and collects any event created after the automation's
    last fire time (or its ``created_at`` for the very first poll, mirroring
    the no-backfill semantics of :class:`CronTrigger`).

    If any matching events are found, :meth:`create_pending_run` creates a
    PENDING ``AutomationRun`` and stores the collected events on
    ``run.event_payload`` so the run's entrypoint can react to them; otherwise
    it returns ``None``.

    Optionally restrict the event types that count using ``event_types`` (e.g.
    ``["PushEvent", "PullRequestEvent"]``); when omitted, any event type
    triggers a fire.
    """

    type: Literal["github"] = "github"
    github_access_token: SecretStr = Field(
        ...,
        description=(
            "GitHub Personal Access Token used to authenticate against the "
            "REST API. Authenticated requests have a 5000/hour rate limit "
            "compared to 60/hour unauthenticated."
        ),
    )
    repositories: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Repositories to poll, each as 'owner/name' "
            "(e.g. 'All-Hands-AI/OpenHands')."
        ),
    )
    event_types: list[str] | None = Field(
        default=None,
        description=(
            "Optional allow-list of GitHub event types (e.g. 'PushEvent'). "
            "When unset, any event type counts as new activity."
        ),
    )

    @field_validator("repositories")
    @classmethod
    def validate_repositories(cls, v: list[str]) -> list[str]:
        cleaned: list[str] = []
        for repo in v:
            repo = repo.strip()
            if not _GITHUB_REPO_RE.match(repo):
                raise ValueError(f"Invalid repository {repo!r}: expected 'owner/name'")
            cleaned.append(repo)
        return cleaned

    @field_validator("event_types")
    @classmethod
    def validate_event_types(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        cleaned = [t.strip() for t in v if t and t.strip()]
        return cleaned or None

    @field_serializer("github_access_token", when_used="always")
    def _serialize_token(self, v: SecretStr) -> str:
        """Emit the raw secret so the trigger can round-trip through the
        JSON column.

        The token must be stored in plain text because the scheduler needs
        to read it back later to authenticate against GitHub. ``SecretStr``
        is still useful at the application layer: it guards against
        accidental logging via ``repr()`` and string interpolation. Treat
        the on-disk JSON as sensitive (same trust level as the rest of the
        automation config) — anyone with read access to the database can
        see it.
        """
        return v.get_secret_value()

    def _build_client(self) -> httpx.AsyncClient:
        """Construct an authenticated GitHub REST client."""
        return httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "openhands-automation",
                "Authorization": (
                    f"Bearer {self.github_access_token.get_secret_value()}"
                ),
            },
            timeout=30.0,
        )

    async def _fetch_new_events(
        self,
        client: httpx.AsyncClient,
        repo: str,
        cutoff: datetime,
    ) -> list[dict[str, Any]]:
        """Return all matching events for ``repo`` newer than ``cutoff``.

        Honours :attr:`event_types`. Errors (HTTP/JSON/non-200) are logged
        and treated as "no new events" so a single bad repo doesn't take
        down the whole trigger.
        """
        try:
            resp = await client.get(
                f"/repos/{repo}/events",
                params={"per_page": 30, "page": 1},
            )
        except httpx.HTTPError as e:
            logger.warning("GitHub poll failed for %s: %s", repo, e)
            return []

        if resp.status_code != 200:
            logger.warning(
                "GitHub poll for %s returned status %s",
                repo,
                resp.status_code,
            )
            return []

        try:
            events = resp.json()
        except ValueError:
            logger.warning("GitHub poll for %s returned non-JSON body", repo)
            return []
        if not isinstance(events, list):
            return []

        allowed: set[str] | None = set(self.event_types) if self.event_types else None
        new_events: list[dict[str, Any]] = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if allowed is not None and ev.get("type") not in allowed:
                continue
            created_raw = ev.get("created_at")
            if not isinstance(created_raw, str):
                continue
            try:
                created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=ZoneInfo("UTC"))
            if created_at > cutoff:
                # Tag with repo so downstream code knows where it came from.
                tagged = dict(ev)
                tagged.setdefault("_repository", repo)
                new_events.append(tagged)
        return new_events

    async def create_pending_run(
        self,
        session: AsyncSession,
        automation: Automation,
        now: datetime | None = None,  # noqa: ARG002
    ) -> AutomationRun | None:
        """Fire if any configured repo has matching new events.

        On fire, attaches the events that caused the fire to
        ``run.event_payload`` as::

            {
                "source": "github_trigger",
                "events": [<github event dict>, ...],
            }
        """
        # Deferred imports avoid circular dependencies at module load time.
        from openhands.automation.utils.async_utils import wait_all
        from openhands.automation.utils.run import (
            create_pending_run as _create_run_util,
        )

        if not automation.enabled or automation.deleted_at is not None:
            return None

        cutoff = automation.last_triggered_at or automation.created_at
        if cutoff is None:
            return None
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=ZoneInfo("UTC"))

        async with self._build_client() as client:
            per_repo: list[list[dict[str, Any]]] = await wait_all(
                [
                    self._fetch_new_events(client, repo, cutoff)
                    for repo in self.repositories
                ],
                timeout=None,
            )

        all_events: list[dict[str, Any]] = [ev for batch in per_repo for ev in batch]
        if not all_events:
            return None

        run = await _create_run_util(session, automation)
        run.event_payload = {"source": "github_trigger", "events": all_events}
        return run


def _get_trigger_discriminator(v: dict | BaseModel) -> str:
    """Discriminator function for Pydantic's discriminated union.

    Returns the trigger type string, which Pydantic uses to select the
    correct model (CronTrigger, EventTrigger, or GithubTrigger) from the union.

    Why sentinel instead of raising ValueError:
        Pydantic discriminator functions must return a string - they cannot
        raise exceptions. By returning an invalid sentinel value, Pydantic
        generates a proper ValidationError with context like:
        "Input tag '__missing_trigger_type__' found using 'type' does not
        match any of the expected tags: 'cron', 'event', 'github'"
        This produces a user-friendly 422 response via FastAPI.
    """
    if isinstance(v, dict):
        trigger_type = v.get("type")
        if not trigger_type:
            return "__missing_trigger_type__"
        return trigger_type
    return getattr(v, "type")


# Union type for all triggers, using discriminated union
Trigger = Annotated[
    Annotated[CronTrigger, Tag("cron")]
    | Annotated[EventTrigger, Tag("event")]
    | Annotated[GithubTrigger, Tag("github")],
    Discriminator(_get_trigger_discriminator),
]

# Reusable adapter for parsing trigger dicts (e.g. ``automation.trigger`` JSON)
# into the correct ``_TriggerBase`` subclass.
TriggerAdapter: TypeAdapter[CronTrigger | EventTrigger | GithubTrigger] = TypeAdapter(
    Trigger
)


class RunStatus(StrEnum):
    """Status of an automation run (for API responses)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def _validate_command_string(
    v: str | None, field_name: str, *, allow_none: bool = True
) -> str | None:
    """Validate a command/path is relative and safe.

    Rejects traversal patterns and shell metacharacters.

    Used for both entrypoint and setup_script_path validation.

    Args:
        v: The value to validate
        field_name: Field name for error messages
        allow_none: If True, None values pass through unchanged

    Returns:
        The validated value
    """
    if v is None:
        if allow_none:
            return v
        raise ValueError(f"{field_name} is required")
    if not v.strip():
        raise ValueError(f"{field_name} must not be blank")
    if v.startswith("/"):
        raise ValueError(f"{field_name} must be a relative path, not an absolute path")
    if _PATH_TRAVERSAL_RE.search(v):
        raise ValueError(f"{field_name} must not contain path traversal (..)")
    if _SHELL_META_RE.search(v):
        raise ValueError(
            f"{field_name} must not contain shell metacharacters (;&|`$(){{}}<>!\\\\)"
        )
    return v


# --- Requests ---


class CreateAutomationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=500)
    trigger: Trigger = Field(
        ..., description="Trigger configuration (cron or event-based)"
    )
    tarball_path: str = Field(
        ..., description="Path to SDK code tarball (e.g., S3 or GCS URL)"
    )
    setup_script_path: str | None = Field(
        default=None,
        description="Relative path inside tarball to setup script (e.g., setup.sh)",
    )
    entrypoint: str = Field(
        ..., description='Command to execute the automation (e.g., "uv run script.py")'
    )
    timeout: int | None = Field(
        default=None,
        description="Maximum execution time in seconds (default: system maximum)",
    )

    @field_validator("tarball_path")
    @classmethod
    def validate_tarball_path(cls, v: str) -> str:
        if not _TARBALL_SCHEME_RE.match(v):
            raise ValueError(
                "tarball_path must start with s3://, gs://, http://, or https://"
            )
        return v

    @field_validator("setup_script_path")
    @classmethod
    def validate_setup_script_path(cls, v: str | None) -> str | None:
        return _validate_command_string(v, "setup_script_path")

    @field_validator("entrypoint")
    @classmethod
    def validate_entrypoint(cls, v: str) -> str:
        result = _validate_command_string(v, "entrypoint", allow_none=False)
        assert result is not None  # satisfy type checker
        return result

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, v: int | None) -> int | None:
        return _validate_timeout(v)


class UpdateAutomationRequest(BaseModel):
    """Request to partially update an automation."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=500)
    prompt: str | None = Field(default=None, max_length=50000)
    trigger: Trigger | None = Field(
        default=None, description="Trigger configuration (cron or event-based)"
    )
    tarball_path: str | None = Field(default=None)
    setup_script_path: str | None = Field(default=None)
    entrypoint: str | None = Field(default=None)
    timeout: int | None = Field(default=None)
    enabled: bool | None = None

    @field_validator("tarball_path")
    @classmethod
    def validate_tarball_path(cls, v: str | None) -> str | None:
        if v is not None and not _TARBALL_SCHEME_RE.match(v):
            raise ValueError(
                "tarball_path must start with s3://, gs://, http://, or https://"
            )
        return v

    @field_validator("setup_script_path")
    @classmethod
    def validate_setup_script_path(cls, v: str | None) -> str | None:
        return _validate_command_string(v, "setup_script_path")

    @field_validator("entrypoint")
    @classmethod
    def validate_entrypoint(cls, v: str | None) -> str | None:
        return _validate_command_string(v, "entrypoint")

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, v: int | None) -> int | None:
        return _validate_timeout(v)


# --- Webhook Schemas ---


class WebhookConfig(BaseModel):
    """Configuration for processing a webhook."""

    model_config = ConfigDict(extra="forbid")

    secret: str
    is_builtin: bool = False  # True for github
    event_key_expr: str = "type"  # JMESPath expression for extracting event key
    signature_header: str = "X-Hub-Signature-256"  # HTTP header for signature


class EventResponse(BaseModel):
    """Response for event processing."""

    received: bool
    matched: int
    runs_created: list[str]  # List of run IDs created


# Valid source name pattern: lowercase alphanumeric with hyphens, 1-50 chars
_SOURCE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}[a-z0-9]$|^[a-z0-9]$")

# Reserved source names (built-in integrations)
RESERVED_SOURCES = frozenset({"github"})


# Valid HTTP header name pattern
_HEADER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,98}[A-Za-z0-9]$|^[A-Za-z]$")


class CustomWebhookCreate(BaseModel):
    """Request schema for creating a custom webhook."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Human-readable name for this webhook",
    )
    source: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description=(
            "Unique source identifier (lowercase, alphanumeric with hyphens). "
            "Used in the webhook URL: /v1/events/{org_id}/{source}"
        ),
    )
    event_key_expr: str = Field(
        default="type",
        max_length=500,
        description=(
            "JMESPath expression to extract event type from payload. "
            "Examples: 'type', 'event.type', 'type || event_name'"
        ),
    )
    signature_header: str = Field(
        default="X-Signature-256",
        max_length=100,
        description=(
            "HTTP header name containing the HMAC signature. "
            "Examples: 'X-Signature-256', 'Stripe-Signature', 'X-Slack-Signature'"
        ),
    )
    webhook_secret: str | None = Field(
        default=None,
        min_length=8,
        max_length=255,
        description=(
            "Optional signing secret. If not provided, one will be generated. "
            "Use this when the external service provides a fixed secret."
        ),
    )

    @field_validator("source")
    @classmethod
    def validate_source_name(cls, v: str) -> str:
        """Validate source name format and check for reserved names."""
        v_lower = v.lower()
        if v_lower in RESERVED_SOURCES:
            raise ValueError(
                f"'{v}' is a reserved source name. "
                "Use the built-in integration instead."
            )
        if not _SOURCE_NAME_RE.match(v_lower):
            raise ValueError(
                "Source must be lowercase alphanumeric with hyphens, 1-50 chars, "
                "starting and ending with alphanumeric"
            )
        return v_lower

    @field_validator("event_key_expr")
    @classmethod
    def validate_event_key_expr(cls, v: str) -> str:
        """Validate JMESPath expression syntax."""
        import jmespath
        from jmespath import exceptions as jmespath_exceptions

        try:
            jmespath.compile(v)
        except jmespath_exceptions.JMESPathError as e:
            raise ValueError(f"Invalid JMESPath expression: {e}") from e
        return v

    @field_validator("signature_header")
    @classmethod
    def validate_signature_header(cls, v: str) -> str:
        """Validate HTTP header name format."""
        if not _HEADER_NAME_RE.match(v):
            raise ValueError(
                "Header must be alphanumeric with hyphens, 1-100 chars, "
                "starting with a letter"
            )
        return v


class CustomWebhookUpdate(BaseModel):
    """Request schema for updating a custom webhook."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    event_key_expr: str | None = Field(default=None, max_length=500)
    signature_header: str | None = Field(default=None, max_length=100)
    enabled: bool | None = None

    @field_validator("event_key_expr")
    @classmethod
    def validate_event_key_expr(cls, v: str | None) -> str | None:
        """Validate JMESPath expression syntax if provided."""
        if v is None:
            return v
        import jmespath
        from jmespath import exceptions as jmespath_exceptions

        try:
            jmespath.compile(v)
        except jmespath_exceptions.JMESPathError as e:
            raise ValueError(f"Invalid JMESPath expression: {e}") from e
        return v

    @field_validator("signature_header")
    @classmethod
    def validate_signature_header(cls, v: str | None) -> str | None:
        """Validate HTTP header name format if provided."""
        if v is None:
            return v
        if not _HEADER_NAME_RE.match(v):
            raise ValueError(
                "Header must be alphanumeric with hyphens, 1-100 chars, "
                "starting with a letter"
            )
        return v


class CustomWebhookResponse(BaseModel):
    """Response schema for custom webhook (without secret)."""

    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    source: str
    webhook_url: str
    event_key_expr: str
    signature_header: str
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CustomWebhookCreateResponse(CustomWebhookResponse):
    """Response schema for webhook creation.

    webhook_secret is only included when the system generated it (user didn't
    provide one). If the user provided their own secret, it won't be echoed back.
    """

    webhook_secret: str | None = Field(
        default=None,
        description=(
            "Webhook signing secret (only shown if system-generated). "
            "Store securely - only shown on create."
        ),
    )


class CustomWebhookSecretResponse(BaseModel):
    """Response schema for secret rotation."""

    webhook_secret: str = Field(
        ...,
        description="New webhook signing secret. Store securely - only shown once.",
    )


class CustomWebhookListResponse(BaseModel):
    """Response schema for listing webhooks."""

    webhooks: list[CustomWebhookResponse]
    total: int


# --- Responses ---


class AutomationResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID
    name: str
    prompt: str | None
    trigger: dict
    tarball_path: str
    setup_script_path: str | None
    entrypoint: str
    timeout: int | None
    enabled: bool
    last_triggered_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AutomationListResponse(BaseModel):
    automations: list[AutomationResponse]
    total: int


# --- Run schemas ---


class RunCompleteRequest(BaseModel):
    """Payload sent by the SDK's OpenHandsCloudWorkspace on context manager exit."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["COMPLETED", "FAILED"]
    run_id: str | None = None
    conversation_id: str | None = None
    error: str | None = None


class AutomationRunResponse(BaseModel):
    """Response for a single automation run."""

    id: uuid.UUID
    automation_id: uuid.UUID
    status: RunStatus
    error_detail: str | None
    conversation_id: str | None
    timeout_at: datetime | None
    keep_alive: bool
    sandbox_id: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class AutomationRunListResponse(BaseModel):
    """Response for listing automation runs (Phase 1b)."""

    runs: list[AutomationRunResponse]
    total: int
