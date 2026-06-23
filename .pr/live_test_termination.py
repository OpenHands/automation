#!/usr/bin/env python3
"""Live integration test: demonstrates SIGTERM callback behavior before and after the fix.

Simulates the automation service killing a long-running preset run:

BEFORE (main branch behavior):
  - No SIGTERM handlers installed
  - SIGTERM kills the process with no callback fired
  - Run would sit in RUNNING until watchdog (default: 10 min)

AFTER (fix/graceful-termination-cleanup behavior):
  - install_termination_handlers() wires SIGTERM → failure callback
  - SIGTERM fires a FAILED completion callback before exit
  - Run is marked FAILED immediately, cleanup can run promptly

Usage:
    python .pr/live_test_termination.py
"""
from __future__ import annotations

import http.server
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from datetime import datetime, timezone

# ── Captured-callback store ──────────────────────────────────────────────────

_callbacks: list[dict] = []
_lock = threading.Lock()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"raw": body}
        ts = datetime.now(timezone.utc).isoformat()
        with _lock:
            _callbacks.append({"received_at": ts, "payload": payload, "path": self.path})
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_):
        pass  # suppress server log noise


def _start_callback_server() -> int:
    """Start a local HTTP server on a free port; return port number."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return port


# ── Simulated preset script: BEFORE (no termination handlers) ───────────────

BEFORE_SCRIPT = textwrap.dedent("""\
    import os, sys, time

    callback_url = os.environ["AUTOMATION_CALLBACK_URL"]
    run_id = os.environ["AUTOMATION_RUN_ID"]

    print("[before] started -- sleeping 60 s to simulate a long-running LLM task")
    sys.stdout.flush()
    # No install_termination_handlers() call.
    # If SIGTERM arrives, the process dies silently -- no callback fired.
    time.sleep(60)

    # This line (and any cleanup) is never reached on hard termination.
    print("[before] completed (never reached on SIGTERM)")
""")

# ── Simulated preset script: AFTER (with termination handlers) ──────────────

# Copy _termination.py into the temp dir so the script can import it.
TERMINATION_SRC = os.path.join(
    os.path.dirname(__file__),
    "..",
    "openhands",
    "automation",
    "presets",
    "_termination.py",
)

AFTER_SCRIPT = textwrap.dedent("""\
    import os, sys, time

    callback_url = os.environ["AUTOMATION_CALLBACK_URL"]
    run_id = os.environ["AUTOMATION_RUN_ID"]

    # Wire up termination handlers -- this is the fix.
    from _termination import install_termination_handlers, mark_completed
    install_termination_handlers()

    print("[after] started -- sleeping 60 s to simulate a long-running LLM task")
    sys.stdout.flush()
    time.sleep(60)

    # Clean exit: record success so atexit hook does not fire spurious FAILED.
    mark_completed()
    print("[after] completed cleanly (never reached on SIGTERM)")
