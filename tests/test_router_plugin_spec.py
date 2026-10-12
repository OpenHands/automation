import json
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from openhands.automation.models import Automation
from openhands.automation.plugin_specs import AutomationPluginSource
from openhands.automation.router import (
    _resolve_plugin_spec_update_fields_reusing_existing_profiles,
)


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
        "/api/automation/v1/plugin-spec",
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
        "/api/automation/v1/plugin-spec",
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
        "/api/automation/v1/plugin-spec",
        json={
            "name": "Hello Plugin",
            "plugin": {"source": str(plugin)},
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
        },
    )

    assert response.status_code == 422
    assert "Plugin automation config is invalid" in response.text


@pytest.mark.asyncio
async def test_patch_plugin_spec_updates_plugin_source(
    async_client, tmp_path, agent_profiles_api
):
    plugin_v1 = write_plugin(tmp_path / "v1")
    plugin_v2 = write_plugin(tmp_path / "v2")
    (plugin_v2 / "setup.sh").write_text("#!/usr/bin/env bash\n")
    manifest = json.loads((plugin_v2 / "automation-plugin.json").read_text())
    manifest["entrypoint"] = {
        "command": "python3 scripts/main_v2.py",
        "setup_script": "setup.sh",
    }
    (plugin_v2 / "automation-plugin.json").write_text(json.dumps(manifest))

    created = await async_client.post(
        "/api/automation/v1/plugin-spec",
        json={
            "name": "Hello Plugin",
            "plugin": {"source": str(plugin_v1)},
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
        },
    )
    assert created.status_code == 201

    response = await async_client.patch(
        f"/api/automation/v1/{created.json()['id']}",
        json={"plugin": {"source": str(plugin_v2), "ref": "main"}},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["plugin_spec"] == {
        "plugin": {"source": str(plugin_v2), "ref": "main"},
        "config": {},
    }
    assert data["tarball_path"] == "plugin-spec://runtime"
    assert data["setup_script_path"] == "setup.sh"
    assert data["entrypoint"] == "python3 scripts/main_v2.py"
    assert data["agent_profile_id"] == agent_profiles_api.profile_id


@pytest.mark.asyncio
async def test_patch_plugin_spec_rejects_runtime_overrides(async_client, tmp_path):
    plugin = write_plugin(tmp_path)
    created = await async_client.post(
        "/api/automation/v1/plugin-spec",
        json={
            "name": "Hello Plugin",
            "plugin": {"source": str(plugin)},
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
        },
    )
    assert created.status_code == 201

    response = await async_client.patch(
        f"/api/automation/v1/{created.json()['id']}",
        json={"plugin": {"source": str(plugin)}, "model": "default"},
    )

    assert response.status_code == 422
    assert "derive runtime fields from the plugin manifest" in response.text


@pytest.mark.asyncio
async def test_patch_plugin_source_rejects_non_plugin_spec_automation(
    async_client, tmp_path
):
    plugin = write_plugin(tmp_path)
    created = await async_client.post(
        "/api/automation/v1",
        json={
            "name": "Raw Automation",
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            "tarball_path": "https://example.com/code.tar.gz",
            "entrypoint": "python3 main.py",
        },
    )
    assert created.status_code == 201

    response = await async_client.patch(
        f"/api/automation/v1/{created.json()['id']}",
        json={"plugin": {"source": str(plugin)}},
    )

    assert response.status_code == 422
    assert "Only plugin-spec automations can update plugin source" in response.text


def test_plugin_spec_update_fallback_reuses_existing_profile(tmp_path):
    plugin_v1 = write_plugin(tmp_path / "v1")
    plugin_v2 = write_plugin(tmp_path / "v2")
    manifest = json.loads((plugin_v2 / "automation-plugin.json").read_text())
    manifest["entrypoint"] = {"command": "python3 scripts/main_v2.py"}
    (plugin_v2 / "automation-plugin.json").write_text(json.dumps(manifest))
    profile_id = uuid.uuid4()
    automation = Automation(
        plugin_spec={"plugin": {"source": str(plugin_v1)}, "config": {}},
        model=None,
        agent_profile_id=profile_id,
    )

    fields = _resolve_plugin_spec_update_fields_reusing_existing_profiles(
        plugin=AutomationPluginSource(source=str(plugin_v2)),
        automation=automation,
    )

    assert fields["plugin_spec"] == {
        "plugin": {"source": str(plugin_v2)},
        "config": {},
    }
    assert fields["agent_profile_id"] == profile_id
    assert fields["model"] is None
    assert fields["entrypoint"] == "python3 scripts/main_v2.py"


def test_plugin_spec_update_fallback_rejects_profile_requirement_changes(tmp_path):
    plugin_v1 = write_plugin(tmp_path / "v1")
    plugin_v2 = write_plugin(tmp_path / "v2")
    manifest = json.loads((plugin_v2 / "automation-plugin.json").read_text())
    manifest["runtime"]["agent_profile"]["allowed_tools"] = ["terminal"]
    (plugin_v2 / "automation-plugin.json").write_text(json.dumps(manifest))
    automation = Automation(
        plugin_spec={"plugin": {"source": str(plugin_v1)}, "config": {}},
        model=None,
        agent_profile_id=uuid.uuid4(),
    )

    with pytest.raises(HTTPException) as exc_info:
        _resolve_plugin_spec_update_fields_reusing_existing_profiles(
            plugin=AutomationPluginSource(source=str(plugin_v2)),
            automation=automation,
        )

    assert exc_info.value.status_code == 403
    assert "changes profile requirements" in exc_info.value.detail
