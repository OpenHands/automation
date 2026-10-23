"""control-automation: the lever behind the verify-automation skill.

Launches an isolated OpenHands Automation Service from this checkout, in local
mode, next to the OpenHands Agent Server it dispatches runs to. Keeps per-run
state on disk and turns every agent-facing API step into a rerunnable command
that prints exactly one JSON object.

Exit codes: 0 ok, 1 action failed, 2 usage, 3 environment.
Run `control-automation --help`, then `control-automation <command> --help`.
"""

# Help epilogs hold copy-pasteable example commands; keep each on one line.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import base64
import contextlib
import functools
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
import tomllib
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NoReturn

import httpx


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
REPO_ROOT = SKILL_DIR.parent.parent.parent
FEATURE_MAP_DIR = SKILL_DIR / "references" / "feature-map"
CLI = "control-automation"
RUN_ENV = "AUTOMATION_VERIFY_RUN"
HOME_ENV = "AUTOMATION_VERIFY_HOME"
CHECKOUT_ENV = "AUTOMATION_VERIFY_CHECKOUT"
# Set by `map run` for each bullet: every CLI call empties this file when it
# starts and writes the JSON object it prints, so a `# bug: PATH OP VALUE`
# predicate can be matched against what the failing command observed.
OBSERVATION_ENV = "AUTOMATION_VERIFY_OBSERVATION"
SESSION_HEADER = "X-Session-API-Key"
BASE_PATH = "/api/automation"
FAMILY_FILE_RE = re.compile(r"^F(\d{2})-[a-z0-9-]+\.md$")
SUB_FEATURE_RE = re.compile(r"^- `(F\d{2}\.[a-z0-9][a-z0-9-]*)`: \S")
RESULTS = ("pass", "fail", "xfail", "xpass", "blocked", "not-run")
FERNET_TOKEN = re.compile(r"gAAAAA[A-Za-z0-9_\-]{40,}={0,2}")
# Generated custom-webhook secrets are never known up front; match them by shape.
WEBHOOK_SECRET = re.compile(r"whsec_[A-Za-z0-9_\-]{16,}")
TERMINAL_RUN_STATUSES = ("COMPLETED", "FAILED", "CANCELLED", "SKIPPED")
SERVICES = ("agent-server", "automation")
# The deterministic local identity (auth.py `_get_local_user`).
LOCAL_USER_ID = str(uuid.uuid5(uuid.NAMESPACE_DNS, "openhands-local-user"))
LOCAL_ORG_ID = str(uuid.uuid5(uuid.NAMESPACE_DNS, "openhands-local-org"))
# Shrunk loop intervals so scheduled and dispatched work shows up within
# seconds; `launch --production-intervals` keeps the service defaults.
FAST_INTERVALS = {
    "AUTOMATION_SCHEDULER_INTERVAL_SECONDS": "5",
    "AUTOMATION_DISPATCHER_INTERVAL_SECONDS": "2",
    "AUTOMATION_WATCHDOG_INTERVAL_SECONDS": "10",
}

# Environment forwarded from the caller into launched services. Everything
# else (LLM keys, AUTOMATION_*/OH_* overrides, cloud tokens) stays out unless
# passed with --env/--as-env or --pass-env, so a run never inherits a
# developer's configuration.
FORWARDED_ENV = re.compile(
    r"^(PATH|LANG|LC_[A-Z]+|TZ|TERM|SHELL|USER|LOGNAME|TMPDIR"
    r"|(HTTP|HTTPS|NO|ALL)_PROXY|(http|https|no|all)_proxy"
    r"|SSL_CERT_FILE|SSL_CERT_DIR|REQUESTS_CA_BUNDLE|CURL_CA_BUNDLE"
    r"|NODE_EXTRA_CA_CERTS|UV_[A-Z_]+)$"
)


# --------------------------------------------------------------------------
# Output, errors, redaction
# --------------------------------------------------------------------------


