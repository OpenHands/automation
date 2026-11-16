"""Cloud sandbox execution backend.

Creates a fresh Cloud sandbox for each automation run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Collection
from typing import TYPE_CHECKING, Any, TypeVar

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from openhands.automation.auth import X_ORG_ID_HEADER
from openhands.automation.backends.base import (
    ConversationStart,
    ExecutionBackend,
    ExecutionContext,
    StartedConversation,
)
from openhands.automation.config import get_config
from openhands.automation.exceptions import (
    ConcurrencyLimitReachedError,
    ConversationStartError,
    ConversationStartUnavailable,
    DailySandboxLimitReachedError,
    SandboxProvisioningError,
)
from openhands.automation.models import AutomationRun
from openhands.automation.plugin_secrets import (
    PluginSourceSecrets,
    cloud_plugin_source_secrets,
)
from openhands.automation.utils.api_key import get_api_key_for_automation_run
from openhands.automation.utils.sandbox import (
    cleanup_sandbox,
    delete_sandbox,
    find_agent_server_url,
    verify_run_status,
)


if TYPE_CHECKING:
    from openhands.automation.utils.agent_server import VerificationResult

T = TypeVar("T")

logger = logging.getLogger(__name__)


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Check if exception is a 429 rate limit error."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429
    return False


def _is_auth_error(exc: BaseException) -> bool:
    """Check if exception is an authentication error (401/403)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (401, 403)
    return False


def _concurrency_limit_detail(resp: httpx.Response) -> dict | None:
    """Return the CONCURRENCY_LIMIT_REACHED detail dict if the response is the
    organization concurrent-sandbox limit 429, else None.

    The OpenHands API raises this as a FastAPI HTTPException, so the body is
    ``{"detail": {"error": "CONCURRENCY_LIMIT_REACHED", ...}}``; we also tolerate
    a flat ``{"error": ...}`` shape. This is distinct from a transient
    rate-limit 429, which should still be retried (see ``_is_rate_limit_error``).
    """
    return _quota_limit_detail(resp, "CONCURRENCY_LIMIT_REACHED")


def _daily_sandbox_limit_detail(resp: httpx.Response) -> dict | None:
    """Return the DAILY_SANDBOX_LIMIT_REACHED detail dict if the response is
    the per-user daily sandbox quota 429, else None.

    Like the concurrency limit this must not be retried (see
    ``_is_rate_limit_error``): the quota only resets at UTC midnight.
    """
    return _quota_limit_detail(resp, "DAILY_SANDBOX_LIMIT_REACHED")


def _quota_limit_detail(resp: httpx.Response, error_code: str) -> dict | None:
    if resp.status_code != 429:
        return None
    try:
        body = resp.json()
    except Exception:
        return None
    detail = body.get("detail", body) if isinstance(body, dict) else None
    if isinstance(detail, dict) and detail.get("error") == error_code:
        return detail
    return None


# Runtime ingress readiness failures are a fixed set of codes, safe to show.
_INGRESS_STATUS_DETAIL = re.compile(r"runtime_ingress:[a-z_]+(?::\d{3})?")
# Raw pod/scheduling text can leak registry hosts, secret names and node
# taints, so only its class is recorded (same markers as the app server).
_STATUS_DETAIL_CLASSES = (
    (("ImagePullBackOff", "ErrImagePull"), "image_pull_error"),
    (("CreateContainerConfigError",), "container_config_error"),
    (("CreateContainerError",), "container_create_error"),
    (("Insufficient cpu",), "insufficient_cpu"),
    (("Insufficient memory",), "insufficient_memory"),
    (("Insufficient ephemeral-storage",), "insufficient_ephemeral_storage"),
    (("Insufficient pods",), "insufficient_pods"),
    (
        (
            "untolerated taint",
            "node affinity",
            "node selector",
            "volume node affinity conflict",
            "topology spread",
            "unbound",
        ),
        "scheduling_constraint",
    ),
)


def summarize_status_detail(detail: Any) -> str | None:
    """Return a user-safe summary of a runtime ``status_detail``."""
    if not isinstance(detail, str) or not detail:
        return None
    if _INGRESS_STATUS_DETAIL.fullmatch(detail):
        return detail
    for markers, label in _STATUS_DETAIL_CLASSES:
        if any(marker in detail for marker in markers):
            return label
    return "unclassified"


