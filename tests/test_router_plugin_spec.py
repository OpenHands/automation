import json
from pathlib import Path

import pytest


def write_plugin(root: Path) -> Path:
    plugin = root / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    (plugin / "scripts" / "main.py").write_text("print('hello')\n")
    (plugin / "automation-plugin.json").write_text(
        json.dumps(
            {
                "schema_version": "openhands.automation.plugin.v1",
                "name": "hello-world-automation",
                "entrypoint": {"command": "python3 scripts/main.py"},
                "runtime": {
                    "agent_profile": {
                        "required": True,
                        "default_name": "agent-default",
                    },
                    "llm_profile": {"required": True, "default_name": "default"},
                },
                "config_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"message": {"type": "string"}},
                },
            }
        )
    )
    return plugin


@pytest.mark.asyncio
async def test_create_plugin_spec_automation(
    async_client, tmp_path, agent_profiles_api
):
    plugin = write_plugin(tmp_path)

    response = await async_client.post(
        "/v1/plugin-spec",
        json={
            "name": "Hello Plugin",
            "plugin": {"source": str(plugin)},
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Hello Plugin"
    assert data["tarball_path"] == "plugin-spec://runtime"
    assert data["entrypoint"] == "python3 scripts/main.py"
    assert data["plugin_spec"] == {
        "plugin": {"source": str(plugin)},
        "config": {},
    }
    assert data["agent_profile_id"] == agent_profiles_api.profile_id
    assert data["model"] is None


@pytest.mark.asyncio
async def test_create_plugin_spec_rejects_runtime_overrides(async_client, tmp_path):
    plugin = write_plugin(tmp_path)

    response = await async_client.post(
        "/v1/plugin-spec",
        json={
            "name": "Hello Plugin",
            "plugin": {"source": str(plugin)},
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            "config": {"message": "hi"},
            "agent_profile_name": "agent-default",
            "model": "default",
        },
    )

    assert response.status_code == 422
    assert "Extra inputs are not permitted" in response.text


@pytest.mark.asyncio
async def test_create_plugin_spec_validates_manifest_config_defaults(
    async_client, tmp_path, agent_profiles_api
):
    plugin = write_plugin(tmp_path)
    manifest = json.loads((plugin / "automation-plugin.json").read_text())
    manifest["config_schema"]["required"] = ["message"]
    (plugin / "automation-plugin.json").write_text(json.dumps(manifest))

    response = await async_client.post(
        "/v1/plugin-spec",
        json={
            "name": "Hello Plugin",
            "plugin": {"source": str(plugin)},
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
        },
    )

    assert response.status_code == 422
    assert "Plugin automation config is invalid" in response.text
