"""Sandbox execution for automation runs.

One function does the whole job: spin up a sandbox, upload a tarball,
extract it, run setup, run the entrypoint, tear down.
"""

import asyncio
import base64
import io
import logging
import os
import re
import tarfile
import tempfile
import textwrap
from typing import Any

import httpx
from pydantic.dataclasses import dataclass
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from openhands.automation.config import get_config
from openhands.automation.exceptions import PermanentDispatchError, TarballNotFoundError
from openhands.automation.utils import log_extra
from openhands.automation.utils.sandbox import delete_sandbox
from openhands.automation.utils.timeout import resolve_automation_timeout_seconds


# Fallback tarball destination when no per-run path is available.
# Uses the platform temp directory so the path is valid on every OS
# (Windows agent-servers reject bare POSIX /tmp/ paths).
# Not a protocol constant — the sandbox has no fixed expectation for this
# filename; it is only used by run_automation() (E2E test helper) and as a
# last-resort fallback in execute_in_context() when run_id is absent.
_FALLBACK_TARBALL_PATH = os.path.join(tempfile.gettempdir(), "automation.tar.gz")

# Default working directory for cloud/container mode
DEFAULT_WORK_DIR = "/workspace/project"

logger = logging.getLogger(__name__)


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Check if exception is a 429 rate limit error."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429
    return False


