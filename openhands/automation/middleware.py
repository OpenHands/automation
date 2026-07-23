"""ASGI middleware for the automations service."""

from dataclasses import dataclass

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send


TELEMETRY_DISTINCT_ID_HEADER = "x-openhands-telemetry-distinct-id"
CLIENT_SOURCE_HEADER = "x-openhands-client"
CLIENT_VERSION_HEADER = "x-openhands-client-version"


@dataclass(frozen=True)
class TelemetryRequestContext:
    frontend_distinct_id: str | None = None
    client_source: str | None = None
    client_version: str | None = None


def _clean_header(value: bytes | None, max_length: int = 256) -> str | None:
    if value is None:
        return None
    decoded = value.decode("latin-1").strip()
    if not decoded:
        return None
    return decoded[:max_length]


def build_telemetry_request_context(scope: Scope) -> TelemetryRequestContext:
    headers = dict(scope.get("headers") or [])
    return TelemetryRequestContext(
        frontend_distinct_id=_clean_header(
            headers.get(TELEMETRY_DISTINCT_ID_HEADER.encode("latin-1"))
        ),
        client_source=_clean_header(
            headers.get(CLIENT_SOURCE_HEADER.encode("latin-1"))
        ),
        client_version=_clean_header(
            headers.get(CLIENT_VERSION_HEADER.encode("latin-1"))
        ),
    )


class TelemetryContextMiddleware:
    """Extract best-effort frontend telemetry context from request headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope.setdefault("state", {})["telemetry_context"] = (
                build_telemetry_request_context(scope)
            )
        await self.app(scope, receive, send)


# Header names (lowercase) that carry an explicit API key. Cookie auth is
# deliberately excluded — cookie requests must stay on the strict policy.
_API_KEY_HEADERS = {"authorization", "x-session-api-key"}


class ApiKeyAwareCORSMiddleware:
    """CORS dispatcher that loosens the policy for credential-less requests.

    Requests that authenticate via API key (``Authorization: Bearer …`` or
    ``X-Session-API-Key``) get ``Access-Control-Allow-Origin: *`` with
    credentials disabled — the wildcard is safe because the browser cannot
    attach cookies when credentials are off, so the only way to authenticate
    is the explicit key. This lets API-key clients (e.g. the local
    agent-server GUI on ``localhost``) call the service directly from the
    browser without a server-side proxy hop.

    Cookie/session requests keep the strict origin allowlist with
    credentials enabled, exactly as before.
    """

    def __init__(self, app: ASGIApp, allow_origins: list[str]) -> None:
        self._permissive = CORSMiddleware(
            app,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        self._strict = CORSMiddleware(
            app,
            allow_origins=allow_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self._is_credentialless(scope):
            await self._permissive(scope, receive, send)
        else:
            await self._strict(scope, receive, send)

    @staticmethod
    def _is_credentialless(scope: Scope) -> bool:
        if scope["method"] == "OPTIONS":
            # Preflight: the auth header hasn't been sent yet, so look at the
            # headers the browser is asking permission to send. Parse the
            # comma-separated list into a set so we match whole header names
            # only — otherwise something like ``x-my-authorization-token``
            # would substring-match ``authorization``.
            for name, value in scope["headers"]:
                if name == b"access-control-request-headers":
                    requested_headers = {
                        h.strip() for h in value.decode("latin-1").lower().split(",")
                    }
                    return bool(requested_headers & _API_KEY_HEADERS)
            return False
        for name, value in scope["headers"]:
            if name == b"authorization" and value[:7].lower() == b"bearer ":
                return True
            if name == b"x-session-api-key":
                return True
        return False
