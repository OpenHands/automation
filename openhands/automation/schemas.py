"""Pydantic request/response schemas for the API."""

from __future__ import annotations

import logging
import re
import uuid
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal
from zoneinfo import ZoneInfo

import httpx
import jmespath
from croniter import croniter
from jmespath.exceptions import JMESPathError
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
from openhands.automation.constants import MODEL_PROFILE_PATTERN
from openhands.automation.utils.time import UtcDatetime


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from openhands.automation.models import Automation, AutomationRun


logger = logging.getLogger(__name__)

_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")

# Slack channel IDs are uppercase alphanumeric (e.g. 'C0123ABC', 'D…', 'G…').
# Channel *names* aren't supported here because they're not stable across renames.
_SLACK_CHANNEL_ID_RE = re.compile(r"^[A-Z][A-Z0-9]+$")


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


class _PollingTriggerBase(_TriggerBase):
    """Base for triggers that poll external services for new items.

    Concrete subclasses (e.g. :class:`GithubTrigger`, :class:`SlackTrigger`)
    own three pieces of behaviour:

    1. **Authentication / client construction** — :meth:`_build_client`.
    2. **Fan-out fetching** — :meth:`_fetch_all_new` returns the flat list of
       items (already filtered against the cutoff and :attr:`event_filter`)
       collected from every configured resource, typically running per-resource
       calls concurrently via :func:`wait_all`.
    3. **Payload labelling** — :attr:`_PAYLOAD_SOURCE` and :attr:`_PAYLOAD_KEY`
       (e.g. ``"github_trigger"`` / ``"events"``).

    The base class itself handles:

    - The ``enabled`` / ``deleted_at`` short-circuit.
    - Computing the cutoff (``last_triggered_at`` or ``created_at``).
    - Calling :func:`openhands.automation.utils.run.create_pending_run` once
      items are gathered and attaching them to ``run.event_payload``.
    - Validating and compiling the optional JMESPath ``event_filter``.
    """

    # Pydantic config. ``arbitrary_types_allowed`` lets us cache a compiled
    # JMESPath parser object on ``self.__dict__`` without Pydantic objecting.
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    # Subclass-supplied metadata for the run's ``event_payload`` envelope.
    _PAYLOAD_SOURCE: ClassVar[str] = ""
    _PAYLOAD_KEY: ClassVar[str] = "items"

    event_filter: str | None = Field(
        default=None,
        description=(
            "Optional JMESPath expression evaluated against each polled item "
            "(see https://jmespath.org/). An item matches when the expression "
            "returns a truthy value; non-matching items are dropped. The "
            "trigger fires only if at least one item matches."
        ),
    )

    @field_validator("event_filter")
    @classmethod
    def validate_event_filter(cls, v: str | None) -> str | None:
        """Parse the JMESPath at validation time so bad syntax fails fast.

        The compiled expression itself is *not* stored on the field (so the
        JSON round-trip stays clean) — it's cached lazily on first use via
        :attr:`_compiled_event_filter`.
        """
        if v is None:
            return v
        expr = v.strip()
        if not expr:
            return None
        try:
            jmespath.compile(expr)
        except JMESPathError as e:
            raise ValueError(f"Invalid JMESPath expression: {e}") from e
        return expr

    @property
    def _compiled_event_filter(self) -> jmespath.parser.ParsedResult | None:
        """Compile and cache the JMESPath expression on first use."""
        if self.event_filter is None:
            return None
        cached = self.__dict__.get("_compiled_filter")
        if cached is None:
            # Already validated in ``validate_event_filter`` — won't raise.
            cached = jmespath.compile(self.event_filter)
            self.__dict__["_compiled_filter"] = cached
        return cached

    def _item_matches_filter(self, item: dict[str, Any], context: str) -> bool:
        """Return True if ``item`` passes ``event_filter`` (or no filter).

        ``context`` is a human-readable label (repo / channel id) used only
        for logging if the JMESPath evaluator throws.
        """
        compiled = self._compiled_event_filter
        if compiled is None:
            return True
        try:
            return bool(compiled.search(item))
        except JMESPathError as e:
            logger.warning("JMESPath evaluation failed for item in %s: %s", context, e)
            return False

    def _build_client(self) -> httpx.AsyncClient:
        """Construct an authenticated HTTP client for the upstream service."""
        raise NotImplementedError

    async def _fetch_all_new(
        self,
        client: httpx.AsyncClient,
        cutoff: datetime,
    ) -> list[dict[str, Any]]:
        """Return all items newer than ``cutoff`` from every configured resource.

        Implementations are expected to fan out across their resources
        concurrently (typically via
        :func:`openhands.automation.utils.async_utils.wait_all`) and apply
        :meth:`_item_matches_filter` per item.
        """
        raise NotImplementedError

    async def create_pending_run(
        self,
        session: AsyncSession,
        automation: Automation,
        now: datetime | None = None,  # noqa: ARG002
    ) -> AutomationRun | None:
        """Fire if any configured resource yields a matching new item.

        On fire, attaches the gathered items to ``run.event_payload`` as::

            {"source": <subclass _PAYLOAD_SOURCE>,
             <subclass _PAYLOAD_KEY>: [...]}
        """
        # Deferred import avoids a circular dependency at module load.
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
            items = await self._fetch_all_new(client, cutoff)

        if not items:
            return None

        run = await _create_run_util(session, automation)
        run.event_payload = {
            "source": self._PAYLOAD_SOURCE,
            self._PAYLOAD_KEY: items,
        }
        return run