class CliError(Exception):
    """A failure reported as one JSON object with an actionable hint."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int = 1,
        hint: str | None = None,
        example: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.hint = hint
        self.example = example
        self.data = data or {}


def usage_error(message: str, example: str | None = None) -> NoReturn:
    raise CliError(message, exit_code=2, example=example)


def env_error(message: str, hint: str | None = None) -> NoReturn:
    raise CliError(message, exit_code=3, hint=hint)


class Redactor:
    """Replaces every known secret value with a stable label in all output."""

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}

    def add(self, value: str | None, label: str) -> None:
        if value and len(value) >= 6:
            self._secrets[value] = label

    def text(self, value: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret in value:
                value = value.replace(secret, f"<redacted:{self._secrets[secret]}>")
        value = WEBHOOK_SECRET.sub("<redacted:webhook-secret>", value)
        return FERNET_TOKEN.sub("<redacted:encrypted>", value)

    def obj(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(str(k)): self.obj(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.obj(v) for v in value]
        return value


REDACT = Redactor()
for _name in ("DEEPSEEK_API_KEY", "LLM_API_KEY", "OPENHANDS_API_KEY"):
    REDACT.add(os.environ.get(_name), _name)


def emit(payload: dict[str, Any]) -> None:
    text = json.dumps(REDACT.obj(payload), indent=2, default=str)
    sys.stdout.write(text + "\n")
    observation = os.environ.get(OBSERVATION_ENV)
    if observation:
        with contextlib.suppress(OSError):
            Path(observation).write_text(text)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_pairs(items: Sequence[str] | None, what: str) -> list[tuple[str, str]]:
    """KEY=VALUE pairs in order, repeated keys kept (ids=A&ids=B)."""
    pairs = []
    for item in items or []:
        if "=" not in item:
            usage_error(f"{what} must look like KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        pairs.append((key, value))
    return pairs


def parse_kv(items: Sequence[str] | None, what: str) -> dict[str, str]:
    return dict(parse_pairs(items, what))


def git(*args: str, cwd: Path = REPO_ROOT) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip()


def locked_sdk_version(checkout: Path) -> str:
    """The openhands-sdk pin of the checkout: the Agent Server release to pair."""
    data = tomllib.loads((checkout / "pyproject.toml").read_text())
    for dep in data.get("project", {}).get("dependencies", []):
        match = re.match(r"^openhands-sdk\s*==\s*([\w.]+)", dep)
        if match:
            return match.group(1)
    env_error(f"No `openhands-sdk==X` pin in {checkout}/pyproject.toml")


# --------------------------------------------------------------------------
# Processes and ports (no psutil: the service venv does not ship it)
# --------------------------------------------------------------------------


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def available_memory() -> int:
    with contextlib.suppress(OSError, ValueError):
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    return 1 << 40


def listening_inodes(port: int) -> set[str]:
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        with contextlib.suppress(OSError):
            for line in Path(table).read_text().splitlines()[1:]:
                parts = line.split()
                local, state, inode = parts[1], parts[3], parts[9]
                if state == "0A" and int(local.rsplit(":", 1)[1], 16) == port:
                    inodes.add(inode)
    return inodes


def listening_pids(port: int) -> set[int]:
    """Processes listening on PORT (Linux /proc; empty where unavailable)."""
    inodes = listening_inodes(port)
    pids: set[int] = set()
    if not inodes:
        return pids
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        with contextlib.suppress(OSError):
            for fd in (proc / "fd").iterdir():
                target = os.readlink(fd)
                if target.startswith("socket:[") and target[8:-1] in inodes:
                    pids.add(int(proc.name))
                    break
    return pids


def pgid_of(pid: int) -> int:
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return -1


def group_alive(pgid: int | None) -> bool:
    if not pgid:
        return False
    try:
        os.killpg(int(pgid), 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def stop_process_group(pgid: int, grace: float = 15.0) -> str:
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pgid, os.WNOHANG)
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "already-exited"
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pgid, os.WNOHANG)
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return "terminated"
        except PermissionError:
            pass
        time.sleep(0.2)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)
    return "killed"


def processes_with_home(home: str) -> list[tuple[int, str]]:
    """(pid, cmdline) of processes whose HOME lies inside HOME."""
    own = {os.getpid(), os.getppid()}
    found = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) in own:
            continue
        with contextlib.suppress(OSError):
            environ = (proc / "environ").read_bytes().split(b"\0")
            if any(e.startswith(f"HOME={home}".encode()) for e in environ):
                cmd = (proc / "cmdline").read_bytes().replace(b"\0", b" ")
                found.append((int(proc.name), cmd.decode(errors="replace")[:160]))
    return found


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


def runs_home() -> Path:
    default = Path(os.environ.get("TMPDIR", "/tmp")) / "automation-verify"
    return Path(os.environ.get(HOME_ENV, str(default)))


@dataclass
class Run:
    """One launched stack (automation service + agent server) and what it owns."""

    dir: Path
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.dir.name

    @property
    def url(self) -> str:
        return str(self.data["automation"]["url"])

    @property
    def agent_server_url(self) -> str:
        return str(self.data["agent_server"]["url"])

    def service_url(self, target: str) -> str:
        return self.agent_server_url if target == "agent-server" else self.url

    @property
    def private(self) -> Path:
        return self.dir / "private"

    @property
    def evidence(self) -> Path:
        return self.dir / "evidence"

    @property
    def fixtures(self) -> Path:
        return self.dir / "fixtures"

    @property
    def home(self) -> Path:
        return self.dir / "home"

    @property
    def db_path(self) -> Path:
        return self.private / "automations.db"

    @property
    def session_key(self) -> str | None:
        path = self.private / "session_api_key"
        return path.read_text().strip() if path.exists() else None

    def agent_server_key(self) -> str | None:
        path = self.private / "agent_server_api_key"
        return path.read_text().strip() if path.exists() else self.session_key

    def log_path(self, service: str) -> Path:
        return self.private / f"{service}.log"

    def save(self) -> None:
        (self.dir / "run.json").write_text(json.dumps(self.data, indent=2) + "\n")

    def register_secrets(self) -> None:
        for name in (
            "session_api_key",
            "agent_server_api_key",
            "secret_key",
            "kv_secret",
            "previous_session_api_key",
        ):
            path = self.private / name
            if path.exists():
                REDACT.add(path.read_text().strip(), name.replace("_", "-"))

    def service(self, name: str) -> dict[str, Any]:
        return self.data[name.replace("-", "_")]

    def service_alive(self, name: str) -> bool:
        svc = self.service(name)
        if svc.get("mode") == "attach":
            return True
        return not svc.get("stopped_at") and group_alive(svc.get("pgid"))

    def alive(self) -> bool:
        if self.data.get("stopped_at"):
            return False
        if all(self.service(s).get("mode") == "attach" for s in SERVICES):
            return True
        return any(
            self.service(s).get("mode") != "attach" and self.service_alive(s)
            for s in SERVICES
        )

    def processes(self) -> list[dict[str, Any]]:
        return list(self.data.setdefault("processes", []))


def load_run(path: Path) -> Run:
    run_json = path / "run.json"
    if not run_json.exists():
        env_error(
            f"{path} is not a run directory (no run.json).",
            hint=f"Start one with `{CLI} launch --new --print-run`.",
        )
    run = Run(path, json.loads(run_json.read_text()))
    run.register_secrets()
    return run


def all_runs() -> list[Run]:
    home = runs_home()
    if not home.exists():
        return []
    return [
        load_run(child)
        for child in sorted(home.iterdir())
        if (child / "run.json").exists()
    ]


def resolve_run(explicit: str | None, *, require_alive: bool = True) -> Run:
    """--run DIR, else $AUTOMATION_VERIFY_RUN, else the only live run."""
    chosen = explicit or os.environ.get(RUN_ENV)
    if chosen:
        candidate = Path(chosen)
        if not candidate.is_absolute() and not candidate.exists():
            candidate = runs_home() / chosen
        run = load_run(candidate)
    else:
        live = [r for r in all_runs() if r.alive()]
        if not live:
            env_error(
                "No live run.",
                hint=f"export {RUN_ENV}=$({CLI} launch --new --print-run)",
            )
        if len(live) > 1:
            env_error(
                f"{len(live)} live runs; refusing to guess which one to drive.",
                hint=f"export {RUN_ENV}=<run dir> (see `{CLI} runs`).",
            )
        run = live[0]
    if require_alive and not run.alive():
        env_error(
            f"Run {run.id} is not running.",
            hint=f"`{CLI} restart --run {run.dir}` or launch a new run.",
        )
    return run


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def http_client(timeout: float = 60.0) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False)


AUTH_MODES = ("key", "none", "bad", "bearer", "bearer-bad", "previous", "agent-key")


def auth_headers(
    run: Run, mode: str = "key", target: str = "automation"
) -> dict[str, str]:
    key = run.agent_server_key() if target == "agent-server" else run.session_key
    if mode == "agent-key":
        key = run.agent_server_key()
        mode = "key"
    if mode == "none" or not key:
        return {}
    if mode == "bad":
        return {SESSION_HEADER: "qa-wrong-session-key"}
    if mode == "bearer":
        return {"Authorization": f"Bearer {key}"}
    if mode == "bearer-bad":
        return {"Authorization": "Bearer qa-wrong-session-key"}
    if mode == "previous":
        previous = run.private / "previous_session_api_key"
        if not previous.exists():
            usage_error("--auth previous needs a key rotated by `restart --rotate-key`")
        return {SESSION_HEADER: previous.read_text().strip()}
    return {SESSION_HEADER: key}


def decode_body(response: httpx.Response, limit: int) -> tuple[Any, bool]:
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            return response.json(), False
        except ValueError:
            pass
    if content_type.startswith(("text/", "application/x-ndjson")) or not content_type:
        text = response.text
        return (text[:limit], len(text) > limit)
    return (f"<{len(response.content)} bytes of {content_type}>", False)


def status_matches(status: int, expect: str | None) -> bool:
    if not expect:
        return status < 400
    for token in expect.split(","):
        token = token.strip()
        if token.endswith("xx") and str(status)[0] == token[0]:
            return True
        if token.isdigit() and int(token) == status:
            return True
    return False


def dig(value: Any, path: str) -> Any:
    """Follow a dotted path (`items.0.id`) through dicts and lists."""
    current = value
    for part in [p for p in path.split(".") if p]:
        if isinstance(current, list) and part.lstrip("-").isdigit():
            index = int(part)
            if -len(current) <= index < len(current):
                current = current[index]
                continue
            return None
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        return None
    return current


def has_path(value: Any, path: str) -> bool:
    """Whether every step of a dotted path exists; a stored `null` counts.

    `dig` answers `None` both for a missing key and for a `null` value, so a
    `# bug:` predicate checks this first: an error object without `checks`
    must not match `checks.0.actual eq null`.
    """
    current = value
    for part in [p for p in path.split(".") if p]:
        if isinstance(current, list) and part.lstrip("-").isdigit():
            if not -len(current) <= int(part) < len(current):
                return False
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False
    return True


def save_evidence(run: Run, target: str, payload: Any, suffix: str = ".json") -> Path:
    """Save under evidence/<feature>/<name>; never overwrite (name-2, name-3)."""
    if "/" not in target:
        usage_error("--save needs FEATURE/NAME", example="--save F05.create/response")
    feature, name = target.split("/", 1)
    folder = run.evidence / feature
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}{suffix}"
    counter = 2
    while path.exists():
        path = folder / f"{name}-{counter}{suffix}"
        counter += 1
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    elif isinstance(payload, str):
        path.write_text(REDACT.text(payload))
    else:
        path.write_text(json.dumps(REDACT.obj(payload), indent=2, default=str) + "\n")
    return path


def api_call(
    run: Run,
    method: str,
    path: str,
    *,
    target: str = "automation",
    body: Any = None,
    params: list[tuple[str, str]] | None = None,
    auth: str = "key",
    headers: dict[str, str] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
    form: dict[str, str] | None = None,
    content: bytes | None = None,
    timeout: float = 60.0,
) -> httpx.Response:
    request_headers = auth_headers(run, auth, target)
    request_headers.update(headers or {})
    kwargs: dict[str, Any] = {"headers": request_headers}
    if params:
        kwargs["params"] = params
    if files or form:
        kwargs["files"] = files
        kwargs["data"] = form
    elif content is not None:
        kwargs["content"] = content
    elif body is not None:
        kwargs["json"] = body
    with http_client(timeout) as client:
        return client.request(method.upper(), run.service_url(target) + path, **kwargs)


def api_json(run: Run, method: str, path: str, **kwargs: Any) -> Any:
    """Call an API and return JSON, raising CliError on a non-2xx status."""
    response = api_call(run, method, path, **kwargs)
    body, _ = decode_body(response, 2000)
    if response.status_code >= 400:
        raise CliError(
            f"{method} {path} returned {response.status_code}",
            data={"status": response.status_code, "body": body},
        )
    return body


# --------------------------------------------------------------------------
# launch / stop / restart / service / runs / logs
# --------------------------------------------------------------------------


def checkout_python(checkout: Path) -> list[str]:
    venv_python = checkout / ".venv" / "bin" / "python"
    if venv_python.exists():
        return [str(venv_python)]
    env_error(
        f"No Python environment for checkout {checkout}.",
        hint=f"Run `uv sync --dev` in {checkout}.",
    )


def base_env(run: Run, pass_env: Sequence[str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if FORWARDED_ENV.match(k)}
    for name in pass_env:
        if name not in os.environ:
            usage_error(f"--pass-env {name}: not set in this shell")
        env[name] = os.environ[name]
    real_home = Path.home()
    env.update(
        {
            "HOME": str(run.home),
            "GIT_CONFIG_GLOBAL": str(run.home / ".gitconfig"),
            "UV_CACHE_DIR": env.get("UV_CACHE_DIR", str(real_home / ".cache" / "uv")),
            "UV_PYTHON_INSTALL_DIR": env.get(
                "UV_PYTHON_INSTALL_DIR",
                str(real_home / ".local" / "share" / "uv" / "python"),
            ),
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
        }
    )
    for name in ("NO_PROXY", "no_proxy"):
        hosts = [h for h in env.get(name, "").split(",") if h]
        env[name] = ",".join(dict.fromkeys([*hosts, "127.0.0.1", "localhost"]))
    return env


def agent_server_command(run: Run) -> list[str]:
    svc = run.data["agent_server"]
    if svc.get("path"):
        sdk = Path(svc["path"])
        return [
            "uvx",
            "--reinstall",
            "--from",
            str(sdk / "openhands-agent-server"),
            "--with-editable",
            str(sdk / "openhands-sdk"),
            "--with-editable",
            str(sdk / "openhands-tools"),
            "--with-editable",
            str(sdk / "openhands-workspace"),
            "agent-server",
        ]
    return [
        "uvx",
        "--from",
        f"openhands-agent-server=={svc['version']}",
        "agent-server",
    ]


def agent_server_env(run: Run) -> dict[str, str]:
    svc = run.data["agent_server"]
    state = run.home / ".openhands"
    env = base_env(run, run.data.get("pass_env", []))
    env.update(
        {
            "OH_PERSISTENCE_DIR": str(state),
            "OH_CONVERSATIONS_PATH": str(state / "conversations"),
            "OH_BASH_EVENTS_DIR": str(state / "bash_events"),
            "OH_SESSION_API_KEYS_0": run.agent_server_key() or "",
            "OH_SECRET_KEY": (run.private / "secret_key").read_text().strip(),
            "OH_ENABLE_VSCODE": "false",
            "OH_PRELOAD_TOOLS": "false",
            "TMUX_TMPDIR": tmux_dir(run),
            # What Agent Canvas exports (scripts/dev-safe.mjs): the preset
            # scripts read AGENT_SERVER_URL, and agents creating automations
            # read OPENHANDS_AUTOMATION_API_KEY.
            "AGENT_SERVER_URL": svc["url"],
            "OPENHANDS_AUTOMATION_API_KEY": run.session_key or "",
            "OPENHANDS_SUPPRESS_BANNER": "1",
        }
    )
    env.update(svc.get("extra_env", {}))
    return env


def automation_env(run: Run) -> dict[str, str]:
    svc = run.data["automation"]
    env = base_env(run, run.data.get("pass_env", []))
    env.update(
        {
            "AUTOMATION_AGENT_SERVER_URL": run.agent_server_url,
            "AUTOMATION_AGENT_SERVER_API_KEY": run.agent_server_key() or "",
            "AUTOMATION_LOCAL_API_KEY": run.session_key or "",
            "AUTOMATION_DB_URL": f"sqlite+aiosqlite:///{run.db_path}",
            "AUTOMATION_BASE_URL": svc["url"],
            "AUTOMATION_WORKSPACE_BASE": str(run.dir / "workspaces"),
            "AUTOMATION_KV_SECRET": (run.private / "kv_secret").read_text().strip(),
            "AUTOMATION_CORS_ORIGINS": "http://localhost:3001,http://127.0.0.1:3001",
            "AUTOMATION_POSTHOG_API_KEY": "",
            "FILE_STORE": "local",
            "LOCAL_STORAGE_PATH": str(run.dir / "storage"),
            "OPENHANDS_SUPPRESS_BANNER": "1",
        }
    )
    if run.data.get("fast_intervals", True):
        env.update(FAST_INTERVALS)
    if run.data.get("telemetry_sink"):
        env["AUTOMATION_POSTHOG_API_KEY"] = "phc_qa_verify"
        env["AUTOMATION_POSTHOG_HOST"] = run.data["telemetry_sink"]
    env.update(svc.get("extra_env", {}))
    return env


def tmux_dir(run: Run) -> str:
    """Short per-run tmux dir: a Unix socket path must stay under 108 bytes."""
    path = run.data.get("tmux_dir")
    if not path:
        path = f"/tmp/oav-{secrets.token_hex(3)}"
        run.data["tmux_dir"] = path
        run.save()
    Path(path).mkdir(parents=True, exist_ok=True)
    return path


def start_service(run: Run, name: str, timeout: float) -> dict[str, Any]:
    svc = run.service(name)
    if svc.get("mode") == "attach":
        return {"service": name, "attached": True}
    if name == "agent-server":
        command = [
            *agent_server_command(run),
            "--host",
            str(run.data["host"]),
            "--port",
            str(svc["port"]),
        ]
        env = agent_server_env(run)
        cwd = run.dir / "agent-server"
        ready_path = "/ready"
    else:
        checkout = Path(run.data["checkout"])
        command = [
            *checkout_python(checkout),
            "-m",
            "uvicorn",
            "openhands.automation.app:app",
            "--host",
            str(run.data["host"]),
            "--port",
            str(svc["port"]),
        ]
        env = automation_env(run)
        cwd = run.dir / "automation"
        ready_path = "/ready"
    cwd.mkdir(exist_ok=True)
    log = run.log_path(name).open("ab")
    log.write(f"\n=== {now_iso()} start: {' '.join(command)}\n".encode())
    log.flush()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    svc.update(
        {
            "pid": process.pid,
            "pgid": os.getpgid(process.pid),
            "started_at": now_iso(),
            "stopped_at": None,
            "command": command,
        }
    )
    run.data["stopped_at"] = None
    run.save()
    started = time.monotonic()
    last_error = ""
    with http_client(2.0) as client:
        while time.monotonic() - started < timeout:
            if process.poll() is not None:
                raise CliError(
                    f"{name} exited with code {process.returncode} during startup.",
                    exit_code=3,
                    hint=f"Read the log: `{CLI} logs --service {name} --tail 80`.",
                    data={"log": str(run.log_path(name))},
                )
            try:
                response = client.get(svc["url"] + ready_path)
                owners = listening_pids(int(svc["port"]))
                ours = not owners or any(pgid_of(p) == svc["pgid"] for p in owners)
                if response.status_code == 200 and ours:
                    return {
                        "service": name,
                        "ready_ms": int((time.monotonic() - started) * 1000),
                    }
                last_error = f"{ready_path} -> {response.status_code}"
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
            time.sleep(0.3)
    raise CliError(
        f"{name} not ready after {timeout:.0f}s ({last_error}).",
        exit_code=3,
        hint=f"`{CLI} logs --service {name} --tail 80`, then `{CLI} stop`.",
    )


def stop_service(run: Run, name: str) -> dict[str, Any]:
    svc = run.service(name)
    if svc.get("mode") == "attach" or not svc.get("pgid") or svc.get("stopped_at"):
        return {"service": name, "outcome": "not-running"}
    outcome = stop_process_group(int(svc["pgid"]))
    port = int(svc["port"])
    deadline = time.monotonic() + 10
    while port_open(str(run.data["host"]), port) and time.monotonic() < deadline:
        time.sleep(0.2)
    svc["stopped_at"] = now_iso()
    run.save()
    return {
        "service": name,
        "outcome": outcome,
        "port_closed": not port_open(str(run.data["host"]), port),
    }


def new_run_dir(label: str | None) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{secrets.token_hex(2)}" + (f"-{label}" if label else "")
    path = runs_home() / name
    for sub in ("private", "evidence", "fixtures", "home", "workspaces", "storage"):
        (path / sub).mkdir(parents=True, exist_ok=True)
    os.chmod(path / "private", 0o700)
    (path / "home" / ".gitconfig").write_text(
        "[user]\n\tname = QA Verify\n\temail = qa-verify@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n[safe]\n\tdirectory = *\n"
    )
    return path


def cmd_launch(args: argparse.Namespace) -> dict[str, Any]:
    if not args.new:
        live = [r for r in all_runs() if r.alive()]
        chosen = os.environ.get(RUN_ENV)
        if chosen:
            live = [r for r in live if str(r.dir) == chosen or r.id == chosen]
        if len(live) == 1:
            run = live[0]
            if args.print_run:
                print(run.dir)
                raise SystemExit(0)
            return {"ok": True, "already_running": True, "run": str(run.dir)}
    checkout = Path(args.checkout or os.environ.get(CHECKOUT_ENV, REPO_ROOT)).resolve()
    if not (checkout / "openhands" / "automation" / "app.py").exists():
        usage_error(f"{checkout} is not an OpenHands/automation checkout")
    if args.agent_server_url and not args.agent_server_key_env:
        usage_error(
            "--agent-server-url needs --agent-server-key-env VAR",
            example=f"{CLI} launch --new --agent-server-url http://127.0.0.1:18000 --agent-server-key-env LOCAL_BACKEND_API_KEY",
        )
    available = available_memory()
    if available < 1_200_000_000 and not args.force:
        env_error(
            f"Only {available // 1_000_000} MB of memory free; a run needs ~1.2 GB.",
            hint=f"Stop runs you are done with (`{CLI} runs`), or pass --force.",
        )
    if not args.agent_server_url and not shutil.which("uvx"):
        env_error("uvx not found; it starts the Agent Server.", hint="Install uv.")
    run = Run(new_run_dir(args.name))
    host = "127.0.0.1"
    as_port = free_port(host)
    au_port = args.port or free_port(host)
    agent_server: dict[str, Any] = {
        "mode": "attach" if args.agent_server_url else "launch",
        "port": as_port,
        "url": (args.agent_server_url or f"http://{host}:{as_port}").rstrip("/"),
        "version": args.agent_server_version or locked_sdk_version(checkout),
        "path": str(Path(args.agent_server_path).resolve())
        if args.agent_server_path
        else None,
        "extra_env": parse_kv(args.as_env, "--as-env"),
    }
    run.data = {
        "id": run.id,
        "host": host,
        "checkout": str(checkout),
        "checkout_sha": git("rev-parse", "HEAD", cwd=checkout),
        "checkout_ref": git("rev-parse", "--abbrev-ref", "HEAD", cwd=checkout),
        "fast_intervals": not args.production_intervals,
        "pass_env": list(args.pass_env or []),
        "created_at": now_iso(),
        "agent_server": agent_server,
        "automation": {
            "mode": "launch",
            "port": au_port,
            "url": f"http://{host}:{au_port}",
            "extra_env": parse_kv(args.env, "--env"),
        },
        "processes": [],
    }
    (run.private / "session_api_key").write_text(secrets.token_urlsafe(24))
    (run.private / "secret_key").write_text(secrets.token_urlsafe(32))
    (run.private / "kv_secret").write_text(secrets.token_urlsafe(32))
    if args.agent_server_url:
        if args.agent_server_key_env not in os.environ:
            usage_error(
                f"--agent-server-key-env {args.agent_server_key_env} is not set"
            )
        (run.private / "agent_server_api_key").write_text(
            os.environ[args.agent_server_key_env]
        )
    run.register_secrets()
    run.save()
    if args.telemetry_sink:
        sink = start_http_sink(run, "telemetry")
        run.data["telemetry_sink"] = sink["url"]
        run.save()
    readiness = []
    try:
        readiness.append(start_service(run, "agent-server", args.timeout))
        readiness.append(start_service(run, "automation", args.timeout))
    except CliError:
        for name in reversed(SERVICES):
            stop_service(run, name)
        run.data["stopped_at"] = now_iso()
        run.save()
        raise
    if args.print_run:
        print(run.dir)
        raise SystemExit(0)
    return {
        "ok": True,
        "run": str(run.dir),
        "automation_url": run.url,
        "agent_server_url": run.agent_server_url,
        "agent_server": {
            k: agent_server[k] for k in ("mode", "version", "path") if agent_server[k]
        },
        "checkout_sha": run.data["checkout_sha"],
        "fast_intervals": run.data["fast_intervals"],
        "readiness": readiness,
        "next": [f"export {RUN_ENV}={run.dir}", f"{CLI} doctor"],
    }


def stop_helpers(run: Run) -> list[dict[str, Any]]:
    stopped = []
    for proc in run.data.get("processes", []):
        if proc.get("stopped_at"):
            continue
        outcome = stop_process_group(int(proc["pgid"]), grace=5.0)
        proc["stopped_at"] = now_iso()
        stopped.append({"name": proc.get("name"), "outcome": outcome})
    run.save()
    return stopped


def cmd_stop(args: argparse.Namespace) -> dict[str, Any]:
    run = resolve_run(args.run, require_alive=False)
    result: dict[str, Any] = {"ok": True, "run": str(run.dir)}
    result["helpers"] = stop_helpers(run)
    result["services"] = [stop_service(run, s) for s in reversed(SERVICES)]
    if any(s.get("port_closed") is False for s in result["services"]):
        result["ok"] = False
    reaped = []
    for pid, cmd in processes_with_home(str(run.home)):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
            reaped.append({"pid": pid, "cmdline": cmd})
    result["orphans_reaped"] = reaped
    run.data["stopped_at"] = now_iso()
    run.save()
    if run.data.get("tmux_dir"):
        shutil.rmtree(run.data["tmux_dir"], ignore_errors=True)
    if args.purge_private:
        for sub in (
            "private",
            "home",
            "workspaces",
            "storage",
            "fixtures",
            "agent-server",
            "automation",
        ):
            shutil.rmtree(run.dir / sub, ignore_errors=True)
        result["purged"] = True
    result["evidence"] = str(run.evidence)
    result["evidence_files"] = sum(1 for p in run.evidence.rglob("*") if p.is_file())
    return result


def selected_services(value: str) -> list[str]:
    return list(SERVICES) if value == "all" else [value]


def cmd_restart(args: argparse.Namespace) -> dict[str, Any]:
    run = resolve_run(args.run, require_alive=False)
    services = selected_services(args.service)
    if args.rotate_key:
        services = list(SERVICES)
        old = run.session_key or ""
        (run.private / "previous_session_api_key").write_text(old)
        (run.private / "session_api_key").write_text(secrets.token_urlsafe(24))
        run.register_secrets()
    for name in services:
        svc = run.service(name)
        if args.reset_env:
            svc["extra_env"] = {}
        if args.env:
            if name != "automation" and args.service == "all":
                continue
            svc.setdefault("extra_env", {}).update(parse_kv(args.env, "--env"))
    if args.fast is not None:
        run.data["fast_intervals"] = args.fast == "on"
    run.save()
    stopped = []
    for name in reversed(services):
        if (
            args.hard
            and run.service_alive(name)
            and run.service(name).get("mode") != "attach"
        ):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(int(run.service(name)["pgid"]), signal.SIGKILL)
            run.service(name)["stopped_at"] = now_iso()
            stopped.append({"service": name, "outcome": "killed"})
        else:
            stopped.append(stop_service(run, name))
    readiness = [start_service(run, name, args.timeout) for name in services]
    return {
        "ok": True,
        "run": str(run.dir),
        "services": services,
        "stopped": stopped,
        "readiness": readiness,
        "rotated_key": bool(args.rotate_key),
    }


def cmd_service(args: argparse.Namespace) -> dict[str, Any]:
    run = resolve_run(args.run, require_alive=False)
    if args.action == "stop":
        return {"ok": True, **stop_service(run, args.name)}
    if args.action == "start":
        if run.service_alive(args.name):
            return {"ok": True, "service": args.name, "already_running": True}
        return {"ok": True, **start_service(run, args.name, args.timeout)}
    return {
        "ok": True,
        "service": args.name,
        "alive": run.service_alive(args.name),
        "url": run.service_url(args.name),
        "pgid": run.service(args.name).get("pgid"),
    }


def cmd_runs(args: argparse.Namespace) -> dict[str, Any]:
    del args
    rows = [
        {
            "run": str(run.dir),
            "alive": run.alive(),
            "automation_url": run.data.get("automation", {}).get("url"),
            "agent_server_url": run.data.get("agent_server", {}).get("url"),
            "created_at": run.data.get("created_at"),
            "checkout_sha": run.data.get("checkout_sha"),
        }
        for run in all_runs()
    ]
    return {"ok": True, "home": str(runs_home()), "runs": rows}


def cmd_logs(args: argparse.Namespace) -> dict[str, Any]:
    run = resolve_run(args.run, require_alive=False)
    path = run.log_path(args.service)
    if not path.exists():
        return {"ok": True, "lines": [], "note": f"no {args.service} log (attached?)"}
    deadline = time.monotonic() + (args.wait or 0)
    while True:
        lines = path.read_text(errors="replace").splitlines()
        if args.since_restart:
            starts = [i for i, line in enumerate(lines) if line.startswith("=== ")]
            if starts:
                lines = lines[starts[-1] :]
        if args.grep:
            pattern = re.compile(args.grep)
            lines = [line for line in lines if pattern.search(line)]
        ok = args.expect_min is None or len(lines) >= args.expect_min
        if ok or args.expect_min is None or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    if args.expect_none:
        ok = not lines
    return {
        "ok": ok,
        "log": str(path),
        "lines": [
            REDACT.text(line) if args.full else REDACT.text(line)[:2000]
            for line in lines[-args.tail :]
        ],
        "total_matching": len(lines),
    }


# --------------------------------------------------------------------------
# HTTP sink (telemetry capture, external tarball host, callback catcher)
# --------------------------------------------------------------------------


def serve_http_sink(port: int, out: Path, serve_dir: str | None) -> None:
    """Hidden helper process: record every request as one JSONL line.

    With a serve directory, GET answers files from it (an external tarball
    host); other requests reply QA_SINK_STATUS with QA_SINK_BODY.
    """

    class Handler(BaseHTTPRequestHandler):
        def _record(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body: Any = json.loads(raw) if raw else None
            except ValueError:
                body = raw.decode(errors="replace")[:4000]
            entry = {
                "at": now_iso(),
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": body,
            }
            with out.open("a") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")
            if serve_dir and self.command == "GET":
                target = (
                    Path(serve_dir) / self.path.lstrip("/").split("?")[0]
                ).resolve()
                if target.is_file() and target.is_relative_to(
                    Path(serve_dir).resolve()
                ):
                    data = target.read_bytes()
                    self.send_response(200)
                    self.send_header("content-type", "application/gzip")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
            status = int(os.environ.get("QA_SINK_STATUS", "200"))
            reply = os.environ.get("QA_SINK_BODY", '{"ok": true}').encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        do_POST = do_PUT = do_PATCH = do_GET = do_DELETE = _record

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def start_http_sink(
    run: Run,
    name: str,
    status: int = 200,
    body: str | None = None,
    serve_dir: Path | None = None,
) -> dict[str, Any]:
    for proc in run.processes():
        if proc.get("name") == name and not proc.get("stopped_at"):
            return {"path": proc["out"], "url": proc["url"], "already_running": True}
    port = free_port()
    out = run.fixtures / f"{name}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.touch()
    log = (run.fixtures / f"{name}.log").open("ab")
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "_http-sink",
            str(port),
            str(out),
            str(serve_dir or ""),
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={
            **os.environ,
            "QA_SINK_STATUS": str(status),
            "QA_SINK_BODY": body or '{"ok": true}',
        },
    )
    deadline = time.monotonic() + 10
    while not port_open("127.0.0.1", port) and time.monotonic() < deadline:
        time.sleep(0.1)
    url = f"http://127.0.0.1:{port}"
    run.data.setdefault("processes", []).append(
        {
            "name": name,
            "kind": "http-sink",
            "pid": process.pid,
            "pgid": os.getpgid(process.pid),
            "url": url,
            "out": str(out),
            "started_at": now_iso(),
        }
    )
    run.save()
    return {"path": str(out), "url": url}


def _brief(frame: Any) -> Any:
    text = json.dumps(frame, default=str)
    return frame if len(text) <= 400 else text[:400] + "…"


def cmd_sink_read(args: argparse.Namespace) -> dict[str, Any]:
    run = resolve_run(args.run, require_alive=False)
    out = run.fixtures / f"{args.name}.jsonl"
    if not out.exists():
        usage_error(
            f"No HTTP sink named {args.name}",
            example=f"{CLI} fixture http-sink --name {args.name}",
        )
    deadline = time.monotonic() + args.wait
    while True:
        entries = [
            json.loads(line) for line in out.read_text().splitlines() if line.strip()
        ]
        if args.path:
            entries = [e for e in entries if e["path"].startswith(args.path)]
        for needle in args.contains or []:
            entries = [e for e in entries if needle in json.dumps(e, default=str)]
        # Without --expect-min, --wait waits its full time (an absence check).
        enough = args.expect_min is not None and len(entries) >= args.expect_min
        if enough or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    paths: dict[str, int] = {}
    for entry in entries:
        paths[entry["path"]] = paths.get(entry["path"], 0) + 1
    result: dict[str, Any] = {
        "ok": True,
        "requests": len(entries),
        "paths": paths,
        "last": [_brief(e) for e in entries[-args.show :]] if args.show else [],
    }
    if args.expect_min is not None:
        result["ok"] = len(entries) >= args.expect_min
    if args.expect_max is not None:
        result["ok"] = result["ok"] and len(entries) <= args.expect_max
    if args.save:
        result["saved"] = str(save_evidence(run, args.save, {"requests": entries}))
    return result


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> dict[str, Any]:
    run = resolve_run(args.run, require_alive=False)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any, required: bool = True) -> None:
        checks.append({"name": name, "ok": ok, "required": required, "detail": detail})

    for name in SERVICES:
        svc = run.service(name)
        if svc.get("mode") == "attach":
            check(f"{name}-process", True, {"mode": "attach", "url": svc["url"]})
            continue
        check(f"{name}-process", run.service_alive(name), {"pgid": svc.get("pgid")})
        owners = listening_pids(int(svc["port"]))
        owned = any(pgid_of(pid) == svc.get("pgid") for pid in owners)
        check(
            f"{name}-port-owner",
            owned or not owners,
            {"port": svc["port"], "listening_pids": sorted(owners)},
        )
    with http_client(10) as client:
        for path in ("/health", "/ready", f"{BASE_PATH}/health", f"{BASE_PATH}/ready"):
            try:
                response = client.get(run.url + path)
                check(
                    f"automation {path}",
                    response.status_code == 200,
                    response.status_code,
                )
            except httpx.HTTPError as exc:
                check(f"automation {path}", False, f"{type(exc).__name__}: {exc}")
        try:
            info = client.get(run.url + f"{BASE_PATH}/server_info").json()
            check("automation-server-info", True, info)
        except Exception as exc:
            check("automation-server-info", False, f"{type(exc).__name__}: {exc}")
        try:
            sdk = client.get(run.url + f"{BASE_PATH}/sdk-version")
            check(
                "automation-sdk-version",
                sdk.status_code == 200,
                decode_body(sdk, 200)[0],
            )
        except httpx.HTTPError as exc:
            check("automation-sdk-version", False, f"{type(exc).__name__}: {exc}")
        probe = f"{BASE_PATH}/v1"
        denied = client.get(run.url + probe)
        check("auth-rejects-missing-key", denied.status_code == 401, denied.status_code)
        wrong = client.get(run.url + probe, headers=auth_headers(run, "bad"))
        check("auth-rejects-wrong-key", wrong.status_code == 401, wrong.status_code)
        allowed = client.get(run.url + probe, headers=auth_headers(run))
        check("auth-accepts-key", allowed.status_code == 200, allowed.status_code)
        if (run.private / "previous_session_api_key").exists():
            stale = client.get(run.url + probe, headers=auth_headers(run, "previous"))
            check(
                "auth-rejects-rotated-key", stale.status_code == 401, stale.status_code
            )
        try:
            ready = client.get(run.agent_server_url + "/ready")
            check("agent-server /ready", ready.status_code == 200, ready.status_code)
            info = client.get(run.agent_server_url + "/server_info").json()
            check(
                "agent-server-info",
                True,
                {k: info.get(k) for k in ("version", "sdk_version", "build_git_sha")},
            )
            key_ok = client.get(
                run.agent_server_url + "/api/settings",
                headers=auth_headers(run, "key", "agent-server"),
            )
            check(
                "agent-server-accepts-key",
                key_ok.status_code == 200,
                key_ok.status_code,
            )
            profiles = client.get(
                run.agent_server_url + "/api/settings",
                headers=auth_headers(run, "key", "agent-server"),
            ).json()
            check(
                "agent-server-llm",
                bool(
                    profiles.get("active_profile")
                    or dig(profiles, "agent_settings.llm.model")
                ),
                {
                    "active_profile": profiles.get("active_profile"),
                    "hint": f"`{CLI} llm preset deepseek` before driving runs",
                },
                required=False,
            )
        except Exception as exc:
            check("agent-server", False, f"{type(exc).__name__}: {exc}")
    check("database", run.db_path.exists(), str(run.db_path))
    for name in SERVICES:
        log = run.log_path(name)
        if log.exists():
            text = log.read_text(errors="replace")
            count = text.count("Traceback (most recent call last)")
            check(
                f"{name}-log-tracebacks", count == 0, {"count": count}, required=False
            )
    ok = all(c["ok"] for c in checks if c["required"])
    result: dict[str, Any] = {
        "ok": ok,
        "run": str(run.dir),
        "automation_url": run.url,
        "agent_server_url": run.agent_server_url,
        "checks": checks,
    }
    if not ok:
        result["hint"] = (
            f"Fix the failing check before driving; `{CLI} logs --service automation --tail 80`."
        )
    return result


# --------------------------------------------------------------------------
# api
# --------------------------------------------------------------------------


def full_path(path: str, target: str) -> str:
    """Recipes may write `/v1/...` for the service; it lives under BASE_PATH."""
    if target == "automation" and (path == "/v1" or path.startswith(("/v1/", "/v1?"))):
        return BASE_PATH + path
    return path


def read_body(args: argparse.Namespace) -> Any:
    sources = [s for s in (args.json, args.json_file, args.stdin) if s]
    if len(sources) > 1:
        usage_error("Use only one of --json, --json-file, --stdin")
    if args.json:
        return json.loads(args.json)
    if args.json_file:
        return json.loads(Path(args.json_file).read_text())
    if args.stdin:
        return json.loads(sys.stdin.read())
    return None


CHECK_OPS = (
    "eq",
    "ne",
    "lt",
    "le",
    "gt",
    "ge",
    "contains",
    "not-contains",
    "matches",
    "exists",
    "missing",
    "len-eq",
    "len-ge",
)


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)


def evaluate_check(body: Any, spec: Sequence[str]) -> dict[str, Any]:
    """One `--check FIELD OP [VALUE]` assertion against a JSON body."""
    if len(spec) < 2 or spec[1] not in CHECK_OPS:
        usage_error(
            f"--check needs FIELD OP [VALUE] with OP in {', '.join(CHECK_OPS)}",
            example="--check status eq COMPLETED",
        )
    path, op = spec[0], spec[1]
    expected = spec[2] if len(spec) > 2 else ""
    actual = body if path in (".", "") else dig(body, path)
    present = actual is not None or path in (".", "")
    ok: bool
    if op == "exists":
        ok = present
    elif op == "missing":
        ok = not present
    elif op in ("eq", "ne"):
        same = _as_text(actual) == expected or (
            _as_number(actual) is not None
            and _as_number(actual) == _as_number(expected)
        )
        ok = same if op == "eq" else not same
    elif op in ("lt", "le", "gt", "ge"):
        a, b = _as_number(actual), _as_number(expected)
        ok = (
            a is not None
            and b is not None
            and {"lt": a < b, "le": a <= b, "gt": a > b, "ge": a >= b}[op]
        )
    elif op in ("contains", "not-contains"):
        if isinstance(actual, list):
            found = any(_as_text(item) == expected for item in actual) or any(
                expected in _as_text(item) for item in actual
            )
        else:
            found = actual is not None and expected in _as_text(actual)
        ok = found if op == "contains" else not found
    elif op == "matches":
        ok = actual is not None and re.search(expected, _as_text(actual)) is not None
    else:
        size = len(actual) if isinstance(actual, list | dict | str) else None
        target = _as_number(expected)
        ok = (
            size is not None
            and target is not None
            and (size == target if op == "len-eq" else size >= target)
        )
    shown = REDACT.text(_as_text(actual))
    return {
        "check": " ".join(spec),
        "ok": ok,
        "actual": shown if len(shown) <= 200 else shown[:200] + "…",
    }


def parse_headers(items: Sequence[str] | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in items or []:
        if ":" not in item:
            usage_error("--header must look like 'Name: value'")
        name, value = item.split(":", 1)
        headers[name.strip()] = value.strip()
    return headers


KEPT_HEADERS = (
    "content-type",
    "content-disposition",
    "content-length",
    "location",
    "retry-after",
    "allow",
    "access-control-allow-origin",
    "access-control-allow-credentials",
    "access-control-allow-methods",
    "access-control-allow-headers",
)


def finish_exchange(
    run: Run,
    args: argparse.Namespace,
    response: httpx.Response,
    decoded: Any,
    truncated: bool,
    request: dict[str, Any],
    elapsed: int,
    attempts: int,
) -> dict[str, Any]:
    """Shared tail of `api` and `event send`: checks, evidence, --field."""
    exchange = {
        "request": request,
        "response": {
            "status": response.status_code,
            "headers": {
                k: v for k, v in response.headers.items() if k.lower() in KEPT_HEADERS
            },
            "body": decoded,
            "truncated": truncated,
        },
        "elapsed_ms": elapsed,
    }
    ok = status_matches(response.status_code, args.expect)
    checks = [evaluate_check(decoded, spec) for spec in args.check or []]
    lowered = {k.lower(): v for k, v in response.headers.items()}
    for spec in getattr(args, "check_header", None) or []:
        name = spec[0].lower()
        verdict = evaluate_check({name: lowered.get(name)}, [name, *spec[1:]])
        checks.append({**verdict, "check": "header " + verdict["check"]})
    if any(not c["ok"] for c in checks):
        ok = False
    if getattr(args, "all_headers", False):
        exchange["response"]["headers"] = dict(response.headers.items())
    result: dict[str, Any] = {"ok": ok, "status": response.status_code, **exchange}
    if checks:
        result["checks"] = checks
    if getattr(args, "until_ok", 0):
        result["attempts"] = attempts
    if getattr(args, "quiet", False):
        result["request"] = {k: v for k, v in request.items() if k != "body"}
        result["response"] = {
            k: v for k, v in result["response"].items() if k != "body"
        }
    if getattr(args, "raw_out", None):
        Path(args.raw_out).write_bytes(response.content)
        result["raw_out"] = args.raw_out
    if args.save:
        result["saved"] = str(save_evidence(run, args.save, exchange))
    if not ok:
        failed = [c["check"] for c in checks if not c["ok"]]
        if not status_matches(response.status_code, args.expect):
            result["hint"] = (
                f"expected {args.expect or '<400'}, got {response.status_code}"
            )
        elif failed:
            result["hint"] = "failed check: " + "; ".join(failed)
    if getattr(args, "print_header", None) is not None:
        header = response.headers.get(args.print_header)
        if header is None or not ok:
            emit(result)
            raise SystemExit(1)
        print(header if args.unsafe_unredacted else REDACT.text(header))
        raise SystemExit(0)
    if args.field is not None:
        value = decoded if args.field in (".", "") else dig(decoded, args.field)
        if value is None or not ok:
            emit(result)
            raise SystemExit(1)
        value = value if args.unsafe_unredacted else REDACT.obj(value)
        print(value if isinstance(value, str) else json.dumps(value))
        raise SystemExit(0)
    return result


def cmd_api(args: argparse.Namespace) -> dict[str, Any]:
    if (
        args.unsafe_unredacted
        and args.field is None
        and getattr(args, "print_header", None) is None
    ):
        usage_error("--unsafe-unredacted requires --field or --print-header")
    run = resolve_run(args.run)
    target = "agent-server" if args.agent_server else "automation"
    if not args.path.startswith("/"):
        usage_error("PATH must start with /", example=f"{CLI} api GET /v1")
    path = full_path(args.path, target)
    body = read_body(args)
    content: bytes | None = None
    if args.data_file:
        content = Path(args.data_file).read_bytes()
    elif args.data is not None:
        content = args.data.encode()
    if content is not None and body is not None:
        usage_error("Use either a JSON body or --data/--data-file, not both")
    headers = parse_headers(args.header)
    if args.content_type:
        headers["Content-Type"] = args.content_type
    files: dict[str, tuple[str, bytes, str]] = {}
    for item in args.file or []:
        if "=" not in item:
            usage_error(
                "--file must look like FIELD=PATH", example="--file file=./a.txt"
            )
        name, file_path = item.split("=", 1)
        files[name] = (
            Path(file_path).name,
            Path(file_path).read_bytes(),
            "application/octet-stream",
        )
    form = parse_kv(args.form, "--form")
    params = parse_pairs(args.query, "--query")
    started = time.monotonic()
    attempts = 0
    while True:
        attempts += 1
        response = api_call(
            run,
            args.method,
            path,
            target=target,
            body=body,
            params=params,
            auth=args.auth,
            headers=headers,
            files=files or None,
            form=form or None,
            content=content,
            timeout=args.timeout,
        )
        decoded, truncated = decode_body(response, args.max_chars)
        settled = status_matches(response.status_code, args.expect) and all(
            evaluate_check(decoded, spec)["ok"] for spec in args.check or []
        )
        if settled or time.monotonic() - started >= args.until_ok:
            break
        time.sleep(0.5)
    request = {
        "target": target,
        "method": args.method.upper(),
        "path": path,
        "query": params,
        "auth": args.auth,
        "body": body if content is None else f"<{len(content)} bytes>",
        "files": sorted(files),
    }
    elapsed = int((time.monotonic() - started) * 1000)
    return finish_exchange(
        run, args, response, decoded, truncated, request, elapsed, attempts
    )


# --------------------------------------------------------------------------
# llm (profiles live on the Agent Server; preset runs read them from there)
# --------------------------------------------------------------------------

PRESETS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "env": "DEEPSEEK_API_KEY",
        "profiles": [
            {
                "name": "deepseek-flash",
                "model": "deepseek/deepseek-flash",
                "activate": True,
            },
            {
                "name": "deepseek-pro",
                "model": "deepseek/deepseek-v4-pro",
                "activate": False,
            },
        ],
    }
}


def read_api_key(args: argparse.Namespace, default_env: str) -> str:
    if args.api_key_file:
        key = Path(args.api_key_file).read_text().strip()
    else:
        env_name = args.api_key_env or default_env
        key = os.environ.get(env_name, "")
        if not key:
            env_error(
                f"${env_name} is empty.",
                hint="Export the key, or pass --api-key-file PATH. Never put keys in argv.",
            )
    REDACT.add(key, "llm-api-key")
    return key


def save_llm_profile(
    run: Run,
    name: str,
    model: str,
    key: str,
    base_url: str | None,
    activate: bool,
    validate: bool,
) -> dict[str, Any]:
    llm: dict[str, Any] = {"model": model, "api_key": key}
    if base_url:
        llm["base_url"] = base_url
    row: dict[str, Any] = {"profile": name, "model": model}
    if validate:
        checked = api_call(
            run,
            "POST",
            f"/api/profiles/{name}/validate",
            target="agent-server",
            body={"llm": llm},
            timeout=120,
        )
        verdict = decode_body(checked, 500)[0]
        row["validate"] = {"status": checked.status_code, "body": verdict}
        if checked.status_code >= 400 or not dig(verdict, "valid"):
            row["error"] = "validation failed; profile not saved"
            return row
    saved = api_call(
        run,
        "POST",
        f"/api/profiles/{name}",
        target="agent-server",
        body={"llm": llm, "include_secrets": True},
    )
    row["saved"] = saved.status_code
    if saved.status_code >= 400:
        row["error"] = decode_body(saved, 500)[0]
        return row
    if activate:
        activated = api_call(
            run, "POST", f"/api/profiles/{name}/activate", target="agent-server"
        )
        row["activated"] = activated.status_code
    return row


def cmd_llm_preset(args: argparse.Namespace) -> dict[str, Any]:
    run = resolve_run(args.run)
    preset = PRESETS.get(args.preset)
    if not preset:
        usage_error(f"Unknown preset {args.preset!r}; known: {', '.join(PRESETS)}")
    key = read_api_key(args, str(preset["env"]))
    rows = [
        save_llm_profile(
            run,
            p["name"],
            p["model"],
            key,
            None,
            p["activate"],
            not args.no_validate and p["activate"],
        )
        for p in preset["profiles"]
    ]
    ok = all("error" not in r and r.get("activated", 200) < 400 for r in rows)
    return {"ok": ok, "profiles": rows}


def cmd_llm_set(args: argparse.Namespace) -> dict[str, Any]:
    run = resolve_run(args.run)
    key = read_api_key(args, "LLM_API_KEY")
    row = save_llm_profile(
        run,
        args.profile,
        args.model,
        key,
        args.base_url,
        not args.no_activate,
        not args.no_validate,
    )
    return {
        "ok": "error" not in row and row.get("activated", 200) < 400,
        "profile": row,
    }


def cmd_llm_show(args: argparse.Namespace) -> dict[str, Any]:
    run = resolve_run(args.run)
    profiles = api_json(run, "GET", "/api/profiles", target="agent-server")
    settings = api_json(run, "GET", "/api/settings", target="agent-server")
    return {
        "ok": True,
        "active_profile": settings.get("active_profile")
        if isinstance(settings, dict)
        else None,
        "profiles": profiles,
    }


# --------------------------------------------------------------------------
# run wait
# --------------------------------------------------------------------------


def find_run(run: Run, automation_id: str, run_id: str | None) -> dict[str, Any] | None:
    body = api_json(
        run, "GET", f"{BASE_PATH}/v1/{automation_id}/runs", params=[("limit", "100")]
    )
    runs = body.get("runs", []) if isinstance(body, dict) else []
    if run_id:
        return next((r for r in runs if r.get("id") == run_id), None)
    return runs[0] if runs else None


def cmd_run_wait(args: argparse.Namespace) -> dict[str, Any]:
    run = resolve_run(args.run)
    wanted = (
        [s.strip().upper() for s in args.status.split(",")]
        if args.status
        else list(TERMINAL_RUN_STATUSES)
    )
    started = time.monotonic()
    seen: list[str] = []
    found: dict[str, Any] | None = None
    while True:
        found = find_run(run, args.automation_id, args.run_id)
        if found:
            status = str(found.get("status"))
            if status not in seen:
                seen.append(status)
            phase = found.get("current_phase")
            if phase and f"phase:{phase[:120]}" not in seen:
                seen.append(f"phase:{phase[:120]}")
            # A non-terminal match keeps polling until its --check specs hold
            # too (e.g. RUNNING with a given phase); a terminal one cannot change.
            if status in wanted and (
                status in TERMINAL_RUN_STATUSES
                or all(evaluate_check(found, spec)["ok"] for spec in args.check or [])
            ):
                break
            if status in TERMINAL_RUN_STATUSES and not args.keep_waiting:
                break
        if time.monotonic() - started >= args.timeout:
            break
        time.sleep(args.interval)
    elapsed = int((time.monotonic() - started) * 1000)
    result: dict[str, Any] = {
        "ok": bool(found) and str(found.get("status")) in wanted,
        "elapsed_ms": elapsed,
        "wanted": wanted,
        "history": seen,
        "run": found,
    }
    checks = [evaluate_check(found, spec) for spec in args.check or []] if found else []
    if checks:
        result["checks"] = checks
        result["ok"] = result["ok"] and all(c["ok"] for c in checks)
    if not result["ok"]:
        result["hint"] = f"run is {found.get('status') if found else 'missing'}" + (
            f": {found.get('error_detail')}"
            if found and found.get("error_detail")
            else ""
        )
    if args.save:
        result["saved"] = str(save_evidence(run, args.save, result))
    if args.field is not None:
        value = dig(found, args.field) if found else None
        if value is None or not result["ok"]:
            emit(result)
            raise SystemExit(1)
        value = REDACT.obj(value)
        print(value if isinstance(value, str) else json.dumps(value))
        raise SystemExit(0)
    return result


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

# Callback/phase helpers for custom tarballs, using only the stdlib so a run
# needs no setup.sh and no network beyond the service.
QA_LIB = r"""import json, os, sys, urllib.request

