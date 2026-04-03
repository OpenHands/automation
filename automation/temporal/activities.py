"""Temporal Activity definitions for automation execution.

Activities are the building blocks of workflows. Each activity represents
a single unit of work that may fail and be retried. Activities can have
side effects (HTTP calls, database writes, etc.) unlike workflows which
must be deterministic.

Key activities:
- get_api_key: Fetch per-user API key from OpenHands SaaS
- create_sandbox: Create sandbox and wait until RUNNING
- download_tarball: Download internal tarball from storage
- upload_tarball: Upload tarball to sandbox or trigger download
- execute_entrypoint: Start entrypoint command and wait for completion
- cleanup_sandbox: Delete sandbox (runs even on failure)
"""

import asyncio
import json
import logging
import uuid

import httpx
from temporalio import activity

from automation.config import get_settings
from automation.constants import (
    EXTERNAL_DOWNLOAD_TIMEOUT,
    EXTERNAL_MAX_FILESIZE,
    SANDBOX_POLL_INTERVAL,
    SANDBOX_READY_TIMEOUT,
    TARBALL_PATH,
    WORK_DIR,
)
from automation.temporal.types import (
    CleanupSandboxInput,
    CreateSandboxInput,
    DownloadTarballInput,
    ExecuteEntrypointInput,
    ExecutionResult,
    GetApiKeyInput,
    SandboxInfo,
    UploadTarballInput,
)


logger = logging.getLogger(__name__)


# --- API Key Activity ---