class GithubTrigger(_PollingTriggerBase):
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

    Optionally narrow which events fire the trigger using
    :attr:`event_filter` — a `JMESPath <https://jmespath.org/>`_ expression
    evaluated against each event. An event is kept when the expression
    returns a truthy value. Examples::

        type == 'PushEvent'
        type == 'PullRequestEvent' && payload.action == 'opened'
        type == 'PushEvent' && payload.ref == 'refs/heads/main'

    When ``event_filter`` is omitted, every event newer than the cutoff
    fires the trigger.
    """

    _PAYLOAD_SOURCE: ClassVar[str] = "github_trigger"
    _PAYLOAD_KEY: ClassVar[str] = "events"

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

    @field_serializer("github_access_token", when_used="always")
    def _serialize_token(self, v: SecretStr) -> str:
        """Emit the raw secret so the trigger round-trips through the JSON column.

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

        Errors (HTTP/JSON/non-200) are logged and treated as "no new events"
        so a single bad repo doesn't take down the whole trigger.
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

        new_events: list[dict[str, Any]] = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if not self._item_matches_filter(ev, repo):
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
                # Tag with the source repo so downstream code knows the origin.
                tagged = dict(ev)
                tagged.setdefault("_repository", repo)
                new_events.append(tagged)
        return new_events

    async def _fetch_all_new(
        self,
        client: httpx.AsyncClient,
        cutoff: datetime,
    ) -> list[dict[str, Any]]:
        from openhands.automation.utils.async_utils import wait_all

        per_repo: list[list[dict[str, Any]]] = await wait_all(
            [
                self._fetch_new_events(client, repo, cutoff)
                for repo in self.repositories
            ],
            timeout=None,
        )
        return [ev for batch in per_repo for ev in batch]


