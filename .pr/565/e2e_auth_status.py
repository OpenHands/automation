"""Call the automation service with an X-Org-Id the key may not act in.

Usage, from this repository with its venv:
    uv run python .pr/565/e2e_auth_status.py

Starts a stand-in for OpenHands ``GET /api/v1/users/me`` on a local port: the
key's own org gets 200, any other org gets the app server's 403, and the org
named "outage" gets a 503. Starts the automation service with uvicorn in cloud
mode against it (SQLite), then calls it with curl.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

OWN_ORG = "11111111-1111-4111-8111-111111111111"
OTHER_ORG = "22222222-2222-4222-8222-222222222222"
OUTAGE_ORG = "33333333-3333-4333-8333-333333333333"


class UsersMe(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        org = self.headers.get("X-Org-Id", OWN_ORG)
        if org == OWN_ORG:
            code, body = 200, {
                "id": str(uuid.UUID(int=1)), "org_id": OWN_ORG, "email": "a@example.com",
                "role": "admin", "permissions": ["view_automations"],
            }
        elif org == OUTAGE_ORG:
            code, body = 503, {"detail": "Service Unavailable"}
        else:
            code, body = 403, {"detail": "API key is not authorized for this organization"}
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


upstream = HTTPServer(("127.0.0.1", 0), UsersMe)
threading.Thread(target=upstream.serve_forever, daemon=True).start()
port = free_port()
work = Path(tempfile.mkdtemp(prefix="e2e-auth-"))
env = {k: v for k, v in os.environ.items() if k != "AUTOMATION_AGENT_SERVER_URL"}
env.update({
    "AUTOMATION_DB_URL": f"sqlite+aiosqlite:///{work}/automation.db",
    "AUTOMATION_OPENHANDS_API_BASE_URL": f"http://127.0.0.1:{upstream.server_port}",
    "FILE_STORE": "local",
    "FILE_STORE_PATH": str(work / "files"),
    "LOG_LEVEL": "WARNING",
})
service = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "openhands.automation.app:app", "--port", str(port)],
    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/ready")
            break
        except Exception:
            time.sleep(0.3)
    base = f"http://127.0.0.1:{port}/api/automation"
    for label, org, path in [
        ("key's own org", OWN_ORG, "/v1?limit=1"),
        ("org the key may not act in", OTHER_ORG, "/v1?limit=1"),
        ("org the key may not act in", OTHER_ORG, f"/v1/kv?automation_id={uuid.UUID(int=7)}"),
        ("OpenHands API answering 503", OUTAGE_ORG, "/v1?limit=1"),
    ]:
        cmd = ["curl", "-s", "-w", "  -> HTTP %{http_code}", "-H", "Authorization: Bearer sk-oh-demo",
               "-H", f"X-Org-Id: {org}", base + path]
        out = subprocess.run(cmd, capture_output=True, text=True).stdout
        print(f"# {label}: GET /api/automation{path.split('?')[0]}\n{out}")
finally:
    service.terminate()
    service.wait()
    upstream.shutdown()