# Module-level retry decorator for sandbox operations.
# Config is read at import time and frozen for the process lifetime.
_sandbox_config = get_config().sandbox
_sandbox_retry = retry(
    retry=retry_if_exception(_is_rate_limit_error),
    stop=stop_after_attempt(_sandbox_config.rate_limit_max_retries),
    wait=wait_exponential(
        min=_sandbox_config.rate_limit_min_wait,
        max=_sandbox_config.rate_limit_max_wait,
    ),
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


async def _create_sandbox(
    client: httpx.AsyncClient, api_url: str, headers: dict[str, str]
) -> str:
    """Create a sandbox and return its ID. Retries on rate limit."""

    @_sandbox_retry
    async def _do_create():
        resp = await client.post(f"{api_url}/api/v1/sandboxes", headers=headers)
        resp.raise_for_status()
        return resp.json()["id"]

    return await _do_create()


async def _poll_sandbox(
    client: httpx.AsyncClient, api_url: str, sandbox_id: str, headers: dict[str, str]
) -> dict[str, Any]:
    """Poll sandbox status. Retries on rate limit."""

    @_sandbox_retry
    async def _do_poll():
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

    return await _do_poll()


async def _create_and_wait(
    client: httpx.AsyncClient,
    api_url: str,
    api_key: str,
    ready_timeout: float | None = None,
) -> tuple[str, str, str]:
    """Create a sandbox and poll until RUNNING.

    Returns ``(sandbox_id, session_api_key, agent_server_url)``.
    Handles 429 rate limits via tenacity retry.
    """
    sandbox_config = get_config().sandbox
    if ready_timeout is None:
        ready_timeout = sandbox_config.sandbox_ready_timeout
    poll_interval = sandbox_config.sandbox_poll_interval

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

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    raise TimeoutError(f"Sandbox {sandbox_id} not ready after {ready_timeout}s")


async def _upload(
    client: httpx.AsyncClient,
    agent_url: str,
    session_key: str,
    data: bytes,
    dest: str,
) -> None:
    """Upload bytes to the sandbox via the agent-server file API.

    Uses query parameter for the path to avoid URL normalization issues
    with proxies that collapse double-slashes (e.g. //tmp -> /tmp).
    See: https://github.com/All-Hands-AI/OpenHands/commit/a14158e
    """
    # Use query param instead of path param to avoid double-slash normalization
    from urllib.parse import urlencode

    params = urlencode({"path": dest})
    resp = await client.post(
        f"{agent_url}/api/file/upload?{params}",
        files={"file": ("upload", data)},
        headers={"X-Session-API-Key": session_key},
    )
    resp.raise_for_status()


def _get_python_launcher() -> str:
    """Return a cross-platform Python launcher for inline runner commands.

    Preset automations run inside sandboxes where uv is guaranteed to be
    present but a bare ``python`` binary may not be on PATH.  ``uv run python``
    delegates interpreter resolution to uv so we never rely on a system-level
    Python installation.
    """
    return "uv run python"


def _build_python_runner_command(
    entrypoint: str,
    tarball_path: str,
    work_dir: str,
    env_vars: dict[str, str],
) -> str:
    """Build a cross-platform Python runner command.

    This path avoids POSIX shell features entirely. The inline Python snippet
    creates the working directory, extracts the tarball, injects environment
    variables, and then launches the requested entrypoint from inside the
    extracted automation directory.
    """
    payload = textwrap.dedent(
        f"""
        import os
        import pathlib
        import subprocess
        import tarfile

        tarball_path = {tarball_path!r}
        work_dir = {work_dir!r}
        entrypoint = {entrypoint!r}
        env_updates = {env_vars!r}

        pathlib.Path(work_dir).mkdir(parents=True, exist_ok=True)
        with tarfile.open(tarball_path, 'r:gz') as tar:
            tar.extractall(work_dir)
        try:
            os.remove(tarball_path)
        except FileNotFoundError:
            pass
        env = os.environ.copy()
        env.update(env_updates)
        result = subprocess.run(entrypoint, shell=True, cwd=work_dir, env=env)
        raise SystemExit(result.returncode)
        """
    ).strip()
    encoded_payload = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    return (
        f"{_get_python_launcher()} -c "
        f"\"import base64; exec(base64.b64decode('{encoded_payload}').decode())\""
    )


def _build_shell_runner_command(
    entrypoint: str,
    tarball_path: str,
    work_dir: str,
    env_vars: dict[str, str],
    setup_script_path: str | None,
) -> str:
    """Build the legacy POSIX shell runner command."""
    exports = ""
    if env_vars:
        parts = [f"export {k}={_shell_quote(v)}" for k, v in env_vars.items()]
        exports = " && ".join(parts) + " && "

    base_command = (
        f"mkdir -p {work_dir}"
        f" && tar xzf {tarball_path} -C {work_dir}"
        f" && rm -f {tarball_path}"
        f" && cd {work_dir}"
    )

    if not setup_script_path:
        return f"{base_command} && {exports}{entrypoint}"

    quoted_setup_path = _shell_quote(setup_script_path)
    return (
        f"{base_command}"
        f" && {exports}([ ! -f {quoted_setup_path} ] || bash {quoted_setup_path})"
        f" && {entrypoint}"
    )


async def _bash(
    client: httpx.AsyncClient,
    agent_url: str,
    session_key: str,
    command: str,
    timeout: int | None = None,
) -> tuple[int | None, str, str]:
    """Run a bash command synchronously. Returns ``(exit_code, stdout, stderr)``."""
    if timeout is None:
        timeout = resolve_automation_timeout_seconds(None)
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
    timeout: int | None = None,
) -> str:
    """Start a bash command in the background. Returns the command ID."""
    if timeout is None:
        timeout = resolve_automation_timeout_seconds(None)
    http_timeout = get_config().http.http_timeout
    resp = await client.post(
        f"{agent_url}/api/bash/start_bash_command",
        json={"command": command, "timeout": timeout},
        headers={"X-Session-API-Key": session_key},
        timeout=http_timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    return body.get("id")


def _is_permanent_http_error(stderr: str) -> bool:
    """Check if curl stderr indicates a permanent HTTP error (4xx client errors).

    We only treat 4xx errors as permanent because they indicate the URL is wrong
    or inaccessible (404 Not Found, 403 Forbidden, 401 Unauthorized, etc.).
    5xx errors are transient server issues that may resolve on retry.

    Returns True if the error is permanent and the automation should be disabled.
    """
    # curl error format: "The requested URL returned error: 404"
    # We look for 4xx status codes
    match = re.search(r"returned error:\s*(\d{3})", stderr)
    if match:
        status_code = int(match.group(1))
        return 400 <= status_code < 500
    return False


async def _download_in_sandbox(
    client: httpx.AsyncClient,
    agent_url: str,
    session_key: str,
    tarball_url: str,
    dest: str,
    timeout: int | None = None,
    max_filesize: int | None = None,
) -> None:
    """Download a tarball directly inside the sandbox using curl.

    This is used for external URLs (https://) to avoid downloading
    untrusted, potentially large files on the automation service.

    Raises:
        TarballNotFoundError: If the URL returns a 4xx HTTP error (permanent).
            This indicates the URL is wrong or inaccessible.
        RuntimeError: For other download failures (transient).
    """
    sandbox_config = get_config().sandbox
    if timeout is None:
        timeout = sandbox_config.external_download_timeout
    if max_filesize is None:
        max_filesize = sandbox_config.external_max_filesize

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

        # Check if this is a permanent HTTP error (4xx)
        if exit_code == 22 and _is_permanent_http_error(stderr):
            raise TarballNotFoundError(
                f"External tarball URL is not accessible: {tarball_url}. "
                f"HTTP error: {stderr.strip()}"
            )

        raise RuntimeError(f"Failed to download tarball (exit={exit_code}): {stderr}")


# -- Public API ---------------------------------------------------------------


@dataclass(frozen=True)
class DispatchResult:
    """Result of dispatching an automation to a sandbox (fire-and-forget)."""

    success: bool
    sandbox_id: str | None = None
    error: str | None = None
    # Agent-server BashCommand id assigned to this dispatch. Surfaced so the
    # caller can persist it on the AutomationRun and the verifier can later
    # filter BashOutput by exactly this command (see watchdog → backend.
    # verify_run → get_last_bash_command_result(command_id=...)). Only set
    # when dispatch reached `_start_bash`; remains None on earlier failures.
    bash_command_id: str | None = None


async def execute_in_context(
    client: httpx.AsyncClient,
    agent_url: str,
    session_key: str,
    entrypoint: str,
    tarball_source: bytes | str,
    work_dir: str,
    env_vars: dict[str, str] | None = None,
    timeout: int | None = None,
    run_id: str | None = None,
    sandbox_id: str | None = None,
    setup_script_path: str | None = "setup.sh",
) -> DispatchResult:
    """Execute automation code in an existing execution context.

    This is the core execution logic used by both Cloud and Local modes.
    The context (agent_url, session_key) is obtained from the backend.

    1. Get tarball into environment (upload bytes OR download from URL).
    2. Extract it, run ``setup.sh`` (if present), then start *entrypoint*.
    3. Return immediately without waiting for the entrypoint to complete.

    Args:
        client: HTTP client for making requests
        agent_url: URL of the agent server
        session_key: API key for the agent server
        entrypoint: Command to run
        tarball_source: Either raw bytes or URL string
        work_dir: Working directory for tarball extraction
        env_vars: Environment variables to export
        timeout: Max execution time
        run_id: Run ID — used for logging and to derive an isolated tarball
            path (<tempdir>/automation-<run_id>.tar.gz) that prevents collisions
            when concurrent runs share the same filesystem (sandboxless mode)
        sandbox_id: Sandbox ID for logging (Cloud mode only)

    Returns:
        DispatchResult with success status
    """
    timeout = resolve_automation_timeout_seconds(timeout)

    env_vars = dict(env_vars) if env_vars else {}

    def _log_ctx() -> dict[str, Any]:
        return log_extra(run_id=run_id, sandbox_id=sandbox_id)

    # Use a per-run tarball path to avoid collisions when multiple automations
    # run concurrently on a shared filesystem (sandboxless/local mode).
    # Guard against path separators in run_id before embedding it in a shell command.
    # Use tempfile.gettempdir() so the path is valid on every platform (Windows
    # agent-servers reject POSIX /tmp/ because it lacks a drive-letter prefix).
    tarball_path = (
        os.path.join(tempfile.gettempdir(), f"automation-{run_id}.tar.gz")
        if run_id and "/" not in run_id
        else _FALLBACK_TARBALL_PATH
    )

    try:
        # Get tarball into environment: upload bytes or download from URL
        if isinstance(tarball_source, bytes):
            logger.info("Uploading tarball", extra=_log_ctx())
            await _upload(client, agent_url, session_key, tarball_source, tarball_path)
        else:
            logger.info("Downloading tarball from URL", extra=_log_ctx())
            await _download_in_sandbox(
                client, agent_url, session_key, tarball_source, tarball_path
            )

        if setup_script_path is None:
            cmd = _build_python_runner_command(
                entrypoint=entrypoint,
                tarball_path=tarball_path,
                work_dir=work_dir,
                env_vars=env_vars,
            )
        else:
            cmd = _build_shell_runner_command(
                entrypoint=entrypoint,
                tarball_path=tarball_path,
                work_dir=work_dir,
                env_vars=env_vars,
                setup_script_path=setup_script_path,
            )

        logger.info("Starting entrypoint: %s", entrypoint, extra=_log_ctx())
        command_id = await _start_bash(
            client, agent_url, session_key, cmd, timeout=timeout
        )
        logger.info(
            "Entrypoint started (command_id=%s), disconnecting",
            command_id,
            extra=_log_ctx(),
        )

        return DispatchResult(
            success=True,
            sandbox_id=sandbox_id,
            bash_command_id=command_id,
        )

    except PermanentDispatchError:
        # Re-raise so caller can handle (e.g., disable automation)
        raise
    except Exception as e:
        logger.exception("Execution failed", extra=_log_ctx())
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
    timeout: int | None = None,
    callback_url: str | None = None,
    run_id: str | None = None,
    keep_sandbox: bool = False,
    work_dir: str = DEFAULT_WORK_DIR,
    setup_script_path: str | None = "setup.sh",
) -> AutomationResult:
    """Execute an automation end-to-end in a fresh sandbox (blocking).

    Use this for testing or when you need to wait for the result immediately.
    For production async execution, use the dispatcher with execute_in_context().

    1. Create sandbox and wait until RUNNING.
    2. Get tarball into sandbox (upload bytes OR download from URL).
    3. Extract it, run ``setup.sh`` (if present), then run *entrypoint*.
    4. Wait for completion and return the result.
    5. Delete the sandbox (unless *keep_sandbox* is True).

    *tarball_source*: Either raw bytes (uploaded to sandbox) or a URL string
    (downloaded directly inside sandbox via curl). URLs avoid downloading
    untrusted/large files on the automation service.

    *env_vars* are exported before setup.sh and the entrypoint run,
    so setup.sh can consume injected values such as ``AUTOMATION_API_URL``.
    The sandbox identity env vars (``SANDBOX_ID``, ``SESSION_API_KEY``) are
    **always** injected so the SDK's ``local_agent_server_mode`` works.
    If *callback_url* / *run_id* are set they are injected as
    ``AUTOMATION_CALLBACK_URL`` / ``AUTOMATION_RUN_ID`` so the SDK's
    ``OpenHandsCloudWorkspace`` can POST completion status on exit.

    *work_dir* is the working directory for tarball extraction
    (default: /workspace/project).
    """
    timeout = resolve_automation_timeout_seconds(timeout)
    http_timeout = get_config().http.http_long_timeout

    env_vars = dict(env_vars) if env_vars else {}
    if callback_url:
        env_vars["AUTOMATION_CALLBACK_URL"] = callback_url
    if run_id:
        env_vars["AUTOMATION_RUN_ID"] = run_id
    api_url = api_url.rstrip("/")
    sandbox_id: str | None = None

    # Helper for consistent structured logging with run_id/sandbox_id
    def _log_ctx() -> dict[str, Any]:
        return log_extra(run_id=run_id, sandbox_id=sandbox_id)

    logger.info("Starting automation execution", extra=_log_ctx())

    async with httpx.AsyncClient(timeout=http_timeout) as client:
        try:
            sandbox_id, session_key, agent_url = await _create_and_wait(
                client, api_url, api_key
            )
            logger.info(
                "Sandbox ready: %s at %s", sandbox_id, agent_url, extra=_log_ctx()
            )
        except Exception as e:
            # If sandbox creation started but failed to reach RUNNING,
            # still attempt cleanup.
            logger.exception("Sandbox creation failed", extra=_log_ctx())
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
                logger.info("Uploading tarball to sandbox", extra=_log_ctx())
                await _upload(
                    client,
                    agent_url,
                    session_key,
                    tarball_source,
                    _FALLBACK_TARBALL_PATH,
                )
            else:
                logger.info("Downloading tarball in sandbox from URL", extra=_log_ctx())
                await _download_in_sandbox(
                    client,
                    agent_url,
                    session_key,
                    tarball_source,
                    _FALLBACK_TARBALL_PATH,
                )

            if setup_script_path is None:
                cmd = _build_python_runner_command(
                    entrypoint=entrypoint,
                    tarball_path=_FALLBACK_TARBALL_PATH,
                    work_dir=work_dir,
                    env_vars=env_vars,
                )
            else:
                cmd = _build_shell_runner_command(
                    entrypoint=entrypoint,
                    tarball_path=_FALLBACK_TARBALL_PATH,
                    work_dir=work_dir,
                    env_vars=env_vars,
                    setup_script_path=setup_script_path,
                )

            logger.info("Executing entrypoint: %s", entrypoint, extra=_log_ctx())
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
                    "Entrypoint failed with exit_code=%s", exit_code, extra=_log_ctx()
                )
            else:
                logger.info("Entrypoint completed successfully", extra=_log_ctx())

            return AutomationResult(
                success=success,
                sandbox_id=sandbox_id,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                error=error_msg,
            )

        except Exception as e:
            logger.exception("Automation execution failed", extra=_log_ctx())
            return AutomationResult(success=False, sandbox_id=sandbox_id, error=str(e))
        finally:
            if not keep_sandbox:
                logger.info("Deleting sandbox", extra=_log_ctx())
                await delete_sandbox(client, api_url, api_key, sandbox_id)


def _shell_quote(s: str) -> str:
    """Single-quote a string for safe shell interpolation."""
    return "'" + s.replace("'", "'\\''") + "'"