class SlackTrigger(_PollingTriggerBase):
    """Poll-based trigger that fires when new messages appear in Slack channels.

    On each scheduler poll, the trigger calls
    ``https://slack.com/api/conversations.history`` for each configured
    channel **concurrently**, using ``oldest=<unix_seconds>`` as the
    high-water mark (derived from the automation's last fire time, or
    ``created_at`` for the very first poll). Messages with a Slack ``ts``
    strictly greater than the cutoff are kept.

    Optionally narrow which messages fire the trigger using
    :attr:`event_filter` — a `JMESPath <https://jmespath.org/>`_ expression
    evaluated against each message dict. A message is kept when the
    expression returns a truthy value. Examples::

        subtype == null                   # regular user messages only
        type == 'message' && user == 'U0123ABC'
        contains(text, '@here')

    Required Slack OAuth scopes depend on the channel types polled:
    ``channels:history``, ``groups:history``, ``im:history``, ``mpim:history``.
    Bot tokens (``xoxb-…``) work but the bot must be a member of each
    channel; user tokens (``xoxp-…``) generally don't need membership.

    Notes:

    - The Slack Web API returns HTTP 200 even for application errors; we
      detect those via ``body["ok"]`` and log ``body["error"]``.
    - On HTTP 429 (rate limit) we log the ``Retry-After`` value and treat
      the channel as having no new messages this cycle. The next scheduler
      tick will retry.
    - Only the first page (up to 200 messages) is fetched per channel per
      poll. Channels exceeding that between polls will skip the gap —
      tighten ``scheduler_interval`` or shorten poll cycles to compensate.
    """

    _PAYLOAD_SOURCE: ClassVar[str] = "slack_trigger"
    _PAYLOAD_KEY: ClassVar[str] = "messages"
    _SLACK_MESSAGE_PAGE_LIMIT: ClassVar[int] = 200

    type: Literal["slack"] = "slack"
    slack_token: SecretStr = Field(
        ...,
        description=(
            "Slack token used to authenticate against the Web API. Either a "
            "user token ('xoxp-…') or a bot token ('xoxb-…'). The token "
            "needs the appropriate '*:history' OAuth scopes for the "
            "channel types being polled."
        ),
    )
    channels: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Slack channel IDs to poll (e.g. 'C0123ABC'). IDs — not names — "
            "are required because names are not stable across renames. Find a "
            "channel's ID in its 'About' panel inside the Slack client."
        ),
    )

    @field_validator("channels")
    @classmethod
    def validate_channels(cls, v: list[str]) -> list[str]:
        cleaned: list[str] = []
        for c in v:
            c = c.strip()
            if not _SLACK_CHANNEL_ID_RE.match(c):
                raise ValueError(
                    f"Invalid Slack channel id {c!r}: expected uppercase "
                    "alphanumeric like 'C0123ABC' (channel IDs, not names)."
                )
            cleaned.append(c)
        return cleaned

    @field_serializer("slack_token", when_used="always")
    def _serialize_token(self, v: SecretStr) -> str:
        """Emit the raw secret so the trigger round-trips through the JSON column.

        See the corresponding note on :class:`GithubTrigger` — the token is
        persisted in plain text because the scheduler must reuse it on every
        poll. ``SecretStr`` still protects against accidental logging at the
        application layer.
        """
        return v.get_secret_value()

    def _build_client(self) -> httpx.AsyncClient:
        """Construct an authenticated Slack Web API client."""
        return httpx.AsyncClient(
            base_url="https://slack.com/api",
            headers={
                "Accept": "application/json",
                "User-Agent": "openhands-automation",
                "Authorization": f"Bearer {self.slack_token.get_secret_value()}",
            },
            timeout=30.0,
        )

    async def _fetch_new_messages(
        self,
        client: httpx.AsyncClient,
        channel: str,
        cutoff: datetime,
    ) -> list[dict[str, Any]]:
        """Return matching messages for ``channel`` newer than ``cutoff``.

        Uses Slack's ``oldest`` parameter (exclusive) for server-side
        filtering. Errors — HTTP, JSON, rate-limit, or ``ok=false`` —
        are logged and treated as "no new messages" so a single bad
        channel cannot take down the whole trigger.
        """
        oldest = f"{cutoff.timestamp():.6f}"
        try:
            resp = await client.get(
                "/conversations.history",
                params={
                    "channel": channel,
                    "oldest": oldest,
                    "limit": self._SLACK_MESSAGE_PAGE_LIMIT,
                },
            )
        except httpx.HTTPError as e:
            logger.warning("Slack poll failed for %s: %s", channel, e)
            return []

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "unknown")
            logger.warning(
                "Slack poll for %s rate-limited (Retry-After=%s)",
                channel,
                retry_after,
            )
            return []
        if resp.status_code != 200:
            logger.warning(
                "Slack poll for %s returned status %s",
                channel,
                resp.status_code,
            )
            return []

        try:
            body = resp.json()
        except ValueError:
            logger.warning("Slack poll for %s returned non-JSON body", channel)
            return []
        if not isinstance(body, dict):
            return []
        if not body.get("ok"):
            logger.warning(
                "Slack poll for %s returned error: %s",
                channel,
                body.get("error", "unknown"),
            )
            return []

        messages = body.get("messages") or []
        if not isinstance(messages, list):
            return []

        # Slack returns newest-first; deliver in natural chronological order.
        messages_sorted = sorted(messages, key=lambda m: float(m.get("ts", "0") or "0"))

        new_messages: list[dict[str, Any]] = []
        for msg in messages_sorted:
            if not isinstance(msg, dict):
                continue
            ts_raw = msg.get("ts")
            if not isinstance(ts_raw, str):
                continue
            try:
                ts_unix = float(ts_raw)
            except ValueError:
                continue
            # Defense in depth — `oldest` is exclusive but check anyway.
            if ts_unix <= cutoff.timestamp():
                continue
            if not self._item_matches_filter(msg, channel):
                continue
            tagged = dict(msg)
            tagged.setdefault("_channel", channel)
            new_messages.append(tagged)
        return new_messages

    async def _fetch_all_new(
        self,
        client: httpx.AsyncClient,
        cutoff: datetime,
    ) -> list[dict[str, Any]]:
        from openhands.automation.utils.async_utils import wait_all

        per_channel: list[list[dict[str, Any]]] = await wait_all(
            [self._fetch_new_messages(client, c, cutoff) for c in self.channels],
            timeout=None,
        )
        return [m for batch in per_channel for m in batch]


