"""Tests for the cancel run endpoint."""

import os
import re
import signal
import socket
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from openhands.automation.config import clear_config_cache
from openhands.automation.dispatcher import _execute_run
from openhands.automation.execution import DispatchResult
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
    Base,
)
from openhands.automation.utils import utcnow


TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
OTHER_USER_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER_ORG_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


async def _create_automation_with_run(
    session, status=AutomationRunStatus.PENDING, sandbox_id=None
):
    """Helper to create an automation with a run in the given status."""
    automation = Automation(
        user_id=TEST_USER_ID,
        org_id=TEST_ORG_ID,
        name="Test Automation",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="https://example.com/code.tar.gz",
        entrypoint="python main.py",
    )
    session.add(automation)
    await session.flush()

    run = AutomationRun(
        id=uuid.uuid4(),
        automation_id=automation.id,
        status=status,
        sandbox_id=sandbox_id,
    )
    if status == AutomationRunStatus.RUNNING:
        run.started_at = utcnow()
    session.add(run)
    await session.flush()
    return automation, run


async def test_cancel_pending_run(async_client, async_session):
    """Cancelling a PENDING run should succeed."""
    _, run = await _create_automation_with_run(
        async_session, status=AutomationRunStatus.PENDING
    )

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "CANCELLED"
    assert data["error_detail"] == "Cancelled by user"
    assert data["completed_at"] is not None


async def test_cancel_running_run(async_client, async_session):
    """Cancelling a RUNNING run should succeed."""
    _, run = await _create_automation_with_run(
        async_session, status=AutomationRunStatus.RUNNING
    )

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "CANCELLED"


async def test_cancel_completed_run_returns_409(async_client, async_session):
    """Cancelling a COMPLETED run should return 409."""
    _, run = await _create_automation_with_run(
        async_session, status=AutomationRunStatus.COMPLETED
    )

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")
    assert resp.status_code == 409
    assert "COMPLETED" in resp.json()["detail"]


async def test_cancel_failed_run_returns_409(async_client, async_session):
    """Cancelling a FAILED run should return 409."""
    _, run = await _create_automation_with_run(
        async_session, status=AutomationRunStatus.FAILED
    )

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")
    assert resp.status_code == 409
    assert "FAILED" in resp.json()["detail"]


async def test_cancel_already_cancelled_run_returns_409(async_client, async_session):
    """Cancelling an already-cancelled run should return 409."""
    _, run = await _create_automation_with_run(
        async_session, status=AutomationRunStatus.CANCELLED
    )

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")
    assert resp.status_code == 409
    assert "CANCELLED" in resp.json()["detail"]


async def test_cancel_nonexistent_run_returns_404(async_client, async_session):
    """Cancelling a non-existent run should return 404."""
    fake_id = uuid.uuid4()
    resp = await async_client.post(f"/api/automation/v1/runs/{fake_id}/cancel")
    assert resp.status_code == 404


async def test_cancel_other_orgs_run_returns_403(async_client, async_session):
    """Cancelling a run from another org should return 403."""
    automation = Automation(
        user_id=OTHER_USER_ID,
        org_id=OTHER_ORG_ID,
        name="Other User Automation",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="https://example.com/code.tar.gz",
        entrypoint="python main.py",
    )
    async_session.add(automation)
    await async_session.flush()

    run = AutomationRun(
        id=uuid.uuid4(),
        automation_id=automation.id,
        status=AutomationRunStatus.RUNNING,
    )
    async_session.add(run)
    await async_session.flush()

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")
    assert resp.status_code == 403


async def test_cancel_same_org_other_users_run(async_client, async_session):
    """Cancelling a run owned by another member of the same org should succeed."""
    automation = Automation(
        user_id=OTHER_USER_ID,
        org_id=TEST_ORG_ID,
        name="Teammate Automation",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="https://example.com/code.tar.gz",
        entrypoint="python main.py",
    )
    async_session.add(automation)
    await async_session.flush()

    run = AutomationRun(
        id=uuid.uuid4(),
        automation_id=automation.id,
        status=AutomationRunStatus.RUNNING,
        started_at=utcnow(),
    )
    async_session.add(run)
    await async_session.flush()

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"


# --- Local mode: cancel stops the run's entrypoint BashCommand (#514) ---