""")


# ── Run a script, send SIGTERM after `kill_after` seconds ───────────────────

def run_with_sigterm(
    label: str,
    script: str,
    callback_url: str,
    run_id: str,
    kill_after: float = 2.0,
) -> dict:
    """Spawn `script` in a subprocess, send SIGTERM after `kill_after`s."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write the preset script
        script_path = os.path.join(tmpdir, "sdk_main.py")
        with open(script_path, "w") as f:
            f.write(script)

        # Copy _termination.py into tmpdir so the AFTER script can import it
        term_dst = os.path.join(tmpdir, "_termination.py")
        with open(TERMINATION_SRC) as src, open(term_dst, "w") as dst:
            dst.write(src.read())

        env = {
            **os.environ,
            "AUTOMATION_CALLBACK_URL": callback_url,
            "AUTOMATION_RUN_ID": run_id,
            "PYTHONPATH": tmpdir,
        }

        proc = subprocess.Popen(
            [sys.executable, script_path],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        started_at = time.monotonic()
        time.sleep(kill_after)
        kill_at = time.monotonic()
        print(f"  -> sending SIGTERM to {label} PID {proc.pid} "
              f"after {round(kill_at - started_at, 2)}s")
        proc.send_signal(signal.SIGTERM)

        # Give the handler 3 s to fire and the process to exit
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()

        elapsed = time.monotonic() - started_at
        stdout = proc.stdout.read().decode()
        stderr = proc.stderr.read().decode()

        return {
            "label": label,
            "returncode": proc.returncode,
            "elapsed_s": round(elapsed, 2),
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
            "sigterm_after_s": round(kill_at - started_at, 2),
        }


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 72)
    print("Live Termination Test -- BEFORE vs AFTER the fix")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 72)

    port = _start_callback_server()
    base_url = f"http://127.0.0.1:{port}"
    print(f"\nCallback server listening on {base_url}\n")

    results = []

    # ── BEFORE ──────────────────────────────────────────────────────────────
    print("-- BEFORE (main branch -- no termination handlers) --")
    before_cb_count_before = len(_callbacks)
    r_before = run_with_sigterm(
        label="BEFORE",
        script=BEFORE_SCRIPT,
        callback_url=f"{base_url}/runs/before-run-id/complete",
        run_id="before-run-id",
        kill_after=2.0,
    )
    time.sleep(0.5)  # let callback arrive if somehow sent
    before_cb_count_after = len(_callbacks)
    callbacks_fired_before = before_cb_count_after - before_cb_count_before
    results.append({**r_before, "callbacks_fired": callbacks_fired_before})

    print(f"  returncode : {r_before['returncode']}")
    print(f"  stdout     : {r_before['stdout'] or '(empty)'}")
    print(f"  stderr     : {r_before['stderr'] or '(empty)'}")
    print(f"  callbacks  : {callbacks_fired_before}  <- expected 0 (run stuck in RUNNING)")
    print()

    # ── AFTER ───────────────────────────────────────────────────────────────
    print("-- AFTER (fix branch -- install_termination_handlers() wired) --")
    after_cb_count_before = len(_callbacks)
    r_after = run_with_sigterm(
        label="AFTER",
        script=AFTER_SCRIPT,
        callback_url=f"{base_url}/runs/after-run-id/complete",
        run_id="after-run-id",
        kill_after=2.0,
    )
    time.sleep(0.5)  # let callback arrive
    after_cb_count_after = len(_callbacks)
    callbacks_fired_after = after_cb_count_after - after_cb_count_before
    results.append({**r_after, "callbacks_fired": callbacks_fired_after})

    print(f"  returncode : {r_after['returncode']}")
    print(f"  stdout     : {r_after['stdout'] or '(empty)'}")
    print(f"  stderr     : {r_after['stderr'] or '(empty)'}")
    print(f"  callbacks  : {callbacks_fired_after}  <- expected 1 (FAILED callback fired promptly)")
    print()

    # ── Summary ─────────────────────────────────────────────────────────────
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    for r in results:
        status = "PASS" if (
            (r["label"] == "BEFORE" and r["callbacks_fired"] == 0) or
            (r["label"] == "AFTER" and r["callbacks_fired"] >= 1)
        ) else "FAIL"
        print(f"  {r['label']:8s}  callbacks_fired={r['callbacks_fired']}  "
              f"exit={r['returncode']}  [{status}]")

    print()
    print("Callback payloads received:")
    with _lock:
        for i, cb in enumerate(_callbacks, 1):
            print(f"  [{i}] {cb['received_at']}  {cb['path']}")
            print(f"       payload: {json.dumps(cb['payload'])}")

    # Emit machine-readable JSON for PR evidence capture
    evidence = {
        "test_time": datetime.now(timezone.utc).isoformat(),
        "results": results,
        "callbacks": list(_callbacks),
    }
    evidence_path = os.path.join(os.path.dirname(__file__), "termination_evidence.json")
    with open(evidence_path, "w") as f:
        json.dump(evidence, f, indent=2)
    print(f"\nEvidence written to: {evidence_path}")

    # Exit non-zero if expectations not met
    ok = (
        results[0]["callbacks_fired"] == 0
        and results[1]["callbacks_fired"] >= 1
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
