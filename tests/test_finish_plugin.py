"""The plugin that makes an app-server-started preset run end with the finish tool.

The app server's start request takes plugins but no hook configuration, so the
presets' finish-tool-required hooks ride in a plugin the service writes into the
run's sandbox. These tests load it with the SDK's plugin loader and run its hooks.
"""

import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from openhands.automation.utils.finish_plugin import (
    FINISH_TOOL_REQUIRED_MESSAGE,
    finish_plugin_dir,
    finish_plugin_files,
)
from openhands.sdk.plugin import Plugin


@pytest.fixture
def written_plugin():
    run_id = str(uuid.uuid4())
    root = Path(finish_plugin_dir(run_id))
    for path, data in finish_plugin_files(run_id).items():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(data)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _run(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, timeout=10
    )


def test_the_plugin_lives_under_the_run_and_only_there():
    run_id = "4f1c0d2e-0000-4000-8000-000000000001"
    root = finish_plugin_dir(run_id)
    assert root == f"/tmp/automation-{run_id}-finish-required"
    assert set(finish_plugin_files(run_id)) == {
        f"{root}/.plugin/plugin.json",
        f"{root}/hooks/hooks.json",
    }
    # A run id cannot steer the path elsewhere.
    assert finish_plugin_dir("../../etc") == "/tmp/automation-______etc-finish-required"


def test_the_sdk_loads_its_hooks(written_plugin):
    plugin = Plugin.load(written_plugin)

    hooks = plugin.hooks
    assert hooks is not None
    assert [h.name for m in hooks.session_start for h in m.hooks] == [
        "reset-finish-tool-marker"
    ]
    assert [m.matcher for m in hooks.post_tool_use] == ["/(?:finish|FinishTool)/"]
    assert [h.name for m in hooks.stop for h in m.hooks] == ["require-finish-tool"]


def test_stop_is_refused_until_the_finish_tool_ran(written_plugin):
    hooks = Plugin.load(written_plugin).hooks
    assert hooks is not None
    reset = hooks.session_start[0].hooks[0].command
    mark = hooks.post_tool_use[0].hooks[0].command
    stop = hooks.stop[0].hooks[0].command

    assert _run(reset).returncode == 0
    refused = _run(stop)
    assert refused.returncode == 2
    decision = json.loads(refused.stdout)
    assert decision["decision"] == "deny"
    assert decision["additionalContext"] == FINISH_TOOL_REQUIRED_MESSAGE

    assert _run(mark).returncode == 0
    assert _run(stop).returncode == 0

    # A new session starts unfinished again.
    assert _run(reset).returncode == 0
    assert _run(stop).returncode == 2
