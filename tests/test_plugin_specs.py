import json
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

from openhands.automation.plugin_specs import (
    AutomationPluginSource,
    PluginSpecError,
    build_execution_payload,
    fetch_manifest,
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
    (plugin / "automation-plugin.json").write_text(
        json.dumps(
            {
                "schema_version": "openhands.automation.plugin.v1",
                "name": "hello-world-automation",
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
        )
    )
    return plugin


def write_conversation_plugin(root: Path) -> Path:
    plugin = root / "conversation-plugin"
    (plugin / "commands").mkdir(parents=True)
    (plugin / "commands" / "hello.md").write_text("Say hello to $ARGUMENTS.\n")
    (plugin / ".plugin").mkdir()
    (plugin / ".plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "hello-world-automation",
                "version": "0.1.0",
                "description": "test plugin",
            }
        )
    )
    (plugin / "automation-plugin.json").write_text(
        json.dumps(
            {
                "schema_version": "openhands.automation.plugin.v1",
                "name": "hello-world-automation",
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
        )
    )
    return plugin


def test_fetch_manifest_and_build_execution_payload(tmp_path):
    plugin = write_plugin(tmp_path, setup_script="scripts/setup.sh")

    _, _, manifest = fetch_manifest(AutomationPluginSource(source=str(plugin)))
    assert manifest.name == "hello-world-automation"

    tarball, env, runtime = build_execution_payload(
        plugin_spec={"plugin": {"source": str(plugin)}, "config": {"message": "hi"}},
        agent_profile_id="profile-id",
        agent_profile_name="agent-default",
        llm_profile_name="default",
        work_dir="/tmp/automation-run",
    )

    assert env["AUTOMATION_AGENT_PROFILE_ID"] == "profile-id"
    assert env["AUTOMATION_AGENT_PROFILE_NAME"] == "agent-default"
    assert env["AUTOMATION_LLM_PROFILE_NAME"] == "default"
    assert (
        env["AUTOMATION_CONFIG_PATH"] == "/tmp/automation-run/.automation/config.json"
    )
    assert runtime["entrypoint"] == "python3 scripts/main.py"
    assert runtime["manifest"]["runtime"]["agent_profile"]["allowed_tools"] == [
        "terminal",
        "file_editor",
    ]

    assert runtime["manifest"]["name"] == "hello-world-automation"

    with tarfile.open(fileobj=BytesIO(tarball), mode="r:gz") as tar:
        names = set(tar.getnames())
        assert "automation-plugin.json" in names
        assert "scripts/main.py" in names
        assert "scripts/setup.sh" in names
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

    tarball, env, runtime = build_execution_payload(
        plugin_spec={"plugin": {"source": str(plugin)}, "config": {"message": "hi"}},
        agent_profile_id="profile-id",
        agent_profile_name="agent-default",
        llm_profile_name="default",
        work_dir="/tmp/automation-run",
    )

    assert runtime["runner"] == "conversation"
    assert runtime["entrypoint"].endswith(" main.py")
    assert (
        env["AUTOMATION_CONFIG_PATH"] == "/tmp/automation-run/.automation/config.json"
    )

    with tarfile.open(fileobj=BytesIO(tarball), mode="r:gz") as tar:
        names = set(tar.getnames())
        assert {
            "main.py",
            "finish_tool_hook.py",
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
        assert prompt_file is not None
        assert plugins_file is not None
        prompt = prompt_file.read().decode()
        plugins_config = json.loads(plugins_file.read())

    assert prompt == "/hello-world-automation:hello world"
    assert plugins_config == [{"source": str(plugin)}]


def test_conversation_runner_rejects_missing_plugin_command(tmp_path):
    plugin = write_conversation_plugin(tmp_path)
    (plugin / "commands" / "hello.md").unlink()

    with pytest.raises(
        PluginSpecError, match="Conversation runner command does not exist"
    ):
        fetch_manifest(AutomationPluginSource(source=str(plugin)))
