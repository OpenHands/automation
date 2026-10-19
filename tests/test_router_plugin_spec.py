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


@pytest.fixture(autouse=True)
def allow_local_plugin_sources_in_router_tests(monkeypatch):
    monkeypatch.setattr(
        "openhands.automation.router._allow_local_plugin_sources",
        lambda: True,
    )


def test_preflight_manifest_preserves_retryable_plugin_source_error(monkeypatch):
    from contextlib import contextmanager

    from openhands.automation.plugin_specs import preflight_manifest
    from openhands.sdk.plugin import PluginSourceError

    @contextmanager
    def fail_preflight(**kwargs):
        raise PluginSourceError("source_unavailable", "GitHub unavailable")
        yield

    monkeypatch.setattr(
        "openhands.automation.plugin_specs.preflight_plugin", fail_preflight
    )

    with pytest.raises(Exception) as exc_info:
        preflight_manifest(AutomationPluginSource(source="github:org/plugin"))

    exc = exc_info.value
    assert getattr(exc, "code") == "source_unavailable"
    assert getattr(exc, "status_code") == 502
    assert "GitHub unavailable" in str(exc)


def write_plugin(root: Path) -> Path:
    plugin = root / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    (plugin / "scripts" / "main.py").write_text("print('hello')\n")
    automation = {
        "schema_version": "openhands.automation.plugin.v1",
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
    (plugin / ".plugin").mkdir()
    (plugin / ".plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "hello-world-automation",
                "version": "0.1.0",
                "description": "test plugin",
                "automation": automation,
            }
        )
    )
    (plugin / "automation-plugin.json").write_text(
        json.dumps({"name": "hello-world-automation", **automation})
    )
    return plugin


def write_conversation_plugin(root: Path) -> Path:
    plugin = root / "conversation-plugin"
    (plugin / "commands").mkdir(parents=True)
    (plugin / "commands" / "hello.md").write_text("Say hello from the automation.\n")
    (plugin / ".plugin").mkdir()
    automation = {
        "schema_version": "openhands.automation.plugin.v1",
        "entrypoint": {"runner": "conversation", "command": "hello"},
        "runtime": {
            "agent_profile": {
                "required": True,
                "default_name": "agent-default",
            },
        },
    }
    (plugin / ".plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "hello-world-automation",
                "version": "0.1.0",
                "description": "test plugin",
                "automation": automation,
            }
        )
    )
    (plugin / "automation-plugin.json").write_text(
        json.dumps({"name": "hello-world-automation", **automation})
    )
    return plugin


def _write_plugin_automation(plugin: Path, manifest: dict) -> None:
    (plugin / "automation-plugin.json").write_text(json.dumps(manifest))
    metadata = json.loads((plugin / ".plugin" / "plugin.json").read_text())
    metadata["automation"] = {
        key: value for key, value in manifest.items() if key != "name"
    }
    (plugin / ".plugin" / "plugin.json").write_text(json.dumps(metadata))


def write_suggested_trigger_plugin(root: Path, suggested_trigger: dict) -> Path:
    plugin = write_conversation_plugin(root)
    data = json.loads((plugin / "automation-plugin.json").read_text())
    data["suggested_trigger"] = suggested_trigger
    _write_plugin_automation(plugin, data)
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
            "config": {"message": "hi"},
        },
        headers=agent_profiles_api.caller_auth,
    )

    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Hello Plugin"
    assert data["tarball_path"] == "plugin-spec://runtime"
    assert (
        data["entrypoint"] == 'cd "$AUTOMATION_PLUGIN_ROOT" && python3 scripts/main.py'
    )
    assert data["plugin_spec"] == {
        "plugin": {"source": str(plugin)},
        "config": {"message": "hi"},
    }
    assert data["agent_profile_id"] == agent_profiles_api.profile_id
    assert data["model"] is None


@pytest.mark.asyncio
async def test_create_plugin_spec_uses_manifest_suggested_trigger(
    async_client, tmp_path, agent_profiles_api
):
    plugin = write_suggested_trigger_plugin(
        tmp_path, {"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"}
    )

    response = await async_client.post(
        "/api/automation/v1/plugin-spec",
        json={"name": "Hello Plugin", "plugin": {"source": str(plugin)}},
        headers=agent_profiles_api.caller_auth,
    )

    assert response.status_code == 201
    data = response.json()
    assert data["trigger"] == {
        "type": "cron",
        "schedule": "0 9 * * *",
        "timezone": "UTC",
    }
    assert data["keep_alive"] is None


