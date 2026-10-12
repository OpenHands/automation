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
async def test_create_plugin_spec_automation(async_client, tmp_path):
    plugin = write_plugin(tmp_path)

    response = await async_client.post(
        "/v1/plugin-spec",
        json={
            "name": "Hello Plugin",
            "plugin": {"source": str(plugin)},
            "config": {"message": "hi"},
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
        "config": {"message": "hi"},
        "agent_profile_name": "agent-default",
    }
    assert data["model"] == "default"


@pytest.mark.asyncio
async def test_create_plugin_spec_validates_config(async_client, tmp_path):
    plugin = write_plugin(tmp_path)

    response = await async_client.post(
        "/v1/plugin-spec",
        json={
            "name": "Hello Plugin",
            "plugin": {"source": str(plugin)},
            "config": {"message": 1},
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
        },
    )

    assert response.status_code == 422
    assert "Plugin automation config is invalid" in response.text
