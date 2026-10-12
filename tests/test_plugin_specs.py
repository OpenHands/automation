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
        env["AUTOMATION_CONFIG_PATH"]
        == "/tmp/automation-run/.automation/config.json"
    )
    assert runtime["entrypoint"] == "python3 scripts/main.py"
    assert runtime["manifest"]["name"] == "hello-world-automation"

    with tarfile.open(fileobj=BytesIO(tarball), mode="r:gz") as tar:
        names = set(tar.getnames())
        assert "automation-plugin.json" in names
        assert "scripts/main.py" in names
        assert "scripts/setup.sh" in names
        assert "setup.sh" in names
        config = json.loads(tar.extractfile(".automation/config.json").read())
        execution = json.loads(tar.extractfile(".automation/execution.json").read())

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