@pytest.mark.asyncio
async def test_create_plugin_spec_renders_suggested_trigger_template(
    async_client, tmp_path, agent_profiles_api
):
    plugin = write_plugin(tmp_path)
    manifest = json.loads((plugin / "automation-plugin.json").read_text())
    manifest["config_schema"] = {
        "type": "object",
        "required": ["repository"],
        "properties": {
            "repository": {"type": "string"},
            "label": {"type": "string", "default": "ready-for-review"},
        },
    }
    manifest["suggested_trigger_template"] = {
        "type": "event",
        "source": "github",
        "on": "pull_request.labeled",
        "filter": (
            "repository.full_name == {{ json config.repository }} && "
            "contains(pull_request.labels[].name, {{ json config.label }})"
        ),
    }
    _write_plugin_automation(plugin, manifest)

    response = await async_client.post(
        "/api/automation/v1/plugin-spec",
        json={
            "name": "Hello Plugin",
            "plugin": {"source": str(plugin)},
            "config": {"repository": "OpenHands/automation"},
        },
        headers=agent_profiles_api.caller_auth,
    )

    assert response.status_code == 201
    data = response.json()
    assert data["plugin_spec"]["config"] == {
        "repository": "OpenHands/automation",
        "label": "ready-for-review",
    }
    assert data["trigger"] == {
        "type": "event",
        "source": "github",
        "on": "pull_request.labeled",
        "filter": (
            'repository.full_name == "OpenHands/automation" && '
            'contains(pull_request.labels[].name, "ready-for-review")'
        ),
        "destination": "dispatch_run",
        "subject_key_expr": None,
        "turn_text_expr": None,
        "wake_agent": True,
    }


@pytest.mark.asyncio
async def test_create_plugin_spec_continue_conversation_defaults_keep_alive_true(
    async_client, tmp_path, agent_profiles_api
):
    plugin = write_suggested_trigger_plugin(
        tmp_path,
        {
            "type": "event",
            "source": "github",
            "on": "issue_comment.created",
            "destination": "continue_conversation",
            "subject_key_expr": "repository.full_name",
        },
    )

    response = await async_client.post(
        "/api/automation/v1/plugin-spec",
        json={"name": "Hello Plugin", "plugin": {"source": str(plugin)}},
        headers=agent_profiles_api.caller_auth,
    )

    assert response.status_code == 201
    data = response.json()
    assert data["trigger"]["destination"] == "continue_conversation"
    assert data["keep_alive"] is True


@pytest.mark.asyncio
async def test_create_plugin_spec_rejects_keep_alive_false_for_continue_conversation(
    async_client, tmp_path, agent_profiles_api
):
    plugin = write_suggested_trigger_plugin(
        tmp_path,
        {
            "type": "event",
            "source": "github",
            "on": "issue_comment.created",
            "destination": "continue_conversation",
            "subject_key_expr": "repository.full_name",
        },
    )

    response = await async_client.post(
        "/api/automation/v1/plugin-spec",
        json={
            "name": "Hello Plugin",
            "plugin": {"source": str(plugin)},
            "keep_alive": False,
        },
        headers=agent_profiles_api.caller_auth,
    )

    assert response.status_code == 400
    assert "keep_alive cannot be false" in response.text


@pytest.mark.asyncio
async def test_create_plugin_spec_rejects_unconfigured_suggested_event_source(
    async_client, tmp_path, agent_profiles_api
):
    plugin = write_suggested_trigger_plugin(
        tmp_path,
        {
            "type": "event",
            "source": "linear",
            "on": "Issue",
        },
    )

    response = await async_client.post(
        "/api/automation/v1/plugin-spec",
        json={"name": "Hello Plugin", "plugin": {"source": str(plugin)}},
        headers=agent_profiles_api.caller_auth,
    )

    assert response.status_code == 422
    assert "Event source `linear` is not configured" in response.text


