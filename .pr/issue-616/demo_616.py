"""Simulation for #616: does the app server get a run conversation's last events?

A real agent-server (1.62.0) runs one conversation against a mock LLM (one
tool call, then a final reply) and posts its events to a fake app-server webhook, as in a
sandbox. The fake runtime API kills the agent-server process when the sandbox
is deleted (SIGSTOP on pause). The real `complete_run` route and the real
watchdog sweep (`cleanup_due_sandboxes`, called every 5 s) tear the sandbox
down. Copy into tests/ and run:
DEMO_OUT=out.txt uv run pytest tests/demo_616.py -s
"""

import asyncio
import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from openhands.automation.app import app
from openhands.automation.auth import authenticate_request, create_http_client
from openhands.automation.config import clear_config_cache
from openhands.automation.db import get_session
from openhands.automation.models import Automation, AutomationRun, AutomationRunStatus
from openhands.automation.utils.change_events import bind_request
from openhands.automation.watchdog import cleanup_due_sandboxes


OUT = os.environ["DEMO_OUT"]
AGENT_SERVER = os.path.expanduser("~/work/as-venv/bin/agent-server")
AS_PORT = 18311
T0 = time.monotonic()


def log(line=""):
    with open(OUT, "a") as f:
        f.write(line + "\n")
    print(line)


def ts():
    return f"t={time.monotonic() - T0:5.1f}s"


class Fake:
    """App server webhook + runtime API + service API + mock LLM."""

    def __init__(self):
        self.received: list[str] = []
        self.received_ids: set[str] = set()
        self.agent_server: subprocess.Popen | None = None
        self.llm_calls = 0
        fake = self

        class H(BaseHTTPRequestHandler):
            def _reply(self, code=200, body=b"{}"):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                if self.path.startswith("/hook/events/"):
                    batch = json.loads(body)
                    kinds = [e.get("kind") for e in batch]
                    fake.received.extend(kinds)
                    fake.received_ids.update(e.get("id") for e in batch)
                    log(f"{ts()}  app server webhook <- batch of {len(kinds)}: {kinds}")
                    return self._reply()
                if self.path.startswith("/hook/"):
                    return self._reply()
                if re.match(r"/api/service/users/.*/api-keys", self.path):
                    return self._reply(body=b'{"key": "fake-user-key"}')
                if re.match(r"/api/v1/sandboxes/[^/]+/pause", self.path):
                    log(f"{ts()}  runtime API: PAUSE sandbox -> agent-server frozen")
                    os.kill(fake.agent_server.pid, signal.SIGSTOP)
                    return self._reply()
                if self.path.endswith("/chat/completions"):
                    # Only agent steps carry tools; a title request does not.
                    agent_step = bool(json.loads(body).get("tools"))
                    fake.llm_calls += agent_step
                    reply = _completion(tool_call=agent_step and fake.llm_calls == 1)
                    return self._reply(body=json.dumps(reply).encode())
                return self._reply(404)

            def do_DELETE(self):  # noqa: N802
                if self.path.startswith("/api/v1/sandboxes/"):
                    log(f"{ts()}  runtime API: DELETE sandbox -> agent-server killed")
                    fake.agent_server.send_signal(signal.SIGCONT)
                    fake.agent_server.kill()
                    return self._reply()
                return self._reply(404)

            def do_GET(self):  # noqa: N802
                return self._reply(404)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def _completion(tool_call: bool):
    """First a task_tracker call, then the agent's final reply."""
    if tool_call:
        call = {"name": "task_tracker", "arguments": json.dumps({"command": "view"})}
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "type": "function", "function": call}],
        }
    else:
        message = {"role": "assistant", "content": "Done: the weekly report is posted."}
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "mock",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if tool_call else "stop",
                "message": message,
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _describe(event):
    if event["kind"] == "ConversationStateUpdateEvent" and event.get("key") == "execution_status":
        return f"ConversationStateUpdateEvent(execution_status={event.get('value')})"
    if event["kind"] == "MessageEvent":
        return f"MessageEvent({event.get('source')}: final reply)"
    return event["kind"]


