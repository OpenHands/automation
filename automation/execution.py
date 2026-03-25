"""Sandbox execution for automation runs.

One function does the whole job: spin up a sandbox, upload a tarball,
extract it, run setup, run the entrypoint, tear down.
"""

import asyncio
import io
import logging
import tarfile
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from automation.constants import (
    EXTERNAL_DOWNLOAD_TIMEOUT,
    EXTERNAL_MAX_FILESIZE,
    MAX_RUN_DURATION_SECONDS,
    RATE_LIMIT_MAX_RETRIES,
    RATE_LIMIT_MAX_WAIT,
    RATE_LIMIT_MIN_WAIT,
    SANDBOX_POLL_INTERVAL,
    SANDBOX_READY_TIMEOUT,
    TARBALL_PATH,
    WORK_DIR,
)
from automation.utils.sandbox import delete_sandbox


logger = logging.getLogger(__name__)


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Check if exception is a 429 rate limit error."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429
    return False


def _log_extra(
    run_id: str | None = None, sandbox_id: str | None = None
) -> dict[str, Any]:
    """Build extra dict for structured logging with run/sandbox IDs."""
    extra: dict[str, Any] = {}
    if run_id:
        extra["run_id"] = run_id
    if sandbox_id:
        extra["sandbox_id"] = sandbox_id
    return extra


