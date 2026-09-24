# Run Phase Reporting Guide

This guide covers how automations report live progress phases that the dashboard shows while a run is active.

## Overview

While an automation run is `PENDING` or `RUNNING`, it can publish a short, human-readable "current phase" message (e.g. `Cloning repositories`, `Running QA checks`, `Posting review comments`). The dashboard and the automation detail page poll run data while a run is in flight and render the latest phase next to the run status — so users can see what a long run is doing without opening logs.

Phases are **cosmetic, best-effort telemetry**:

- They never affect the run outcome. A failed phase POST must never fail your automation.
- Only the most recent phase is stored (one message per run, overwritten on each report).
- Once the run reaches a terminal status (`COMPLETED`, `FAILED`, `CANCELLED`, `SKIPPED`), further phase reports are rejected with `409` and the UI stops rendering the phase.
- Preset automations report standard phases automatically (environment setup, repo cloning, skill loading, agent configuration, and live agent activity). This guide is for **custom automations** that want the same visibility.

## Environment Variables

The dispatcher injects everything you need into the run's environment:

| Variable | Description |
|----------|-------------|
| `AUTOMATION_PHASE_URL` | Full URL to POST phase updates to (`{service}/v1/runs/{run_id}/phase`) |
| `AUTOMATION_CALLBACK_API_KEY` | Auth token (local / self-hosted mode) |
| `OPENHANDS_API_KEY` | Auth token (Cloud mode) |
| `AUTOMATION_RUN_ID` | This run's ID (informational; already encoded in the URL) |

Use whichever token is present: `AUTOMATION_CALLBACK_API_KEY` in local mode, `OPENHANDS_API_KEY` in Cloud mode.

## Reporting a Phase

```bash
PHASE_TOKEN="${AUTOMATION_CALLBACK_API_KEY:-${OPENHANDS_API_KEY:-}}"
if [ -n "${AUTOMATION_PHASE_URL:-}" ] && [ -n "$PHASE_TOKEN" ]; then
    curl -sf -m 5 -X POST "$AUTOMATION_PHASE_URL" \
      -H "Authorization: Bearer $PHASE_TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"phase": "Checking out PR #123"}' >/dev/null 2>&1 || true
fi
```

The trailing `|| true` (and the env guards) keep the report non-fatal — recommended for every caller, especially scripts running under `set -e`.

Python equivalent:

```python
import os

import httpx

def report_phase(message: str) -> None:
    url = os.environ.get("AUTOMATION_PHASE_URL", "")
    token = (
        os.environ.get("AUTOMATION_CALLBACK_API_KEY")
        or os.environ.get("OPENHANDS_API_KEY")
        or ""
    )
    if not url or not token or not message:
        return
    try:
        httpx.post(
            url,
            json={"phase": message[:200]},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
    except Exception:
        pass  # phases are cosmetic; never fail the run because of them
```

## API Contract

`POST /v1/runs/{run_id}/phase`

**Request body:**
```json
{"phase": "Running QA checks"}
```

- `phase` must be 1–200 characters after normalization. Control characters (including newlines) and runs of whitespace are collapsed to single spaces; leading/trailing whitespace is stripped.

**Responses:**

| Status | Meaning |
|--------|---------|
| `200` | Phase recorded; body is the updated run |
| `403` | Caller does not own the run's parent automation |
| `404` | Run not found |
| `409` | Run is already terminal (`COMPLETED`/`FAILED`/`CANCELLED`/`SKIPPED`) |
| `422` | Invalid body (empty/whitespace-only phase, or longer than 200 chars) |

A `409` near the end of a run is normal (e.g. the completion callback won the race) — ignore it.

## Safety Guidelines

Phase messages are rendered directly in the dashboard, so:

- **Never include secrets, tokens, or credentials.** Phase text is stored in the automation service and shown in the UI.
- **Never include raw payloads, stack traces, or customer data.** Use `error_detail` semantics (the completion callback) for failures; phases are for progress.
- Keep messages **concise and user-facing** — a short present-tense action ("Examining the diff") reads best. Long messages are truncated by the 200-char limit.
- Report at meaningful step boundaries, not in tight loops. One update every few seconds is plenty; only the latest message is kept.

## Version Compatibility

- On automation services older than the release that added this endpoint, `AUTOMATION_PHASE_URL` is not set — guard on its presence (as in the examples) and your automation degrades silently.
- Missing phase information is always safe: the dashboard falls back to the plain run status.
