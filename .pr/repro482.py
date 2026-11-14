"""automation#482: what a run is handed when the bot is @-mentioned in a thread.

Drives the real Slack Socket Mode provider (`SlackStreamProvider.handle`) with
an `app_mention` envelope, a mocked Slack Web API on localhost, the real
`accept_event()` against a migrated SQLite database, the dispatcher's
`_build_event_payload()`, and the plugin-spec preset's `build_user_prompt()`.
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

tmp = tempfile.mkdtemp()
os.environ["AUTOMATION_DB_URL"] = f"sqlite+aiosqlite:///{tmp}/automations.db"

from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: E402
from slack_sdk.web.async_client import AsyncWebClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from openhands.automation.app import run_migrations  # noqa: E402
from openhands.automation.config import get_settings  # noqa: E402
from openhands.automation.db import create_engine, create_session_factory  # noqa: E402
from openhands.automation.dispatcher import _build_event_payload  # noqa: E402
from openhands.automation.ingest import accept_event  # noqa: E402
from openhands.automation.models import Automation, AutomationRun  # noqa: E402
from openhands.automation.presets.plugin_spec.prompt_builder import (  # noqa: E402
    build_user_prompt,
)
from openhands.automation.streams.slack import SlackStreamProvider  # noqa: E402

THREAD = [
    {"user": "U_ALICE", "ts": "1755000000.000100",
     "text": "Checkout is broken, see screenshot",
     "files": [{"name": "checkout-error.png"}]},
    {"user": "U_OCRBOT", "ts": "1755000030.000200",
     "text": "Transcript of checkout-error.png: 'Error 502: payment-service timeout'"},
    {"user": "U_ALICE", "ts": "1755000060.000300",
     "text": "<@U_BOT> what does the error in the screenshot say?"},
]
CALLS: list[str] = []


class SlackApi(BaseHTTPRequestHandler):
    def do_GET(self):
        path, _, query = self.path.partition("?")
        args = {k: v[0] for k, v in parse_qs(query).items()}
        CALLS.append(f"GET {path} channel={args.get('channel')} ts={args.get('ts')}")
        data = {"ok": True, "messages": THREAD, "has_more": False}
        out = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


class SocketClient:
    def __init__(self, web_client):
        self.web_client = web_client

    async def send_socket_mode_response(self, response):
        pass


async def main() -> None:
    server = HTTPServer(("127.0.0.1", 0), SlackApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = get_settings()
    await run_migrations(settings)
    engine = (await create_engine(settings)).engine
    sessions = create_session_factory(engine)

    org_id = uuid.uuid4()
    async with sessions() as session:
        session.add(Automation(
            user_id=uuid.uuid4(), org_id=org_id, name="Slack helper",
            tarball_path="oh-internal://uploads/x.tar.gz", entrypoint="python3 main.py",
            trigger={"type": "event", "source": "slack", "on": "app_mention",
                     "filter": "team_id == 'T1'"},
        ))
        await session.commit()

    provider = SlackStreamProvider(org_id=org_id, app_token="xapp-dummy",
                                   bot_token="xoxb-dummy", team_id="T1",
                                   bot_user_id="U_BOT")
    mention = dict(THREAD[-1], type="app_mention", channel="C1",
                   thread_ts=THREAD[0]["ts"])
    envelope = {"type": "event_callback", "team_id": "T1", "event_id": "Ev1",
                "event": mention}
    web = AsyncWebClient(token="xoxb-dummy",
                         base_url=f"http://127.0.0.1:{server.server_port}/api/")

    async def emit(event):
        async with sessions() as session:
            await accept_event(org_id, event, session)

    await provider.handle(
        SocketClient(web),
        SocketModeRequest(type="events_api", envelope_id="e1", payload=envelope),
        emit,
    )

    async with sessions() as session:
        run = (await session.execute(select(AutomationRun))).scalar_one()
        automation = await session.get(Automation, run.automation_id)
        payload = _build_event_payload(automation, run)

    print(f"Slack thread: {len(THREAD)} messages; the mention is reply #{len(THREAD) - 1}")
    print(f"mocked Slack Web API calls: {CALLS or 'none'}")
    prompt = build_user_prompt("Answer the user's question.", payload)
    print("--- prompt the run's agent receives (event JSON elided) ---")
    in_json = False
    for line in prompt.splitlines():
        if line == "```json":
            in_json = True
            print(line)
            print('{ "payload": { "event": { "text": "<@U_BOT> what does the error in '
                  'the screenshot say?", ... } } }')
            continue
        if in_json and line != "```":
            continue
        in_json = False
        print(line)
    has_transcript = "payment-service timeout" in prompt
    print("--- transcript ('Error 502: payment-service timeout') in prompt:",
          "YES" if has_transcript else "NO")
    await engine.dispose()
    server.shutdown()
    sys.exit(0 if has_transcript else 1)


asyncio.run(main())
