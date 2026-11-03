import json
import tarfile
from io import BytesIO
from pathlib import Path

import pytest
from pydantic import ValidationError

import openhands.automation.plugin_specs as plugin_specs
from openhands.automation.plugin_specs import (
    AutomationPluginSource,
    PluginSpecError,
    build_execution_payload,
    fetch_manifest,
    preflight_manifest,
    resolve_suggested_trigger,
    validate_plugin_config,
    validate_required_plugins,
)


def write_plugin(root: Path, *, setup_script: str | None = None) -> Path:
    plugin = root / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    (plugin / "scripts" / "main.py").write_text("print('hello')\n")
    if setup_script:
        (plugin / setup_script).parent.mkdir(parents=True, exist_ok=True)
        (plugin / setup_script).write_text("echo setup\n")
    entrypoint = {"command": "python3 scripts/main.py"}
    if setup_script:
        entrypoint["setup_script"] = setup_script
    automation = {
        "schema_version": "openhands.automation.plugin.v1",
        "entrypoint": entrypoint,
        "runtime": {
            "agent_profile": {
                "required": True,
                "default_name": "agent-default",
                "allowed_tools": ["terminal", "file_editor"],
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


def _write_automation_manifest(plugin: Path, manifest: dict) -> None:
    (plugin / "automation-plugin.json").write_text(json.dumps(manifest))
    plugin_manifest = json.loads((plugin / ".plugin" / "plugin.json").read_text())
    plugin_manifest["automation"] = {
        key: value for key, value in manifest.items() if key != "name"
    }
    (plugin / ".plugin" / "plugin.json").write_text(json.dumps(plugin_manifest))


def write_conversation_plugin(root: Path) -> Path:
    plugin = root / "conversation-plugin"
    (plugin / "commands").mkdir(parents=True)
    (plugin / "commands" / "hello.md").write_text("Say hello to $ARGUMENTS.\n")
    (plugin / ".plugin").mkdir()
    automation = {
        "schema_version": "openhands.automation.plugin.v1",
        "entrypoint": {
            "runner": "conversation",
            "command": "hello",
            "arguments": "world",
        },
        "runtime": {
            "agent_profile": {
                "required": True,
                "default_name": "agent-default",
                "allowed_tools": ["terminal"],
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


def write_merged_conversation_plugin(root: Path) -> Path:
    plugin = root / "merged-conversation-plugin"
    (plugin / "commands").mkdir(parents=True)
    (plugin / "commands" / "hello.md").write_text("Say hello.\n")
    (plugin / ".plugin").mkdir()
    (plugin / ".plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "hello-world-automation",
                "version": "0.1.0",
                "description": "test plugin",
                "automation": {
                    "schema_version": "openhands.automation.plugin.v1",
                    "entrypoint": {"runner": "conversation", "command": "hello"},
                    "suggested_trigger": {
                        "type": "cron",
                        "schedule": "0 9 * * *",
                        "timezone": "UTC",
                    },
                    "runtime": {
                        "agent_profile": {
                            "required": True,
                            "default_name": "agent-default",
                            "allowed_tools": ["terminal"],
                        },
                    },
                },
            }
        )
    )
    return plugin


def test_plugin_source_rejects_url_credentials():
    with pytest.raises(ValidationError, match="must not include credentials"):
        AutomationPluginSource(source="https://user:pass@github.com/acme/private.git")


def test_plugin_source_rejects_github_shorthand_credentials():
    with pytest.raises(ValidationError, match="must not include credentials"):
        AutomationPluginSource(source="github:user:token@acme/private")


def test_required_plugin_source_rejects_github_shorthand_credentials(tmp_path):
    plugin = write_conversation_plugin(tmp_path)
    manifest = json.loads((plugin / "automation-plugin.json").read_text())
    manifest["runtime"]["plugins"] = [{"source": "github:user:token@acme/private"}]

    with pytest.raises(ValidationError, match="must not include credentials"):
        plugin_specs.AutomationPluginManifest.model_validate(manifest)


def test_preflight_rejects_local_source_when_local_sources_disabled(tmp_path):
    plugin = write_plugin(tmp_path)

    with pytest.raises(
        PluginSpecError, match="Local plugin sources are not allowed in cloud mode"
    ):
        preflight_manifest(
            AutomationPluginSource(source=str(plugin)),
            allow_local_sources=False,
        )


def test_preflight_rejects_file_source_when_local_sources_disabled():
    with pytest.raises(
        PluginSpecError, match="file:// plugin sources are not allowed in cloud mode"
    ):
        preflight_manifest(
            AutomationPluginSource(source="file:///tmp/private-plugin"),
            allow_local_sources=False,
        )


def test_preflight_rejects_non_github_git_source_when_local_sources_disabled():
    with pytest.raises(
        PluginSpecError,
        match="Cloud plugin sources must use github:owner/repo",
    ):
        preflight_manifest(
            AutomationPluginSource(source="https://127.0.0.1/acme/private.git"),
            allow_local_sources=False,
        )


def test_required_plugins_reject_local_source_when_local_sources_disabled(tmp_path):
    plugin = write_plugin(tmp_path)
    required_plugin = write_plugin(tmp_path / "required")
    manifest_data = json.loads((plugin / "automation-plugin.json").read_text())
    manifest_data.setdefault("runtime", {})["plugins"] = [
        {"source": str(required_plugin)}
    ]
    _write_automation_manifest(plugin, manifest_data)
    _resolved_ref, manifest = preflight_manifest(
        AutomationPluginSource(source=str(plugin))
    )

    with pytest.raises(
        PluginSpecError, match="Local plugin sources are not allowed in cloud mode"
    ):
        validate_required_plugins(manifest.runtime.plugins, allow_local_sources=False)


def test_validate_plugin_config_applies_defaults(tmp_path):
    plugin = write_plugin(tmp_path)
    data = json.loads((plugin / "automation-plugin.json").read_text())
    data["config_schema"] = {
        "type": "object",
        "required": ["repository"],
        "additionalProperties": False,
        "properties": {
            "repository": {"type": "string"},
            "label": {"type": "string", "default": "ready-for-review"},
        },
    }
    _write_automation_manifest(plugin, data)

    _resolved_ref, manifest = preflight_manifest(
        AutomationPluginSource(source=str(plugin))
    )

    assert validate_plugin_config(manifest, {"repository": "OpenHands/automation"}) == {
        "repository": "OpenHands/automation",
        "label": "ready-for-review",
    }


def test_resolve_suggested_trigger_template_uses_normal_install_config(tmp_path):
    plugin = write_plugin(tmp_path)
    data = json.loads((plugin / "automation-plugin.json").read_text())
    data["config_schema"] = {
        "type": "object",
        "required": ["repository"],
        "properties": {
            "repository": {"type": "string"},
            "label": {"type": "string", "default": "ready-for-review"},
        },
    }
    data["suggested_trigger_template"] = {
        "type": "event",
        "source": "github",
        "on": "pull_request.labeled",
        "filter": (
            "repository.full_name == {{ json config.repository }} && "
            "contains(pull_request.labels[].name, {{ json config.label }})"
        ),
    }
    _write_automation_manifest(plugin, data)
    _resolved_ref, manifest = preflight_manifest(
        AutomationPluginSource(source=str(plugin))
    )
    config = validate_plugin_config(manifest, {"repository": "OpenHands/automation"})

    trigger = resolve_suggested_trigger(manifest, config)

    assert trigger is not None
    assert trigger.model_dump() == {
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


def test_fetch_manifest_and_build_execution_payload(tmp_path):
    plugin = write_plugin(tmp_path, setup_script="scripts/setup.sh")

    _, _, manifest = fetch_manifest(AutomationPluginSource(source=str(plugin)))
    assert manifest.name == "hello-world-automation"

    tarball, env, runtime, plugin_fetches = build_execution_payload(
        plugin_spec={"plugin": {"source": str(plugin)}, "config": {"message": "hi"}},
        agent_profile_id="profile-id",
        agent_profile_name="agent-default",
        llm_profile_name="default",
        work_dir="/tmp/automation-run",
    )

    assert plugin_fetches == [{"source": str(plugin)}]
    assert env["AUTOMATION_AGENT_PROFILE_ID"] == "profile-id"
    assert env["AUTOMATION_AGENT_PROFILE_NAME"] == "agent-default"
    assert env["AUTOMATION_LLM_PROFILE_NAME"] == "default"
    assert (
        env["AUTOMATION_CONFIG_PATH"] == "/tmp/automation-run/.automation/config.json"
    )
    assert (
        runtime["entrypoint"]
        == 'cd "$AUTOMATION_PLUGIN_ROOT" && python3 scripts/main.py'
    )
    assert runtime["manifest"]["runtime"]["agent_profile"]["allowed_tools"] == [
        "terminal",
        "file_editor",
    ]

    assert runtime["manifest"]["name"] == "hello-world-automation"

    with tarfile.open(fileobj=BytesIO(tarball), mode="r:gz") as tar:
        names = set(tar.getnames())
        assert "automation-plugin.json" not in names
        assert "scripts/main.py" not in names
        assert "scripts/setup.sh" not in names
        assert "setup.sh" in names
        config_file = tar.extractfile(".automation/config.json")
        execution_file = tar.extractfile(".automation/execution.json")
        assert config_file is not None
        assert execution_file is not None
        config = json.loads(config_file.read())
        execution = json.loads(execution_file.read())

    assert config == {"message": "hi"}
    assert execution["agent_profile_name"] == "agent-default"


def test_fetch_manifest_rejects_missing_entrypoint(tmp_path):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "automation-plugin.json").write_text(
        json.dumps(
            {
                "schema_version": "openhands.automation.plugin.v1",
                "name": "broken",
                "entrypoint": {"command": "./missing.py"},
            }
        )
    )

    with pytest.raises(PluginSpecError, match="Entrypoint target does not exist"):
        fetch_manifest(AutomationPluginSource(source=str(plugin)))


def test_stock_conversation_runner_packages_primary_plugin_command(tmp_path):
    plugin = write_conversation_plugin(tmp_path)

    _, _, manifest = fetch_manifest(AutomationPluginSource(source=str(plugin)))
    assert manifest.entrypoint.runner == "conversation"

    tarball, env, runtime, plugin_fetches = build_execution_payload(
        plugin_spec={"plugin": {"source": str(plugin)}, "config": {"message": "hi"}},
        agent_profile_id="profile-id",
        agent_profile_name="agent-default",
        llm_profile_name="default",
        work_dir="/tmp/automation-run",
    )

    assert plugin_fetches == [{"source": str(plugin)}]
    assert runtime["runner"] == "conversation"
    assert runtime["entrypoint"].endswith(" main.py")
    assert (
        env["AUTOMATION_CONFIG_PATH"] == "/tmp/automation-run/.automation/config.json"
    )

    with tarfile.open(fileobj=BytesIO(tarball), mode="r:gz") as tar:
        names = set(tar.getnames())
        assert {
            "main.py",
            "prompt_builder.py",
            "setup.sh",
            "prompt.txt",
            "plugins_config.json",
            ".automation/config.json",
            ".automation/execution.json",
            ".automation/plugins.json",
        }.issubset(names)
        assert "automation-plugin.json" not in names
        prompt_file = tar.extractfile("prompt.txt")
        plugins_file = tar.extractfile("plugins_config.json")
        main_file = tar.extractfile("main.py")
        assert prompt_file is not None
        assert plugins_file is not None
        assert main_file is not None
        prompt = prompt_file.read().decode()
        plugins_config = json.loads(plugins_file.read())
        main_source = main_file.read().decode()

    assert prompt == "/hello-world-automation:hello world"
    assert plugins_config == [{"source": str(plugin)}]
    assert "StartConversationRequest" in main_source
    assert "RemoteConversation.create" in main_source
    assert "get_default_agent" not in main_source
    assert "Conversation(**conversation_kwargs)" not in main_source
    assert "clone_repos" not in main_source
    assert "repos_config.json" not in main_source
    assert "AUTOMATION_LLM_PROFILE_NAME" not in main_source
    assert "AgentLaunchAdditions" not in main_source
    assert "agent_launch_additions" not in main_source
    assert "experiment_config.json" not in main_source
    assert "threading" not in main_source
    assert "conversation_title" not in main_source
    assert "report_phase" not in main_source


def test_stock_conversation_runner_uses_plugin_spec_preset(tmp_path, monkeypatch):
    plugin = write_conversation_plugin(tmp_path)
    sentinel_main = "print('plugin-spec conversation runner')\n"
    monkeypatch.setattr(
        plugin_specs,
        "_PLUGIN_SPEC_PRESET_CACHE",
        {
            "main.py": sentinel_main,
            "prompt_builder.py": (
                "def build_user_prompt(prompt, event_context):\n    return prompt\n"
            ),
            "setup.sh": "#!/usr/bin/env bash\n",
        },
    )

    tarball, _env, _runtime, _plugin_fetches = build_execution_payload(
        plugin_spec={"plugin": {"source": str(plugin)}, "config": {}},
        agent_profile_id="profile-id",
        agent_profile_name="agent-default",
        llm_profile_name=None,
        work_dir="/tmp/automation-run",
    )

    with tarfile.open(fileobj=BytesIO(tarball), mode="r:gz") as tar:
        main_file = tar.extractfile("main.py")
        assert main_file is not None
        assert main_file.read().decode() == sentinel_main


def test_conversation_runner_rejects_missing_plugin_command(tmp_path):
    plugin = write_conversation_plugin(tmp_path)
    (plugin / "commands" / "hello.md").unlink()

    with pytest.raises(
        PluginSpecError, match="Conversation runner command does not exist"
    ):
        fetch_manifest(AutomationPluginSource(source=str(plugin)))


def test_fetch_manifest_reads_automation_from_plugin_json(tmp_path):
    plugin = write_merged_conversation_plugin(tmp_path)

    _, _, manifest = fetch_manifest(AutomationPluginSource(source=str(plugin)))

    assert manifest.name == "hello-world-automation"
    assert manifest.description == "test plugin"
    assert manifest.entrypoint.runner == "conversation"
    assert manifest.entrypoint.command == "hello"
    assert manifest.suggested_trigger is not None
    assert manifest.suggested_trigger.type == "cron"
    assert manifest.suggested_trigger.schedule == "0 9 * * *"


def test_fetch_manifest_rejects_invalid_suggested_cron(tmp_path):
    plugin = write_merged_conversation_plugin(tmp_path)
    data = json.loads((plugin / ".plugin" / "plugin.json").read_text())
    data["automation"]["suggested_trigger"]["schedule"] = "0 0 31 2 *"
    (plugin / ".plugin" / "plugin.json").write_text(json.dumps(data))

    with pytest.raises(PluginSpecError, match="Invalid automation plugin manifest"):
        fetch_manifest(AutomationPluginSource(source=str(plugin)))
