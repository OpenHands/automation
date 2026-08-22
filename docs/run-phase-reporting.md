# Run Phase Reporting

How to report human-readable progress from inside a running automation, so
users see more than "running" for the whole duration — for example a
code-review automation moving through "polling PRs by label" → "checking out
PR #123" → "studying the diff" → "QA checks" → "posting comments".

## Who needs this

The service already reports its own phases up through `entrypoint_start`
(queued → sandbox provisioning → bundle upload → entrypoint start). From
`entrypoint_start` onward, phase reporting is up to your code — otherwise the
phase goes dark for the rest of the run. The `prompt` and `plugin` presets do
this already (see `openhands/automation/presets/{prompt,plugin}/sdk_main.py`,
function `_emit_phase`); if you write a custom entrypoint script, add the same
call yourself.

## How

`AUTOMATION_PHASE_URL` is injected into the sandbox as a complete, ready-to-POST
URL (not a base to build on). POST it a JSON body with the phase's `code` and
`label`:

```python
import json
import os
from urllib.request import Request, urlopen

def emit_phase(code: str, label: str) -> None:
    phase_url = os.environ.get("AUTOMATION_PHASE_URL")
    if not phase_url:
        return
    credential = os.environ.get("AUTOMATION_CALLBACK_API_KEY") or os.environ.get(
        "OPENHANDS_API_KEY"
    )
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    try:
        body = json.dumps({"code": code, "label": label}).encode()
        urlopen(Request(phase_url, data=body, headers=headers, method="POST"), timeout=5)
    except Exception:
        pass  # a phase is telemetry — never let it break the run
```

```bash
curl -s -X POST "$AUTOMATION_PHASE_URL" \
  -H "Authorization: Bearer ${AUTOMATION_CALLBACK_API_KEY:-$OPENHANDS_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"code": "checking_out", "label": "Checking out PR #123"}'
```

Authenticate with whichever of these two the sandbox actually has — they are
mutually exclusive by mode, so trying one then the other is safe:

- **Local mode**: `AUTOMATION_CALLBACK_API_KEY` — the automation service's own
  key, injected by the local backend for exactly this purpose.
- **Cloud mode**: `OPENHANDS_API_KEY` — the per-user Cloud API key.

Do **not** use `SESSION_API_KEY` (or `OH_SESSION_API_KEYS_0`) here even though
it is also present in the sandbox: that key authenticates to the *agent
server*, a different service. In local mode the automation service checks the
presented key against its own configured key and rejects anything else with
401 — so sending the agent-server key silently drops every phase update.

If the local deployment has no key configured at all, `AUTOMATION_CALLBACK_API_KEY`
is never injected and an unauthenticated request is rejected too, so no phase can
be reported by any means from inside the sandbox. Configure the service's key if
you want phases in local mode.

## Request body

| Field | Type | Limit |
|-------|------|-------|
| `code` | string, optional | ≤ 128 chars, machine-readable, e.g. `"checking_out"` |
| `label` | string, optional | ≤ 200 chars, free-form, e.g. `"Checking out PR #123"` |

- At least one of `code`/`label` must be non-blank after `.strip()`, or the
  request is rejected with 422.
- `label` is what users read. Send one whenever the phase is meant for a
  human: without it the UI falls back to showing the raw `code`, so
  `checking_out` appears exactly like that, underscores and all. A
  whitespace-only label counts as none — it is stored as sent, and the UI
  falls back the same way rather than rendering blank text.
- Neither field may contain Unicode `Cc` (control) or `Zl`/`Zp` (line/paragraph
  separator) characters — also 422. `Cf` (format) characters are allowed, so
  emoji sequences and non-Latin labels pass through fine.
- The body accepts only `code` and `label` — any other field is rejected with
  422 (unknown fields are not silently ignored).
- **A phase is one value** — the `(code, label)` pair. Every call replaces it
  whole: send both fields even if only one changed, or the omitted one is
  stored as `NULL`. There is no history, only the current phase.

## Responses

| Code | Meaning |
|------|---------|
| 200 | Recorded; the response is the updated run |
| 401 | The credential was missing or not the one this service accepts |
| 403 | The caller does not own the automation this run belongs to |
| 404 | No run with that `run_id` |
| 409 | The run has already finished — see below |
| 422 | The body broke one of the rules above |

None of the error responses change the run's status or its stored phase —
validation happens before anything is written.

Only a `PENDING` or `RUNNING` run accepts a phase; any other status answers
409 and keeps the phase it already had. A finished run's phase is a record of
how far it got — for a failed run it is the place it stopped, which is what
the UI shows beside the failure — so a straggling write from a sandbox that
outlived its run cannot move it. A 409 late in a run is normal and needs no
handling beyond the rule in the next section.

## Failure handling

Emitting a phase must never break the automation. If `AUTOMATION_PHASE_URL` is
absent, or the request fails, times out, or gets a non-2xx response, catch it
and move on — a phase is telemetry, not a control signal.

## Out of scope

`scripts/test_tarball/main.py` is a manual smoke-test script for exercising
the dispatch pipeline, not a template shipped to users. It does not report
phases and is not part of this recipe.
