"""Sandbox execution for automation runs.

One function does the whole job: spin up a sandbox, upload a tarball,
extract it, run setup, run the entrypoint, tear down.
"""

import asyncio
import io
import logging
import tarfile
from dataclasses import dataclass

import httpx


logger = logging.getLogger(__name__)

SANDBOX_POLL_INTERVAL = 5
SANDBOX_READY_TIMEOUT = 300
DEFAULT_TIMEOUT = 600
WORK_DIR = "/workspace/automation"
TARBALL_PATH = "/tmp/automation.tar.gz"

# Limits for external tarball downloads (in sandbox)
EXTERNAL_DOWNLOAD_TIMEOUT = 120  # seconds
EXTERNAL_MAX_FILESIZE = 100 * 1024 * 1024  # 100 MB


@dataclass(frozen=True)
class AutomationResult:
    success: bool
    sandbox_id: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


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


async def _create_and_wait(
    client: httpx.AsyncClient,
    api_url: str,
    api_key: str,
    ready_timeout: float = SANDBOX_READY_TIMEOUT,
) -> tuple[str, str, str]:
    """Create a sandbox and poll until RUNNING.

    Returns ``(sandbox_id, session_api_key, agent_server_url)``.
    """
    headers = {"Authorization": f"Bearer {api_key}"}

    resp = await client.post(f"{api_url}/api/v1/sandboxes", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    sandbox_id = data["id"]
    logger.info("Created sandbox %s", sandbox_id)

    elapsed = 0.0
    while elapsed < ready_timeout:
        resp = await client.get(
            f"{api_url}/api/v1/sandboxes",
            params={"id": sandbox_id},
            headers=headers,
        )
        resp.raise_for_status()
        items = resp.json()
        if not items:
            raise RuntimeError(f"Sandbox {sandbox_id} disappeared")

        sb = items[0]
        status = sb.get("status", "UNKNOWN")

        if status == "RUNNING":
            result = _find_agent_server_url(sb)
            if result is None:
                raise RuntimeError(f"No AGENT_SERVER URL in sandbox {sandbox_id}")
            agent_url, session_key = result
            logger.info("Sandbox %s ready at %s", sandbox_id, agent_url)
            return sandbox_id, session_key, agent_url

        if status in ("ERROR", "MISSING"):
            raise RuntimeError(f"Sandbox {sandbox_id} failed with status {status}")

        await asyncio.sleep(SANDBOX_POLL_INTERVAL)
        elapsed += SANDBOX_POLL_INTERVAL

    raise TimeoutError(f"Sandbox {sandbox_id} not ready after {ready_timeout}s")


async def _delete_sandbox(
    client: httpx.AsyncClient, api_url: str, api_key: str, sandbox_id: str
) -> None:
    """Best-effort sandbox deletion."""
    try:
        resp = await client.delete(
            f"{api_url}/api/v1/sandboxes/{sandbox_id}",
            params={"sandbox_id": sandbox_id},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if resp.status_code < 300:
            logger.info("Deleted sandbox %s", sandbox_id)
        else:
            logger.warning("Delete sandbox %s: %s", sandbox_id, resp.text)
    except Exception:
        logger.exception("Error deleting sandbox %s", sandbox_id)


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
    timeout: int = DEFAULT_TIMEOUT,
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

    logger.info("Downloaded tarball from URL to %s in sandbox", dest)


# -- Public API ---------------------------------------------------------------


async def run_automation(
    api_url: str,
    api_key: str,
    entrypoint: str,
    tarball_source: bytes | str,
    env_vars: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    callback_url: str | None = None,
    run_id: str | None = None,
    keep_sandbox: bool = False,
) -> AutomationResult:
    """Execute an automation end-to-end in a fresh sandbox.

    1. Create sandbox and wait until RUNNING.
    2. Get tarball into sandbox (upload bytes OR download from URL).
    3. Extract it, run ``setup.sh`` (if present), then run *entrypoint*.
    4. Delete the sandbox (unless *keep_sandbox* is True).

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

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            sandbox_id, session_key, agent_url = await _create_and_wait(
                client, api_url, api_key
            )
        except Exception as e:
            # If sandbox creation started but failed to reach RUNNING,
            # still attempt cleanup.
            logger.exception("Sandbox creation failed")
            if sandbox_id:
                await _delete_sandbox(client, api_url, api_key, sandbox_id)
            return AutomationResult(success=False, sandbox_id=sandbox_id, error=str(e))

        try:
            # Always inject sandbox identity so the SDK can call
            # get_llm() / get_secrets() inside the sandbox.
            env_vars.setdefault("SANDBOX_ID", sandbox_id)
            env_vars.setdefault("SESSION_API_KEY", session_key)

            # Get tarball into sandbox: upload bytes or download from URL
            if isinstance(tarball_source, bytes):
                await _upload(
                    client, agent_url, session_key, tarball_source, TARBALL_PATH
                )
            else:
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

            return AutomationResult(
                success=success,
                sandbox_id=sandbox_id,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                error=error_msg,
            )

        except Exception as e:
            logger.exception("Automation execution failed")
            return AutomationResult(success=False, sandbox_id=sandbox_id, error=str(e))
        finally:
            if not keep_sandbox:
                await _delete_sandbox(client, api_url, api_key, sandbox_id)


def _shell_quote(s: str) -> str:
    """Single-quote a string for safe shell interpolation."""
    return "'" + s.replace("'", "'\\''") + "'"