def post(url, body):
    key = os.environ.get("AUTOMATION_CALLBACK_API_KEY", "")
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode()

def kv(method, key, body=None, suffix=""):
    base = os.environ["AUTOMATION_API_URL"]
    req = urllib.request.Request(f"{base}/v1/kv/{key}{suffix}", method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + os.environ["AUTOMATION_KV_TOKEN"]})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())

def dump_env(path="qa-env.json"):
    keep = {k: v for k, v in os.environ.items()
            if k.startswith(("AUTOMATION_", "OPENHANDS_OBSERVABILITY")) or k in ("AGENT_SERVER_URL", "WORKSPACE_BASE")}
    for secret in ("AUTOMATION_CALLBACK_API_KEY", "AUTOMATION_KV_TOKEN"):
        if secret in keep:
            keep[secret] = "<present>"
    with open(path, "w") as fh:
        json.dump(keep, fh, indent=2)

if __name__ == "__main__":
    action = sys.argv[1]
    if action == "phase":
        print(post(os.environ["AUTOMATION_PHASE_URL"], {"phase": sys.argv[2]}))
    elif action == "complete":
        body = {"status": sys.argv[2], "run_id": os.environ["AUTOMATION_RUN_ID"]}
        if len(sys.argv) > 3:
            body["error"] = sys.argv[3]
        print(post(os.environ["AUTOMATION_CALLBACK_URL"], body))
    elif action == "env":
        dump_env()
    elif action == "kv":
        count = kv("POST", "qa-runs", None, "/incr")
        kv("PUT", "qa-last-run", {"run_id": os.environ["AUTOMATION_RUN_ID"], "count": count["value"]})
        print(json.dumps(count))
