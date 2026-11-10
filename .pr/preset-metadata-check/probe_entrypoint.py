"""Run the real prompt entrypoint up to (but not into) model execution.

Use the Python interpreter from benchmark.json's fixed_runtime/.venv. Requires
an isolated Agent Server with the dummy local LLM profile described in README.
"""

import argparse
import ast
import json
import os
import runpy
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class ConversationCreated(BaseException):
    pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--session-key-file", type=Path, required=True)
    parser.add_argument("--agent-server-url", required=True)
    args = parser.parse_args()
    args.work_dir.mkdir(exist_ok=True)
    preset = ROOT / "openhands/automation/presets"
    entry = (args.work_dir / "main.py").resolve()
    shutil.copyfile(preset / "prompt/sdk_main.py", entry)
    shutil.copyfile(
        preset / "finish_tool_hook.py", args.work_dir / "finish_tool_hook.py"
    )
    (args.work_dir / "prompt.txt").write_text(
        "Verification stops before sending this prompt."
    )
    stop_line = next(
        node.lineno
        for node in ast.walk(ast.parse(entry.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "send_message"
    )
    # No callback, repo cloning, provider credentials or user's local SDK state.
    for key in list(os.environ):
        if key.startswith("AUTOMATION_") or key in ("OPENHANDS_API_KEY", "SANDBOX_ID"):
            os.environ.pop(key)
    home = args.work_dir / "private-home"
    home.mkdir(exist_ok=True)
    os.environ.update(
        HOME=str(home),
        AUTOMATION_RUN_MODE="local",
        AGENT_SERVER_URL=args.agent_server_url,
        OH_SESSION_API_KEYS_0=args.session_key_file.read_text().strip(),
        WORKSPACE_BASE=str(args.work_dir),
    )
    result = {}

    def trace(frame, event, _arg):
        if frame.f_code.co_filename != str(entry):
            return None
        if event == "line" and frame.f_lineno == stop_line:
            conversation = frame.f_locals["conversation"]
            result.update(
                conversation_id=str(conversation.id),
                conversation_type=type(conversation).__name__,
                stopped_before="conversation.send_message and conversation.run",
                entrypoint=str(entry),
            )
            raise ConversationCreated()
        return trace

    sys.path.insert(0, str(args.work_dir))
    sys.settrace(trace)
    try:
        runpy.run_path(str(entry), run_name="__main__")
    except ConversationCreated:
        pass
    finally:
        sys.settrace(None)
    assert result, "Entrypoint did not reach conversation creation"
    target = Path(__file__).resolve().parent / "entrypoint.json"
    target.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
