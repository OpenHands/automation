"""Best-effort cleanup on automation-run termination.

Preset ``sdk_main.py`` scripts run inside a single bash command that the
automation service kills after ``max_run_duration`` (default 600s). When that
happens the process receives ``SIGTERM`` and Python's ``finally`` blocks /
``atexit`` handlers / context-manager ``__exit__`` methods are *not* guaranteed
to run, so the completion callback (normally sent by the workspace context
manager) is never delivered and the run is left for the watchdog to mark
``FAILED``.

This module registers a ``SIGTERM``/``SIGINT`` handler and an ``atexit`` hook
that fire a *failure* completion callback (``AUTOMATION_CALLBACK_URL``) before
the process is torn down. This is best-effort:

- ``SIGTERM`` (the signal the bash service sends on timeout) gives the handler
  a short window to run before the process exits.
- ``SIGKILL`` / OOM / abrupt host termination cannot be caught; for those the
  watchdog remains the source of truth (it marks the run ``FAILED`` and cleans
  up the sandbox).

The module is stdlib-only so it can be imported and unit-tested without the
OpenHands SDK, and so it can be packaged into both the prompt and plugin
preset tarballs.

Usage (in ``sdk_main.py``, after env vars are read)::

    from _termination import install_termination_handlers
    install_termination_handlers(callback_url=os.environ.get("AUTOMATION_CALLBACK_URL"))

The handler is idempotent: firing the callback more than once (e.g. once from
the signal handler and again from normal ``__exit__``) is harmless because the
automation service only honors the first terminal status transition per run.
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import sys
import urllib.error
import urllib.request

# How long (seconds) the signal handler may spend trying to deliver the
# callback before letting the process exit. Kept short so we don't delay
# teardown, and bounded so a dead callback endpoint can't hang the handler.
_CALLBACK_TIMEOUT_SECONDS = 5.0

_STATE = {
    "installed": False,
    "fired": False,
    "callback_url": "",
    "callback_api_key": "",
    "run_id": "",
}


def _send_failure_callback(reason: str) -> None:
    """POST a FAILED completion callback. Idempotent and non-raising."""
    if _STATE["fired"]:
        return
    url = _STATE["callback_url"]
    if not url:
        return
    _STATE["fired"] = True
    body = {
        "status": "FAILED",
        "run_id": _STATE["run_id"],
        "error": f"Automation run terminated before completing: {reason}",
    }
    headers = {"Content-Type": "application/json"}
    api_key = _STATE["callback_api_key"]
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), headers=headers
        )
        urllib.request.urlopen(req, timeout=_CALLBACK_TIMEOUT_SECONDS)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        # Best-effort: if the callback can't be delivered, the watchdog will
        # still mark the run FAILED. Never raise from a signal/atexit handler.
        print(f"[termination] callback failed (non-fatal): {exc}", file=sys.stderr)


def _signal_handler(signum: int, _frame) -> None:
    """Handle SIGTERM/SIGINT by firing the failure callback, then re-raise."""
    name = signal.Signals(signum).name
    print(f"[termination] received {name}, firing failure callback", file=sys.stderr)
    _send_failure_callback(reason=f"received {name}")
    # Restore default behavior and re-deliver so the process actually exits.
    signal.signal(signum, signal.SIG_DFL)
    raise SystemExit(128 + signum)


def install_termination_handlers(
    callback_url: str | None = None,
    callback_api_key: str | None = None,
    run_id: str | None = None,
) -> None:
    """Register SIGTERM/SIGINT + atexit handlers that fire a failure callback.

    Safe to call once per process; subsequent calls are no-ops. Reads env vars
    (``AUTOMATION_CALLBACK_URL``, ``AUTOMATION_CALLBACK_API_KEY``,
    ``AUTOMATION_RUN_ID``) when arguments are omitted, so the typical call from
    ``sdk_main.py`` is ``install_termination_handlers()`` with no arguments.

    ``callback_url`` may be empty/None — in that case the handlers are still
    installed (so at least a log line is emitted on termination) but no HTTP
    callback is attempted.
    """
    if _STATE["installed"]:
        return
    _STATE["installed"] = True
    _STATE["callback_url"] = callback_url or os.environ.get(
        "AUTOMATION_CALLBACK_URL", ""
    )
    _STATE["callback_api_key"] = callback_api_key or os.environ.get(
        "AUTOMATION_CALLBACK_API_KEY", ""
    )
    _STATE["run_id"] = run_id or os.environ.get("AUTOMATION_RUN_ID", "")

    # atexit runs on normal interpreter shutdown (including SystemExit raised
    # from the signal handler), giving us a second chance if the signal path
    # didn't fire. It will *not* fire on SIGKILL.
    def _atexit_cb() -> None:
        _send_failure_callback(reason="interpreter shutdown")

    _STATE["_atexit_cb"] = _atexit_cb
    atexit.register(_atexit_cb)
    for sig in (signal.SIGTERM, signal.SIGINT):
        # signal.signal can only be called from the main thread; preset
        # scripts always run in the main thread.
        try:
            signal.signal(sig, _signal_handler)
        except (ValueError, OSError):
            # Not in main thread or signal unsupported — skip silently.
            pass


def mark_completed() -> None:
    """Record that the run completed successfully.

    Call this at the very end of ``sdk_main.py`` (after ``ALL_OK`` is printed)
    so that the atexit hook knows *not* to send a spurious FAILED callback
    during normal shutdown.
    """
    _STATE["fired"] = True