"""

TARBALL_KINDS: dict[str, str] = {
    # exit 0, no callback: the watchdog verifies it through the Agent Server.
    "echo": 'echo "qa echo run ${AUTOMATION_RUN_ID}"\npython3 qa_lib.py env\n',
    # reports a phase, dumps its env, and completes through the callback.
    "callback": (
        'python3 qa_lib.py phase "qa: running"\npython3 qa_lib.py env\n'
        'echo "qa callback run ${AUTOMATION_RUN_ID}"\npython3 qa_lib.py complete COMPLETED\n'
    ),
    # reports FAILED through the callback with a plain error string.
    "fail-callback": 'python3 qa_lib.py complete FAILED "qa: deliberate failure"\n',
    # exits non-zero with no callback.
    "fail": 'echo "qa failing on purpose" >&2\nexit 3\n',
    # never finishes: for timeouts, cancel and the watchdog.
    "sleep": 'python3 qa_lib.py phase "qa: sleeping"\nsleep 99999\n',
    # uses AUTOMATION_KV_TOKEN to count runs in the KV store, then completes.
    "kv": "python3 qa_lib.py kv\npython3 qa_lib.py complete COMPLETED\n",
}


def build_tarball(kind: str, setup: str | None = None) -> bytes:
    if kind not in TARBALL_KINDS:
        usage_error(f"Unknown tarball kind {kind!r}; known: {', '.join(TARBALL_KINDS)}")
    members = {
        "run.sh": "#!/usr/bin/env bash\nset -euo pipefail\n" + TARBALL_KINDS[kind],
        "qa_lib.py": QA_LIB,
    }
    if setup is not None:
        members["setup.sh"] = setup
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, text in members.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def upload_tarball(run: Run, data: bytes, name: str) -> dict[str, Any]:
    response = api_call(
        run,
        "POST",
        f"{BASE_PATH}/v1/uploads",
        params=[("name", name)],
        content=data,
        headers={"Content-Type": "application/gzip"},
    )
    body = decode_body(response, 2000)[0]
    if response.status_code >= 400:
        raise CliError(f"upload returned {response.status_code}", data={"body": body})
    return body


def fixture_tarball(run: Run, args: argparse.Namespace) -> dict[str, Any]:
    data = build_tarball(args.kind, args.setup)
    path = run.fixtures / f"{args.name or args.kind}.tar.gz"
    path.write_bytes(data)
    result: dict[str, Any] = {
        "ok": True,
        "path": str(path),
        "bytes": len(data),
        "entrypoint": "bash run.sh",
    }
    if args.upload:
        upload = upload_tarball(run, data, args.name or f"qa-{args.kind}")
        result["upload"] = upload
        result["tarball_path"] = upload.get("tarball_path")
    return result


def fixture_automation(run: Run, args: argparse.Namespace) -> dict[str, Any]:
    upload = upload_tarball(
        run, build_tarball(args.kind, args.setup), f"qa-{args.kind}"
    )
    if args.event_source:
        trigger: dict[str, Any] = {
            "type": "event",
            "source": args.event_source,
            "on": args.on or "*",
        }
        if args.filter:
            trigger["filter"] = args.filter
    else:
        trigger = {"type": "cron", "schedule": args.cron, "timezone": args.timezone}
    body: dict[str, Any] = {
        "name": args.name or f"qa {args.kind}",
        "trigger": trigger,
        "tarball_path": upload["tarball_path"],
        "entrypoint": "bash run.sh",
    }
    if args.timeout is not None:
        body["timeout"] = args.timeout
    if args.extra_json:
        body.update(json.loads(args.extra_json))
    created = api_json(run, "POST", f"{BASE_PATH}/v1", body=body)
    return {"ok": True, "id": created.get("id"), "automation": created}


def fixture_git_remote(run: Run, name: str) -> dict[str, Any]:
    bare = run.fixtures / f"{name}.git"
    work = run.fixtures / f"{name}-work"
    env = {
        **os.environ,
        "HOME": str(run.home),
        "GIT_CONFIG_GLOBAL": str(run.home / ".gitconfig"),
    }
    if not bare.exists():
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(bare)],
            check=True,
            capture_output=True,
            env=env,
        )
    if not work.exists():
        subprocess.run(
            ["git", "clone", str(bare), str(work)],
            check=True,
            capture_output=True,
            env=env,
        )
    return {
        "ok": True,
        "bare": str(bare),
        "url": f"file://{bare}",
        "work": str(work),
        "hint": f"git -C {work} pull, edit, commit, push to change the remote; HOME={run.home} keeps your identity out",
    }


def cmd_fixture(args: argparse.Namespace) -> dict[str, Any]:
    run = resolve_run(
        args.run,
        require_alive=args.kind_cmd in ("automation", "tarball")
        and getattr(args, "upload", True),
    )
    if args.kind_cmd == "tarball":
        result = fixture_tarball(run, args)
    elif args.kind_cmd == "automation":
        result = fixture_automation(run, args)
    elif args.kind_cmd == "git-remote":
        result = fixture_git_remote(run, args.name)
    else:
        serve = None
        if args.serve_tarball:
            serve = run.fixtures / "served"
            serve.mkdir(exist_ok=True)
            (serve / f"{args.serve_tarball}.tar.gz").write_bytes(
                build_tarball(args.serve_tarball)
            )
        result = {
            "ok": True,
            **start_http_sink(run, args.name, args.status, args.body, serve),
        }
        if serve:
            result["tarball_url"] = f"{result['url']}/{args.serve_tarball}.tar.gz"
    if args.field:
        value = dig(result, args.field)
        if value is None:
            emit(result)
            raise SystemExit(1)
        value = REDACT.obj(value)
        print(value if isinstance(value, str) else json.dumps(value))
        raise SystemExit(0)
    return result


# --------------------------------------------------------------------------
# event (signed webhook deliveries)
# --------------------------------------------------------------------------

SCHEMES = ("hmac_sha256_hex", "standard_webhooks", "slack_v0")


def sign_delivery(
    scheme: str, secret: str, body: bytes, header: str, msg_id: str, timestamp: int
) -> dict[str, str]:
    if scheme == "hmac_sha256_hex":
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return {header: "sha256=" + digest}
    if scheme == "slack_v0":
        base = b"v0:" + str(timestamp).encode() + b":" + body
        digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
        return {header: "v0=" + digest, "X-Slack-Request-Timestamp": str(timestamp)}
    try:
        key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    except ValueError:
        key = secret.encode()
    message = f"{msg_id}.{timestamp}.".encode() + body
    signature = base64.b64encode(
        hmac.new(key, message, hashlib.sha256).digest()
    ).decode()
    return {
        header: "v1," + signature,
        "webhook-id": msg_id,
        "webhook-timestamp": str(timestamp),
    }


def run_webhook_secret(run: Run) -> str | None:
    return run.data["automation"].get("extra_env", {}).get("AUTOMATION_WEBHOOK_SECRET")


def cmd_event_send(args: argparse.Namespace) -> dict[str, Any]:
    if (
        args.unsafe_unredacted
        and args.field is None
        and getattr(args, "print_header", None) is None
    ):
        usage_error("--unsafe-unredacted requires --field or --print-header")
    run = resolve_run(args.run)
    payload = read_body(args)
    if payload is None and args.data is None:
        usage_error(
            "event send needs --json/--json-file/--stdin or --data",
            example=f'{CLI} event send qa-src --secret "$SECRET" --json \'{{"type":"order.created"}}\'',
        )
    if args.builtin:
        secret = args.secret or run_webhook_secret(run)
        if not secret:
            usage_error(
                "--builtin needs the run launched with --env AUTOMATION_WEBHOOK_SECRET=... (or --secret)"
            )
        payload = {"payload": payload}
        header = args.signature_header or "X-Hub-Signature-256"
        scheme = "hmac_sha256_hex"
    else:
        secret = args.secret
        if args.secret_file:
            secret = Path(args.secret_file).read_text().strip()
        header = args.signature_header or (
            "X-Slack-Signature" if args.scheme == "slack_v0" else "X-Signature-256"
        )
        scheme = args.scheme
    REDACT.add(secret, "webhook-secret")
    raw = args.data.encode() if args.data is not None else json.dumps(payload).encode()
    msg_id = args.event_id or f"msg_{secrets.token_hex(8)}"
    timestamp = int(time.time()) - (args.age or 0)
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if secret and not args.unsigned:
        headers.update(sign_delivery(scheme, secret, raw, header, msg_id, timestamp))
        if args.tamper:
            headers[header] = headers[header][:-4] + (
                "0000" if not headers[header].endswith("0000") else "1111"
            )
        if args.bare_hex and scheme == "hmac_sha256_hex":
            headers[header] = headers[header].removeprefix("sha256=")
    if args.delivery_id:
        name, _, value = args.delivery_id.partition("=")
        headers[name] = value
    headers.update(parse_headers(args.header))
    for name in args.drop_header or []:
        headers = {k: v for k, v in headers.items() if k.lower() != name.lower()}
    org = args.org or LOCAL_ORG_ID
    path = f"{BASE_PATH}/v1/events/{org}/{args.source}"
    started = time.monotonic()
    response = api_call(
        run,
        "POST",
        path,
        auth="none",
        headers=headers,
        content=raw,
        timeout=args.timeout,
    )
    decoded, truncated = decode_body(response, 4000)
    request = {
        "method": "POST",
        "path": path,
        "scheme": scheme if secret and not args.unsigned else None,
        "signature_header": header,
        "headers_sent": sorted(headers),
        "body": payload if args.data is None else args.data[:2000],
    }
    elapsed = int((time.monotonic() - started) * 1000)
    return finish_exchange(run, args, response, decoded, truncated, request, elapsed, 1)


def cmd_event_requested_types(args: argparse.Namespace) -> dict[str, Any]:
    if (
        args.unsafe_unredacted
        and args.field is None
        and getattr(args, "print_header", None) is None
    ):
        usage_error("--unsafe-unredacted requires --field or --print-header")
    run = resolve_run(args.run)
    secret = args.secret or run_webhook_secret(run)
    REDACT.add(secret, "webhook-secret")
    headers: dict[str, str] = {}
    if secret and not args.unsigned:
        digest = hmac.new(
            secret.encode(), args.source.encode(), hashlib.sha256
        ).hexdigest()
        headers["X-Hub-Signature-256"] = "sha256=" + (
            ("0" * 64) if args.tamper else digest
        )
    path = f"{BASE_PATH}/v1/events/{args.source}/requested-types"
    started = time.monotonic()
    response = api_call(run, "GET", path, auth="none", headers=headers)
    decoded, truncated = decode_body(response, 4000)
    request = {"method": "GET", "path": path, "signed": bool(headers)}
    elapsed = int((time.monotonic() - started) * 1000)
    return finish_exchange(run, args, response, decoded, truncated, request, elapsed, 1)


# --------------------------------------------------------------------------
# kv token
# --------------------------------------------------------------------------


def cmd_kv_token(args: argparse.Namespace) -> dict[str, Any]:
    run = resolve_run(args.run, require_alive=False)
    try:
        import jwt  # pyjwt ships with the service
    except ImportError:
        env_error("pyjwt is not importable; run with the checkout's .venv")
    secret = (run.private / "kv_secret").read_text().strip()
    if args.wrong_secret:
        secret = "qa-not-the-kv-secret-" + "x" * 16
    now = int(time.time())
    claims: dict[str, Any] = {"run_id": args.run_id or str(uuid.uuid4()), "iat": now}
    if args.automation_id:
        claims["automation_id"] = args.automation_id
    if args.expired:
        claims["exp"] = now - 60
    elif not args.no_exp:
        claims["exp"] = now + 3600
    print(jwt.encode(claims, secret, algorithm="HS256"))
    raise SystemExit(0)


# --------------------------------------------------------------------------
# state (read-only views of the run's database and files)
# --------------------------------------------------------------------------


def cmd_state_sql(args: argparse.Namespace) -> dict[str, Any]:
    run = resolve_run(args.run, require_alive=False)
    if not run.db_path.exists():
        env_error(f"No database at {run.db_path}")
    connection = sqlite3.connect(f"file:{run.db_path}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in connection.execute(args.query).fetchmany(args.limit)]
    except sqlite3.Error as exc:
        usage_error(f"SQL error: {exc}")
    finally:
        connection.close()
    for row in rows:
        for key, value in row.items():
            if isinstance(value, bytes):
                row[key] = f"<{len(value)} bytes>"
    body = {"rows": rows, "count": len(rows)}
    checks = [evaluate_check(body, spec) for spec in args.check or []]
    result: dict[str, Any] = {"ok": all(c["ok"] for c in checks), **body}
    if checks:
        result["checks"] = checks
    if args.save:
        saved = {"query": args.query, **result}
        result["saved"] = str(save_evidence(run, args.save, saved))
    if args.raw:
        # Show stored values as they are (e.g. to prove Fernet encryption).
        result["rows"] = [
            {k: (v[:80] if isinstance(v, str) else v) for k, v in r.items()}
            for r in rows
        ]
        sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")
        raise SystemExit(0 if result["ok"] else 1)
    if args.field is not None:
        value = dig(body, args.field)
        if value is None or not result["ok"]:
            emit(result)
            raise SystemExit(1)
        value = REDACT.obj(value)
        print(value if isinstance(value, str) else json.dumps(value))
        raise SystemExit(0)
    return result


def run_relative(run: Run, path: str) -> Path:
    target = (run.dir / path).resolve()
    if not target.is_relative_to(run.dir.resolve()):
        usage_error(f"{path} is outside the run directory")
    return target


def cmd_state_ls(args: argparse.Namespace) -> dict[str, Any]:
    run = resolve_run(args.run, require_alive=False)
    root = run_relative(run, args.path)
    if not root.exists():
        return {
            # A required entry cannot exist in a missing directory.
            "ok": not args.expect_exists and not args.expect,
            "path": args.path,
            "exists": False,
            "entries": [],
        }
    entries = sorted(
        str(p.relative_to(root)) + ("/" if p.is_dir() else "")
        for p in (root.rglob("*") if args.recursive else root.iterdir())
        if ".venv" not in p.parts
    )
    ok = True
    for want in args.expect or []:
        ok = ok and any(e.rstrip("/") == want for e in entries)
    if args.expect_empty:
        ok = ok and not entries
    return {
        "ok": ok,
        "path": args.path,
        "exists": True,
        "entries": entries[: args.limit],
        "count": len(entries),
    }


def cmd_state_cat(args: argparse.Namespace) -> dict[str, Any]:
    run = resolve_run(args.run, require_alive=False)
    target = run_relative(run, args.path)
    if not target.is_file():
        return {"ok": False, "path": args.path, "hint": "no such file"}
    text = target.read_text(errors="replace")
    body: Any = text
    if target.suffix == ".json":
        with contextlib.suppress(ValueError):
            body = json.loads(text)
    checks = [evaluate_check(body, spec) for spec in args.check or []]
    missing = [t for t in args.contains or [] if t not in text]
    present = [t for t in args.not_contains or [] if t in text]
    ok = not missing and not present and all(c["ok"] for c in checks)
    result: dict[str, Any] = {"ok": ok, "path": args.path, "bytes": len(text)}
    if checks:
        result["checks"] = checks
    if missing or present:
        result["hint"] = {"missing": missing, "unexpected": present}
    result["content"] = (
        body if len(text) <= args.max_chars else text[: args.max_chars] + "…"
    )
    if args.field is not None:
        value = dig(body, args.field)
        if value is None or not ok:
            emit(result)
            raise SystemExit(1)
        value = REDACT.obj(value)
        print(value if isinstance(value, str) else json.dumps(value))
        raise SystemExit(0)
    return result


# --------------------------------------------------------------------------
# conversation (what a run's agent did, read from the Agent Server)
# --------------------------------------------------------------------------


def event_kind(event: dict[str, Any]) -> str:
    return str(event.get("kind") or event.get("type") or "?")


def cmd_conversation_events(args: argparse.Namespace) -> dict[str, Any]:
    run = resolve_run(args.run)
    events: list[dict[str, Any]] = []
    page_id: str | None = None
    while len(events) < args.max_events:
        params = [("limit", "100")]
        if page_id:
            params.append(("page_id", page_id))
        page = api_json(
            run,
            "GET",
            f"/api/conversations/{args.conversation_id}/events/search",
            target="agent-server",
            params=params,
        )
        events += page.get("items", [])
        page_id = page.get("next_page_id")
        if not page_id:
            break
    kinds: dict[str, int] = {}
    for event in events:
        kinds[event_kind(event)] = kinds.get(event_kind(event), 0) + 1
    tools = [
        dig(e, "tool_name") or dig(e, "action.kind")
        for e in events
        if event_kind(e) == "ActionEvent"
    ]
    text = json.dumps(events, default=str)
    ok = True
    for kind in args.expect_kind or []:
        ok = ok and kinds.get(kind, 0) > 0
    for tool in args.expect_tool or []:
        ok = ok and tool in tools
    missing = [t for t in args.contains or [] if t not in text]
    ok = ok and not missing
    result: dict[str, Any] = {
        "ok": ok,
        "conversation_id": args.conversation_id,
        "events": len(events),
        "kinds": kinds,
        "tools": tools,
        "last": [_brief(e) for e in events[-args.show :]] if args.show else [],
    }
    if missing:
        result["hint"] = {"missing": missing}
    if args.save:
        result["saved"] = str(save_evidence(run, args.save, {"events": events}))
    return result


# --------------------------------------------------------------------------
# helper / exec
# --------------------------------------------------------------------------


def _helper_entry(run: Run, name: str) -> dict[str, Any] | None:
    for proc in run.data.get("processes", []):
        if proc.get("name") == name and proc.get("kind") == "helper":
            return proc
    return None


def cmd_assert(args: argparse.Namespace) -> dict[str, Any]:
    """`--check` a value a recipe computed, so a `# bug:` predicate can name it."""
    if not args.check:
        usage_error(
            "assert needs at least one --check",
            example=f'{CLI} assert --value "$N" --check . eq 2',
        )
    if args.json is not None:
        path = Path(args.json)
        if not path.is_file():
            usage_error(f"{args.json} does not exist")
        raw = path.read_text(errors="replace")
    elif args.value is not None:
        raw = args.value
    else:
        raw = sys.stdin.read()
    try:
        value: Any = json.loads(raw)
    except ValueError:
        if args.json is not None:
            usage_error(f"{args.json} is not JSON")
        value = raw.rstrip("\n")
    checks = [evaluate_check(value, spec) for spec in args.check]
    result: dict[str, Any] = {
        "ok": all(c["ok"] for c in checks),
        "value": value,
        "checks": checks,
    }
    if not result["ok"]:
        result["hint"] = "failed check: " + "; ".join(
            c["check"] for c in checks if not c["ok"]
        )
    return result


def cmd_helper_start(args: argparse.Namespace) -> dict[str, Any]:
    run = resolve_run(args.run, require_alive=False)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        usage_error(
            "helper start needs a command after --",
            example=f"{CLI} helper start --name qa-http -- python3 -m http.server 8123",
        )
    existing = _helper_entry(run, args.name)
    if existing and not existing.get("stopped_at"):
        usage_error(f"helper {args.name} is already running (pid {existing['pid']})")
    log_path = run.fixtures / "helpers" / f"{args.name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            # A helper outlives the bullet: keep its output out of the
            # observation that `# bug:` predicates read.
            env={k: v for k, v in os.environ.items() if k != OBSERVATION_ENV},
        )
    entry = {
        "name": args.name,
        "kind": "helper",
        "pid": process.pid,
        "pgid": os.getpgid(process.pid),
        "command": REDACT.text(shlex.join(command))[:500],
        "log": str(log_path),
        "started_at": now_iso(),
    }
    processes = run.data.setdefault("processes", [])
    if existing:
        processes.remove(existing)
    processes.append(entry)
    run.save()
    time.sleep(args.settle)
    code = process.poll()
    if code is not None:
        entry["stopped_at"] = now_iso()
        run.save()
        raise CliError(
            f"helper {args.name} exited with {code} within {args.settle}s",
            hint=f"read its log: {log_path}",
        )
    return {"ok": True, "helper": args.name, "pid": process.pid, "log": str(log_path)}


def cmd_helper_stop(args: argparse.Namespace) -> dict[str, Any]:
    run = resolve_run(args.run, require_alive=False)
    stopped = []
    for name in args.names:
        entry = _helper_entry(run, name)
        if entry is None:
            usage_error(f"No helper named {name}")
        if entry.get("stopped_at"):
            outcome = "already-stopped"
        else:
            outcome = stop_process_group(int(entry["pgid"]), grace=5.0)
            entry["stopped_at"] = now_iso()
        stopped.append({"name": name, "outcome": outcome})
    run.save()
    return {"ok": True, "stopped": stopped}


def cmd_exec(args: argparse.Namespace) -> dict[str, Any]:
    """Run a client program (SDK script, curl, a frontend test) against the run."""
    run = resolve_run(args.run)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        usage_error(
            "exec needs a command after --",
            example=f"{CLI} exec -- curl -s $AUTOMATION_URL/health",
        )
    key = run.session_key or ""
    env = {
        **os.environ,
        **parse_kv(args.env, "--env"),
        "AUTOMATION_URL": run.url,
        "AUTOMATION_API_URL": run.url + BASE_PATH,
        "AUTOMATION_API_KEY": key,
        "OPENHANDS_AUTOMATION_API_KEY": key,
        "AGENT_SERVER_URL": run.agent_server_url,
        "SESSION_API_KEY": run.agent_server_key() or "",
        RUN_ENV: str(run.dir),
    }
    started = time.monotonic()

    # Its own process group, so a timeout also ends what it spawned. That group
    # is outside the caller's, so a SIGTERM from the caller (a `map run` recipe
    # timeout) must reach it through us: kill it at once, well inside the
    # caller's grace period, then exit.
    def interrupt(_signum: int, _frame: Any) -> NoReturn:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        proc = subprocess.Popen(
            command,
            cwd=args.cwd or run.data.get("checkout", REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=args.timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            stop_process_group(proc.pid, grace=2.0)
            stdout, stderr = proc.communicate()
            code = 124
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            raise
    finally:
        signal.signal(signal.SIGTERM, previous)
    elapsed = int((time.monotonic() - started) * 1000)
    result: dict[str, Any] = {
        "ok": code == 0,
        "exit_code": code,
        "elapsed_ms": elapsed,
        "stdout_tail": stdout[-args.tail_chars :],
        "stderr_tail": stderr[-args.tail_chars :],
    }
    missing = [t for t in args.expect_output or [] if t not in stdout + stderr]
    present = [t for t in args.reject_output or [] if t in stdout + stderr]
    if missing or present:
        result["ok"] = False
        result["hint"] = {"missing": missing, "unexpected": present}
    if args.save:
        transcript = {
            "command": command,
            "exit_code": code,
            "elapsed_ms": elapsed,
            "stdout": stdout,
            "stderr": stderr,
        }
        result["saved"] = str(save_evidence(run, args.save, transcript))
    return result


# --------------------------------------------------------------------------
# evidence
# --------------------------------------------------------------------------


def ledger_path(run: Run) -> Path:
    return run.evidence / "ledger.jsonl"


def read_ledger(run: Run) -> list[dict[str, Any]]:
    path = ledger_path(run)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def cmd_evidence_add(args: argparse.Namespace) -> dict[str, Any]:
    run = resolve_run(args.run, require_alive=False)
    known = {row["id"] for row in collect_ids()}
    if args.feature not in known and not args.allow_unknown:
        usage_error(
            f"{args.feature} is not a sub-feature ID in the map.",
            example=f"{CLI} map ids | grep {args.feature.split('.')[0]}",
        )
    artifacts = []
    for artifact in args.artifact or []:
        path = Path(artifact)
        if not path.is_absolute():
            path = run.dir / artifact
        if not path.exists():
            usage_error(f"artifact {artifact} does not exist")
        artifacts.append(
            str(path.relative_to(run.dir))
            if path.is_relative_to(run.dir)
            else str(path)
        )
    row = append_ledger(
        run,
        args.feature,
        args.result,
        args.entry,
        args.expected,
        args.actual,
        artifacts,
        args.note,
    )
    return {"ok": True, "ledger": str(ledger_path(run)), "row": row}


def latest_rows(
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        latest[(row["run"], row["feature"])] = row
    return latest


def cmd_evidence_report(args: argparse.Namespace) -> dict[str, Any]:
    runs = (
        [resolve_run(r, require_alive=False) for r in args.runs]
        if args.runs
        else [resolve_run(args.run, require_alive=False)]
    )
    rows = [row | {"run_dir": str(r.dir)} for r in runs for row in read_ledger(r)]
    latest = latest_rows(rows)
    order = {"fail": 0, "xpass": 1, "blocked": 2, "not-run": 3, "xfail": 4, "pass": 5}
    ordered = sorted(
        latest.values(),
        key=lambda r: (order.get(r["result"], 9), r["feature"], r["run"]),
    )
    counts: dict[str, int] = {}
    for row in ordered:
        counts[row["result"]] = counts.get(row["result"], 0) + 1
    all_ids = [row["id"] for row in collect_ids()]
    features = {r["feature"] for r in ordered}
    missing = [i for i in all_ids if i not in features]
    lines = [
        "| Feature | Run | Checkout | Result | Entry point | Expected | Actual | Evidence |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in ordered:
        cells = [
            row["feature"],
            row["run"],
            row.get("checkout_sha") or "unknown",
            row["result"],
            row.get("entry") or "",
            row.get("expected") or "",
            row.get("actual") or "",
            ", ".join(
                str(Path(row["run_dir"]) / a) for a in row.get("artifacts") or []
            ),
        ]
        lines.append(
            "| "
            + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells)
            + " |"
        )
    markdown = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(REDACT.text(markdown) + "\n")
    return {
        "ok": True,
        "counts": counts,
        "features_with_evidence": len(features),
        "attempts_with_evidence": len(ordered),
        "map_ids_without_evidence": len(missing),
        "missing_sample": missing[:50],
        "markdown": markdown if args.print_markdown else None,
        "out": args.out,
    }


# --------------------------------------------------------------------------
# map
# --------------------------------------------------------------------------


def family_files(only: str | None = None) -> list[Path]:
    files = sorted(
        p for p in FEATURE_MAP_DIR.glob("F*.md") if FAMILY_FILE_RE.match(p.name)
    )
    if only:
        files = [
            p for p in files if p.name == Path(only).name or p.stem.startswith(only)
        ]
        if not files:
            usage_error(f"No family file matches {only}")
    return files


def collect_ids(files: Sequence[Path] | None = None) -> list[dict[str, Any]]:
    rows = []
    for path in files or family_files():
        section = None
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if line.startswith("## "):
                section = line[3:].strip()
                continue
            if section == "Sub-features":
                match = SUB_FEATURE_RE.match(line)
                if match:
                    rows.append(
                        {"id": match.group(1), "file": path.name, "line": number}
                    )
    return rows


def route_templates(checkout: Path | None = None) -> list[dict[str, Any]]:
    """Every HTTP route of the service, by importing the app in a subprocess."""
    checkout = checkout or REPO_ROOT
    code = (
        "import json, logging\n"
        "logging.disable(logging.CRITICAL)\n"
        "from fastapi.routing import APIRoute, APIWebSocketRoute\n"
        "from starlette.routing import Route\n"
        "from openhands.automation.app import app\n"
        "out = []\n"
        "for r in app.routes:\n"
        "    if isinstance(r, APIRoute):\n"
        "        for m in sorted(r.methods - {'HEAD', 'OPTIONS'}):\n"
        "            out.append({'method': m, 'path': r.path, 'name': r.name,\n"
        "                        'module': r.endpoint.__module__.rsplit('.', 1)[-1],\n"
        "                        'deprecated': bool(r.deprecated)})\n"
        "    elif isinstance(r, APIWebSocketRoute):\n"
        "        out.append({'method': 'WS', 'path': r.path, 'name': r.name,\n"
        "                    'module': r.endpoint.__module__.rsplit('.', 1)[-1],\n"
        "                    'deprecated': False})\n"
        "    elif isinstance(r, Route) and r.methods:\n"
        "        for m in sorted(r.methods - {'HEAD', 'OPTIONS'}):\n"
        "            out.append({'method': m, 'path': r.path, 'name': r.name,\n"
        "                        'module': 'fastapi', 'deprecated': False})\n"
        "print('ROUTES-JSON' + json.dumps(out))\n"
    )
    home = runs_home_ensure() / ".map-home"
    home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "OPENHANDS_SUPPRESS_BANNER": "1",
        "AUTOMATION_AGENT_SERVER_URL": "http://127.0.0.1:9",
        "AUTOMATION_LOCAL_API_KEY": "map-routes",
        "AUTOMATION_DB_URL": f"sqlite+aiosqlite:///{home}/routes.db",
    }
    completed = subprocess.run(
        [*checkout_python(checkout), "-c", code],
        cwd=home,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    for line in completed.stdout.splitlines():
        if line.startswith("ROUTES-JSON"):
            routes = json.loads(line[len("ROUTES-JSON") :])
            seen: set[tuple[str, str]] = set()
            unique = []
            for route in routes:
                key = (route["method"], route["path"])
                if key not in seen:
                    seen.add(key)
                    unique.append(route)
            return unique
    env_error(
        "Could not import the automation app to list routes.",
        hint=completed.stderr[-800:],
    )


def runs_home_ensure() -> Path:
    home = runs_home()
    home.mkdir(parents=True, exist_ok=True)
    return home


ROUTE_LINE_RE = re.compile(r"`(GET|POST|PUT|PATCH|DELETE|WS) (/[^`\s]*)`")
# Service calls only: `api --agent-server METHOD PATH` (flag first) targets the
# Agent Server and is not matched against the service's routes.
API_CMD_RE = re.compile(
    rf"{CLI} api (GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD) '?\"?(/[^\s'\"`]*)"
)
EVENT_CMD_RE = re.compile(rf"{CLI} event (send|requested-types) ")
CMD_START_RE = re.compile(rf"(?:^|[`\s($|&;]){CLI} ")


def extract_commands(text: str) -> list[str]:
    """Every `control-automation ...` invocation in markdown text.

    Finds commands in inline code, fenced blocks, `$(...)` substitutions and
    `&&` chains, stopping at the end of the shell word sequence.
    """
    commands = []
    for match in CMD_START_RE.finditer(text):
        start = match.end() - len(CLI) - 1
        quote = ""
        depth = 0
        i = start
        while i < len(text):
            ch = text[i]
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in "'\"":
                quote = ch
            elif ch == "\\" and text[i + 1 : i + 2] == "\n":
                i += 2
                continue
            elif ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            elif ch in "`\n;|" or text.startswith("&&", i):
                break
            elif ch == "&" and text[i - 1] not in "<>&" and text[i + 1 : i + 2] != ">":
                break
            i += 1
        commands.append(text[start:i].replace("\\\n", " ").strip())
    return commands


def template_regex(template: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[^}]+\})", template)
    pattern = ""
    for part in parts:
        if part.startswith("{") and part.endswith("}"):
            pattern += ".+" if part.endswith(":path}") else "[^/]+"
        else:
            pattern += re.escape(part)
    return re.compile(f"^{pattern}/?$")


SHELL_EXPANSION_RE = re.compile(r"\$\{[^}]*\}")


def match_route(
    method: str, concrete: str, routes: Sequence[dict[str, Any]]
) -> dict[str, Any] | None:
    concrete = full_path(
        SHELL_EXPANSION_RE.sub("X", concrete.split("?", 1)[0]), "automation"
    )
    # Config-only catch-all proxies (the Docker runtime's {tail:path}) would
    # match any typo, so recipe paths never resolve to them.
    candidates = [
        r
        for r in routes
        if r["method"] == method
        and not (r.get("config") and r["path"].endswith(":path}"))
        and template_regex(r["path"]).match(concrete)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda r: r["path"].count("{"))


def owned_routes(path: Path) -> list[tuple[str, str, int]]:
    owned = []
    in_routes = False
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if line.startswith("Routes:"):
            in_routes = True
        elif in_routes and (line.startswith("## ") or not line.strip()):
            in_routes = False
        if in_routes:
            for method, route in ROUTE_LINE_RE.findall(line):
                owned.append((method, route, number))
    return owned


def driven_routes(path: Path, routes: Sequence[dict[str, Any]]) -> set[tuple[str, str]]:
    text = path.read_text()
    driven: set[tuple[str, str]] = set()
    for method, concrete in API_CMD_RE.findall(text):
        match = match_route(method, concrete, routes)
        if match:
            driven.add((match["method"], match["path"]))
    for verb in EVENT_CMD_RE.findall(text):
        if verb == "send":
            driven.add(("POST", f"{BASE_PATH}/v1/events/{{org_id}}/{{source}}"))
        else:
            driven.add(("GET", f"{BASE_PATH}/v1/events/{{source}}/requested-types"))
    return driven


REDIRECT_RE = re.compile(r"^\d?>>?(&\d)?$|^\d?<$")
REDIRECT_WITH_TARGET_RE = re.compile(r"^\d?>>?\S+$")


def drop_redirections(argv: list[str]) -> list[str]:
    """Remove shell redirections (`> file`, `2>&1`, `>/dev/null`) from argv."""
    kept: list[str] = []
    skip = False
    for token in argv:
        if skip:
            skip = False
        elif REDIRECT_RE.match(token):
            skip = not token.endswith(("&1", "&2"))
        elif not REDIRECT_WITH_TARGET_RE.match(token):
            kept.append(token)
    return kept


def validate_command(command: str) -> str | None:
    """Parse one recipe command with the real parser, without executing it."""
    text = command.strip()
    if text.endswith("`"):
        text = text[:-1]
    try:
        argv = shlex.split(re.sub(r"<([A-Za-z_][\w-]*)>", r"\1", text), comments=True)
    except ValueError as exc:
        return f"unparseable: {exc}"
    argv = drop_redirections(argv[1:])
    parser = recipe_parser()
    stderr = io.StringIO()
    try:
        with (
            contextlib.redirect_stderr(stderr),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code not in (0, None):
            message = (
                stderr.getvalue().strip().splitlines()[-1]
                if stderr.getvalue()
                else "invalid"
            )
            # A value that comes from the shell ('$N', '$(...)') is only known
            # at run time, so a type or choice error about it is not a defect.
            if "invalid" in message and "'$" in message:
                return None
            return message
    return None


def check_family(path: Path, routes: Sequence[dict[str, Any]] | None) -> list[str]:
    problems: list[str] = []
    text = path.read_text()
    lines = text.splitlines()
    family = "F" + FAMILY_FILE_RE.match(path.name).group(1)  # type: ignore[union-attr]
    if not lines or not lines[0].startswith("# "):
        problems.append("first line must be an H1 title")
    if not any(line.startswith("Source:") for line in lines):
        problems.append("missing `Source:` line")
    if not any(line.startswith("Routes:") for line in lines):
        problems.append(
            "missing `Routes:` line (use `Routes: none` for behavior-only families)"
        )
    h2 = [line[3:].strip() for line in lines if line.startswith("## ")]
    expected = [
        "Sub-features",
        "How to get to it (agent POV)",
        f"Driving it with {CLI}",
        "Gotchas",
    ]
    if h2 != expected:
        problems.append(f"H2 sections must be exactly {expected}, got {h2}")
    ids = collect_ids([path])
    if not ids:
        problems.append("no sub-feature IDs under ## Sub-features")
    for row in ids:
        if not row["id"].startswith(family + "."):
            problems.append(
                f"line {row['line']}: {row['id']} does not start with {family}."
            )
    driving = text.split(f"## Driving it with {CLI}", 1)[-1].split("## Gotchas", 1)[0]
    if "Preconditions:" not in driving:
        problems.append("Driving section must start with `Preconditions:`")
    for row in ids:
        if f"`{row['id']}`" not in driving and f"({row['id']})" not in driving:
            problems.append(f"{row['id']} is never referenced in the Driving section")
    unknown_needs = [n for n in family_needs(path) if n not in NEEDS]
    if unknown_needs:
        problems.append(
            f"Needs: unknown tokens {unknown_needs}; use {', '.join(NEEDS)}"
        )
    flags = launch_flags(path)
    if flags:
        error = validate_command(
            " ".join([CLI, "launch", "--new", *map(shlex.quote, flags)])
        )
        if error:
            problems.append(f"Launch: flags do not parse: {error}")
    in_driving = False
    in_fence = False
    for number, line in enumerate(lines, 1):
        if line.startswith("## "):
            in_driving = line == f"## Driving it with {CLI}"
        if not in_driving:
            continue
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence and line.strip().startswith("! "):
            problems.append(
                f"line {number}: `! cmd` never fails under set -e; use "
                "`if cmd; then false; fi` or a --check/--expect assertion"
            )
        if not in_fence and line.startswith("- **") and line.count("**") < 2:
            problems.append(
                f"line {number}: bold bullet label is not closed on its own line; "
                "map run would merge this bullet into the previous one"
            )
    bullets = driving_bullets(path)
    try:
        select_bullets(bullets, None)
    except CliError as exc:
        problems.append(str(exc))
    problems.extend(undeclared_dependencies(bullets))
    for bullet in bullets:
        unknown = [n for n in bullet.needs if n not in NEEDS]
        if unknown:
            problems.append(
                f"{bullet.label}: Needs: unknown tokens {unknown}; use {', '.join(NEEDS)}"
            )
        if bullet.label == "Preconditions" and bullet.needs:
            problems.append(
                "Preconditions: put family-wide needs on the family's Needs: line"
            )
    for bullet in bullets:
        blocked = "blocked" in bullet.label.lower()
        if (
            bullet.label != "Preconditions"
            and not bullet.script.strip()
            and not blocked
        ):
            problems.append(
                f"line {bullet.line}: bullet '{bullet.label}' has no ```sh block"
            )
        if bullet.label != "Preconditions" and not bullet.ids:
            problems.append(
                f"line {bullet.line}: bullet '{bullet.label}' names no sub-feature ID"
            )
        known_bug = "known bug" in bullet.label.lower()
        marked = bool(bug_marks(bullet.script))
        if known_bug and not blocked and not marked:
            problems.append(
                f"line {bullet.line}: known-bug bullet '{bullet.label}' marks no "
                "command with `# bug`; end the line of the assertion that fails "
                "because of the bug with `# bug`"
            )
        if marked and not known_bug:
            problems.append(
                f"line {bullet.line}: bullet '{bullet.label}' has a `# bug` marker "
                "but its label does not say known bug"
            )
        for mark in bug_marks(bullet.script) if known_bug else []:
            if NEGATIVE_BUG_RE.search(mark.command):
                problems.append(
                    f"line {bullet.line}: bullet '{bullet.label}': assert the "
                    "correct behavior positively on the `# bug` line (an "
                    "unrelated error in `if cmd; then false; fi`, `test !` or "
                    "`! cmd` would read as fixed); use the verb's own expect "
                    f"flag or `{CLI} assert`"
                )
            try:
                clauses = parse_bug_predicate(mark.predicate)
            except ValueError as exc:
                problems.append(f"line {bullet.line}: bullet '{bullet.label}': {exc}")
                continue
            if clauses[0][0] != "exit" or len(clauses[0]) != 2:
                if CLI not in mark.command:
                    problems.append(
                        f"line {bullet.line}: bullet '{bullet.label}': "
                        f"`# bug: {mark.predicate}` reads a {CLI} result, but "
                        f"the marked command does not call {CLI}; use `exit N`"
                    )
    for command in extract_commands(text):
        error = validate_command(command)
        if error:
            problems.append(f"invalid command `{command}`: {error}")
    if routes is not None:
        for method, route, number in owned_routes(path):
            if not any(r["method"] == method and r["path"] == route for r in routes):
                problems.append(
                    f"line {number}: Routes lists {method} {route}, which the app does not serve"
                )
        http_methods = {r["method"] for r in routes} - {"WS"}
        for method, concrete in API_CMD_RE.findall(text):
            # HEAD and OPTIONS (CORS preflight) are answered on any served path.
            candidates = http_methods if method in ("HEAD", "OPTIONS") else {method}
            if not any(match_route(m, concrete, routes) for m in candidates):
                problems.append(
                    f"recipe calls {method} {concrete}, which matches no route"
                )
    return problems


def parse_index() -> list[dict[str, Any]]:
    index = FEATURE_MAP_DIR / "README.md"
    rows = []
    if not index.exists():
        return rows
    for line in index.read_text().splitlines():
        match = re.match(
            r"^\| (F\d{2}) \| \[([^\]]+)\]\(([^)]+)\) \|(.*)\| (\d+) \|$", line
        )
        if match:
            rows.append(
                {
                    "id": match.group(1),
                    "title": match.group(2),
                    "file": match.group(3),
                    "count": int(match.group(5)),
                    "line": line,
                }
            )
    return rows


def cmd_map_check(args: argparse.Namespace) -> dict[str, Any]:
    files = family_files(args.file)
    routes = None if args.no_routes else route_templates()
    problems: dict[str, list[str]] = {}
    for path in files:
        found = check_family(path, routes)
        if found:
            problems[path.name] = found
    all_ids = collect_ids()
    seen: dict[str, str] = {}
    for row in all_ids:
        if row["id"] in seen and seen[row["id"]] != row["file"]:
            problems.setdefault(row["file"], []).append(
                f"duplicate ID {row['id']} (also in {seen[row['id']]})"
            )
        seen[row["id"]] = row["file"]
    counts: dict[str, int] = {}
    for row in all_ids:
        counts[row["file"]] = counts.get(row["file"], 0) + 1
    index_rows = parse_index()
    if not args.file:
        listed = {row["file"] for row in index_rows}
        for path in family_files():
            if path.name not in listed:
                problems.setdefault("README.md", []).append(
                    f"{path.name} is not in the index table"
                )
        for row in index_rows:
            if not (FEATURE_MAP_DIR / row["file"]).exists():
                problems.setdefault("README.md", []).append(
                    f"index links missing file {row['file']}"
                )
            elif counts.get(row["file"], 0) != row["count"]:
                if args.fix_counts:
                    fix_index_count(row, counts.get(row["file"], 0))
                else:
                    problems.setdefault("README.md", []).append(
                        f"{row['file']}: index says {row['count']} sub-features, file has {counts.get(row['file'], 0)}"
                        " (run `map check --fix-counts`)"
                    )
        if args.fix_counts:
            fix_index_total(len(all_ids))
        total_line = re.search(
            r"\*\*(\d+) sub-features\*\*",
            (FEATURE_MAP_DIR / "README.md").read_text()
            if (FEATURE_MAP_DIR / "README.md").exists()
            else "",
        )
        if (
            total_line
            and int(total_line.group(1)) != len(all_ids)
            and not args.fix_counts
        ):
            problems.setdefault("README.md", []).append(
                f"total says {total_line.group(1)}, map has {len(all_ids)} (run `map check --fix-counts`)"
            )
    return {
        "ok": not problems,
        "files": len(files),
        "ids": len(all_ids),
        "problems": problems,
    }


def fix_index_count(row: dict[str, Any], count: int) -> None:
    index = FEATURE_MAP_DIR / "README.md"
    new_line = re.sub(r"\| \d+ \|$", f"| {count} |", row["line"])
    index.write_text(index.read_text().replace(row["line"], new_line))


def fix_index_total(total: int) -> None:
    index = FEATURE_MAP_DIR / "README.md"
    if index.exists():
        index.write_text(
            re.sub(
                r"\*\*\d+ sub-features\*\*",
                f"**{total} sub-features**",
                index.read_text(),
            )
        )


def cmd_map_coverage(args: argparse.Namespace) -> dict[str, Any]:
    routes = route_templates(Path(args.checkout).resolve() if args.checkout else None)
    owners: dict[tuple[str, str], list[str]] = {}
    driven: set[tuple[str, str]] = set()
    for path in family_files():
        for method, route, _ in owned_routes(path):
            owners.setdefault((method, route), []).append(path.name)
        driven |= driven_routes(path, routes)
    excluded = parse_exclusions()
    unowned, undriven, multi = [], [], []
    for route in routes:
        key = (route["method"], route["path"])
        label = f"{route['method']} {route['path']}"
        if key not in owners and label not in excluded:
            unowned.append(label)
        elif key in owners and key not in driven and label not in excluded:
            undriven.append({"route": label, "owner": owners[key]})
        if len(owners.get(key, [])) > 1:
            multi.append({"route": label, "owners": owners[key]})
    stale = [
        e
        for e in excluded
        if not any(f"{r['method']} {r['path']}" == e for r in routes)
    ]
    return {
        "ok": not unowned and not undriven and not multi and not stale,
        "routes": len(routes),
        "owned": len([r for r in routes if (r["method"], r["path"]) in owners]),
        "driven": len(driven),
        "unowned": unowned,
        "owned_but_not_driven": undriven,
        "multiple_owners": multi,
        "stale_exclusions": stale,
    }


def parse_exclusions() -> set[str]:
    index = FEATURE_MAP_DIR / "README.md"
    if not index.exists():
        return set()
    section = index.read_text().split("## Not mapped", 1)
    if len(section) < 2:
        return set()
    body = section[1].split("\n## ", 1)[0]
    return {f"{m} {p}" for m, p in ROUTE_LINE_RE.findall(body)}


BULLET_RE = re.compile(r"^- \*\*(.+?)\*\*")
BUG_MARK_RE = re.compile(r"\s#\s*bug\b(?::(?P<predicate>.*))?\s*$")
FENCE_RE = re.compile(r"^\s*```(\w*)\s*$")
# Assignments at a line start or after `;`, `&&` or `||`: `A=1; B=2` carries both.
ASSIGN_RE = re.compile(
    r"(?:^|;|&&|\|\|)\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", re.MULTILINE
)


@dataclass
class Bullet:
    label: str
    ids: list[str]
    script: str
    line: int
    requires: list[str] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)


# Shapes that pass when the command breaks: `if cmd; then false; fi`, `! cmd`,
# `test ! -e X`, or a check run only `if` something else holds.
NEGATIVE_BUG_RE = re.compile(r"(?:^|[;&|(]\s*)(?:if|!)\s|\btest\s+!|\[\s+!\s")


@dataclass
class BugMark:
    """A command marked `# bug: PREDICATE`, as lines of the bullet script."""

    first: int
    last: int
    command: str
    predicate: str


def bug_marks(script: str) -> list[BugMark]:
    """Commands marked `# bug` (1-based line ranges in the bullet script).

    The marker ends the command's last line; a command continued with `\\`
    starts at the first line of the continuation.
    """
    lines = script.splitlines()
    marks = []
    for number, line in enumerate(lines, 1):
        match = BUG_MARK_RE.search(line)
        if match:
            first = number
            while first > 1 and lines[first - 2].rstrip().endswith("\\"):
                first -= 1
            marks.append(
                BugMark(
                    first,
                    number,
                    "\n".join(lines[first - 1 : number]),
                    (match.group("predicate") or "").strip(),
                )
            )
    return marks


def parse_bug_predicate(text: str) -> list[list[str]]:
    """The clauses of a `# bug:` predicate: the documented failure's identity.

    Either `exit N` (the marked command's exit status, for `test`, `jq -e`
    and other plain commands) or one or more `PATH OP [VALUE]` clauses joined
    by `&&`, matched like `--check` against the JSON object the last
    control-automation call on the marked line printed, for example
    `status eq 500 && response.body.detail contains 'not found'`.
    Every clause but a `missing` one also needs its path present in that
    output (a stored `null` is present), and at least one clause must not be
    `missing`, so an error object never reads as the bug.
    """
    if not text:
        raise ValueError(
            "a `# bug` marker needs the documented failure: "
            "`# bug: status eq 500` or `# bug: exit 1`"
        )
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise ValueError(f"cannot parse `# bug: {text}`: {exc}") from None
    clauses: list[list[str]] = [[]]
    for token in tokens:
        if token == "&&":
            clauses.append([])
        else:
            clauses[-1].append(token)
    for clause in clauses:
        if len(clause) == 2 and clause[0] == "exit":
            if not clause[1].isdigit() or not 0 < int(clause[1]) < 256:
                raise ValueError(f"`exit {clause[1]}` needs a status from 1 to 255")
            if len(clauses) > 1:
                raise ValueError("`exit N` stands alone; it cannot be joined")
            continue
        if clause and clause[0] == "exit":
            # The CLI's own `exit` field is set only on its usage and
            # environment errors, which must never read as the bug.
            raise ValueError(
                f"`{' '.join(clause)}`: `exit` is reserved for the marked "
                "command's status; write `exit N`"
            )
        if len(clause) < 2 or clause[1] not in CHECK_OPS:
            raise ValueError(
                f"`{' '.join(clause)}` is not PATH OP [VALUE] with OP in "
                f"{', '.join(CHECK_OPS)}"
            )
        takes_value = clause[1] not in ("exists", "missing")
        if len(clause) != (3 if takes_value else 2):
            raise ValueError(
                f"`{' '.join(clause)}`: {clause[1]} takes "
                + ("one value" if takes_value else "no value")
            )
    if all(clause[1] == "missing" for clause in clauses):
        raise ValueError(
            "a `missing` clause needs a clause naming what the bug's output "
            "does hold (`status eq 200 && id missing`), or an error object "
            "would match"
        )
    return clauses


def bug_predicate_matches(
    clauses: list[list[str]], exit_status: int | None, observation: Any
) -> bool:
    """Whether the observed failure is the one the `# bug` predicate documents."""
    if clauses[0][0] == "exit" and len(clauses[0]) == 2:
        return exit_status == int(clauses[0][1])
    if not isinstance(observation, dict):
        return False
    return all(
        (clause[1] == "missing" or has_path(observation, clause[0]))
        and evaluate_check(observation, clause)["ok"]
        for clause in clauses
    )


def requires_closure(bullet: Bullet, bullets: Sequence[Bullet]) -> set[str]:
    """Every feature `bullet` needs, directly or through its `Requires:`."""
    by_id = {feature: other for other in bullets for feature in other.ids}
    closure: set[str] = set()
    stack = list(bullet.requires)
    while stack:
        feature = stack.pop()
        if feature in closure:
            continue
        closure.add(feature)
        if feature in by_id:
            stack.extend(by_id[feature].requires)
    return closure


def driving_bullets(path: Path) -> list[Bullet]:
    """The fenced `sh` blocks of the Driving section, grouped by bullet.

    Shell blocks under `Preconditions:` form a first pseudo-bullet labelled
    `Preconditions`. Prose and inline code are never executed.
    """
    lines = path.read_text().splitlines()
    try:
        start = lines.index(f"## Driving it with {CLI}")
    except ValueError:
        return []
    bullets: list[Bullet] = []
    current = Bullet("Preconditions", [], "", start + 1)
    in_fence = False
    fence_lines: list[str] = []
    for number, line in enumerate(lines[start + 1 :], start + 2):
        if line.startswith("## "):
            break
        fence = FENCE_RE.match(line)
        if fence and not in_fence:
            in_fence = fence.group(1) in ("sh", "bash", "shell")
            fence_lines = []
            if not in_fence and fence.group(1):
                in_fence = False
            continue
        if fence and in_fence:
            indent = min(
                (len(x) - len(x.lstrip()) for x in fence_lines if x.strip()), default=0
            )
            current.script += "\n".join(x[indent:] for x in fence_lines) + "\n"
            in_fence = False
            continue
        if in_fence:
            fence_lines.append(line)
            continue
        match = BULLET_RE.match(line)
        if match:
            if current.script or current.label != "Preconditions":
                bullets.append(current)
            label = match.group(1)
            current = Bullet(
                label, re.findall(r"F\d{2}\.[a-z0-9-]+", label), "", number
            )
        elif line.strip().startswith("Requires:"):
            current.requires += re.findall(r"F\d{2}\.[a-z0-9-]+", line)
        elif line.strip().startswith("Needs:"):
            current.needs += needs_tokens(line.strip()[len("Needs:") :])
    if current.script or current.label != "Preconditions":
        bullets.append(current)
    return bullets


def select_bullets(bullets: list[Bullet], only: str | None) -> list[Bullet]:
    available: set[str] = set()
    for bullet in bullets:
        missing = set(bullet.requires) - available
        if missing:
            usage_error(
                f"{bullet.label}: Requires must name earlier recipes: {', '.join(sorted(missing))}"
            )
        available.update(bullet.ids)
    if only is None:
        return bullets
    wanted = {feature.strip() for feature in only.split(",")}
    missing = wanted - available
    if missing:
        usage_error(
            f"--only IDs have no recipe in this family: {', '.join(sorted(missing))}"
        )
    selected = []
    for bullet in reversed(bullets):
        if wanted.intersection(bullet.ids) or bullet.label == "Preconditions":
            selected.append(bullet)
            wanted.update(bullet.requires)
    return list(reversed(selected))


VAR_USE_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
LOCAL_ASSIGN_RE = re.compile(r"(?:^|[\s;&|(])([A-Za-z_][A-Za-z0-9_]*)\+?=")
LOCAL_BIND_RE = re.compile(
    r"\b(?:for|local|export|declare(?:\s+-\w+)*)\s+([A-Za-z_][A-Za-z0-9_]*)"
)
# `read [-ers] [-adinNptu ARG] NAME...`; only the letters in the second set
# take an argument.
READ_RE = re.compile(
    r"\bread\b((?:\s+-[ers]*(?:[adinNptu]\s*(?:'[^']*'|[^\s;]+))?)*)"
    r"((?:\s+[A-Za-z_]\w*)+)"
)
QUOTED_HEREDOC_RE = re.compile(r"<<-?\s*(?:'(\w+)'|\"(\w+)\"|\\(\w+))")


def outside_reads(script: str) -> set[str]:
    """Variables a bullet expands before it binds them itself.

    A name read before the bullet's first binding of it (or in the value of
    that binding, as in `TOKEN=$(... "$TOKEN")`) comes from outside: in a full
    replay, from an earlier bullet. `for`, `read`, `local`, `export` and
    `declare` bind where they appear. Quoted heredoc bodies (`<<'EOF'`) are
    program text, not shell, and are skipped.
    """
    reads: set[str] = set()
    bound: set[str] = set()
    terminator: str | None = None
    for line in script.splitlines():
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        binders = [(m.start(1), m.group(1)) for m in LOCAL_BIND_RE.finditer(line)]
        for match in READ_RE.finditer(line):
            binders += [(match.start(2), name) for name in match.group(2).split()]
        for match in VAR_USE_RE.finditer(line):
            name = match.group(1)
            early = any(n == name and pos < match.start() for pos, n in binders)
            if name not in bound and not early:
                reads.add(name)
        bound.update(name for _, name in binders)
        bound.update(LOCAL_ASSIGN_RE.findall(line))
        heredoc = QUOTED_HEREDOC_RE.search(line)
        if heredoc:
            terminator = next(g for g in heredoc.groups() if g)
    return reads


def undeclared_dependencies(bullets: list[Bullet]) -> list[str]:
    """Recipes that read a variable an earlier recipe sets, without `Requires:`.

    `map run` carries top-level assignments from one bullet to the next, so in
    a full replay a bullet sees the value set by the last earlier bullet that
    assigned it. `--only` runs Preconditions, the selected bullets and their
    `Requires:` closure; anything else leaves the variable empty, and an empty
    id or path can fail the `# bug` line for the wrong reason. Blocked and
    known-bug bullets never save their variables, so they are not producers.
    """
    problems = []
    for index, bullet in enumerate(bullets):
        if bullet.label == "Preconditions" or not bullet.ids:
            continue
        closure = requires_closure(bullet, bullets)
        missing: dict[str, list[str]] = {}
        for name in sorted(outside_reads(bullet.script)):
            for producer in reversed(bullets[:index]):
                marker = producer.label.lower()
                if "blocked" in marker or "known bug" in marker:
                    continue
                if name in ASSIGN_RE.findall(producer.script):
                    if producer.ids and not closure.intersection(producer.ids):
                        missing.setdefault(producer.ids[0], []).append(f"${name}")
                    break
        for producer, names in missing.items():
            problems.append(
                f"{bullet.ids[0]} reads {', '.join(names)}, last set by "
                f"{producer}; add Requires: `{producer}` so --only runs it"
            )
    return problems


def execute_recipe(
    path: Path, env: dict[str, str], timeout: float
) -> tuple[int, str, str, bool]:
    def interrupt(_signum: int, _frame: Any) -> NoReturn:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        with subprocess.Popen(
            ["bash", str(path)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            timed_out = False
            try:
                process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                stop_process_group(process.pid, grace=0.5)
                out, err = process.communicate()
            if timed_out:
                return 124, out, f"{err}\ntimed out after {timeout}s", True
            return process.returncode, out, err, False
    finally:
        signal.signal(signal.SIGTERM, previous)


NEEDS = (
    "llm",
    "git",
    "uvx",
    "pypi",
    "network",
)


def needs_tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[\s,`]+", text) if t]


def family_needs(path: Path) -> list[str]:
    """Tokens from the family's optional `Needs:` line."""
    for line in path.read_text().splitlines():
        if line.startswith("Needs:"):
            return needs_tokens(line[len("Needs:") :])
    return []