# The app server's conversation start: it attaches to the run's sandbox when
# ``sandbox_id`` is set and applies the owner's profile, MCP servers and secrets.
APP_CONVERSATIONS_PATH = "/api/v1/app-conversations"
# The owner's settings; ``provider_tokens_set`` names their git connections.
SETTINGS_PATH = "/api/v1/settings"
# A slow settings read only means the runner clones every repository itself.
GIT_CONNECTIONS_TIMEOUT_SECONDS = 15.0


def _lacks_conversation_start(resp: httpx.Response) -> bool:
    """Whether an error response means the app server has no such contract.

    That is a missing route (FastAPI's plain 404 or a 405) or a body it cannot
    validate (a 422 listing field errors, as for an unknown ``trigger``). A 404
    or 422 with the app server's own detail, such as an agent profile it cannot
    find or resolve, is a refusal: falling back would run the default agent.
    """
    if resp.status_code == 405:
        return True
    if resp.status_code not in (404, 422):
        return False
    try:
        detail = resp.json().get("detail")
    except Exception:
        return resp.status_code == 404
    if resp.status_code == 404:
        return detail == "Not Found"
    return isinstance(detail, list)


def _response_detail(resp: httpx.Response) -> str:
    """The ``detail`` of an API error response, else its text, kept short."""
    try:
        body = resp.json()
    except Exception:
        return resp.text[:500]
    if isinstance(body, dict) and body.get("detail") is not None:
        detail = body["detail"]
        if not isinstance(detail, str):
            detail = json.dumps(detail)
        return detail[:500]
    return resp.text[:500]


def launched_agent_profile_id(conversation: dict[str, Any]) -> str | None:
    """The profile an app-server conversation reports it launched from."""
    launched = conversation.get("launched_agent_profile")
    if not isinstance(launched, dict):
        return None
    profile_id = launched.get("agent_profile_id")
    return str(profile_id) if profile_id else None


