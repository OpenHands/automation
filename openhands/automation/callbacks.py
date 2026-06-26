"""Post-run callback scheduling and dispatch."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.backends import get_backend
from openhands.automation.config import get_config
from openhands.automation.execution import _shell_quote, _start_bash
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunCallback,
    AutomationRunCallbackStatus,
    AutomationRunStatus,
)
from openhands.automation.schemas import CallbackCompleteItem
from openhands.automation.utils import log_extra, utcnow


logger = logging.getLogger("automation.callbacks")

_TERMINAL_CALLBACK_STATUSES = {
    AutomationRunCallbackStatus.COMPLETED,
    AutomationRunCallbackStatus.FAILED,
    AutomationRunCallbackStatus.SKIPPED,
}


def _matching_callbacks(
    automation: Automation, terminal_status: AutomationRunStatus
) -> list[tuple[int, dict[str, Any]]]:
    status_value = terminal_status.value
    return [
        (idx, config)
        for idx, config in enumerate(automation.callbacks or [])
        if status_value in set(config.get("on") or [])
    ]


def _callback_command_from_config(config: dict[str, Any]) -> str:
    entrypoint = config.get("entrypoint")
    if entrypoint:
        return str(entrypoint)

    inline_python = config.get("inline_python")
    if inline_python:
        marker = f"AUTOMATION_CALLBACK_{uuid.uuid4().hex}"
        return f"python - <<'{marker}'\n{inline_python}\n{marker}"

    raise ValueError("callback config must include entrypoint or inline_python")


def _callback_complete_url(run_id: uuid.UUID) -> str:
    base_url = get_config().service.resolved_base_url.rstrip("/")
    return f"{base_url}/v1/runs/{run_id}/callbacks/complete"


def _chain_timeout(callbacks: list[AutomationRunCallback]) -> int:
    sandbox_timeout = get_config().sandbox.max_run_duration
    total = 0
    for callback in callbacks:
        total += callback.timeout or sandbox_timeout
    return max(total, 1)


def _build_callback_chain_command(
    *,
    work_dir: str,
    run: AutomationRun,
    callbacks: list[AutomationRunCallback],
) -> str:
    callback_specs = [
        {
            "id": str(callback.id),
            "name": callback.name,
            "entrypoint": callback.entrypoint,
            "timeout": callback.timeout,
        }
        for callback in callbacks
    ]
    payload = {
        "work_dir": work_dir,
        "callback_url": _callback_complete_url(run.id),
        "callbacks": callback_specs,
        "base_env": {
            "AUTOMATION_RUN_ID": str(run.id),
            "AUTOMATION_MAIN_STATUS": run.status.value,
            "AUTOMATION_MAIN_ERROR": run.error_detail or "",
            "AUTOMATION_CONVERSATION_ID": run.conversation_id or "",
            "AUTOMATION_EVENT_PAYLOAD": json.dumps(run.event_payload or {}),
        },
    }
    marker = f"AUTOMATION_CALLBACK_CHAIN_{uuid.uuid4().hex}"
    script = f"""
import json
import os
import subprocess
import urllib.request
from datetime import datetime, timezone

config = {json.dumps(payload)!r}
config = json.loads(config)
results = []
base_env = os.environ.copy()
base_env.update(config["base_env"])

for callback in config["callbacks"]:
    env = base_env.copy()
    env["AUTOMATION_CALLBACK_NAME"] = callback["name"]
    started_at = datetime.now(timezone.utc).isoformat()
    exit_code = None
    stdout = ""
    stderr = ""
    error_detail = None
    try:
        completed = subprocess.run(
            callback["entrypoint"],
            shell=True,
            cwd=config["work_dir"],
            env=env,
            capture_output=True,
            text=True,
            timeout=callback.get("timeout"),
        )
        exit_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if exit_code != 0:
            parts = [f"exit_code={{exit_code}}"]
            if stderr:
                parts.append(f"stderr: {{stderr[-1000:]}}")
            if stdout:
                parts.append(f"stdout: {{stdout[-500:]}}")
            error_detail = "\\n".join(parts)
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        error_detail = "Timed out"
    except Exception as exc:
        error_detail = str(exc)

    results.append({{
        "id": callback["id"],
        "name": callback["name"],
        "status": "COMPLETED" if exit_code == 0 and error_detail is None else "FAILED",
        "exit_code": exit_code,
        "stdout": stdout[-500:],
        "stderr": stderr[-1000:],
        "error_detail": error_detail,
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }})

body = json.dumps({{"callbacks": results}}).encode("utf-8")
token = (
    os.environ.get("AUTOMATION_CALLBACK_API_KEY")
    or os.environ.get("OPENHANDS_API_KEY")
    or ""
)
headers = {{"Content-Type": "application/json"}}
if token:
    headers["Authorization"] = f"Bearer {{token}}"
