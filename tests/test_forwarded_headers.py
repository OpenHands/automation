"""The service image must honor the ingress's forwarded headers.

`containers/Dockerfile` sets `FORWARDED_ALLOW_IPS` so Uvicorn installs
`ProxyHeadersMiddleware` with a trust-all peer list and `X-Forwarded-Proto:
https` survives into `request.url.scheme` — without it FastAPI's canonical
redirects answer with an `http://` `Location` behind the ingress (issue #245).

Two details make the setting easy to break silently, so they are pinned here:
the exact variable name Uvicorn reads, and the fact that Docker strips the
quotes around `'*'`. A literal `'*'` — quotes included — is not the wildcard,
it is an unmatchable host literal, and the middleware would trust nobody.
"""

import shlex
from pathlib import Path
from typing import Any, cast

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.types import ASGIApp
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "containers" / "Dockerfile"

# Any address outside the container network: the ingress is not a fixed peer,
# which is why the Dockerfile trusts every peer rather than a literal IP.
INGRESS_PEER = ("10.42.7.3", 54321)


def dockerfile_env(name: str) -> str:
    """Return the value Docker stores for an `ENV <name>=<value>` instruction.

    Docker removes the surrounding quotes, so `ENV FOO='*'` stores `*`.
    ``shlex`` unquotes the same way.
    """
    values = [
        assignment.split("=", 1)[1]
        for line in DOCKERFILE.read_text().splitlines()
        if line.startswith("ENV ")
        for assignment in shlex.split(line.removeprefix("ENV "))
        if assignment.split("=", 1)[0] == name
    ]
    assert len(values) == 1, f"expected exactly one ENV {name} in {DOCKERFILE}"
    return values[0]


def scheme_seen_by_the_app(trusted_hosts: str) -> str:
    """Report the scheme the app sees for an `X-Forwarded-Proto: https` request.

    A throwaway app keeps this to the proxy-header behavior alone.
    """
    test_app = FastAPI()

    @test_app.get("/scheme")
    def scheme(request: Request) -> dict[str, str]:
        return {"scheme": request.url.scheme}

    # Uvicorn and Starlette spell the same ASGI signature with different types,
    # so the wrapping needs a cast in each direction.
    proxied = ProxyHeadersMiddleware(cast(Any, test_app), trusted_hosts=trusted_hosts)
    with TestClient(cast(ASGIApp, proxied), client=INGRESS_PEER) as client:
        response = client.get("/scheme", headers={"X-Forwarded-Proto": "https"})

    response.raise_for_status()
    return response.json()["scheme"]


def test_dockerfile_trusts_forwarded_headers_from_every_peer() -> None:
    assert dockerfile_env("FORWARDED_ALLOW_IPS") == "*"


def test_uvicorn_reads_the_container_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Dockerfile's variable is the one Uvicorn's CMD picks up."""
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", dockerfile_env("FORWARDED_ALLOW_IPS"))

    config = uvicorn.Config("openhands.automation.app:app")

    assert config.proxy_headers is True
    assert config.forwarded_allow_ips == "*"


def test_forwarded_proto_reaches_the_app_as_the_request_scheme() -> None:
    assert scheme_seen_by_the_app(dockerfile_env("FORWARDED_ALLOW_IPS")) == "https"


@pytest.mark.parametrize("trusted_hosts", ["'*'", "127.0.0.1"])
def test_forwarded_proto_is_ignored_from_an_untrusted_peer(trusted_hosts: str) -> None:
    assert scheme_seen_by_the_app(trusted_hosts) == "http"