@pytest.mark.asyncio
async def test_create_plugin_spec_conversation_runner_uses_stock_runner(
    async_client, tmp_path, agent_profiles_api
):
    plugin = write_conversation_plugin(tmp_path)

    response = await async_client.post(
        "/api/automation/v1/plugin-spec",
        json={
            "name": "Hello Plugin",
            "plugin": {"source": str(plugin)},
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
        },
        headers=agent_profiles_api.caller_auth,
    )

    assert response.status_code == 201
    data = response.json()
    assert data["tarball_path"] == "plugin-spec://runtime"
    assert data["setup_script_path"] == "setup.sh"
    assert data["entrypoint"].endswith(" main.py")
    assert data["plugin_spec"] == {
        "plugin": {"source": str(plugin)},
        "config": {},
    }
    assert data["agent_profile_id"] == agent_profiles_api.profile_id


@pytest.mark.asyncio
async def test_create_plugin_spec_rejects_runtime_overrides(async_client, tmp_path):
    plugin = write_plugin(tmp_path)

    response = await async_client.post(
        "/api/automation/v1/plugin-spec",
        json={
            "name": "Hello Plugin",
            "plugin": {"source": str(plugin)},
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
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
    _write_plugin_automation(plugin, manifest)

    response = await async_client.post(
        "/api/automation/v1/plugin-spec",
        json={
            "name": "Hello Plugin",
            "plugin": {"source": str(plugin)},
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
        },
        headers=agent_profiles_api.caller_auth,
    )

    assert response.status_code == 422
    assert "Plugin automation config is invalid" in response.text


@pytest.mark.asyncio
async def test_create_plugin_spec_applies_config_defaults(
    async_client, tmp_path, agent_profiles_api
):
    plugin = write_plugin(tmp_path)
    manifest = json.loads((plugin / "automation-plugin.json").read_text())
    manifest["config_schema"]["properties"]["message"]["default"] = "hello"
    _write_plugin_automation(plugin, manifest)

    response = await async_client.post(
        "/api/automation/v1/plugin-spec",
        json={
            "name": "Hello Plugin",
            "plugin": {"source": str(plugin)},
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
        },
        headers=agent_profiles_api.caller_auth,
    )

    assert response.status_code == 201
    assert response.json()["plugin_spec"]["config"] == {"message": "hello"}


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
    _write_plugin_automation(plugin_v2, manifest)

    created = await async_client.post(
        "/api/automation/v1/plugin-spec",
        json={
            "name": "Hello Plugin",
            "plugin": {"source": str(plugin_v1)},
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
        },
        headers=agent_profiles_api.caller_auth,
    )
    assert created.status_code == 201

    response = await async_client.patch(
        f"/api/automation/v1/{created.json()['id']}",
        json={"plugin": {"source": str(plugin_v2), "ref": "main"}},
        headers=agent_profiles_api.caller_auth,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["plugin_spec"] == {
        "plugin": {"source": str(plugin_v2), "ref": "main"},
        "config": {},
    }
    assert data["tarball_path"] == "plugin-spec://runtime"
    assert data["setup_script_path"] == "setup.sh"
    assert (
        data["entrypoint"]
        == 'cd "$AUTOMATION_PLUGIN_ROOT" && python3 scripts/main_v2.py'
    )
    assert data["agent_profile_id"] == agent_profiles_api.profile_id


@pytest.mark.asyncio
async def test_patch_plugin_spec_rejects_runtime_overrides(
    async_client, tmp_path, agent_profiles_api
):
    plugin = write_plugin(tmp_path)
    created = await async_client.post(
        "/api/automation/v1/plugin-spec",
        json={
            "name": "Hello Plugin",
            "plugin": {"source": str(plugin)},
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
        },
        headers=agent_profiles_api.caller_auth,
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
    _write_plugin_automation(plugin_v2, manifest)
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
    assert (
        fields["entrypoint"]
        == 'cd "$AUTOMATION_PLUGIN_ROOT" && python3 scripts/main_v2.py'
    )


def test_plugin_spec_update_fallback_rejects_profile_requirement_changes(tmp_path):
    plugin_v1 = write_plugin(tmp_path / "v1")
    plugin_v2 = write_plugin(tmp_path / "v2")
    manifest = json.loads((plugin_v2 / "automation-plugin.json").read_text())
    manifest["runtime"]["agent_profile"]["allowed_tools"] = ["terminal"]
    _write_plugin_automation(plugin_v2, manifest)
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