# Tenacity retry decorator for rate limit handling
_retry_on_rate_limit = retry(
    retry=retry_if_exception(_is_rate_limit_error),
    stop=stop_after_attempt(RATE_LIMIT_MAX_RETRIES),
    wait=wait_exponential(min=RATE_LIMIT_MIN_WAIT, max=RATE_LIMIT_MAX_WAIT),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


def build_tarball(files: dict[str, str | bytes]) -> bytes:
    """Build a .tar.gz in memory from ``{relative_path: content}``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# -- Sandbox helpers (private) ------------------------------------------------


def _find_agent_server_url(sandbox: dict) -> tuple[str, str] | None:
    """Return ``(agent_url, session_key)`` if an AGENT_SERVER URL exists."""
    for url_info in sandbox.get("exposed_urls") or []:
        if url_info.get("name") == "AGENT_SERVER":
            return url_info["url"].rstrip("/"), sandbox.get("session_api_key", "")
    return None


@_retry_on_rate_limit
async def _create_sandbox(
    client: httpx.AsyncClient, api_url: str, headers: dict[str, str]
) -> str:
    """Create a sandbox and return its ID. Retries on rate limit."""
    resp = await client.post(f"{api_url}/api/v1/sandboxes", headers=headers)
    resp.raise_for_status()
    return resp.json()["id"]


@_retry_on_rate_limit
async def _poll_sandbox(
    client: httpx.AsyncClient, api_url: str, sandbox_id: str, headers: dict[str, str]
) -> dict[str, Any]:
    """Poll sandbox status. Retries on rate limit."""
    resp = await client.get(
        f"{api_url}/api/v1/sandboxes",
        params={"id": sandbox_id},
        headers=headers,
    )
    resp.raise_for_status()
    items = resp.json()
    if not items:
        raise RuntimeError(f"Sandbox {sandbox_id} disappeared")
    return items[0]


async def _create_and_wait(
    client: httpx.AsyncClient,
    api_url: str,
    api_key: str,
    ready_timeout: float = SANDBOX_READY_TIMEOUT,
) -> tuple[str, str, str]:
    """Create a sandbox and poll until RUNNING.

    Returns ``(sandbox_id, session_api_key, agent_server_url)``.
    Handles 429 rate limits via tenacity retry.
    """
    headers = {"Authorization": f"Bearer {api_key}"}

    sandbox_id = await _create_sandbox(client, api_url, headers)

    elapsed = 0.0
    while elapsed < ready_timeout:
        sb = await _poll_sandbox(client, api_url, sandbox_id, headers)
        status = sb.get("status", "UNKNOWN")

        if status == "RUNNING":
            result = _find_agent_server_url(sb)
            if result is None:
                raise RuntimeError(f"No AGENT_SERVER URL in sandbox {sandbox_id}")
            agent_url, session_key = result
            return sandbox_id, session_key, agent_url

        if status in ("ERROR", "MISSING"):
            # Extract error details from sandbox response
            error_code = sb.get("error_code", "")
            error_message = sb.get("error_message", "")
            error_detail = f"status={status}"
            if error_code:
                error_detail += f", error_code={error_code}"
            if error_message:
                error_detail += f", error_message={error_message}"
            raise RuntimeError(f"Sandbox {sandbox_id} failed: {error_detail}")

        await asyncio.sleep(SANDBOX_POLL_INTERVAL)
        elapsed += SANDBOX_POLL_INTERVAL

    raise TimeoutError(f"Sandbox {sandbox_id} not ready after {ready_timeout}s")


async def _upload(
    client: httpx.AsyncClient,
    agent_url: str,
    session_key: str,
    data: bytes,
    dest: str,
) -> None:
    """Upload bytes to the sandbox via the agent-server file API.

    The agent-server expects the absolute path in the URL, e.g.
    ``POST /api/file/upload//tmp/file.tar.gz`` (double-slash is correct).
    """
    resp = await client.post(
        f"{agent_url}/api/file/upload/{dest}",
        files={"file": ("upload", data)},
        headers={"X-Session-API-Key": session_key},
    )
    resp.raise_for_status()


async def _bash(
    client: httpx.AsyncClient,
    agent_url: str,
    session_key: str,
    command: str,
    timeout: int = MAX_RUN_DURATION_SECONDS,
) -> tuple[int | None, str, str]:
    """Run a bash command synchronously. Returns ``(exit_code, stdout, stderr)``."""
    resp = await client.post(
        f"{agent_url}/api/bash/execute_bash_command",
        json={"command": command, "timeout": timeout},
        headers={"X-Session-API-Key": session_key},
        timeout=httpx.Timeout(timeout + 30),
    )
    resp.raise_for_status()
    body = resp.json()
    return body.get("exit_code"), body.get("stdout") or "", body.get("stderr") or ""


async def _start_bash(
    client: httpx.AsyncClient,
    agent_url: str,
    session_key: str,
    command: str,
    timeout: int = MAX_RUN_DURATION_SECONDS,
) -> str:
    """Start a bash command in the background. Returns the command ID."""
    resp = await client.post(
        f"{agent_url}/api/bash/start_bash_command",
        json={"command": command, "timeout": timeout},
        headers={"X-Session-API-Key": session_key},
        timeout=30.0,
    )
    resp.raise_for_status()
    body = resp.json()
    return body.get("id")


async def _download_in_sandbox(
    client: httpx.AsyncClient,
    agent_url: str,
    session_key: str,
    tarball_url: str,
    dest: str,
    timeout: int = EXTERNAL_DOWNLOAD_TIMEOUT,
    max_filesize: int = EXTERNAL_MAX_FILESIZE,
) -> None:
    """Download a tarball directly inside the sandbox using curl.

    This is used for external URLs (https://) to avoid downloading
    untrusted, potentially large files on the automation service.

    Raises RuntimeError if the download fails.
    """
    # Use curl with safety limits:
    # -f: fail silently on HTTP errors (returns exit code 22)
    # -s: silent mode (no progress)
    # -S: show errors even in silent mode
    # -L: follow redirects
    # --max-filesize: limit download size
    # --max-time: limit total time
    cmd = (
        f"curl -fsSL "
        f"--max-filesize {max_filesize} "
        f"--max-time {timeout} "
        f"-o {dest} "
        f"{_shell_quote(tarball_url)}"
    )

    exit_code, stdout, stderr = await _bash(
        client, agent_url, session_key, cmd, timeout=timeout + 30
    )

    if exit_code != 0:
        # curl exit codes: 22 = HTTP error, 63 = max filesize exceeded
        if exit_code == 63:
            raise RuntimeError(
                f"Tarball exceeds size limit ({max_filesize // 1024 // 1024} MB)"
            )
        raise RuntimeError(f"Failed to download tarball (exit={exit_code}): {stderr}")


# -- Public API ---------------------------------------------------------------


@dataclass(frozen=True)
class DispatchResult:
    """Result of dispatching an automation to a sandbox (fire-and-forget)."""

    success: bool
    sandbox_id: str | None = None
    error: str | None = None


async def dispatch_automation(
    api_url: str,
    api_key: str,
    entrypoint: str,
    tarball_source: bytes | str,
    env_vars: dict[str, str] | None = None,
    timeout: int = MAX_RUN_DURATION_SECONDS,
    callback_url: str | None = None,
    run_id: str | None = None,
) -> DispatchResult:
    """Dispatch an automation to a sandbox (fire-and-forget).

    1. Create sandbox and wait until RUNNING.
    2. Get tarball into sandbox (upload bytes OR download from URL).
    3. Extract it, run ``setup.sh`` (if present), then start *entrypoint*.
    4. Return immediately without waiting for the entrypoint to complete.

    The SDK inside the sandbox will POST to callback_url when finished.
    The caller should store sandbox_id to verify status later if needed.

    *tarball_source*: Either raw bytes (uploaded to sandbox) or a URL string
    (downloaded directly inside sandbox via curl). URLs avoid downloading
    untrusted/large files on the automation service.

    *env_vars* are exported before the entrypoint runs.  The sandbox
    identity env vars (``SANDBOX_ID``, ``SESSION_API_KEY``) are
    **always** injected so the SDK's ``saas_runtime_mode`` works.
    If *callback_url* / *run_id* are set they are injected as
    ``AUTOMATION_CALLBACK_URL`` / ``AUTOMATION_RUN_ID`` so the SDK's
    ``OpenHandsCloudWorkspace`` can POST completion status on exit.
    """
    env_vars = dict(env_vars) if env_vars else {}
    if callback_url:
        env_vars["AUTOMATION_CALLBACK_URL"] = callback_url
    if run_id:
        env_vars["AUTOMATION_RUN_ID"] = run_id
    api_url = api_url.rstrip("/")
    sandbox_id: str | None = None

    # Helper for consistent structured logging with run_id/sandbox_id
    def log_extra() -> dict[str, Any]:
        return _log_extra(run_id=run_id, sandbox_id=sandbox_id)

    logger.info("Dispatching automation to sandbox", extra=log_extra())

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            sandbox_id, session_key, agent_url = await _create_and_wait(
                client, api_url, api_key
            )
            logger.info(
                "Sandbox ready: %s at %s", sandbox_id, agent_url, extra=log_extra()
            )
        except Exception as e:
            # If sandbox creation started but failed to reach RUNNING,
            # still attempt cleanup.
            logger.exception("Sandbox creation failed", extra=log_extra())
            if sandbox_id:
                await delete_sandbox(client, api_url, api_key, sandbox_id)
            return DispatchResult(success=False, sandbox_id=sandbox_id, error=str(e))

        try:
            # Always inject sandbox identity so the SDK can call
            # get_llm() / get_secrets() inside the sandbox.
            env_vars.setdefault("SANDBOX_ID", sandbox_id)
            env_vars.setdefault("SESSION_API_KEY", session_key)

            # Get tarball into sandbox: upload bytes or download from URL
            if isinstance(tarball_source, bytes):
                logger.info("Uploading tarball to sandbox", extra=log_extra())
                await _upload(
                    client, agent_url, session_key, tarball_source, TARBALL_PATH
                )
            else:
                logger.info(
                    "Downloading tarball in sandbox from URL", extra=log_extra()
                )
                await _download_in_sandbox(
                    client, agent_url, session_key, tarball_source, TARBALL_PATH
                )

            exports = ""
            if env_vars:
                parts = [f"export {k}={_shell_quote(v)}" for k, v in env_vars.items()]
                exports = " && ".join(parts) + " && "

            cmd = (
                f"mkdir -p {WORK_DIR}"
                f" && tar xzf {TARBALL_PATH} -C {WORK_DIR}"
                f" && cd {WORK_DIR}"
                f" && ([ ! -f setup.sh ] || bash setup.sh)"
                f" && {exports}{entrypoint}"
            )

            logger.info("Starting entrypoint: %s", entrypoint, extra=log_extra())
            command_id = await _start_bash(
                client, agent_url, session_key, cmd, timeout=timeout
            )
            logger.info(
                "Entrypoint started (command_id=%s), disconnecting",
                command_id,
                extra=log_extra(),
            )

            return DispatchResult(success=True, sandbox_id=sandbox_id)

        except Exception as e:
            logger.exception("Automation dispatch failed", extra=log_extra())
            # Delete sandbox on dispatch failure to avoid orphaned sandboxes
            if sandbox_id:
                await delete_sandbox(client, api_url, api_key, sandbox_id)
            return DispatchResult(success=False, sandbox_id=sandbox_id, error=str(e))


@dataclass(frozen=True)
class AutomationResult:
    """Result of running an automation (blocking mode)."""

    success: bool
    sandbox_id: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


async def run_automation(
    api_url: str,
    api_key: str,
    entrypoint: str,
    tarball_source: bytes | str,
    env_vars: dict[str, str] | None = None,
    timeout: int = MAX_RUN_DURATION_SECONDS,
    callback_url: str | None = None,
    run_id: str | None = None,
    keep_sandbox: bool = False,
) -> AutomationResult:
    """Execute an automation end-to-end in a fresh sandbox (blocking).

    Use this for testing or when you need to wait for the result immediately.
    For production async execution, use dispatch_automation() instead.

    1. Create sandbox and wait until RUNNING.
    2. Get tarball into sandbox (upload bytes OR download from URL).
    3. Extract it, run ``setup.sh`` (if present), then run *entrypoint*.
    4. Wait for completion and return the result.
    5. Delete the sandbox (unless *keep_sandbox* is True).

    *tarball_source*: Either raw bytes (uploaded to sandbox) or a URL string
    (downloaded directly inside sandbox via curl). URLs avoid downloading
    untrusted/large files on the automation service.

    *env_vars* are exported before the entrypoint runs.  The sandbox
    identity env vars (``SANDBOX_ID``, ``SESSION_API_KEY``) are
    **always** injected so the SDK's ``saas_runtime_mode`` works.
    If *callback_url* / *run_id* are set they are injected as
    ``AUTOMATION_CALLBACK_URL`` / ``AUTOMATION_RUN_ID`` so the SDK's
    ``OpenHandsCloudWorkspace`` can POST completion status on exit.
    """
    env_vars = dict(env_vars) if env_vars else {}
    if callback_url:
        env_vars["AUTOMATION_CALLBACK_URL"] = callback_url
    if run_id:
        env_vars["AUTOMATION_RUN_ID"] = run_id
    api_url = api_url.rstrip("/")
    sandbox_id: str | None = None

    # Helper for consistent structured logging with run_id/sandbox_id
    def log_extra() -> dict[str, Any]:
        return _log_extra(run_id=run_id, sandbox_id=sandbox_id)

    logger.info("Starting automation execution", extra=log_extra())

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            sandbox_id, session_key, agent_url = await _create_and_wait(
                client, api_url, api_key
            )
            logger.info(
                "Sandbox ready: %s at %s", sandbox_id, agent_url, extra=log_extra()
            )
        except Exception as e:
            # If sandbox creation started but failed to reach RUNNING,
            # still attempt cleanup.
            logger.exception("Sandbox creation failed", extra=log_extra())
            if sandbox_id:
                await delete_sandbox(client, api_url, api_key, sandbox_id)
            return AutomationResult(success=False, sandbox_id=sandbox_id, error=str(e))

        try:
            # Always inject sandbox identity so the SDK can call
            # get_llm() / get_secrets() inside the sandbox.
            env_vars.setdefault("SANDBOX_ID", sandbox_id)
            env_vars.setdefault("SESSION_API_KEY", session_key)

            # Get tarball into sandbox: upload bytes or download from URL
            if isinstance(tarball_source, bytes):
                logger.info("Uploading tarball to sandbox", extra=log_extra())
                await _upload(
                    client, agent_url, session_key, tarball_source, TARBALL_PATH
                )
            else:
                logger.info(
                    "Downloading tarball in sandbox from URL", extra=log_extra()
                )
                await _download_in_sandbox(
                    client, agent_url, session_key, tarball_source, TARBALL_PATH
                )

            exports = ""
            if env_vars:
                parts = [f"export {k}={_shell_quote(v)}" for k, v in env_vars.items()]
                exports = " && ".join(parts) + " && "

            cmd = (
                f"mkdir -p {WORK_DIR}"
                f" && tar xzf {TARBALL_PATH} -C {WORK_DIR}"
                f" && cd {WORK_DIR}"
                f" && ([ ! -f setup.sh ] || bash setup.sh)"
                f" && {exports}{entrypoint}"
            )

            logger.info("Executing entrypoint: %s", entrypoint, extra=log_extra())
            exit_code, stdout, stderr = await _bash(
                client, agent_url, session_key, cmd, timeout=timeout
            )

            success = exit_code == 0
            error_msg = None
            if not success:
                # Include both stderr and stdout tail - some errors go to stdout
                error_parts = [f"exit_code={exit_code}"]
                if stderr:
                    error_parts.append(f"stderr: {stderr[-1000:]}")
                if stdout:
                    error_parts.append(f"stdout: {stdout[-500:]}")
                error_msg = "\n".join(error_parts)
                logger.warning(
                    "Entrypoint failed with exit_code=%s", exit_code, extra=log_extra()
                )
            else:
                logger.info("Entrypoint completed successfully", extra=log_extra())

            return AutomationResult(
                success=success,
                sandbox_id=sandbox_id,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                error=error_msg,
            )

        except Exception as e:
            logger.exception("Automation execution failed", extra=log_extra())
            return AutomationResult(success=False, sandbox_id=sandbox_id, error=str(e))
        finally:
            if not keep_sandbox:
                logger.info("Deleting sandbox", extra=log_extra())
                await delete_sandbox(client, api_url, api_key, sandbox_id)


def _shell_quote(s: str) -> str:
    """Single-quote a string for safe shell interpolation."""
    return "'" + s.replace("'", "'\\''") + "'"