def _get_trigger_discriminator(v: dict | BaseModel) -> str:
    """Discriminator function for Pydantic's discriminated union.

    Returns the trigger type string, which Pydantic uses to select the
    correct model (CronTrigger, EventTrigger, GithubTrigger, or SlackTrigger)
    from the union.

    Why sentinel instead of raising ValueError:
        Pydantic discriminator functions must return a string - they cannot
        raise exceptions. By returning an invalid sentinel value, Pydantic
        generates a proper ValidationError with context like:
        "Input tag '__missing_trigger_type__' found using 'type' does not
        match any of the expected tags: 'cron', 'event', 'github', 'slack'"
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
    | Annotated[GithubTrigger, Tag("github")]
    | Annotated[SlackTrigger, Tag("slack")],
    Discriminator(_get_trigger_discriminator),
]

# Reusable adapter for parsing trigger dicts (e.g. ``automation.trigger`` JSON)
# into the correct ``_TriggerBase`` subclass.
TriggerAdapter: TypeAdapter[
    CronTrigger | EventTrigger | GithubTrigger | SlackTrigger
] = TypeAdapter(Trigger)


class RunStatus(StrEnum):
    """Status of an automation run (for API responses)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


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
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=MODEL_PROFILE_PATTERN,
        description=(
            "Model profile name to use for automation runs. Defaults to the active "
            "profile at creation time when omitted."
        ),
    )

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
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=MODEL_PROFILE_PATTERN,
        description=(
            "Model profile name to use for automation runs. Defaults to the active "
            "profile at creation time when omitted."
        ),
    )

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
    is_builtin: bool = False  # True for built-in OpenHands-forwarded sources
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
RESERVED_SOURCES = frozenset({"bitbucket_data_center", "github", "jira_dc"})


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
    created_at: UtcDatetime
    updated_at: UtcDatetime

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
    model: str | None

    name: str
    prompt: str | None
    trigger: dict
    tarball_path: str
    setup_script_path: str | None
    entrypoint: str
    timeout: int | None
    enabled: bool
    last_triggered_at: UtcDatetime | None
    created_at: UtcDatetime
    updated_at: UtcDatetime

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
    timeout_at: UtcDatetime | None
    keep_alive: bool
    sandbox_id: str | None
    bash_command_id: str | None = None
    created_at: UtcDatetime
    started_at: UtcDatetime | None
    completed_at: UtcDatetime | None

    model_config = {"from_attributes": True}


class AutomationRunListResponse(BaseModel):
    """Response for listing automation runs (Phase 1b)."""

    runs: list[AutomationRunResponse]
    total: int