def recipe_needs(bullets: list[Bullet]) -> dict[int, list[str]]:
    """Each bullet's own `Needs:` plus those of its `Requires:` closure."""
    by_id = {feature: bullet for bullet in bullets for feature in bullet.ids}
    needs: dict[int, list[str]] = {}
    for index, bullet in enumerate(bullets):
        seen: set[str] = set()
        stack = list(bullet.requires)
        wanted = list(bullet.needs)
        while stack:
            feature = stack.pop()
            if feature in seen or feature not in by_id:
                continue
            seen.add(feature)
            wanted += by_id[feature].needs
            stack.extend(by_id[feature].requires)
        needs[index] = sorted(set(wanted))
    return needs


def blocked_recipes(
    bullets: list[Bullet], machine: dict[str, dict[str, Any]]
) -> dict[int, list[str]]:
    """Bullets whose recipe needs a capability this machine lacks."""
    return {
        index: missing
        for index, wanted in recipe_needs(bullets).items()
        if (missing := [n for n in wanted if not machine.get(n, {}).get("available")])
    }


def network_reachable(url: str) -> tuple[bool, str]:
    """Outbound HTTPS, for recipes that need github.com or a provider's auth host."""
    try:
        httpx.head(url, timeout=5, follow_redirects=False)
    except httpx.HTTPError as exc:
        return False, f"{url} unreachable: {type(exc).__name__}"
    return True, f"{url} reachable"