async def test_run_conversation_tail_reaches_app_server(
    async_session_factory, mock_authenticated_user, monkeypatch, tmp_path
):
    fake = Fake()
    env = dict(
        os.environ,
        OH_WEBHOOKS_0_BASE_URL=f"{fake.url}/hook",
        OH_SESSION_API_KEYS_0="sandbox-key",
        OPENHANDS_SUPPRESS_BANNER="1",
        OH_CONVERSATIONS_PATH=str(tmp_path / "conversations"),
    )
    fake.agent_server = subprocess.Popen(
        [AGENT_SERVER, "--host", "127.0.0.1", "--port", str(AS_PORT)],
        env=env,
        stdout=open(tmp_path / "agent-server.log", "w"),
        stderr=subprocess.STDOUT,
    )
    sandbox = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{AS_PORT}",
        headers={"X-Session-API-Key": "sandbox-key"},
        timeout=30,
    )
    try:
        for _ in range(120):
            try:
                if (await sandbox.get("/health")).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.5)

        monkeypatch.setenv("AUTOMATION_OPENHANDS_API_BASE_URL", fake.url)
        monkeypatch.setenv("AUTOMATION_SERVICE_KEY", "fake-service-key")
        monkeypatch.delenv("AUTOMATION_AGENT_SERVER_URL", raising=False)
        clear_config_cache()

        async def session_override(request: Request):
            async with async_session_factory() as session:
                bind_request(session, request)
                yield session
                await session.commit()

        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[authenticate_request] = lambda: mock_authenticated_user
        app.state.session_factory = async_session_factory
        app.state.http_client = create_http_client()

        async with async_session_factory() as session:
            automation = Automation(
                user_id=mock_authenticated_user.user_id,
                org_id=mock_authenticated_user.org_id,
                name="weekly report",
                trigger={"type": "cron", "schedule": "0 9 * * 1", "timezone": "UTC"},
                tarball_path="plugin-spec://runtime",
                entrypoint="python3 main.py",
            )
            session.add(automation)
            await session.flush()
            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.RUNNING,
                sandbox_id="sandbox-616",
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        log(f"{ts()}  run's conversation starts on the sandbox agent-server")
        resp = await sandbox.post(
            "/api/conversations",
            json={
                "agent": {
                    "llm": {
                        "model": "openai/mock",
                        "base_url": f"{fake.url}/llm/v1",
                        "api_key": "dummy",
                    },
                    "tools": [{"name": "task_tracker"}],
                },
                "workspace": {"working_dir": str(tmp_path / "ws")},
                "initial_message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Post the weekly report."}],
                    "run": True,
                },
            },
        )
        resp.raise_for_status()
        cid = resp.json()["id"]
        for _ in range(120):
            info = (await sandbox.get(f"/api/conversations/{cid}")).json()
            if info["execution_status"] in ("finished", "error", "stuck"):
                break
            await asyncio.sleep(0.25)
        events = (
            await sandbox.get(
                f"/api/conversations/{cid}/events/search", params={"limit": 100}
            )
        ).json()["items"]
        sandbox_kinds = [e["kind"] for e in events]
        log(
            f"{ts()}  conversation {info['execution_status']}; the agent-server "
            f"holds {len(sandbox_kinds)} events, last: {sandbox_kinds[-3:]}"
        )

        await asyncio.sleep(5)  # the runner calls /complete ~5 s after the last event
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/automation/v1/runs/{run_id}/complete",
                json={"status": "COMPLETED", "conversation_id": cid},
            )
        log(f"{ts()}  POST /runs/<id>/complete -> {resp.json()['status']}")

        deleted_at = None
        for _ in range(16):  # the watchdog's due-sandbox sweep, every 5 s
            await asyncio.sleep(5)
            await cleanup_due_sandboxes(async_session_factory)
            if fake.agent_server.poll() is not None and deleted_at is None:
                deleted_at = time.monotonic()
            if deleted_at and time.monotonic() - deleted_at > 10:
                break

        log(f"{ts()}  done watching (agent-server exited: {fake.agent_server.poll() is not None})")
        log()
        log(f"agent-server (sandbox) events : {len(sandbox_kinds)}")
        log(f"app server received           : {len(fake.received)}")
        missing = [_describe(e) for e in events if e["id"] not in fake.received_ids]
        if missing:
            log(f"BUG: app server never got the last {len(missing)}: {missing}")
        else:
            log("OK: app server has every event, including the final reply and "
                "the `finished` state update")
    finally:
        if fake.agent_server.poll() is None:
            fake.agent_server.send_signal(signal.SIGCONT)
            fake.agent_server.kill()
        await sandbox.aclose()
        app.dependency_overrides.clear()
        clear_config_cache()