class _FakeAgentServer:
    """Real processes behind the agent-server per-command stop contract.

    ``POST /api/bash/bash_commands/{id}/stop`` stops that command's process
    group, answers 200 for a finished command and 404 for an unknown id.
    """

    def __init__(self) -> None:
        self.processes: dict[str, subprocess.Popen] = {}
        self.stop_requests: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                match = re.fullmatch(r"/api/bash/bash_commands/(\w+)/stop", self.path)
                command_id = match.group(1) if match else ""
                fake.stop_requests.append(command_id)
                process = fake.processes.get(command_id)
                if process is None:
                    self.send_response(404)
                else:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        process.wait(timeout=5)
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success": true}')

            def log_message(self, format, *args):  # noqa: A002
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def start(self, command: str) -> str:
        """Start a command in its own process group, like BashEventService."""
        command_id = uuid.uuid4().hex
        self.processes[command_id] = subprocess.Popen(
            command, shell=True, start_new_session=True
        )
        return command_id

    def running(self, command_id: str) -> bool:
        return self.processes[command_id].poll() is None

    def close(self) -> None:
        for process in self.processes.values():
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def local_agent_server(monkeypatch):
    server = _FakeAgentServer()
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", server.url)
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_API_KEY", "dummy-agent-key")
    clear_config_cache()
    yield server
    clear_config_cache()
    server.close()


async def _running_local_run(session, bash_command_id: str | None):
    _, run = await _create_automation_with_run(
        session, status=AutomationRunStatus.RUNNING
    )
    run.bash_command_id = bash_command_id
    await session.flush()
    return run


def _pid_alive(pid: int) -> bool:
    """Whether *pid* still runs; an unreaped zombie has already exited."""
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def _wait_for_pid(pid_file) -> int:
    deadline = time.monotonic() + 5
    while not pid_file.exists() or not pid_file.read_text().strip():
        assert time.monotonic() < deadline, "entrypoint child never started"
        time.sleep(0.05)
    return int(pid_file.read_text())


async def test_cancel_local_run_stops_only_its_entrypoint(
    async_client, async_session, local_agent_server, tmp_path
):
    pid_file = tmp_path / "child.pid"
    entrypoint = local_agent_server.start(f"sleep 600 & echo $! > {pid_file}; wait")
    concurrent = local_agent_server.start("sleep 600")
    child_pid = _wait_for_pid(pid_file)
    run = await _running_local_run(async_session, entrypoint)

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")

    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"
    assert local_agent_server.stop_requests == [entrypoint]
    assert not local_agent_server.running(entrypoint)
    deadline = time.monotonic() + 5
    while _pid_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_alive(child_pid), "entrypoint descendant survived the cancel"
    assert local_agent_server.running(concurrent)


async def test_cancel_local_run_with_finished_command(
    async_client, async_session, local_agent_server
):
    finished = local_agent_server.start("true")
    local_agent_server.processes[finished].wait(timeout=5)
    run = await _running_local_run(async_session, finished)

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")

    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"
    assert local_agent_server.stop_requests == [finished]


async def test_cancel_local_run_with_unknown_command(
    async_client, async_session, local_agent_server
):
    unknown = uuid.uuid4().hex
    run = await _running_local_run(async_session, unknown)

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")

    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"
    assert local_agent_server.stop_requests == [unknown]


async def test_cancel_local_run_when_agent_server_unreachable(
    async_client, async_session, monkeypatch
):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", f"http://127.0.0.1:{closed_port}")
    clear_config_cache()
    try:
        run = await _running_local_run(async_session, uuid.uuid4().hex)
        resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")
    finally:
        clear_config_cache()

    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"


async def test_cancel_local_run_before_command_started(
    async_client, async_session, local_agent_server
):
    run = await _running_local_run(async_session, None)

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")

    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"
    assert local_agent_server.stop_requests == []


async def test_cancel_during_dispatch_stops_the_command_once_it_starts(
    local_agent_server, mock_settings, tmp_path
):
    """A cancel landing before the command id is recorded still stops it."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with session_factory() as session:
            _, run = await _create_automation_with_run(
                session, status=AutomationRunStatus.RUNNING
            )
            await session.commit()
            run_id = run.id
        async with session_factory() as session:
            run = (
                await session.execute(
                    select(AutomationRun)
                    .options(selectinload(AutomationRun.automation))
                    .where(AutomationRun.id == run_id)
                )
            ).scalar_one()

        command_id = local_agent_server.start("sleep 600")

        async def start_command_while_user_cancels(**_kwargs):
            async with session_factory() as session:
                await session.execute(
                    update(AutomationRun)
                    .where(AutomationRun.id == run_id)
                    .values(status=AutomationRunStatus.CANCELLED)
                )
                await session.commit()
            return DispatchResult(success=True, bash_command_id=command_id)

        with patch(
            "openhands.automation.dispatcher.execute_in_context",
            side_effect=start_command_while_user_cancels,
        ):
            await _execute_run(run, mock_settings, session_factory, MagicMock())
    finally:
        await engine.dispose()

    assert local_agent_server.stop_requests == [command_id]
    assert not local_agent_server.running(command_id)