def machine_capabilities() -> dict[str, dict[str, Any]]:
    key = next(
        (n for n in ("DEEPSEEK_API_KEY", "LLM_API_KEY") if os.environ.get(n)), None
    )
    found = {
        "llm": (key is not None, key or "set DEEPSEEK_API_KEY"),
        "git": (bool(shutil.which("git")), shutil.which("git")),
        "uvx": (bool(shutil.which("uvx")), shutil.which("uvx")),
        # Preset runs install openhands-sdk from PyPI in their setup.sh.
        "pypi": network_reachable("https://pypi.org/simple/openhands-sdk/"),
        "network": network_reachable("https://github.com"),
    }
    return {
        name: {"available": ok, "detail": detail}
        for name, (ok, detail) in found.items()
    }


def cmd_capabilities(args: argparse.Namespace) -> dict[str, Any]:
    del args
    machine = machine_capabilities()
    families = []
    for path in family_files():
        needs = family_needs(path)
        missing = [n for n in needs if not machine.get(n, {}).get("available")]
        bullets = driving_bullets(path)
        blocked = [
            {"ids": bullets[index].ids, "missing": lacking}
            for index, lacking in blocked_recipes(bullets, machine).items()
        ]
        families.append(
            {
                "file": path.name,
                "needs": needs,
                "missing": missing,
                "drivable": not missing,
                "blocked_recipes": [] if missing else blocked,
            }
        )
    return {
        "ok": True,
        "machine": machine,
        "families": families,
        "blocked_families": [f["file"] for f in families if f["missing"]],
        "families_with_blocked_recipes": [
            f["file"] for f in families if f["blocked_recipes"]
        ],
    }


def launch_flags(path: Path) -> list[str]:
    """Flags from the family's optional `Launch:` line (for `map run --fresh`)."""
    for line in path.read_text().splitlines():
        if line.startswith("Launch:"):
            return shlex.split(line[len("Launch:") :].replace("`", " "))
    return []


def self_command(*argv: str) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), *argv],
        capture_output=True,
        text=True,
    )
    lines = completed.stdout.strip().splitlines()
    if completed.returncode != 0 or not lines:
        raise CliError(
            f"`{CLI} {' '.join(argv)}` failed",
            exit_code=3,
            data={
                "stdout": completed.stdout[-1500:],
                "stderr": completed.stderr[-1500:],
            },
        )
    return {"stdout": completed.stdout, "last_line": lines[-1]}


def cmd_map_run(args: argparse.Namespace) -> dict[str, Any]:
    if args.all:
        if args.only is not None:
            usage_error("--only needs --file; it cannot be combined with --all")
        if not args.fresh:
            usage_error(
                "--all needs --fresh (one fresh run per family)",
                example=f"{CLI} map run --all --fresh --record",
            )
        reports = []
        machine = machine_capabilities()
        for path in family_files():
            missing = [
                n for n in family_needs(path) if not machine.get(n, {}).get("available")
            ]
            if missing:
                reports.append(
                    {
                        "ok": True,
                        "file": path.name,
                        "summary": None,
                        "run": None,
                        "error": None,
                        "blocked": missing,
                    }
                )
                continue
            family_args = argparse.Namespace(
                **{**vars(args), "all": False, "file": path.stem}
            )
            try:
                report = cmd_map_run(family_args)
            except CliError as exc:
                report = {"ok": False, "file": path.name, "error": str(exc), **exc.data}
            reports.append(
                {k: report.get(k) for k in ("ok", "file", "summary", "run", "error")}
            )
        blocked = [r["file"] for r in reports if r.get("blocked")]
        failing = [r["file"] for r in reports if not r["ok"]]
        return {
            "ok": not failing,
            "families": len(reports),
            "failing": failing,
            "blocked_by_machine": blocked,
            "reports": reports,
        }
    if not args.file:
        usage_error(
            "map run needs --file (or --all --fresh)",
            example=f"{CLI} map run --file F01",
        )
    files = family_files(args.file)
    if len(files) != 1:
        usage_error(
            "map run needs exactly one --file", example=f"{CLI} map run --file F01"
        )
    path = files[0]
    select_bullets(driving_bullets(path), args.only)
    if not args.fresh:
        return replay_family(resolve_run(args.run), path, args)
    flags = launch_flags(path)
    if args.checkout:
        flags = [*flags, "--checkout", str(Path(args.checkout).resolve())]
    attempts = []
    for _ in range(max(1, args.repeat)):
        launched = self_command(
            "launch",
            "--new",
            "--name",
            f"map-{path.stem[:3].lower()}",
            "--print-run",
            *flags,
        )
        run_dir = launched["last_line"].strip()
        try:
            report = replay_family(load_run(Path(run_dir)), path, args)
        finally:
            self_command("stop", "--run", run_dir)
        attempts.append({**report, "run": run_dir})
    if len(attempts) == 1:
        return {**attempts[0], "launch_flags": flags}
    return {
        "ok": all(a["ok"] for a in attempts),
        "file": path.name,
        "launch_flags": flags,
        "summary": {
            status: [a["summary"].get(status, 0) for a in attempts]
            for status in ("pass", "fail", "xfail", "xpass", "blocked")
        },
        "attempts": [
            {
                "run": a["run"],
                "ok": a["ok"],
                "summary": a["summary"],
                "statuses": {b["bullet"]: b.get("status") for b in a["bullets"]},
            }
            for a in attempts
        ],
    }