@activity.defn
async def get_api_key(input: GetApiKeyInput) -> str:
    """Fetch a per-user API key from the OpenHands SaaS service.

    This creates a temporary API key for the user/org that can be used
    to authenticate sandbox operations.

    Raises:
        Exception: If the API key cannot be retrieved.
    """
    settings = get_settings()

    url = (
        f"{settings.openhands_api_base_url}/api/service/users/{input.user_id}"
        f"/orgs/{input.org_id}/api-keys"
    )

    headers = {
        "X-Service-API-Key": settings.service_key,
        "Content-Type": "application/json",
    }

    payload = {"name": "automation"}

    logger.info(
        "Fetching API key for user=%s org=%s run=%s",
        input.user_id,
        input.org_id,
        input.run_id,
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()

        data = response.json()
        api_key = data.get("key")

        if not api_key:
            raise ValueError(f"API key not found in response: {list(data.keys())}")

        logger.info("API key created for run=%s", input.run_id)
        return api_key


# --- Sandbox Creation Activity ---


def _find_agent_server_url(sandbox: dict) -> tuple[str, str] | None:
    """Extract agent server URL and session key from sandbox response."""
    for url_info in sandbox.get("exposed_urls") or []:
        if url_info.get("name") == "AGENT_SERVER":
            return url_info["url"].rstrip("/"), sandbox.get("session_api_key", "")
    return None


@activity.defn
async def create_sandbox(input: CreateSandboxInput) -> SandboxInfo:
    """Create a sandbox and wait until it's RUNNING.

    This activity polls the sandbox status until it becomes RUNNING,
    then returns the sandbox info including agent URL and session key.

    Heartbeats during polling to let Temporal know we're still alive.

    Raises:
        TimeoutError: If sandbox doesn't become ready in time.
        RuntimeError: If sandbox enters ERROR or MISSING state.
    """
    api_url = input.api_url.rstrip("/")
    headers = {"Authorization": f"Bearer {input.api_key}"}

    logger.info("Creating sandbox for run=%s", input.run_id)

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Create sandbox
        resp = await client.post(f"{api_url}/api/v1/sandboxes", headers=headers)
        resp.raise_for_status()
        sandbox_id = resp.json()["id"]

        logger.info("Sandbox created: sandbox_id=%s run=%s", sandbox_id, input.run_id)

        # Poll until RUNNING
        elapsed = 0.0
        while elapsed < SANDBOX_READY_TIMEOUT:
            # Heartbeat to Temporal so it knows we're still working
            activity.heartbeat(f"Waiting for sandbox {sandbox_id}: {elapsed:.0f}s")

            resp = await client.get(
                f"{api_url}/api/v1/sandboxes",
                params={"id": sandbox_id},
                headers=headers,
            )
            resp.raise_for_status()
            items = resp.json()

            if not items:
                raise RuntimeError(f"Sandbox {sandbox_id} disappeared")

            sandbox = items[0]
            status = sandbox.get("status", "UNKNOWN")

            if status == "RUNNING":
                result = _find_agent_server_url(sandbox)
                if result is None:
                    raise RuntimeError(f"No AGENT_SERVER URL in sandbox {sandbox_id}")

                agent_url, session_key = result
                logger.info(
                    "Sandbox ready: sandbox_id=%s agent_url=%s run=%s",
                    sandbox_id,
                    agent_url,
                    input.run_id,
                )
                return SandboxInfo(
                    sandbox_id=sandbox_id,
                    agent_url=agent_url,
                    session_key=session_key,
                    api_key=input.api_key,
                )

            if status in ("ERROR", "MISSING"):
                error_code = sandbox.get("error_code", "")
                error_message = sandbox.get("error_message", "")
                error_detail = f"status={status}"
                if error_code:
                    error_detail += f", error_code={error_code}"
                if error_message:
                    error_detail += f", error_message={error_message}"
                raise RuntimeError(f"Sandbox {sandbox_id} failed: {error_detail}")

            await asyncio.sleep(SANDBOX_POLL_INTERVAL)
            elapsed += SANDBOX_POLL_INTERVAL

        raise TimeoutError(
            f"Sandbox {sandbox_id} not ready after {SANDBOX_READY_TIMEOUT}s"
        )


# --- Tarball Activities ---


@activity.defn
async def download_tarball(input: DownloadTarballInput) -> bytes:
    """Download an internal tarball from storage.

    Internal tarballs are stored in GCS/S3 and referenced by upload ID.
    This activity downloads the tarball content as bytes.

    Raises:
        ValueError: If the tarball upload record doesn't exist.
    """
    from sqlalchemy import select

    from automation.db import create_engine, create_session_factory
    from automation.models import TarballUpload
    from automation.storage import get_file_store

    logger.info("Downloading internal tarball: upload_id=%s", input.upload_id)

    settings = get_settings()
    engine_result = await create_engine(settings)
    session_factory = create_session_factory(engine_result.engine)

    try:
        async with session_factory() as session:
            result = await session.execute(
                select(TarballUpload).where(
                    TarballUpload.id == uuid.UUID(input.upload_id)
                )
            )
            upload = result.scalars().first()

            if upload is None:
                raise ValueError(
                    f"Internal tarball upload not found: {input.upload_id}"
                )

            store = get_file_store()
            data = store.read(upload.storage_path)
            logger.info(
                "Downloaded tarball: %d bytes, run=%s", len(data), input.run_id
            )
            return data
    finally:
        await engine_result.dispose()


@activity.defn
async def upload_tarball(input: UploadTarballInput) -> None:
    """Upload tarball to sandbox or trigger download inside sandbox.

    For internal tarballs (tarball_data set): uploads bytes to sandbox.
    For external tarballs (tarball_url set): runs curl inside sandbox.

    Raises:
        RuntimeError: If upload/download fails.
    """
    sandbox = input.sandbox_info
    logger.info(
        "Uploading tarball to sandbox: sandbox_id=%s run=%s",
        sandbox.sandbox_id,
        input.run_id,
    )

    async with httpx.AsyncClient(timeout=120.0) as client:
        if input.tarball_data is not None:
            # Upload bytes to sandbox
            resp = await client.post(
                f"{sandbox.agent_url}/api/file/upload/{TARBALL_PATH}",
                files={"file": ("upload", input.tarball_data)},
                headers={"X-Session-API-Key": sandbox.session_key},
            )
            resp.raise_for_status()
            logger.info(
                "Tarball uploaded: %d bytes to %s",
                len(input.tarball_data),
                TARBALL_PATH,
            )

        elif input.tarball_url is not None:
            # Download inside sandbox using curl
            curl_cmd = (
                f"curl -fsSL --max-filesize {EXTERNAL_MAX_FILESIZE} "
                f"-o {TARBALL_PATH} '{input.tarball_url}'"
            )
            resp = await client.post(
                f"{sandbox.agent_url}/api/bash/execute_bash_command",
                json={"command": curl_cmd, "timeout": EXTERNAL_DOWNLOAD_TIMEOUT},
                headers={"X-Session-API-Key": sandbox.session_key},
                timeout=httpx.Timeout(EXTERNAL_DOWNLOAD_TIMEOUT + 30),
            )
            resp.raise_for_status()
            result = resp.json()

            if result.get("exit_code") != 0:
                stderr = result.get("stderr", "")
                raise RuntimeError(f"Failed to download tarball: {stderr}")

            logger.info("Tarball downloaded in sandbox from URL")

        else:
            raise ValueError("Either tarball_data or tarball_url must be provided")


# --- Entrypoint Execution Activity ---


def _shell_quote(s: str) -> str:
    """Single-quote a string for safe shell interpolation."""
    return "'" + s.replace("'", "'\\''") + "'"


@activity.defn
async def execute_entrypoint(input: ExecuteEntrypointInput) -> ExecutionResult:
    """Execute the automation entrypoint in the sandbox.

    Extracts the tarball, runs setup.sh if present, exports env vars,
    and runs the entrypoint command. Waits for completion and returns
    the result.

    Heartbeats periodically while waiting for completion.

    Returns:
        ExecutionResult with success status, exit code, and output.
    """
    sandbox = input.sandbox_info

    logger.info(
        "Executing entrypoint: %s in sandbox=%s run=%s",
        input.entrypoint,
        sandbox.sandbox_id,
        input.run_id,
    )

    # Build env var exports
    exports = ""
    if input.env_vars:
        parts = [f"export {k}={_shell_quote(v)}" for k, v in input.env_vars.items()]
        exports = " && ".join(parts) + " && "

    # Build full command
    cmd = (
        f"mkdir -p {WORK_DIR}"
        f" && tar xzf {TARBALL_PATH} -C {WORK_DIR}"
        f" && cd {WORK_DIR}"
        f" && ([ ! -f setup.sh ] || bash setup.sh)"
        f" && {exports}{input.entrypoint}"
    )

    async with httpx.AsyncClient(timeout=input.timeout_seconds + 60) as client:
        # Start the command
        resp = await client.post(
            f"{sandbox.agent_url}/api/bash/start_bash_command",
            json={"command": cmd, "timeout": input.timeout_seconds},
            headers={"X-Session-API-Key": sandbox.session_key},
            timeout=30.0,
        )
        resp.raise_for_status()
        command_id = resp.json().get("id")

        logger.info(
            "Command started: command_id=%s sandbox=%s",
            command_id,
            sandbox.sandbox_id,
        )

        # Poll for completion
        elapsed = 0
        poll_interval = 5
        while elapsed < input.timeout_seconds + 30:
            # Heartbeat to Temporal
            activity.heartbeat(
                f"Waiting for command {command_id}: {elapsed}s/{input.timeout_seconds}s"
            )

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            # Check command status
            try:
                resp = await client.get(
                    f"{sandbox.agent_url}/api/bash/bash_events/search",
                    params={
                        "kind__eq": "BashOutput",
                        "sort_order": "TIMESTAMP_DESC",
                        "limit": 1,
                    },
                    headers={"X-Session-API-Key": sandbox.session_key},
                    timeout=30.0,
                )
                resp.raise_for_status()
                page = resp.json()

                items = page.get("items", [])
                if items:
                    output = items[0]
                    exit_code = output.get("exit_code")

                    # exit_code is None while command is still running
                    if exit_code is not None:
                        success = exit_code == 0
                        logger.info(
                            "Command completed: exit_code=%s sandbox=%s run=%s",
                            exit_code,
                            sandbox.sandbox_id,
                            input.run_id,
                        )
                        return ExecutionResult(
                            success=success,
                            exit_code=exit_code,
                            stdout=output.get("stdout") or "",
                            stderr=output.get("stderr") or "",
                            error=None if success else f"exit_code={exit_code}",
                        )

            except Exception as e:
                logger.warning("Error polling command status: %s", e)

        # Timeout waiting for command
        logger.warning(
            "Command timed out: sandbox=%s run=%s", sandbox.sandbox_id, input.run_id
        )
        return ExecutionResult(
            success=False,
            exit_code=-1,
            error=f"Command timed out after {input.timeout_seconds}s",
        )


# --- Cleanup Activity ---


@activity.defn
async def cleanup_sandbox(input: CleanupSandboxInput) -> bool:
    """Delete a sandbox.

    This activity is idempotent - it succeeds even if the sandbox
    is already deleted or doesn't exist.

    Returns:
        True if sandbox was deleted, False if it didn't exist.
    """
    api_url = input.api_url.rstrip("/")

    logger.info(
        "Cleaning up sandbox: sandbox_id=%s run=%s", input.sandbox_id, input.run_id
    )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.delete(
                f"{api_url}/api/v1/sandboxes/{input.sandbox_id}",
                params={"sandbox_id": input.sandbox_id},
                headers={"Authorization": f"Bearer {input.api_key}"},
            )

            if resp.status_code == 404:
                logger.info("Sandbox already deleted: %s", input.sandbox_id)
                return False

            if resp.status_code >= 300:
                logger.warning(
                    "Failed to delete sandbox %s: %s", input.sandbox_id, resp.text
                )
                return False

            logger.info("Sandbox deleted: %s", input.sandbox_id)
            return True

    except Exception as e:
        logger.warning("Error deleting sandbox %s: %s", input.sandbox_id, e)
        return False


# List of all activities for worker registration
ALL_ACTIVITIES = [
    get_api_key,
    create_sandbox,
    download_tarball,
    upload_tarball,
    execute_entrypoint,
    cleanup_sandbox,
]