request = urllib.request.Request(
    config["callback_url"],
    data=body,
    headers=headers,
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()
except Exception as exc:
    print(f"Failed to report callback completion: {{exc}}", flush=True)
    raise
""".strip()
    return f"cd {_shell_quote(work_dir)} && python - <<'{marker}'\n{script}\n{marker}"


async def _get_automation(
    session: AsyncSession, run: AutomationRun
) -> Automation | None:
    if run.automation is not None:
        return run.automation
    return await session.get(Automation, run.automation_id)


async def schedule_and_dispatch_callbacks_for_run(
    session: AsyncSession,
    run: AutomationRun,
) -> int:
    """Create callback records and start one in-sandbox callback chain.

    The records are committed before the in-sandbox wrapper starts so a fast
    callback completion request can update durable rows immediately.
    """
    if run.status not in (AutomationRunStatus.COMPLETED, AutomationRunStatus.FAILED):
        return 0

    automation = await _get_automation(session, run)
    if automation is None:
        return 0

    matches = _matching_callbacks(automation, run.status)
    if not matches:
        return 0

    existing = await session.scalar(
        select(func.count())
        .select_from(AutomationRunCallback)
        .where(AutomationRunCallback.run_id == run.id)
    )
    if existing:
        return 0

    records: list[AutomationRunCallback] = []
    now = utcnow()
    for order, config in matches:
        try:
            entrypoint = _callback_command_from_config(config)
            status = AutomationRunCallbackStatus.PENDING
            error_detail = None
        except ValueError as exc:
            entrypoint = ""
            status = AutomationRunCallbackStatus.SKIPPED
            error_detail = str(exc)

        if not run.sandbox_id and not get_config().service.is_local_mode:
            status = AutomationRunCallbackStatus.SKIPPED
            error_detail = "Run has no sandbox for callback execution"

        record = AutomationRunCallback(
            run_id=run.id,
            name=str(config.get("name") or f"callback-{order}"),
            trigger_status=run.status,
            entrypoint=entrypoint,
            timeout=config.get("timeout"),
            status=status,
            error_detail=error_detail,
            order=order,
            completed_at=now if status == AutomationRunCallbackStatus.SKIPPED else None,
        )
        records.append(record)
        session.add(record)

    await session.flush()
    runnable = [r for r in records if r.status == AutomationRunCallbackStatus.PENDING]
    await session.commit()

    if not runnable:
        return len(records)

    backend = get_backend(run)
    extra = log_extra(run_id=str(run.id), sandbox_id=run.sandbox_id)
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            ctx = await backend.get_existing_execution_context(client)
            command = _build_callback_chain_command(
                work_dir=backend.get_work_dir(str(run.id)),
                run=run,
                callbacks=runnable,
            )
            chain_timeout = _chain_timeout(runnable)
            command_id = await _start_bash(
                client,
                ctx.agent_url,
                ctx.session_key,
                command,
                timeout=chain_timeout,
            )

        started_at = utcnow()
        await session.execute(
            update(AutomationRunCallback)
            .where(AutomationRunCallback.id.in_([r.id for r in runnable]))
            .values(
                status=AutomationRunCallbackStatus.RUNNING,
                bash_command_id=command_id,
                started_at=started_at,
                timeout_at=started_at + timedelta(seconds=chain_timeout),
            )
        )
        await session.commit()
        logger.info(
            "Callback chain started (command_id=%s)",
            command_id,
            extra=extra,
        )
    except Exception as exc:
        logger.warning("Callback chain failed to start: %s", exc, extra=extra)
        failed_at = utcnow()
        await session.execute(
            update(AutomationRunCallback)
            .where(AutomationRunCallback.id.in_([r.id for r in runnable]))
            .values(
                status=AutomationRunCallbackStatus.FAILED,
                completed_at=failed_at,
                error_detail=str(exc),
            )
        )
        await session.commit()

    return len(records)


async def complete_callback_records(
    session: AsyncSession,
    run: AutomationRun,
    callbacks: list[CallbackCompleteItem],
) -> None:
    """Persist completion results reported by the in-sandbox callback wrapper."""
    for result in callbacks:
        status = AutomationRunCallbackStatus(result.status)
        await session.execute(
            update(AutomationRunCallback)
            .where(
                AutomationRunCallback.id == result.id,
                AutomationRunCallback.run_id == run.id,
            )
            .values(
                status=status,
                completed_at=result.completed_at or utcnow(),
                error_detail=result.error_detail,
            )
        )


async def run_has_unfinished_callbacks(session: AsyncSession, run_id: Any) -> bool:
    count = await session.scalar(
        select(func.count())
        .select_from(AutomationRunCallback)
        .where(
            AutomationRunCallback.run_id == run_id,
            AutomationRunCallback.status.notin_(list(_TERMINAL_CALLBACK_STATUSES)),
        )
    )
    return bool(count)


async def cleanup_run_after_callbacks_if_ready(
    session: AsyncSession,
    run: AutomationRun,
) -> bool:
    """Clean up explicit-cleanup runs once all callbacks are terminal."""
    automation = await _get_automation(session, run)
    if automation is None or automation.keep_alive is True or not run.sandbox_id:
        return False
    if await run_has_unfinished_callbacks(session, run.id):
        return False

    try:
        await get_backend(run).cleanup_after_verification(str(run.id))
        return True
    except Exception as exc:
        logger.warning(
            "Cleanup after callbacks failed: %s",
            exc,
            extra=log_extra(run_id=str(run.id), sandbox_id=run.sandbox_id),
        )
        return False
