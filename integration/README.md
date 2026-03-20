# Automation Service – Integration Tests

End-to-end tests that run against a **live** deployment of the automation service.

## Prerequisites

| Variable | Description | Example |
|---|---|---|
| `OPENHANDS_API_KEY` | Valid OpenHands API key for the target environment | *(create one at staging.all-hands.dev)* |
| `AUTOMATION_BASE_URL` | Base URL of the deployed automation service | `https://automation-automations-feature.staging.all-hands.dev` |

## Running

```bash
# From the repo root
cd automation/tests/integration

# Single process
OPENHANDS_API_KEY="oh_..." \
AUTOMATION_BASE_URL="https://automation-automations-feature.staging.all-hands.dev" \
  python -m pytest . -v --rootdir=. -c /dev/null

# Parallel (requires pytest-xdist)
OPENHANDS_API_KEY="oh_..." \
AUTOMATION_BASE_URL="https://automation-automations-feature.staging.all-hands.dev" \
  python -m pytest . -v -n auto --rootdir=. -c /dev/null
```

> **Note:** When running with `-n auto`, the `TestCRUDLifecycle` class tests
> are sequential by design (they share state within the class). All other test
> classes are independent and safe to parallelise across workers.

## Test structure

| Class | What it covers | Parallelisable? |
|---|---|---|
| `TestHealthChecks` | `/health` and `/ready` endpoints | ✅ |
| `TestAuthentication` | Missing, invalid, malformed auth headers → 401 | ✅ |
| `TestValidation` | Bad payloads (invalid cron, shell metachar, etc.) → 422 | ✅ |
| `TestCRUDLifecycle` | Full create → read → list → update → delete → verify gone | ❌ sequential |
| `TestEdgeCases` | 404 for missing resources, optional fields, tarball schemes | ✅ |
| `TestPagination` | `limit` / `offset` query parameters | ✅ |
