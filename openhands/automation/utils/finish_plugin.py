"""A plugin that makes a preset run end with the finish tool.

Presets that create their own conversation pass the finish-tool-required hooks
(``presets/finish_tool_hook.py``) in the start request. When the app server
starts the conversation instead, its start request takes no hook configuration,
but it does take plugins, and the agent server merges a plugin's hooks into the
conversation. The service writes this plugin into the run's sandbox and passes
its path with the run's plugins.

The hooks match the preset ones: reset a marker when the session starts, set
it when the finish tool runs, and refuse to stop until it is set.
"""

from __future__ import annotations

import json
import shlex


FINISH_TOOL_REQUIRED_MESSAGE = (
    "The task appears complete, but automation runs must end by calling the "
    "finish tool. Please call the finish tool now with the final task outcome."
)


def finish_plugin_dir(run_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in run_id)
    return f"/tmp/automation-{safe}-finish-required"


def finish_plugin_files(run_id: str) -> dict[str, bytes]:
    """The plugin's files, keyed by absolute path in the sandbox."""
    root = finish_plugin_dir(run_id)
    marker = shlex.quote(f"{root}/state/finish_tool_used")
    state = shlex.quote(f"{root}/state")
    deny = shlex.quote(
        json.dumps(
            {
                "decision": "deny",
                "reason": "finish tool was not used",
                "additionalContext": FINISH_TOOL_REQUIRED_MESSAGE,
            }
        )
    )

    def hook(name: str, command: str) -> dict:
        return {"hooks": [{"name": name, "command": command, "timeout": 5}]}

    hooks = {
        "session_start": [
            hook("reset-finish-tool-marker", f"mkdir -p {state} && rm -f {marker}")
        ],
        "post_tool_use": [
            {
                "matcher": "/(?:finish|FinishTool)/",
                **hook("mark-finish-tool-used", f"mkdir -p {state} && touch {marker}"),
            }
        ],
        "stop": [
            hook(
                "require-finish-tool",
                f"test -f {marker} && exit 0; echo {deny}; exit 2",
            )
        ],
    }
    manifest = {
        "name": "automation-finish-required",
        "version": "1.0.0",
        "description": "Automation runs end by calling the finish tool.",
    }
    return {
        f"{root}/.plugin/plugin.json": json.dumps(manifest, indent=2).encode(),
        f"{root}/hooks/hooks.json": json.dumps(hooks, indent=2).encode(),
    }
