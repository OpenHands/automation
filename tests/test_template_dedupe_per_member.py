"""Template dedupe is per member, not per org (#750).

Two members of one org enabling the same catalog template each get their own
automation, owned by and running as them, built from what they sent. A member
enabling a template they already enabled still gets their own back with a 200.
"""

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from openhands.automation.app import app
from openhands.automation.auth import (
    AuthenticatedUser,
    AuthMethod,
    authenticate_request,
)
from openhands.automation.models import AutomationRun
from openhands.automation.utils.api_key import get_api_key_for_automation_run


ORG = uuid.UUID("87654321-4321-8765-4321-876543218765")
FIRST_MEMBER = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
SECOND_MEMBER = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")

FIRST = {"feeds": "https://first.example/rss", "schedule": "0 8 * * *"}
SECOND = {"feeds": "https://second.example/rss", "schedule": "30 17 * * *"}

BASE = "/api/automation/v1"


@pytest.fixture(autouse=True)
def allow_local_plugin_sources(monkeypatch):
    monkeypatch.setattr(
        "openhands.automation.router._allow_local_plugin_sources", lambda: True
    )


def sign_in(user_id: uuid.UUID) -> None:
    """Make the following requests come from ``user_id``, a member of ``ORG``."""
    user = AuthenticatedUser(
        user_id=user_id,
        org_id=ORG,
        email=f"{user_id}@example.com",
        role="member",
        permissions=["view_org_settings", "view_automations", "manage_automations"],
        auth_method=AuthMethod.API_KEY,
        api_key="test-api-key",
    )

    async def authenticate() -> AuthenticatedUser:
        return user

    app.dependency_overrides[authenticate_request] = authenticate


def _template(settings: dict[str, str]) -> dict[str, Any]:
    return {
        "id": "news-digest",
        "version": "1.1.0",
        "config": {"feeds": settings["feeds"]},
    }


def _cron(settings: dict[str, str]) -> dict[str, str]:
    return {"type": "cron", "schedule": settings["schedule"], "timezone": "UTC"}


def bundle(settings: dict[str, str], plugin: Path) -> tuple[str, dict[str, Any]]:
    return BASE, {
        "name": "Daily news digest",
        "trigger": _cron(settings),
        "tarball_path": "s3://bucket/news-digest.tar.gz",
        "entrypoint": "python3 main.py",
        "template": _template(settings),
    }


def prompt_preset(settings: dict[str, str], plugin: Path) -> tuple[str, dict[str, Any]]:
    return f"{BASE}/preset/prompt", {
        "name": "Daily news digest",
        "prompt": f"Write a digest of {settings['feeds']}.",
        "trigger": _cron(settings),
        "template": _template(settings),
    }


def plugin_preset(settings: dict[str, str], plugin: Path) -> tuple[str, dict[str, Any]]:
    return f"{BASE}/preset/plugin", {
        "name": "Daily news digest",
        "plugins": [{"source": "github:owner/news-plugin"}],
        "prompt": f"Write a digest of {settings['feeds']}.",
        "trigger": _cron(settings),
        "template": _template(settings),
    }


def plugin_spec(settings: dict[str, str], plugin: Path) -> tuple[str, dict[str, Any]]:
    return f"{BASE}/plugin-spec", {
        "name": "Daily news digest",
        "plugin": {"source": str(plugin)},
        "trigger": _cron(settings),
        "config": {"message": settings["feeds"]},
        "template": _template(settings),
    }


def write_plugin(root: Path) -> Path:
    plugin = root / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    (plugin / "scripts" / "main.py").write_text("print('digest')\n")
    (plugin / ".plugin").mkdir()
    (plugin / ".plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "news-digest",
                "version": "1.1.0",
                "description": "test plugin",
                "automation": {
                    "schema_version": "openhands.automation.plugin.v1",
                    "entrypoint": {"command": "python3 scripts/main.py"},
                    "runtime": {
                        "agent_profile": {
                            "required": True,
                            "default_name": "agent-default",
                        },
                    },
                    "config_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"message": {"type": "string"}},
                    },
                },
            }
        )
    )
    return plugin


Builder = Callable[[dict[str, str], Path], tuple[str, dict[str, Any]]]
ENDPOINTS = pytest.mark.parametrize(
    "build",
    [bundle, prompt_preset, plugin_preset, plugin_spec],
    ids=["bundle", "prompt-preset", "plugin-preset", "plugin-spec"],
)


