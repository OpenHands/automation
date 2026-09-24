"""Shared hook config for automation preset conversations."""

import os
import shlex
import sys

from openhands.sdk.hooks import HookConfig, HookDefinition, HookMatcher


FINISH_TOOL_REQUIRED_MESSAGE = (
    "The task appears complete, but automation runs must end by calling the "
    "finish tool. Please call the finish tool now with the final task outcome."
)


def _finish_tool_marker_path(script_dir: str) -> str:
    run_id = os.environ.get("AUTOMATION_RUN_ID") or "current"
    safe_run_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in run_id)
    return os.path.join(
        os.path.abspath(script_dir),
        ".openhands_automation_runtime",
        safe_run_id,
        "finish_tool_used",
    )


def _python_hook_command(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def finish_tool_required_hook_config(script_dir: str) -> HookConfig:
    marker_path = _finish_tool_marker_path(script_dir)
    runtime_dir = os.path.dirname(marker_path)
    deny_payload = {
        "decision": "deny",
        "reason": "finish tool was not used",
        "additionalContext": FINISH_TOOL_REQUIRED_MESSAGE,
    }

    return HookConfig(
        session_start=[
            HookMatcher(
                hooks=[
                    HookDefinition(
                        name="reset-finish-tool-marker",
                        command=_python_hook_command(
                            "from pathlib import Path\n"
                            f"path = Path({marker_path!r})\n"
                            "path.parent.mkdir(parents=True, exist_ok=True)\n"
                            "path.unlink(missing_ok=True)\n"
                        ),
                        timeout=5,
                    )
                ],
            )
        ],
        post_tool_use=[
            HookMatcher(
                matcher="/(?:finish|FinishTool)/",
                hooks=[
                    HookDefinition(
                        name="mark-finish-tool-used",
                        command=_python_hook_command(
                            "from pathlib import Path\n"
                            f"path = Path({marker_path!r})\n"
                            "path.parent.mkdir(parents=True, exist_ok=True)\n"
                            "path.write_text('finish\\n')\n"
                        ),
                        timeout=5,
                    )
                ],
            )
        ],
        stop=[
            HookMatcher(
                hooks=[
                    HookDefinition(
                        name="require-finish-tool",
                        command=_python_hook_command(
                            "import json\n"
                            "import sys\n"
                            "from pathlib import Path\n"
                            f"if Path({marker_path!r}).is_file():\n"
                            "    sys.exit(0)\n"
                            f"print(json.dumps({deny_payload!r}))\n"
                            "sys.exit(2)\n"
                        ),
                        timeout=5,
                    )
                ],
            )
        ],
        session_end=[
            HookMatcher(
                hooks=[
                    HookDefinition(
                        name="cleanup-finish-tool-marker",
                        command=_python_hook_command(
                            "import shutil\n"
                            "from pathlib import Path\n"
                            f"shutil.rmtree(Path({runtime_dir!r}), ignore_errors=True)\n"
                        ),
                        timeout=5,
                    )
                ],
            )
        ],
    )