def replay_family(run: Run, path: Path, args: argparse.Namespace) -> dict[str, Any]:
    every_bullet = driving_bullets(path)
    bullets = select_bullets(every_bullet, args.only)
    checkout = Path(run.data.get("checkout", REPO_ROOT))
    state = run.dir / "map-run" / f"{path.stem}.state.sh"
    state.parent.mkdir(exist_ok=True)
    state.write_text("")
    env = {
        **os.environ,
        RUN_ENV: str(run.dir),
        CHECKOUT_ENV: str(checkout),
        "PATH": f"{SCRIPT_DIR}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    results = []
    failed = False
    xpassed = False
    # Labels and ids of bullets that failed: a known bug whose arrange did not
    # run (with --keep-going) cannot show the correct behavior.
    broken: set[str] = set()
    unavailable: dict[int, list[str]] = {}
    if not args.dry_run and any(recipe_needs(bullets).values()):
        unavailable = blocked_recipes(bullets, machine_capabilities())
    for index, bullet in enumerate(bullets):
        marker = bullet.label.lower()
        if index in unavailable:
            missing = unavailable[index]
            results.append(
                {
                    "bullet": bullet.label,
                    "ids": bullet.ids,
                    "status": "blocked",
                    "missing": missing,
                    "hint": f"this machine lacks {', '.join(missing)} "
                    f"(`{CLI} capabilities`)",
                }
            )
            if args.record:
                for feature in bullet.ids:
                    append_ledger(
                        run,
                        feature,
                        "blocked",
                        f"map run {path.name}: {bullet.label}",
                        "prerequisite available",
                        f"blocked: needs {', '.join(missing)}",
                        [],
                        "the recipe's Needs: (or a Requires: recipe's) is unmet",
                    )
            continue
        if "blocked" in marker:
            results.append(
                {"bullet": bullet.label, "ids": bullet.ids, "status": "blocked"}
            )
            if args.record:
                for feature in bullet.ids:
                    append_ledger(
                        run,
                        feature,
                        "blocked",
                        f"map run {path.name}: {bullet.label}",
                        "prerequisite available",
                        "blocked (see the bullet)",
                        [],
                        "bullet is marked blocked in the map",
                    )
            continue
        if not bullet.script.strip():
            results.append(
                {"bullet": bullet.label, "ids": bullet.ids, "status": "no-commands"}
            )
            continue
        if failed and not args.keep_going:
            results.append(
                {"bullet": bullet.label, "ids": bullet.ids, "status": "skipped"}
            )
            continue
        names = sorted(set(ASSIGN_RE.findall(bullet.script)))
        save_state = (
            f"\ndeclare -p {' '.join(names)} >> {shlex_quote(str(state))} 2>/dev/null || true\n"
            if names
            else ""
        )
        failure = run.dir / "map-run" / f"{path.stem}.failed"
        failure.unlink(missing_ok=True)
        observation = run.dir / "map-run" / f"{path.stem}.observation.json"
        observation.unlink(missing_ok=True)
        # The ERR trap copies the observation at the failing command, before a
        # recipe's own EXIT cleanup calls the CLI again.
        observed_at_failure = observation.with_suffix(".failed.json")
        observed_at_failure.unlink(missing_ok=True)
        preamble = [
            "set -eo pipefail",
            f"source {shlex_quote(str(state))}",
            f"cd {shlex_quote(str(checkout))}",
            'trap \'printf "%s\\t%s\\t%s\\n" "$?" "$LINENO" "$BASH_COMMAND" > '
            + shlex_quote(str(failure))
            + "; cp -f "
            + shlex_quote(str(observation))
            + " "
            + shlex_quote(str(observed_at_failure))
            + " 2>/dev/null' ERR",
        ]
        script = "\n".join(preamble) + f"\n{bullet.script}{save_state}"
        if args.dry_run:
            results.append(
                {"bullet": bullet.label, "ids": bullet.ids, "script": bullet.script}
            )
            continue
        started = time.monotonic()
        recipe_path = run.private / f"{path.stem}.recipe.sh"
        recipe_path.write_text(script)
        code, out, err, timed_out = execute_recipe(
            recipe_path, {**env, OBSERVATION_ENV: str(observation)}, args.timeout
        )
        elapsed = int((time.monotonic() - started) * 1000)
        failed_line: int | None = None
        failed_command: str | None = None
        failed_status: int | None = None
        observed: Any = None
        if code != 0 and failure.exists():
            status_text, _, rest = failure.read_text().rstrip("\n").partition("\t")
            line_text, _, failed_command = rest.partition("\t")
            if status_text.isdigit():
                failed_status = int(status_text)
            if line_text.isdigit():
                failed_line = int(line_text) - len(preamble)
            failed_command = REDACT.text(failed_command)[:500]
            if observed_at_failure.exists():
                with contextlib.suppress(OSError, ValueError):
                    observed = json.loads(observed_at_failure.read_text())
        transcript = {
            "file": path.name,
            "bullet": bullet.label,
            "ids": bullet.ids,
            "line": bullet.line,
            "script": bullet.script,
            "exit_code": code,
            "elapsed_ms": elapsed,
            "failed_line": failed_line,
            "failed_command": failed_command,
            "failed_status": failed_status,
            "observation": observed,
            "stdout": out,
            "stderr": err,
        }
        slug = re.sub(r"[^a-z0-9]+", "-", bullet.label.lower()).strip("-")[:40]
        saved = save_evidence(
            run, f"map-run/{path.stem}-{index:02d}-{slug}", transcript
        )
        known_bug = "known bug" in marker
        mark = next(
            (
                m
                for m in bug_marks(bullet.script)
                if not timed_out
                and failed_line is not None
                and m.first <= failed_line <= m.last
            ),
            None,
        )
        documented = False
        predicate_error: str | None = None
        if known_bug and code != 0 and mark is not None:
            try:
                clauses = parse_bug_predicate(mark.predicate)
                documented = bug_predicate_matches(clauses, failed_status, observed)
            except ValueError as exc:
                predicate_error = str(exc)
        unarranged = sorted(
            broken.intersection(
                ["Preconditions", *requires_closure(bullet, every_bullet)]
            )
        )
        if known_bug and code == 0 and not unarranged:
            status = "xpass"
            xpassed = True
        elif known_bug and documented:
            status = "xfail"
        else:
            status = "pass" if code == 0 and not known_bug else "fail"
            failed = failed or status == "fail"
        if status == "fail":
            broken.update([bullet.label, *bullet.ids])
        row: dict[str, Any] = {
            "bullet": bullet.label,
            "ids": bullet.ids,
            "line": bullet.line,
            "status": status,
            "exit_code": code,
            "elapsed_ms": elapsed,
            "transcript": str(saved),
        }
        if code != 0:
            row["failed_line"] = failed_line
            row["failed_command"] = failed_command
            row["stdout_tail"] = REDACT.text(out[-1200:])
            row["stderr_tail"] = REDACT.text(err[-1200:])
        if known_bug and mark is not None:
            row["bug_predicate"] = mark.predicate
        if known_bug and status == "fail" and mark is None:
            row["hint"] = (
                "no failure was confirmed at the `# bug` assertion; "
                "the bug was not reproduced"
            )
        elif known_bug and status == "fail" and mark is not None:
            row["failed_status"] = failed_status
            if isinstance(observed, dict):
                row["observed"] = {
                    k: observed[k]
                    for k in ("status", "hint", "error", "checks")
                    if k in observed
                }
            row["hint"] = predicate_error or (
                "the `# bug` command failed, but not with the documented "
                f"failure (`# bug: {mark.predicate}`); the bug was not reproduced"
            )
        if status == "xpass":
            row["hint"] = "the known bug no longer reproduces: update the map"
        elif known_bug and code == 0:
            row["hint"] = (
                f"passed, but {', '.join(unarranged)} failed earlier in this run, "
                "so the pass does not show the correct behavior"
            )
        results.append(row)
        if args.record and bullet.ids:
            note = {
                "xfail": "known bug reproduced (expected failure)",
                "xpass": "known bug no longer reproduces; update the map",
            }.get(
                status, "automated map run; prose-only expectations were not re-judged"
            )
            if failed_command:
                note += f"; failed at line {failed_line}: {failed_command}"
            for feature in bullet.ids:
                append_ledger(
                    run,
                    feature,
                    status,
                    f"map run {path.name}: {bullet.label}",
                    "every command in the bullet exits 0 (its --expect/--check assertions hold)",
                    f"exit {code}",
                    [str(saved.relative_to(run.dir))],
                    note,
                )
    summary = {
        status: sum(1 for r in results if r.get("status") == status)
        for status in (
            "pass",
            "fail",
            "xfail",
            "xpass",
            "blocked",
            "skipped",
            "no-commands",
        )
    }
    return {
        "ok": not failed and not xpassed,
        "file": path.name,
        "summary": summary,
        "bullets": results,
    }


def append_ledger(
    run: Run,
    feature: str,
    result: str,
    entry: str,
    expected: str,
    actual: str,
    artifacts: list[str],
    note: str | None,
) -> dict[str, Any]:
    row = {
        "at": now_iso(),
        "run": run.id,
        "checkout_sha": run.data.get("checkout_sha"),
        "feature": feature,
        "result": result,
        "entry": entry,
        "expected": expected,
        "actual": actual,
        "artifacts": artifacts,
        "note": note,
    }
    run.evidence.mkdir(exist_ok=True)
    with ledger_path(run).open("a") as handle:
        handle.write(json.dumps(REDACT.obj(row)) + "\n")
    return row


def shlex_quote(value: str) -> str:
    return shlex.quote(value)


def cmd_map_ids(args: argparse.Namespace) -> dict[str, Any]:
    rows = collect_ids(family_files(args.file) if args.file else None)
    return {"ok": True, "count": len(rows), "ids": rows}


def cmd_map_routes(args: argparse.Namespace) -> dict[str, Any]:
    routes = route_templates(Path(args.checkout).resolve() if args.checkout else None)
    if args.module:
        routes = [r for r in routes if r["module"] == args.module]
    return {"ok": True, "count": len(routes), "routes": routes}


def cmd_map_diff(args: argparse.Namespace) -> dict[str, Any]:
    base = {
        (r["method"], r["path"]): r for r in route_templates(Path(args.base).resolve())
    }
    target_checkout = Path(args.target).resolve() if args.target else None
    target = {(r["method"], r["path"]): r for r in route_templates(target_checkout)}
    owners: dict[tuple[str, str], str] = {}
    for path in family_files():
        for method, route, _ in owned_routes(path):
            owners[(method, route)] = path.name
    added = [f"{m} {p}" for (m, p) in sorted(set(target) - set(base))]
    removed = [
        {"route": f"{m} {p}", "owner": owners.get((m, p))}
        for (m, p) in sorted(set(base) - set(target))
    ]
    newly_deprecated = [
        {"route": f"{m} {p}", "owner": owners.get((m, p))}
        for (m, p) in sorted(set(base) & set(target))
        if target[(m, p)]["deprecated"] and not base[(m, p)]["deprecated"]
    ]
    return {
        "ok": True,
        "base": args.base,
        "target": args.target or str(REPO_ROOT),
        "added": added,
        "removed": removed,
        "newly_deprecated": newly_deprecated,
    }


def source_prefixes(path: Path) -> list[str]:
    prefixes = []
    for line in path.read_text().splitlines():
        if line.startswith("Source:"):
            prefixes += re.findall(r"`([^`]+)`", line)
    return prefixes


def cmd_map_owners(args: argparse.Namespace) -> dict[str, Any]:
    if args.changed:
        diff = git("diff", "--name-only", args.changed)
        if diff == "unknown":
            usage_error(
                f"git diff {args.changed} failed (shallow clone? fetch more history)"
            )
        paths = [p for p in diff.splitlines() if p]
    else:
        paths = list(args.paths)
    prefixes = {f.name: source_prefixes(f) for f in family_files()}
    mapping, unowned = [], []
    for changed in paths:
        owners = [
            name
            for name, pre in prefixes.items()
            if any(changed.startswith(p.rstrip("*")) for p in pre)
        ]
        if owners:
            mapping.append({"path": changed, "families": owners})
        elif re.match(
            r"^openhands/automation/",
            changed,
        ):
            unowned.append(changed)
    return {
        "ok": True,
        "changed": len(paths),
        "mapped": mapping,
        "unowned_product_paths": unowned,
    }


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

TOP_HELP = f"""\
{CLI}: drive an isolated OpenHands Automation Service (local mode) and the
Agent Server it dispatches runs to, through their REST APIs, and capture
evidence. Every command prints one JSON object.
Exit codes: 0 ok, 1 action failed, 2 usage, 3 environment.

Lifecycle:
  launch        Start an Agent Server + this checkout's service (private HOME, DB, keys, ports)
  doctor        Read-only health check; run it first and after every surprise
  restart       Restart services (--env, --rotate-key, --fast on|off, --hard)
  service       stop | start | status one service (outage drills)
  stop          Stop only this run's processes; evidence survives (--purge-private)
  runs          List runs with liveness
  logs          Tail or grep a service log (secrets redacted)

Driving:
  api           Call any endpoint: api METHOD PATH (`/v1/...` means /api/automation/v1/...);
                `api --agent-server METHOD PATH` calls the Agent Server
  llm           preset deepseek | set | show: LLM profiles on the Agent Server (preset runs use them)
  run           wait: poll an automation's runs until a status (PENDING..COMPLETED)
  fixture       tarball | automation | git-remote | http-sink: run-owned test inputs
  event         send | requested-types: signed webhook deliveries (hmac, standard-webhooks, slack)
  kv            token: mint the KV JWT a run receives as AUTOMATION_KV_TOKEN
  state         sql | ls | cat: read-only views of the run's database and files
  conversation  events: what a run's agent did, from the Agent Server
  sink          read: what an HTTP sink (telemetry, external tarball host) received
  helper        start | stop: recipe-local background processes
  exec          Run a client program with AUTOMATION_URL/AUTOMATION_API_KEY set
  capabilities  what this machine can drive (LLM key, PyPI, git) and which recipes are blocked

Proof and map:
  evidence      add | report: the append-only ledger under <run>/evidence/
  map           run | check | coverage | ids | routes | diff | owners: an executable, checked map

Which run: --run DIR, else ${RUN_ENV}, else the only live run.
Run state lives under ${HOME_ENV} (default $TMPDIR/automation-verify).

Examples:
  export {RUN_ENV}=$({CLI} launch --new --print-run)
  {CLI} doctor
  {CLI} llm preset deepseek
  AID=$({CLI} api POST /v1/preset/prompt --json '{{"name":"hello","prompt":"Create hello.txt containing hi","trigger":{{"type":"cron","schedule":"0 0 1 1 *"}}}}' --expect 201 --field id)
  RID=$({CLI} api POST /v1/$AID/dispatch --expect 201 --field id)
  {CLI} run wait "$AID" --run-id "$RID" --status COMPLETED --timeout 900 --save F07.preset/run
  {CLI} stop
"""


class Formatter(argparse.RawDescriptionHelpFormatter):
    pass


def add_run_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--run",
        default=argparse.SUPPRESS,
        help=f"run directory (default: ${RUN_ENV} or the only live run)",
    )


def sub(
    subparsers: Any,
    name: str,
    help_text: str,
    examples: str,
    func: Callable[[argparse.Namespace], dict[str, Any]] | None,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        name,
        help=help_text,
        description=help_text,
        epilog="Examples:\n" + examples,
        formatter_class=Formatter,
    )
    add_run_flag(parser)
    parser.set_defaults(func=func)
    return parser


def add_assertions(
    p: argparse.ArgumentParser,
    *,
    field_help: str = "print one field of the response body (exit 1 if missing or not ok)",
) -> None:
    p.add_argument(
        "--expect",
        metavar="CODES",
        help="accepted statuses, e.g. 201 or 4xx or 200,204 (default <400)",
    )
    p.add_argument(
        "--check",
        nargs="+",
        action="append",
        metavar="ARG",
        help=f"FIELD OP [VALUE] on the JSON body; OP in {', '.join(CHECK_OPS)}",
    )
    p.add_argument("--field", help=field_help)
    p.add_argument(
        "--unsafe-unredacted",
        action="store_true",
        help="with --field/--print-header: print a value without redaction",
    )
    p.add_argument(
        "--save", metavar="FEATURE/NAME", help="save the exchange under <run>/evidence/"
    )
    p.add_argument(
        "--quiet", action="store_true", help="omit bodies from the printed JSON"
    )


