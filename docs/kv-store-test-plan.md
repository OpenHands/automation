# KV Store Test Plan

**PR:** [OpenHands/automation#69](https://github.com/OpenHands/automation/pull/69)

> **Note:** Most test cases are now automated in `scripts/test_kv_e2e.py`.
> This document covers manual testing workflows, debugging, and tests not yet automated.

---

## ⚠️ CRITICAL BUG: Token Injection

**Issue:** The `AUTOMATION_KV_TOKEN` environment variable is **NOT being injected** into the sandbox even when `enable_kv_store: true` is set on the automation.

**Evidence:**
- Created automation with `enable_kv_store: true`
- Dispatched run, confirmed status = COMPLETED  
- Agent output showed: `"Checking if token exists: 0 chars"` (token is empty)
- All KV API calls failed with `"Invalid token: Not enough segments"`

**Root Cause:** The dispatcher is not generating/injecting the KV token into the sandbox environment.

**Workaround:** The E2E test script (`scripts/test_kv_e2e.py`) manually generates and injects the token, bypassing this bug.

---

## Automated Tests

Run the E2E test suite:

```bash
# Quick smoke test (8 tests, ~30s)
python scripts/test_kv_e2e.py

# Full test suite (26 tests, ~2min)
python scripts/test_kv_e2e.py --thorough
```

**Coverage:** Basic CRUD, INCR/DECR, list operations, nested paths, conditional SET, auth errors, type errors, edge cases.

---

## Tests NOT Yet Automated

The following require multi-run or multi-automation coordination:

### Cross-Automation Isolation (TC-9.2)

Verify automation A cannot access automation B's KV data:

```bash
# Create Automation A - writes "shared-name" = "I am A"
curl -X POST "${BASE_URL}/api/automation/v1/preset/prompt" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Isolation Test A",
    "prompt": "Set KV key \"shared-name\" to \"I am Automation A\". Then read and print it.",
    "trigger": {"type": "cron", "schedule": "0 0 1 1 *"}
  }'
# Enable KV, dispatch, note automation_id as A_ID

# Create Automation B - writes "shared-name" = "I am B"
curl -X POST "${BASE_URL}/api/automation/v1/preset/prompt" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Isolation Test B",
    "prompt": "Set KV key \"shared-name\" to \"I am Automation B\". Then read and print it.",
    "trigger": {"type": "cron", "schedule": "0 0 1 1 *"}
  }'
# Enable KV, dispatch, note automation_id as B_ID

# Run A again - should still see "I am Automation A" (not B's value)
curl -X POST "${BASE_URL}/api/automation/v1/${A_ID}/dispatch" \
  -H "Authorization: Bearer ${API_KEY}"
```

### State Persistence Across Runs (TC-10.1)

Verify KV data persists between automation runs:

```bash
curl -X POST "${BASE_URL}/api/automation/v1/preset/prompt" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "KV Persistence Test",
    "prompt": "Read KV key \"run_counter\". Print current value (or \"first run\" if missing). Increment it. Print new value.",
    "trigger": {"type": "cron", "schedule": "0 0 1 1 *"}
  }'

# Enable KV store
curl -X PATCH "${BASE_URL}/api/automation/v1/${ID}" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"enable_kv_store": true}'

# Run 1: Should print "first run", counter = 1
# Run 2: Should print "1", counter = 2
# Run 3: Should print "2", counter = 3
```

---

## Debugging: View Automation Run Results

Automation runs create conversations. To see what happened:

```bash
# 1. Find conversation ID
curl -s "${BASE_URL}/api/v1/app-conversations/search?limit=10" \
  -H "Authorization: Bearer ${API_KEY}" \
  | jq '.items[] | {id, automation_name: .tags.automationname, status: .sandbox_status}'

# 2. Get events for a conversation
CONV_ID="<conversation-id>"
EVENT_IDS=$(curl -s "${BASE_URL}/api/v1/conversation/${CONV_ID}/events/search?limit=50" \
  -H "Authorization: Bearer ${API_KEY}" | jq -r '.items | map("id=" + .id) | join("&")')

# 3. View command outputs
curl -s "${BASE_URL}/api/v1/conversation/${CONV_ID}/events?${EVENT_IDS}" \
  -H "Authorization: Bearer ${API_KEY}" \
  | jq '.[] | select(.kind == "ObservationEvent") | {
      command: .observation.command, 
      output: .observation.content[0].text[0:500]
    }'
```

---

## Quick Reference Commands

```bash
# Create automation
curl -X POST "${BASE_URL}/api/automation/v1/preset/prompt" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"name": "Test", "prompt": "...", "trigger": {"type": "cron", "schedule": "0 0 1 1 *"}}'

# Enable KV store
curl -X PATCH "${BASE_URL}/api/automation/v1/${ID}" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"enable_kv_store": true}'

# Dispatch run
curl -X POST "${BASE_URL}/api/automation/v1/${ID}/dispatch" \
  -H "Authorization: Bearer ${API_KEY}"

# List runs
curl "${BASE_URL}/api/automation/v1/${ID}/runs" \
  -H "Authorization: Bearer ${API_KEY}"

# Delete automation
curl -X DELETE "${BASE_URL}/api/automation/v1/${ID}" \
  -H "Authorization: Bearer ${API_KEY}"
```

---

## Notes

1. **Token is sandbox-only:** `AUTOMATION_KV_TOKEN` is injected at runtime. You cannot extract it externally.

2. **Preset API:** The prompt preset (`/preset/prompt`) does not expose `enable_kv_store` directly—use PATCH after creation.

3. **Token scope:** Each token is scoped to a specific automation ID for strict isolation.