class CloudSandboxBackend(ExecutionBackend):
    """Execution backend that creates Cloud sandboxes per run.

    This is the default backend for OpenHands Cloud deployments.
    Each automation run gets a fresh, isolated sandbox.

    The backend is instantiated per-run with an AutomationRun. The per-user
    API key is minted lazily on first use and cached. If an authentication
    error occurs (401/403), the key is refreshed and the operation retried.
    """

    def __init__(self, api_url: str, run: AutomationRun):
        """Initialize the Cloud sandbox backend for a specific run.

        Args:
            api_url: OpenHands Cloud API URL (e.g., "https://app.all-hands.dev")
            run: The automation run (used to extract user info for API key)
        """
        self.api_url = api_url.rstrip("/")
        self._run = run
        self._api_key: str | None = None  # Lazily minted

        # Load sandbox config for retry/timeout settings
        sandbox_config = get_config().sandbox
        self._ready_timeout = sandbox_config.sandbox_ready_timeout
        self._poll_interval = sandbox_config.sandbox_poll_interval

        # Configure retry decorator for rate limiting
        self._retry = retry(
            retry=retry_if_exception(_is_rate_limit_error),
            stop=stop_after_attempt(sandbox_config.rate_limit_max_retries),
            wait=wait_exponential(
                min=sandbox_config.rate_limit_min_wait,
                max=sandbox_config.rate_limit_max_wait,
            ),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )

    @property
    def is_local_mode(self) -> bool:
        return False

    def get_work_dir(self, run_id: str) -> str:  # noqa: ARG002
        """Return the standard container working directory.

        In Cloud mode, sandboxes have a fresh filesystem with /workspace available.
        """
        return "/workspace/project"

    async def _ensure_api_key(self) -> str:
        """Ensure the per-user API key is minted and return it.

        The key is minted lazily on first call and cached for reuse.
        """
        if self._api_key is None:
            self._api_key = await get_api_key_for_automation_run(self._run)
        return self._api_key

    async def _refresh_api_key(self) -> str:
        """Force refresh the API key (e.g., after auth failure)."""
        logger.info("Refreshing API key after authentication failure")
        self._api_key = await get_api_key_for_automation_run(self._run)
        return self._api_key

    async def _with_auth_retry(
        self,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        """Execute operation with auth retry on 401/403.

        If the operation fails with an authentication error, refresh the
        API key and retry once.
        """
        try:
            return await operation()
        except Exception as e:
            if _is_auth_error(e):
                await self._refresh_api_key()
                return await operation()
            raise

    async def get_execution_context(
        self, client: httpx.AsyncClient
    ) -> ExecutionContext:
        """Create a sandbox and wait for it to be ready."""

        async def _do_acquire() -> tuple[str, str, str]:
            return await self._create_and_wait(client, await self._ensure_api_key())

        sandbox_id, session_key, agent_url = await self._with_auth_retry(_do_acquire)

        return ExecutionContext(
            agent_url=agent_url,
            session_key=session_key,
            sandbox_id=sandbox_id,
            api_url=self.api_url,
            api_key=await self._ensure_api_key(),
        )

    async def release_context(
        self, client: httpx.AsyncClient, ctx: ExecutionContext
    ) -> None:
        """Delete the sandbox."""
        if ctx.sandbox_id and ctx.api_url and ctx.api_key:
            await delete_sandbox(client, ctx.api_url, ctx.api_key, ctx.sandbox_id)

    async def start_conversation(
        self,
        client: httpx.AsyncClient,
        ctx: ExecutionContext,
        start: ConversationStart,
    ) -> StartedConversation:
        """Ask the app server to start the run's conversation in its sandbox.

        The request carries no prompt and no secrets: the runner sends the
        prompt once it has attached, and the app server resolves the LLM key,
        MCP servers and secrets itself. Raises ConversationStartUnavailable
        when the app server has no such contract (the caller falls back to the
        runner creating the conversation) and ConversationStartError when it
        refused or failed.
        """
        if not ctx.sandbox_id:
            raise ConversationStartError(
                "Cloud runs need a sandbox to start the conversation in",
                code="no_sandbox",
            )
        body: dict[str, Any] = {
            "sandbox_id": ctx.sandbox_id,
            "trigger": "automation",
            "tags": start.tags,
        }
        if start.agent_profile_id:
            body["agent_profile_id"] = start.agent_profile_id
        if start.llm_profile:
            body["llm_profile"] = start.llm_profile
        if start.conversation_id:
            body["conversation_id"] = start.conversation_id
        if start.plugins:
            body["plugins"] = start.plugins
        if start.repositories:
            primary, *others = start.repositories
            body["selected_repository"] = primary["repository"]
            body["git_provider"] = primary["git_provider"]
            if primary.get("branch"):
                body["selected_branch"] = primary["branch"]
            if others:
                body["additional_repositories"] = [
                    {k: v for k, v in repo.items() if v is not None} for repo in others
                ]
                # As the runner did: a repository that fails to clone fails the run.
                body["require_all"] = True

        task = await self._app_server_request(
            client, "POST", APP_CONVERSATIONS_PATH, json=body, start_request=True
        )
        conversation_id = await self._wait_for_start_task(client, task)
        conversations = await self._app_server_request(
            client, "GET", APP_CONVERSATIONS_PATH, params={"ids": conversation_id}
        )
        conversation = (
            conversations[0]
            if isinstance(conversations, list) and conversations
            else None
        )
        if not isinstance(conversation, dict):
            raise ConversationStartError(
                f"Conversation {conversation_id} is not visible to its owner",
                code="conversation_missing",
            )
        return StartedConversation(
            conversation_id=conversation_id,
            agent_profile_id=launched_agent_profile_id(conversation),
            reports_repositories="additional_repositories" in conversation,
        )

    async def git_connections(
        self,
        client: httpx.AsyncClient,
        ctx: ExecutionContext,  # noqa: ARG002
    ) -> dict[str, str | None]:
        """The git providers the run's owner is connected to, with their hosts.

        The app server clones a repository only through the owner's connection
        to its provider, on that connection's host. ``provider_tokens_set`` in
        the owner's settings lists them as ``{provider: host}``; it holds hosts,
        not tokens.
        """
        settings = await self._app_server_request(
            client,
            "GET",
            SETTINGS_PATH,
            action="read the owner's git connections",
            timeout=GIT_CONNECTIONS_TIMEOUT_SECONDS,
        )
        connections = (
            settings.get("provider_tokens_set") if isinstance(settings, dict) else None
        )
        if not isinstance(connections, dict):
            return {}
        return {
            str(provider): host if isinstance(host, str) and host else None
            for provider, host in connections.items()
        }

    async def _wait_for_start_task(
        self, client: httpx.AsyncClient, task: dict[str, Any]
    ) -> str:
        """Poll a conversation start task until READY; return the conversation id."""
        task_id = task.get("id")
        elapsed = 0.0
        while True:
            status = task.get("status")
            if status == "READY" and task.get("app_conversation_id"):
                return str(task["app_conversation_id"])
            if status == "ERROR":
                raise ConversationStartError(
                    "OpenHands API failed to start the conversation: "
                    f"{task.get('detail') or 'unknown error'}",
                    code="start_task_error",
                )
            if elapsed >= self._ready_timeout:
                raise ConversationStartError(
                    f"Conversation start task {task_id} not ready after "
                    f"{self._ready_timeout}s",
                    transient=True,
                    code="start_task_timeout",
                )
            await asyncio.sleep(self._poll_interval)
            elapsed += self._poll_interval
            tasks = await self._app_server_request(
                client,
                "GET",
                f"{APP_CONVERSATIONS_PATH}/start-tasks",
                params={"ids": task_id},
            )
            if not (isinstance(tasks, list) and tasks and isinstance(tasks[0], dict)):
                raise ConversationStartError(
                    f"Conversation start task {task_id} disappeared",
                    code="start_task_missing",
                )
            task = tasks[0]

    async def _app_server_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        start_request: bool = False,
        action: str = "start the conversation",
        **kwargs: Any,
    ) -> Any:
        """Call the OpenHands API as the run's user, in the automation's org.

        429s are retried and a 401/403 re-mints the key once, as for sandboxes.
        ``start_request`` marks the conversation start, whose errors can also
        mean an app server without that contract.
        """

        @self._retry
        async def _do_request() -> Any:
            resp = await client.request(
                method,
                f"{self.api_url}{path}",
                headers={
                    "Authorization": f"Bearer {await self._ensure_api_key()}",
                    X_ORG_ID_HEADER: str(self._run.automation.org_id),
                },
                **kwargs,
            )
            if resp.status_code in (401, 403, 429) or resp.status_code >= 500:
                resp.raise_for_status()
            if start_request and _lacks_conversation_start(resp):
                raise ConversationStartUnavailable(
                    "OpenHands API cannot start conversations in a sandbox "
                    f"(HTTP {resp.status_code}): {_response_detail(resp)}",
                    status_code=resp.status_code,
                )
            if resp.status_code >= 400:
                raise ConversationStartError(
                    f"OpenHands API refused to {action} "
                    f"(HTTP {resp.status_code}): {_response_detail(resp)}",
                    status_code=resp.status_code,
                )
            return resp.json()

        return await self._with_auth_retry(_do_request)

    async def get_api_key(self) -> str:
        """Return the per-user API key (minting if needed)."""
        return await self._ensure_api_key()

    async def plugin_source_secrets(
        self, client: httpx.AsyncClient, names: Collection[str]
    ) -> PluginSourceSecrets:
        """Lookup references for the identity the automation runs as."""
        if not names:
            return PluginSourceSecrets()
        automation = self._run.automation
        return await cloud_plugin_source_secrets(
            client,
            names,
            user_id=automation.user_id,
            org_id=automation.org_id,
            automation_id=automation.id,
        )

    def build_env_vars(self) -> dict[str, str]:
        """Build Cloud mode environment variables.

        Note: This is synchronous. Caller must ensure _ensure_api_key()
        was called first (e.g., via acquire() or get_api_key()).
        """
        if self._api_key is None:
            raise RuntimeError(
                "API key not initialized. Call get_api_key() or acquire() first."
            )
        return {
            "OPENHANDS_API_KEY": self._api_key,
            "OPENHANDS_CLOUD_API_URL": self.api_url,
            # The presets pick their workspace from this. Cloud runs never
            # get AGENT_SERVER_URL; scripts read it as a local Agent Canvas.
            "AUTOMATION_RUN_MODE": "cloud",
        }

    async def verify_run(self, run_id: str) -> VerificationResult:
        """Verify run status via sandbox discovery."""
        sandbox_id = self._run.sandbox_id
        if not sandbox_id:
            from openhands.automation.utils.agent_server import (
                VerificationOutcome,
                VerificationResult,
            )

            return VerificationResult(
                outcome=VerificationOutcome.ENVIRONMENT_UNAVAILABLE,
                detail="No sandbox_id available for verification",
            )

        async def _do_verify() -> VerificationResult:
            return await verify_run_status(
                api_url=self.api_url,
                api_key=await self._ensure_api_key(),
                sandbox_id=sandbox_id,
                run_id=run_id,
                bash_command_id=self._run.bash_command_id,
            )

        return await self._with_auth_retry(_do_verify)

    async def cleanup_after_verification(self, run_id: str) -> None:
        """Clean up sandbox after verification failure."""
        sandbox_id = self._run.sandbox_id
        if sandbox_id:

            async def _do_cleanup() -> None:
                await cleanup_sandbox(
                    api_url=self.api_url,
                    api_key=await self._ensure_api_key(),
                    sandbox_id=sandbox_id,
                    run_id=run_id,
                )

            await self._with_auth_retry(_do_cleanup)

    async def _create_and_wait(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        ready_timeout: float | None = None,
    ) -> tuple[str, str, str]:
        """Create a sandbox and poll until RUNNING.

        Returns (sandbox_id, session_api_key, agent_server_url). If the sandbox
        never becomes usable it is deleted before the error propagates.
        """
        if ready_timeout is None:
            ready_timeout = self._ready_timeout

        headers = {"Authorization": f"Bearer {api_key}"}
        sandbox_id = await self._create_sandbox(client, headers)
        try:
            return await self._wait_until_running(
                client, sandbox_id, headers, ready_timeout
            )
        except Exception:
            # Nothing has run in it yet; don't leave it counting against the
            # user's sandbox limit. delete_sandbox never raises.
            await delete_sandbox(client, self.api_url, api_key, sandbox_id)
            raise

    async def _wait_until_running(
        self,
        client: httpx.AsyncClient,
        sandbox_id: str,
        headers: dict[str, str],
        ready_timeout: float,
    ) -> tuple[str, str, str]:
        elapsed = 0.0
        sb: dict[str, Any] = {}
        while elapsed < ready_timeout:
            sb = await self._poll_sandbox(client, sandbox_id, headers)
            status = sb.get("status", "UNKNOWN")

            if status == "RUNNING":
                result = find_agent_server_url(sb)
                if result is None:
                    raise RuntimeError(f"No AGENT_SERVER URL in sandbox {sandbox_id}")
                agent_url, session_key = result
                return sandbox_id, session_key, agent_url

            if status in ("ERROR", "MISSING"):
                raise self._provisioning_error(sandbox_id, sb, f"status={status}")

            await asyncio.sleep(self._poll_interval)
            elapsed += self._poll_interval

        raise self._provisioning_error(
            sandbox_id,
            sb,
            f"not ready after {ready_timeout}s, status={sb.get('status', 'UNKNOWN')}",
            timed_out=True,
        )

    @staticmethod
    def _provisioning_error(
        sandbox_id: str,
        sandbox: dict[str, Any],
        what: str,
        timed_out: bool = False,
    ) -> SandboxProvisioningError:
        raw_detail = sandbox.get("status_detail")
        reason = summarize_status_detail(raw_detail)
        logger.warning(
            "Sandbox %s failed to provision: %s, status_detail=%r",
            sandbox_id,
            what,
            raw_detail,
        )
        message = f"Sandbox failed to start: {what}"
        if reason:
            message += f", status_detail={reason}"
        return SandboxProvisioningError(
            message,
            sandbox_id=sandbox_id,
            status=str(sandbox.get("status", "UNKNOWN")),
            reason=reason,
            timed_out=timed_out,
        )

    async def _create_sandbox(
        self, client: httpx.AsyncClient, headers: dict[str, str]
    ) -> str:
        """Create a sandbox and return its ID."""

        @self._retry
        async def _do_create():
            resp = await client.post(
                f"{self.api_url}/api/v1/sandboxes", headers=headers
            )
            # A concurrency-limit 429 is not transient: retrying won't free a
            # slot. Raise a non-HTTPStatusError so the retry predicate skips it
            # and the dispatcher can mark the run SKIPPED instead of FAILED.
            detail = _concurrency_limit_detail(resp)
            if detail is not None:
                raise ConcurrencyLimitReachedError(
                    detail.get("message") or "Concurrency limit reached"
                )
            # Same for the daily sandbox quota: it only resets at UTC
            # midnight, so retrying now cannot succeed.
            detail = _daily_sandbox_limit_detail(resp)
            if detail is not None:
                raise DailySandboxLimitReachedError(
                    detail.get("message") or "Daily sandbox limit reached"
                )
            resp.raise_for_status()
            return resp.json()["id"]

        return await _do_create()

    async def _poll_sandbox(
        self, client: httpx.AsyncClient, sandbox_id: str, headers: dict[str, str]
    ) -> dict[str, Any]:
        """Poll sandbox status."""

        @self._retry
        async def _do_poll():
            resp = await client.get(
                f"{self.api_url}/api/v1/sandboxes",
                params={"id": sandbox_id},
                headers=headers,
            )
            resp.raise_for_status()
            items = resp.json()
            if not items:
                raise RuntimeError(f"Sandbox {sandbox_id} disappeared")
            # The batch endpoint returns null for an unknown id.
            return items[0] or {"id": sandbox_id, "status": "MISSING"}

        return await _do_poll()