@ENDPOINTS
async def test_second_member_gets_their_own_automation(
    async_client: httpx.AsyncClient, agent_profiles_api, tmp_path, build: Builder
) -> None:
    plugin = write_plugin(tmp_path)
    headers = agent_profiles_api.caller_auth

    sign_in(FIRST_MEMBER)
    path, body = build(FIRST, plugin)
    first = await async_client.post(path, json=body, headers=headers)
    assert first.status_code == 201, first.text

    sign_in(SECOND_MEMBER)
    path, body = build(SECOND, plugin)
    second = await async_client.post(path, json=body, headers=headers)

    assert second.status_code == 201, second.text
    created = second.json()
    assert created["id"] != first.json()["id"]
    assert created["user_id"] == str(SECOND_MEMBER)
    assert created["created_by_user_id"] == str(SECOND_MEMBER)
    assert created["trigger"]["schedule"] == SECOND["schedule"]
    assert created["preset_metadata"]["template"]["config"] == {
        "feeds": SECOND["feeds"]
    }

    # The first member's automation is left as it was.
    unchanged = await async_client.get(f"{BASE}/{first.json()['id']}")
    assert unchanged.status_code == 200
    assert unchanged.json()["user_id"] == str(FIRST_MEMBER)
    assert unchanged.json()["trigger"]["schedule"] == FIRST["schedule"]
    assert unchanged.json()["preset_metadata"]["template"]["config"] == {
        "feeds": FIRST["feeds"]
    }


@ENDPOINTS
async def test_same_member_enabling_again_gets_their_own_back(
    async_client: httpx.AsyncClient, agent_profiles_api, tmp_path, build: Builder
) -> None:
    plugin = write_plugin(tmp_path)
    headers = agent_profiles_api.caller_auth
    sign_in(SECOND_MEMBER)
    path, body = build(SECOND, plugin)
    first = await async_client.post(path, json=body, headers=headers)
    assert first.status_code == 201, first.text

    again = await async_client.post(path, json=body, headers=headers)

    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    listing = await async_client.get(BASE)
    assert listing.json()["total"] == 1


async def test_same_member_replays_across_endpoints(
    async_client: httpx.AsyncClient,
) -> None:
    """A template a member enabled as a prompt preset replays on the bundle path."""
    sign_in(FIRST_MEMBER)
    path, body = prompt_preset(FIRST, Path("."))
    first = await async_client.post(path, json=body)
    assert first.status_code == 201

    path, body = bundle(SECOND, Path("."))
    replay = await async_client.post(path, json=body)

    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]


async def test_members_still_manage_each_others_installs(
    async_client: httpx.AsyncClient,
) -> None:
    """Management stays org-scoped: list, get and delete see both installs."""
    sign_in(FIRST_MEMBER)
    first = await async_client.post(BASE, json=bundle(FIRST, Path("."))[1])
    sign_in(SECOND_MEMBER)
    second = await async_client.post(BASE, json=bundle(SECOND, Path("."))[1])
    assert (first.status_code, second.status_code) == (201, 201)

    listing = await async_client.get(BASE)
    assert listing.json()["total"] == 2
    assert (await async_client.get(f"{BASE}/{first.json()['id']}")).status_code == 200
    deleted = await async_client.delete(f"{BASE}/{first.json()['id']}")
    assert deleted.status_code == 204


async def test_second_members_run_mints_their_own_key(
    async_client: httpx.AsyncClient, async_session
) -> None:
    sign_in(FIRST_MEMBER)
    await async_client.post(BASE, json=bundle(FIRST, Path("."))[1])
    sign_in(SECOND_MEMBER)
    created = await async_client.post(BASE, json=bundle(SECOND, Path("."))[1])
    assert created.status_code == 201

    dispatched = await async_client.post(f"{BASE}/{created.json()['id']}/dispatch")
    assert dispatched.status_code == 201
    run = (
        await async_session.execute(
            select(AutomationRun)
            .where(AutomationRun.id == uuid.UUID(dispatched.json()["id"]))
            .options(selectinload(AutomationRun.automation))
        )
    ).scalar_one()

    minted_for: list[str] = []

    def openhands_api(request: httpx.Request) -> httpx.Response:
        minted_for.append(request.url.path)
        return httpx.Response(200, json={"key": "sk-oh-test"})

    real_client = httpx.AsyncClient
    with patch(
        "openhands.automation.utils.api_key.httpx.AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(openhands_api), **kwargs
        ),
    ):
        await get_api_key_for_automation_run(run)

    assert minted_for == [f"/api/service/users/{SECOND_MEMBER}/orgs/{ORG}/api-keys"]