def add_body(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", help="JSON request body")
    p.add_argument("--json-file", help="read the JSON body from a file")
    p.add_argument("--stdin", action="store_true", help="read the JSON body from stdin")


def add_key_source(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--api-key-env", help="env var holding the key (never put keys in argv)"
    )
    p.add_argument("--api-key-file", help="file holding the key")


@functools.cache
def recipe_parser() -> argparse.ArgumentParser:
    """One parser for validating every recipe command."""
    return build_parser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=CLI, description=TOP_HELP, formatter_class=Formatter
    )
    parser.add_argument("--run", default=None, help=argparse.SUPPRESS)
    parser.set_defaults(func=None)
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub(
        commands,
        "launch",
        "Start an isolated Agent Server and this checkout's Automation Service in local mode.",
        f"""\
  export {RUN_ENV}=$({CLI} launch --new --print-run)
  {CLI} launch --new --env AUTOMATION_WEBHOOK_SECRET=qa-builtin-secret
  {CLI} launch --new --agent-server-path ../software-agent-sdk     # Agent Server from a checkout
  {CLI} launch --new --agent-server-url http://127.0.0.1:18000 --agent-server-key-env LOCAL_BACKEND_API_KEY
  {CLI} launch --new --telemetry-sink --production-intervals""",
        cmd_launch,
    )
    p.add_argument("--new", action="store_true", help="always start a new run")
    p.add_argument("--name", help="label appended to the run directory name")
    p.add_argument(
        "--print-run", action="store_true", help="print only the run directory"
    )
    p.add_argument("--checkout", help="automation checkout to run (default: this one)")
    p.add_argument(
        "--agent-server-url",
        help="attach to a running Agent Server instead of launching one",
    )
    p.add_argument(
        "--agent-server-key-env",
        help="env var holding the attached Agent Server's session key",
    )
    p.add_argument(
        "--agent-server-version",
        help="openhands-agent-server release (default: the checkout's openhands-sdk pin)",
    )
    p.add_argument(
        "--agent-server-path",
        help="software-agent-sdk checkout to run the Agent Server from",
    )
    p.add_argument(
        "--as-env",
        action="append",
        metavar="K=V",
        help="extra Agent Server environment",
    )
    p.add_argument(
        "--env",
        action="append",
        metavar="K=V",
        help="extra Automation Service environment",
    )
    p.add_argument(
        "--pass-env",
        action="append",
        metavar="NAME",
        help="forward this variable from your shell to both services",
    )
    p.add_argument("--port", type=int, help="service port (default: a free one)")
    p.add_argument(
        "--production-intervals",
        action="store_true",
        help="keep the 60s scheduler/watchdog defaults",
    )
    p.add_argument(
        "--telemetry-sink",
        action="store_true",
        help="point PostHog at a local HTTP sink named telemetry",
    )
    p.add_argument(
        "--timeout", type=float, default=240, help="seconds to wait for readiness"
    )
    p.add_argument(
        "--force", action="store_true", help="launch even when memory is low"
    )

    sub(
        commands,
        "doctor",
        "Read-only health check of both services, auth and logs.",
        f"  {CLI} doctor",
        cmd_doctor,
    )

    p = sub(
        commands,
        "restart",
        "Restart services of the same run (state and DB survive).",
        f"""\
  {CLI} restart
  {CLI} restart --service automation --env AUTOMATION_FAILURE_DISABLE_THRESHOLD=2
  {CLI} restart --rotate-key
  {CLI} restart --service automation --hard     # SIGKILL, then start""",
        cmd_restart,
    )
    p.add_argument("--service", choices=[*SERVICES, "all"], default="all")
    p.add_argument(
        "--rotate-key",
        action="store_true",
        help="new local API key; the old one is kept for --auth previous",
    )
    p.add_argument(
        "--reset-env", action="store_true", help="drop extra env set at launch/restart"
    )
    p.add_argument(
        "--env",
        action="append",
        metavar="K=V",
        help="extra environment for the restarted service",
    )
    p.add_argument(
        "--fast", choices=["on", "off"], help="shrunk loop intervals on or off"
    )
    p.add_argument(
        "--hard", action="store_true", help="SIGKILL instead of a graceful stop"
    )
    p.add_argument("--timeout", type=float, default=240)

    p = sub(
        commands,
        "service",
        "Stop, start or inspect one service of the run (outage drills).",
        f"""\
  {CLI} service stop agent-server
  {CLI} service start agent-server
  {CLI} service status automation""",
        cmd_service,
    )
    p.add_argument("action", choices=["stop", "start", "status"])
    p.add_argument("name", choices=SERVICES)
    p.add_argument("--timeout", type=float, default=240)

    p = sub(
        commands,
        "stop",
        "Stop this run's processes; evidence survives.",
        f"  {CLI} stop\n  {CLI} stop --purge-private",
        cmd_stop,
    )
    p.add_argument(
        "--purge-private",
        action="store_true",
        help="also delete keys, DB, workspaces and fixtures",
    )

    sub(commands, "runs", "List runs with liveness.", f"  {CLI} runs", cmd_runs)

    p = sub(
        commands,
        "logs",
        "Tail or grep a service log (secrets redacted).",
        f"""\
  {CLI} logs --service automation --tail 80
  {CLI} logs --service automation --grep 'Traceback' --expect-none
  {CLI} logs --service automation --grep 'stream supervisor' --expect-min 1""",
        cmd_logs,
    )
    p.add_argument("--service", choices=SERVICES, default="automation")
    p.add_argument("--grep", help="regular expression")
    p.add_argument("--tail", type=int, default=40)
    p.add_argument("--expect-min", type=int, help="fail unless at least N lines match")
    p.add_argument(
        "--wait",
        type=float,
        metavar="SECONDS",
        help="with --expect-min: poll the log until enough lines match",
    )
    p.add_argument(
        "--expect-none", action="store_true", help="fail if any line matches"
    )
    p.add_argument(
        "--since-restart",
        action="store_true",
        help="only lines since the last (re)start",
    )
    p.add_argument(
        "--full",
        action="store_true",
        help="do not cut lines at 2000 characters (tracebacks in JSON logs)",
    )

    p = sub(
        commands,
        "api",
        "Call any REST endpoint with the run's auth.",
        f"""\
  {CLI} api GET /v1 --expect 200 --check total ge 0
  {CLI} api POST /v1/uploads --query name=qa --data-file ./qa.tar.gz --content-type application/gzip --expect 201 --field tarball_path
  {CLI} api GET /v1/$AID --auth none --expect 401
  {CLI} api PUT /v1/kv/greeting --query automation_id=$AID --json '"hi"' --expect 201
  {CLI} api GET /health --expect 200
  {CLI} api --agent-server GET /api/conversations/$CID --expect 200 --check tags.automationrunid eq "$RID\"""",
        cmd_api,
    )
    p.add_argument(
        "--agent-server",
        action="store_true",
        help="call the Agent Server instead (put this flag before METHOD)",
    )
    p.add_argument(
        "method",
        type=str.upper,
        choices=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        metavar="METHOD",
    )
    p.add_argument("path", metavar="PATH")
    add_body(p)
    p.add_argument("--data", help="raw request body (string)")
    p.add_argument("--data-file", help="raw request body from a file (tarball uploads)")
    p.add_argument("--content-type", help="Content-Type for --data/--data-file")
    p.add_argument("--query", action="append", metavar="K=V")
    p.add_argument("--header", action="append", metavar="'Name: value'")
    p.add_argument(
        "--file", action="append", metavar="FIELD=PATH", help="multipart file part"
    )
    p.add_argument(
        "--form", action="append", metavar="K=V", help="multipart form field"
    )
    p.add_argument(
        "--auth",
        choices=AUTH_MODES,
        default="key",
        help="key (X-Session-API-Key), bearer, none, bad, bearer-bad, previous",
    )
    add_assertions(p)
    p.add_argument(
        "--check-header",
        nargs="+",
        action="append",
        metavar="ARG",
        help="NAME OP [VALUE] on a response header",
    )
    p.add_argument("--print-header", metavar="NAME", help="print one response header")
    p.add_argument("--all-headers", action="store_true")
    p.add_argument(
        "--until-ok",
        type=float,
        default=0,
        metavar="SECONDS",
        help="retry until --expect and --check hold",
    )
    p.add_argument(
        "--raw-out", metavar="PATH", help="write the raw response body to a file"
    )
    p.add_argument("--max-chars", type=int, default=20000)
    p.add_argument("--timeout", type=float, default=60)

    llm = commands.add_parser(
        "llm",
        help="LLM profiles on the Agent Server",
        description="llm: preset | set | show",
        formatter_class=Formatter,
        epilog=f"Examples:\n  {CLI} llm preset deepseek\n  {CLI} llm show",
    )
    add_run_flag(llm)
    llm_sub = llm.add_subparsers(dest="llm_command", metavar="<preset|set|show>")
    p = sub(
        llm_sub,
        "preset",
        "Save deepseek-flash (active) and deepseek-pro profiles from $DEEPSEEK_API_KEY.",
        f"  {CLI} llm preset deepseek",
        cmd_llm_preset,
    )
    p.add_argument("preset", choices=sorted(PRESETS))
    add_key_source(p)
    p.add_argument("--no-validate", action="store_true")
    p = sub(
        llm_sub,
        "set",
        "Save one LLM profile from an env key.",
        f"  {CLI} llm set --profile qa --model deepseek/deepseek-flash --api-key-env DEEPSEEK_API_KEY",
        cmd_llm_set,
    )
    p.add_argument("--profile", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--base-url")
    add_key_source(p)
    p.add_argument("--no-activate", action="store_true")
    p.add_argument("--no-validate", action="store_true")
    sub(
        llm_sub,
        "show",
        "List Agent Server profiles and the active one.",
        f"  {CLI} llm show",
        cmd_llm_show,
    )

    rn = commands.add_parser(
        "run",
        help="automation runs",
        description="run: wait",
        formatter_class=Formatter,
        epilog=f"Examples:\n  {CLI} run wait $AID --run-id $RID --status COMPLETED --timeout 900",
    )
    add_run_flag(rn)
    rn_sub = rn.add_subparsers(dest="run_command", metavar="<wait>")
    p = sub(
        rn_sub,
        "wait",
        "Poll GET /v1/{id}/runs until a run reaches a status (default: any terminal one).",
        f"""\
  {CLI} run wait $AID --run-id $RID --status RUNNING --timeout 60
  {CLI} run wait $AID --run-id $RID --status COMPLETED --timeout 900 --check conversation_id exists --save F07.preset/run
  {CLI} run wait $AID --status PENDING --timeout 150          # newest run, e.g. a cron fire
  CID=$({CLI} run wait $AID --run-id $RID --status COMPLETED --field conversation_id)""",
        cmd_run_wait,
    )
    p.add_argument("automation_id")
    p.add_argument("--run-id", help="the run to follow (default: the newest run)")
    p.add_argument("--status", help="comma-separated statuses to wait for")
    p.add_argument("--timeout", type=float, default=300)
    p.add_argument("--interval", type=float, default=2)
    p.add_argument(
        "--keep-waiting",
        action="store_true",
        help="do not stop at a terminal status that is not wanted",
    )
    p.add_argument(
        "--check",
        nargs="+",
        action="append",
        metavar="ARG",
        help="FIELD OP [VALUE] on the run object",
    )
    p.add_argument("--field", help="print one field of the run")
    p.add_argument("--save", metavar="FEATURE/NAME")

    fx = commands.add_parser(
        "fixture",
        help="run-owned test inputs",
        description="fixture: tarball | automation | git-remote | http-sink",
        formatter_class=Formatter,
        epilog=f"Examples:\n  {CLI} fixture tarball --kind callback --upload --field tarball_path\n  {CLI} fixture automation --kind echo --field id",
    )
    add_run_flag(fx)
    fx_sub = fx.add_subparsers(
        dest="kind_cmd", metavar="<tarball|automation|git-remote|http-sink>"
    )
    p = sub(
        fx_sub,
        "tarball",
        f"Build a stdlib-only automation tarball (entrypoint `bash run.sh`); kinds: {', '.join(TARBALL_KINDS)}.",
        f"""\
  TP=$({CLI} fixture tarball --kind callback --upload --field tarball_path)
  {CLI} fixture tarball --kind sleep --field path""",
        cmd_fixture,
    )
    p.add_argument("--kind", choices=sorted(TARBALL_KINDS), required=True)
    p.add_argument("--name")
    p.add_argument("--setup", help="setup.sh content to include")
    p.add_argument("--upload", action="store_true", help="also POST it to /v1/uploads")
    p.add_argument("--field", help="print one field (path, tarball_path, upload.id)")
    p = sub(
        fx_sub,
        "automation",
        "Upload a tarball fixture and create an automation from it (cron by default).",
        f"""\
  AID=$({CLI} fixture automation --kind callback --field id)
  AID=$({CLI} fixture automation --kind echo --cron '* * * * *' --field id)
  AID=$({CLI} fixture automation --kind env --event-source qa-orders --on 'order.*' --field id)""",
        cmd_fixture,
    )
    p.add_argument("--kind", choices=sorted(TARBALL_KINDS), required=True)
    p.add_argument("--name")
    p.add_argument("--setup", help="setup.sh content to include")
    p.add_argument(
        "--cron",
        default="0 0 1 1 *",
        help="cron schedule (default: yearly, so it never fires during a run)",
    )
    p.add_argument("--timezone", default="UTC")
    p.add_argument("--event-source", help="make it an event automation for this source")
    p.add_argument("--on", help="event key pattern (default *)")
    p.add_argument("--filter", help="JMESPath filter")
    p.add_argument("--timeout", type=int, help="automation timeout in seconds")
    p.add_argument("--extra-json", help="extra create-body fields as JSON")
    p.add_argument("--field", help="print one field (id, automation.state)")
    p = sub(
        fx_sub,
        "git-remote",
        "Create a bare git repo (and a work clone) for git sync.",
        f"  URL=$({CLI} fixture git-remote --name sync --field url)",
        cmd_fixture,
    )
    p.add_argument("--name", default="sync")
    p.add_argument("--field")
    p = sub(
        fx_sub,
        "http-sink",
        "Start a local HTTP server that records requests (and can serve a tarball).",
        f"""\
  {CLI} fixture http-sink --name tarballs --serve-tarball callback --field tarball_url
  {CLI} fixture http-sink --name broken --status 404""",
        cmd_fixture,
    )
    p.add_argument("--name", required=True)
    p.add_argument("--status", type=int, default=200)
    p.add_argument("--body")
    p.add_argument(
        "--serve-tarball",
        choices=sorted(TARBALL_KINDS),
        help="serve this tarball kind at /<kind>.tar.gz",
    )
    p.add_argument("--field")

    evt = commands.add_parser(
        "event",
        help="signed webhook deliveries",
        description="event: send | requested-types",
        formatter_class=Formatter,
        epilog=f'Examples:\n  {CLI} event send qa-orders --secret "$SECRET" --json \'{{"type":"order.created"}}\' --expect 200',
    )
    add_run_flag(evt)
    evt_sub = evt.add_subparsers(dest="event_command", metavar="<send|requested-types>")
    p = sub(
        evt_sub,
        "send",
        "POST a signed delivery to /v1/events/{org}/{source} (no API key: the signature is the auth).",
        f"""\
  {CLI} event send qa-orders --secret "$SECRET" --json '{{"type":"order.created","amount":500}}' --expect 200 --check matched eq 1
  {CLI} event send qa-orders --secret "$SECRET" --tamper --json '{{"type":"x"}}' --expect 401
  {CLI} event send qa-std --scheme standard_webhooks --secret "$SECRET" --age 600 --json '{{"type":"x"}}' --expect 401
  {CLI} event send github --builtin --delivery-id X-GitHub-Delivery=d-1 --json-file pr_opened.json --expect 200""",
        cmd_event_send,
    )
    p.add_argument("source")
    add_body(p)
    p.add_argument("--data", help="raw body instead of JSON (e.g. not-json)")
    p.add_argument(
        "--org", help=f"org id in the URL (default: the local org {LOCAL_ORG_ID})"
    )
    p.add_argument("--secret", help="webhook secret (test secrets only)")
    p.add_argument("--secret-file")
    p.add_argument("--scheme", choices=SCHEMES, default="hmac_sha256_hex")
    p.add_argument(
        "--signature-header",
        help="default X-Signature-256 (X-Slack-Signature for slack_v0, X-Hub-Signature-256 built-in)",
    )
    p.add_argument(
        "--builtin",
        action="store_true",
        help="built-in source: wrap as {payload: ...}, sign with AUTOMATION_WEBHOOK_SECRET",
    )
    p.add_argument("--event-id", help="standard-webhooks webhook-id (default random)")
    p.add_argument(
        "--delivery-id",
        metavar="HEADER=VALUE",
        help="delivery id header used for dedupe",
    )
    p.add_argument(
        "--age", type=int, help="sign with a timestamp this many seconds old"
    )
    p.add_argument("--tamper", action="store_true", help="corrupt the signature")
    p.add_argument(
        "--bare-hex", action="store_true", help="hmac without the sha256= prefix"
    )
    p.add_argument("--unsigned", action="store_true", help="send no signature")
    p.add_argument(
        "--drop-header",
        action="append",
        metavar="NAME",
        help="remove a header after signing",
    )
    p.add_argument("--header", action="append", metavar="'Name: value'")
    p.add_argument("--timeout", type=float, default=60)
    add_assertions(p)
    p = sub(
        evt_sub,
        "requested-types",
        "GET /v1/events/{source}/requested-types, signed over the source name.",
        f"""\
  {CLI} event requested-types github --expect 200 --check event_types contains pull_request
  {CLI} event requested-types github --tamper --expect 401""",
        cmd_event_requested_types,
    )
    p.add_argument("source")
    p.add_argument("--secret", help="default: the run's AUTOMATION_WEBHOOK_SECRET")
    p.add_argument("--tamper", action="store_true")
    p.add_argument("--unsigned", action="store_true")
    add_assertions(p)

    kv = commands.add_parser(
        "kv",
        help="KV store helpers",
        description="kv: token",
        formatter_class=Formatter,
        epilog=f"Examples:\n  TOKEN=$({CLI} kv token --automation-id $AID)",
    )
    add_run_flag(kv)
    kv_sub = kv.add_subparsers(dest="kv_command", metavar="<token>")
    p = sub(
        kv_sub,
        "token",
        "Print an HS256 KV JWT signed with the run's AUTOMATION_KV_SECRET (what runs get as AUTOMATION_KV_TOKEN).",
        f"""\
  TOKEN=$({CLI} kv token --automation-id $AID)
  BAD=$({CLI} kv token --automation-id $AID --expired)
  BAD=$({CLI} kv token --automation-id $AID --wrong-secret)""",
        cmd_kv_token,
    )
    p.add_argument(
        "--automation-id", help="the automation_id claim (omit to test a missing claim)"
    )
    p.add_argument("--run-id")
    p.add_argument("--expired", action="store_true")
    p.add_argument("--no-exp", action="store_true")
    p.add_argument("--wrong-secret", action="store_true")

    st = commands.add_parser(
        "state",
        help="read-only views of the run's DB and files",
        description="state: sql | ls | cat",
        formatter_class=Formatter,
        epilog=f'Examples:\n  {CLI} state sql "SELECT status FROM automation_runs"',
    )
    add_run_flag(st)
    st_sub = st.add_subparsers(dest="state_command", metavar="<sql|ls|cat>")
    p = sub(
        st_sub,
        "sql",
        "Run a read-only SQL query against the run's SQLite database.",
        f"""\
  {CLI} state sql "SELECT count(*) AS n FROM automation_kv" --check rows.0.n eq 1
  {CLI} state sql "SELECT value_encrypted FROM automation_kv LIMIT 1" --raw --check rows.0.value_encrypted matches '^gAAAAA'""",
        cmd_state_sql,
    )
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--check", nargs="+", action="append", metavar="ARG")
    p.add_argument("--field")
    p.add_argument(
        "--raw",
        action="store_true",
        help="print stored values unredacted (first 80 chars)",
    )
    p.add_argument(
        "--save", metavar="FEATURE/NAME", help="save the rows (redacted) as evidence"
    )
    p = sub(
        st_sub,
        "ls",
        "List files under the run directory (e.g. a run's workspace).",
        f"""\
  {CLI} state ls workspaces/automation-runs/$RID --expect run.sh
  {CLI} state ls fixtures/sync-work/automations --recursive""",
        cmd_state_ls,
    )
    p.add_argument("path")
    p.add_argument("--recursive", action="store_true")
    p.add_argument(
        "--expect", action="append", metavar="NAME", help="an entry that must exist"
    )
    p.add_argument("--expect-empty", action="store_true")
    p.add_argument("--expect-exists", action="store_true")
    p.add_argument("--limit", type=int, default=200)
    p = sub(
        st_sub,
        "cat",
        "Show a file under the run directory, with assertions.",
        f"""\
  {CLI} state cat workspaces/automation-runs/$RID/qa-env.json --check AUTOMATION_TRIGGER_SOURCE eq manual
  {CLI} state cat workspaces/automation-runs/$RID/hello.txt --contains hi""",
        cmd_state_cat,
    )
    p.add_argument("path")
    p.add_argument("--check", nargs="+", action="append", metavar="ARG")
    p.add_argument("--contains", action="append")
    p.add_argument("--not-contains", action="append")
    p.add_argument("--field")
    p.add_argument("--max-chars", type=int, default=4000)

    cv = commands.add_parser(
        "conversation",
        help="a run's conversation on the Agent Server",
        description="conversation: events",
        formatter_class=Formatter,
        epilog=f"Examples:\n  {CLI} conversation events $CID --expect-tool finish",
    )
    add_run_flag(cv)
    cv_sub = cv.add_subparsers(dest="conversation_command", metavar="<events>")
    p = sub(
        cv_sub,
        "events",
        "Summarize a conversation's events (kinds, tools) from the Agent Server.",
        f"""\
  {CLI} conversation events $CID --expect-kind ActionEvent --expect-tool finish --save F16.events/events""",
        cmd_conversation_events,
    )
    p.add_argument("conversation_id")
    p.add_argument("--expect-kind", action="append")
    p.add_argument("--expect-tool", action="append")
    p.add_argument("--contains", action="append")
    p.add_argument("--show", type=int, default=0)
    p.add_argument("--max-events", type=int, default=1000)
    p.add_argument("--save", metavar="FEATURE/NAME")

    sk = commands.add_parser(
        "sink",
        help="HTTP sink captures",
        description="sink: read",
        formatter_class=Formatter,
        epilog=f"Examples:\n  {CLI} sink read telemetry --expect-min 1",
    )
    add_run_flag(sk)
    sk_sub = sk.add_subparsers(dest="sink_command", metavar="<read>")
    p = sub(
        sk_sub,
        "read",
        "What an HTTP sink received.",
        f"  {CLI} sink read telemetry --wait 20 --expect-min 1 --contains automation",
        cmd_sink_read,
    )
    p.add_argument("name")
    p.add_argument("--path")
    p.add_argument(
        "--contains",
        action="append",
        help="keep requests containing this text (repeatable, all must match)",
    )
    p.add_argument("--wait", type=float, default=0)
    p.add_argument("--expect-min", type=int)
    p.add_argument("--expect-max", type=int)
    p.add_argument("--show", type=int, default=3)
    p.add_argument("--save", metavar="FEATURE/NAME")

    hp = commands.add_parser(
        "helper",
        help="recipe-local background processes",
        description="helper: start | stop",
        formatter_class=Formatter,
        epilog=f"Examples:\n  {CLI} helper start --name web -- python3 -m http.server 8123",
    )
    add_run_flag(hp)
    hp_sub = hp.add_subparsers(dest="helper_command", metavar="<start|stop>")
    p = sub(
        hp_sub,
        "start",
        "Start a background process owned by the run.",
        f"  {CLI} helper start --name web -- python3 -m http.server 8123",
        cmd_helper_start,
    )
    p.add_argument("--name", required=True)
    p.add_argument("--settle", type=float, default=1.0)
    p.add_argument("command", nargs=argparse.REMAINDER)
    p = sub(
        hp_sub,
        "stop",
        "Stop helpers by name.",
        f"  {CLI} helper stop web",
        cmd_helper_stop,
    )
    p.add_argument("names", nargs="+")

    p = sub(
        commands,
        "assert",
        "Check a value a recipe computed (a variable, a JSON file, stdin).",
        f"""\
  {CLI} assert --value "$N" --check . eq 2
  {CLI} assert --json "$F/run.json" --check status eq COMPLETED
  some-program | {CLI} assert --check items len-eq 2

Prints the value (parsed as JSON when it is JSON) and the --check results like
`api --check`, and exits 1 when a check fails. Use it instead of `test` or
`jq -e` on a `# bug` line, so the marker can name the value the bug produces:
`# bug: value eq 404`.""",
        cmd_assert,
    )
    source = p.add_mutually_exclusive_group()
    source.add_argument("--value", help="the value itself (JSON or text)")
    source.add_argument("--json", metavar="FILE", help="a JSON file")
    p.add_argument("--check", action="append", nargs="+", metavar="FIELD OP [VALUE]")

    p = sub(
        commands,
        "exec",
        "Run a program with AUTOMATION_URL, AUTOMATION_API_URL, AUTOMATION_API_KEY, AGENT_SERVER_URL set.",
        f"""\
  {CLI} exec --expect-output '"status"' -- sh -c 'curl -s "$AUTOMATION_API_URL/health"'""",
        cmd_exec,
    )
    p.add_argument("--env", action="append", metavar="K=V")
    p.add_argument("--cwd")
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--expect-output", action="append")
    p.add_argument("--reject-output", action="append")
    p.add_argument("--tail-chars", type=int, default=1500)
    p.add_argument("--save", metavar="FEATURE/NAME")
    p.add_argument("command", nargs=argparse.REMAINDER)

    sub(
        commands,
        "capabilities",
        "What this machine can drive, and which families/recipes are blocked.",
        f"""\
  {CLI} capabilities

A family or recipe declares what it needs on a `Needs:` line
({", ".join(NEEDS)}); `map run` reports those recipes as blocked.""",
        cmd_capabilities,
    )

    ev = commands.add_parser(
        "evidence",
        help="the evidence ledger",
        description="Evidence: add | report",
        formatter_class=Formatter,
        epilog=f"""Examples:
  {CLI} evidence add --feature F05.create --result pass --entry 'POST /v1' --expected 201 --actual 201 --artifact evidence/F05.create/response.json
  {CLI} evidence report --out report.md""",
    )
    add_run_flag(ev)
    ev_sub = ev.add_subparsers(dest="evidence_command", metavar="<add|report>")
    ea = ev_sub.add_parser(
        "add",
        help="append a ledger row",
        formatter_class=Formatter,
        epilog=f"Examples:\n  {CLI} evidence add --feature F01.health --result pass --entry 'GET /health' --expected 200 --actual 200",
    )
    add_run_flag(ea)
    ea.set_defaults(func=cmd_evidence_add)
    ea.add_argument("--feature", required=True)
    ea.add_argument("--result", required=True, choices=RESULTS)
    ea.add_argument("--entry", required=True)
    ea.add_argument("--expected", required=True)
    ea.add_argument("--actual", required=True)
    ea.add_argument("--artifact", action="append")
    ea.add_argument("--note")
    ea.add_argument("--allow-unknown", action="store_true")
    er = ev_sub.add_parser(
        "report",
        help="latest result per sub-feature",
        formatter_class=Formatter,
        epilog=f"Examples:\n  {CLI} evidence report --out report.md --print-markdown",
    )
    add_run_flag(er)
    er.set_defaults(func=cmd_evidence_report)
    er.add_argument("--runs", nargs="+")
    er.add_argument("--out")
    er.add_argument("--print-markdown", action="store_true")

    mp = commands.add_parser(
        "map",
        help="feature map tooling",
        description="Map: run | check | coverage | ids | routes | diff | owners",
        formatter_class=Formatter,
        epilog=f"""Examples:
  {CLI} map check
  {CLI} map coverage
  {CLI} map run --file F05 --fresh --record
  {CLI} map run --all --fresh --record
  {CLI} map owners --changed origin/main...HEAD""",
    )
    mp_sub = mp.add_subparsers(
        dest="map_command", metavar="<run|check|coverage|ids|routes|diff|owners>"
    )
    mc = mp_sub.add_parser(
        "check",
        help="validate the map",
        formatter_class=Formatter,
        epilog=f"Examples:\n  {CLI} map check --fix-counts",
    )
    mc.set_defaults(func=cmd_map_check)
    mc.add_argument("--file")
    mc.add_argument("--fix-counts", action="store_true")
    mc.add_argument("--no-routes", action="store_true")
    mv = mp_sub.add_parser(
        "coverage",
        help="routes no family owns or drives",
        formatter_class=Formatter,
        epilog=f"Examples:\n  {CLI} map coverage",
    )
    mv.set_defaults(func=cmd_map_coverage)
    mv.add_argument("--checkout")
    mrun = mp_sub.add_parser(
        "run",
        help="execute a family's fenced sh blocks",
        formatter_class=Formatter,
        epilog=f"""Examples:
  {CLI} map run --file F05
  {CLI} map run --file F11 --only F11.filter --fresh --record
  {CLI} map run --all --fresh --record

Each bullet's ```sh blocks run in Bash with `set -eo pipefail`, from the
repository root, with this run exported. Variables assigned in one bullet are
carried into later bullets. A "known bug" bullet is an expected failure (xfail)
only when the command marked `# bug: PREDICATE` fails with the documented
failure: `PATH OP [VALUE]` clauses joined by `&&` and matched like --check
against the JSON the last control-automation call on that line printed
(`# bug: status eq 500`), or `exit N` as a last resort. Any other failure is a
fail; an unexpected pass, xpass, fails the run so the map gets updated. A
"blocked" bullet is not executed.""",
    )
    add_run_flag(mrun)
    mrun.set_defaults(func=cmd_map_run)
    mrun.add_argument("--file")
    mrun.add_argument("--fresh", action="store_true")
    mrun.add_argument("--all", action="store_true")
    mrun.add_argument("--checkout")
    mrun.add_argument("--repeat", type=int, default=1)
    mrun.add_argument("--only")
    mrun.add_argument("--keep-going", action="store_true")
    mrun.add_argument("--record", action="store_true")
    mrun.add_argument("--dry-run", action="store_true")
    mrun.add_argument("--timeout", type=float, default=1200, help="seconds per bullet")
    mi = mp_sub.add_parser(
        "ids",
        help="every sub-feature ID",
        formatter_class=Formatter,
        epilog=f"Examples:\n  {CLI} map ids --file F05",
    )
    mi.set_defaults(func=cmd_map_ids)
    mi.add_argument("--file")
    mr = mp_sub.add_parser(
        "routes",
        help="every route of the service",
        formatter_class=Formatter,
        epilog=f"Examples:\n  {CLI} map routes --module kv_router",
    )
    mr.set_defaults(func=cmd_map_routes)
    mr.add_argument("--module")
    mr.add_argument("--checkout")
    md = mp_sub.add_parser(
        "diff",
        help="routes added/removed/deprecated between checkouts",
        formatter_class=Formatter,
        epilog=f"Examples:\n  {CLI} map diff --base ../automation-base",
    )
    md.set_defaults(func=cmd_map_diff)
    md.add_argument("--base", required=True)
    md.add_argument("--target")
    mo = mp_sub.add_parser(
        "owners",
        help="map changed source paths to families",
        formatter_class=Formatter,
        epilog=f"Examples:\n  {CLI} map owners --changed $BASE..$TARGET\n  {CLI} map owners openhands/automation/kv_router.py",
    )
    mo.set_defaults(func=cmd_map_owners)
    mo.add_argument("paths", nargs="*")
    mo.add_argument("--changed", metavar="BASE..TARGET")
    return parser


def extract_run_flag(argv: list[str]) -> tuple[list[str], str | None]:
    """--run DIR may appear anywhere on the line."""
    out, run = [], None
    skip = False
    for i, token in enumerate(argv):
        if skip:
            skip = False
            continue
        if token == "--run" and i + 1 < len(argv):
            run = argv[i + 1]
            skip = True
        elif token.startswith("--run="):
            run = token.split("=", 1)[1]
        else:
            out.append(token)
    return out, run


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--" in raw:
        split = raw.index("--")
        head, tail = raw[:split], raw[split:]
    else:
        head, tail = raw, []
    head, run_flag = extract_run_flag(head)
    if head[:1] == ["_http-sink"]:
        serve_http_sink(
            int(head[1]), Path(head[2]), head[3] if len(head) > 3 and head[3] else None
        )
        return 0
    observation = os.environ.get(OBSERVATION_ENV)
    if observation:
        with contextlib.suppress(OSError):
            Path(observation).unlink()
    parser = build_parser()
    if not head:
        parser.print_help()
        return 0
    args = parser.parse_args(head + tail)
    args.run = run_flag
    if args.func is None:
        parser.parse_args([*head, "--help"])
        return 2
    try:
        result = args.func(args)
    except CliError as exc:
        payload: dict[str, Any] = {
            "ok": False,
            "exit": exc.exit_code,
            "error": str(exc),
        }
        if exc.hint:
            payload["hint"] = exc.hint
        if exc.example:
            payload["example"] = exc.example
        payload.update(exc.data)
        emit(payload)
        return exc.exit_code
    except json.JSONDecodeError as exc:
        emit({"ok": False, "exit": 2, "error": f"invalid JSON: {exc}"})
        return 2
    except KeyboardInterrupt:
        return 130
    except httpx.HTTPError as exc:
        emit(
            {
                "ok": False,
                "exit": 3,
                "error": f"{type(exc).__name__}: {exc}",
                "hint": f"Is the stack up? `{CLI} doctor`",
            }
        )
        return 3
    emit(result)
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
