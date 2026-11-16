"""A local smart-HTTP git server that serves only clients holding its token.

It stands in for a private repository: anonymous and wrong-token requests get
401 with a Basic challenge, as GitHub, GitLab and Bitbucket Data Center answer.
"""

from __future__ import annotations

import base64
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **_GIT_ENV},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_bare_repo(root: Path, name: str, files: dict[str, str]) -> str:
    """Create ``root/name.git`` holding one commit of ``files``; return its SHA."""
    work = root / f"{name}-work"
    work.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=work)
    for relative, content in files.items():
        path = work / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git("add", "-A", cwd=work)
    _git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-q",
        "-m",
        "plugin",
        cwd=work,
    )
    _git("clone", "-q", "--bare", str(work), str(root / f"{name}.git"))
    return _git("rev-parse", "HEAD", cwd=work)


class PrivateGitServer:
    """Serves the bare repositories under ``root`` to clients with ``token``."""

    def __init__(self, root: Path, token: str) -> None:
        self.root = root
        self.token = token
        # (path, authorized) of every request, for assertions.
        self.requests: list[tuple[str, bool]] = []
        handler = type("_Handler", (_GitHandler,), {"git_server": self})
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> PrivateGitServer:
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class _GitHandler(BaseHTTPRequestHandler):
    git_server: PrivateGitServer

    def log_message(self, format, *args) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:  # noqa: N802
        self._serve()

    def do_POST(self) -> None:  # noqa: N802
        self._serve()

    def _authorized_user(self) -> str | None:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return None
        try:
            user, _, password = (
                base64.b64decode(header.removeprefix("Basic ")).decode().partition(":")
            )
        except ValueError:
            return None
        return user if password == self.git_server.token else None

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            body = b""
            while True:
                size = int(self.rfile.readline().strip(), 16)
                if size == 0:
                    self.rfile.readline()
                    return body
                body += self.rfile.read(size)
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _serve(self) -> None:
        path, _, query = self.path.partition("?")
        user = self._authorized_user()
        self.git_server.requests.append((path, user is not None))
        if user is None:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="private"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self._read_body()
        env = {
            **os.environ,
            **_GIT_ENV,
            "GIT_PROJECT_ROOT": str(self.git_server.root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "REQUEST_METHOD": self.command,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": user,
            "REMOTE_ADDR": "127.0.0.1",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "uploadpack.allowAnySHA1InWant",
            "GIT_CONFIG_VALUE_0": "true",
        }
        if encoding := self.headers.get("Content-Encoding"):
            env["HTTP_CONTENT_ENCODING"] = encoding
        if protocol := self.headers.get("Git-Protocol"):
            env["GIT_PROTOCOL"] = protocol
        output = subprocess.run(
            ["git", "http-backend"], input=body, env=env, capture_output=True
        ).stdout
        head, separator, payload = output.partition(b"\r\n\r\n")
        if not separator:
            head, _, payload = output.partition(b"\n\n")
        status = 200
        headers: list[tuple[str, str]] = []
        for raw_line in head.splitlines():
            key, _, value = raw_line.decode().partition(":")
            if not key:
                continue
            if key.lower() == "status":
                status = int(value.split()[0])
            else:
                headers.append((key, value.strip()))
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class SecretStoreServer:
    """The agent server's ``/api/settings/secrets/{name}`` route, on its own.

    It answers only requests carrying ``session_key``; an unknown name is 404,
    as on the agent server.
    """

    def __init__(self, session_key: str, secrets: dict[str, str]) -> None:
        self.session_key = session_key
        self.secrets = secrets
        self.requests: list[str] = []
        handler = type("_Handler", (_SecretHandler,), {"store": self})
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> SecretStoreServer:
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class _SecretHandler(BaseHTTPRequestHandler):
    store: SecretStoreServer

    def log_message(self, format, *args) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:  # noqa: N802
        self.store.requests.append(self.path)
        prefix = "/api/settings/secrets/"
        name = self.path.removeprefix(prefix)
        if self.headers.get("X-Session-API-Key") != self.store.session_key:
            status, body = 401, b""
        elif not self.path.startswith(prefix) or name not in self.store.secrets:
            status, body = 404, b'{"detail": "Secret not found"}'
        else:
            status, body = 200, self.store.secrets[name].encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
