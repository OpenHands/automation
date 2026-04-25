# KV Store Test Plan

<!-- Paste your test plan content here -->

# Manual Test Plan: KV Store API for Automation State Persistence

**PR:** [OpenHands/automation#69](https://github.com/OpenHands/automation/pull/69)  
**Staging Environment:** https://au-pr-69.staging.all-hands.dev  
**API Key:** `sk-oh-4qEHoRWN7KtS5hXwF9W3UhobmLxmXfKy`

---

## ⚠️ CRITICAL BUG FOUND

**Issue:** The `AUTOMATION_KV_TOKEN` environment variable is **NOT being injected** into the sandbox even when `enable_kv_store: true` is set on the automation.

**Evidence from testing:**
- Created automation with `enable_kv_store: true`
- Dispatched run, confirmed status = COMPLETED
- Checked conversation events via `/api/v1/conversation/{id}/events`
- Agent output showed: `"Checking if token exists: 0 chars"` (token is empty)
- All KV API calls failed with `"Invalid authorization header format"` or `"Invalid token: Not enough segments"`

**Root Cause:** The dispatcher is not generating/injecting the KV token into the sandbox environment.

**Blocking:** All KV operation tests (Categories 3-11) are blocked until this is fixed.

---

## Overview

This test plan covers the KV Store API feature that enables automations to persist state between runs. The feature includes:
- Enable/disable KV store per automation (`enable_kv_store` flag)
- JWT-based authentication scoped per automation
- Full CRUD operations on keys
- Atomic increment/decrement operations
- List operations (LPUSH, RPUSH, LPOP, RPOP, LEN)
- Nested path access and updates
- Application-level encryption for stored values

---

## Prerequisites

```bash
export BASE_URL="https://au-pr-69.staging.all-hands.dev"
export API_KEY="sk-oh-4qEHoRWN7KtS5hXwF9W3UhobmLxmXfKy"
```

---

## How to View Automation Run Results

Automation runs create conversations. Use this 3-step process to view what the agent did:

### Step 1: Find the Conversation ID

List conversations and find your automation run by name:

```bash
curl -s "${BASE_URL}/api/v1/app-conversations/search?limit=10" \
  -H "Authorization: Bearer ${API_KEY}" \
  | jq '.items[] | {
      conversation_id: .id, 
      sandbox_id, 
      automation_name: .tags.automationname, 
      automation_run_id: .tags.automationrunid, 
      status: .sandbox_status
    }'
```

**Example output:**
```json
{
  "conversation_id": "4ec5184247bd4be1a73c6201e602fa71",
  "sandbox_id": "3DiQDstrENTVx3XaHAksKh",
  "automation_name": "KV Test - Basic Operations",
  "automation_run_id": "535745b5-fe0d-42e7-b051-0bc35b222a80",
  "status": "MISSING"
}
```

### Step 2: List Event IDs

Get the event IDs for that conversation (note: this endpoint returns metadata only, not payloads):

```bash
CONV_ID="4ec5184247bd4be1a73c6201e602fa71"

curl -s "${BASE_URL}/api/v1/conversation/${CONV_ID}/events/search?limit=50" \
  -H "Authorization: Bearer ${API_KEY}" \
  | jq '.items[] | {id, kind, timestamp}'
```

**Example output:**
```json
{"id": "1564b4de-0069-40ad-9ee2-0dece6a8016c", "kind": "ActionEvent", "timestamp": "2026-04-25T01:41:11"}
{"id": "576ab2b1-5325-44df-9db2-6874bf4c4d40", "kind": "ObservationEvent", "timestamp": "2026-04-25T01:41:12"}
```

### Step 3: Fetch Full Events with Payloads

Use the batch endpoint to get complete event details including command outputs:

```bash
CONV_ID="4ec5184247bd4be1a73c6201e602fa71"

# Build the query string from event IDs
EVENT_IDS=$(curl -s "${BASE_URL}/api/v1/conversation/${CONV_ID}/events/search?limit=50" \
  -H "Authorization: Bearer ${API_KEY}" \
  | jq -r '.items | map("id=" + .id) | join("&")')

# Fetch full events and filter to ObservationEvents (command outputs)
curl -s "${BASE_URL}/api/v1/conversation/${CONV_ID}/events?${EVENT_IDS}" \
  -H "Authorization: Bearer ${API_KEY}" \
  | jq '.[] | select(.kind == "ObservationEvent") | {
      command: .observation.command, 
      output: .observation.content[0].text[0:300]
    }'
```

**Example output:**
```json
{
  "command": "echo \"${AUTOMATION_KV_TOKEN:0:20}\"",
  "output": ""
}
{
  "command": "env | grep -i -E \"^(AUTOMATION|KV|TOKEN|KEY)\" || echo \"No matching env vars found\"",
  "output": "No matching env vars found"
}
```

### One-Liner (Combined Steps 2 & 3)

```bash
CONV_ID="<your-conversation-id>" && \
EVENT_IDS=$(curl -s "${BASE_URL}/api/v1/conversation/${CONV_ID}/events/search?limit=50" \
  -H "Authorization: Bearer ${API_KEY}" | jq -r '.items | map("id=" + .id) | join("&")') && \
curl -s "${BASE_URL}/api/v1/conversation/${CONV_ID}/events?${EVENT_IDS}" \
  -H "Authorization: Bearer ${API_KEY}" \
  | jq '.[] | select(.kind == "ObservationEvent") | {command: .observation.command, output: .observation.content[0].text[0:200]}'
```

### Helper: Parse Test Results from Events

Once KV token injection is fixed, use this to extract test results:

```bash
# Find the test output in events
curl -s "${BASE_URL}/api/v1/conversation/${CONV_ID}/events?${EVENT_IDS}" \
  -H "Authorization: Bearer ${API_KEY}" \
  | jq -r '.[] | select(.kind == "ObservationEvent") | .observation.content[0].text' \
  | grep -A 100 "TEST RESULTS START" | grep -B 100 "TEST RESULTS END"
```

---

## Testing Strategy

### Challenge
- KV API requires `AUTOMATION_KV_TOKEN` which is only available inside the sandbox
- Automation runs use an OpenHands agent which is non-deterministic
- Cannot run tests directly via curl from outside

### Recommended Approach

**Phase 1: External Tests (Categories 1-2)**
- Test automation CRUD and auth rejection directly via curl
- These don't require the KV token

**Phase 2: Agent-Based Tests (Categories 3-11)**
Once token injection is fixed:

1. Create automation with a prompt containing explicit test commands
2. Dispatch the run
3. Wait for completion
4. Fetch conversation events
5. Parse ObservationEvents for test output
6. Look for markers like `=== TEST RESULTS START ===` to find results

**Example Test Prompt:**
```
Run these exact commands and print all output:

echo "=== TEST RESULTS START ==="
echo "TC-3.1:" && curl -s -X PUT "$BASE/api/automation/v1/kv/test" -H "Authorization: Bearer $AUTOMATION_KV_TOKEN" -H "Content-Type: application/json" -d '"value"'
echo "TC-3.3:" && curl -s "$BASE/api/automation/v1/kv/test" -H "Authorization: Bearer $AUTOMATION_KV_TOKEN"  
echo "=== TEST RESULTS END ==="
```

**Limitation:** Agent may not execute commands exactly as written. Results should be validated by checking:
- HTTP status codes in responses
- Expected JSON structure in response bodies
- Absence of error messages

---

## Test Category 1: Automation Creation with KV Store Flag

### TC-1.1: Create automation with `enable_kv_store: true`

**Steps:**
```bash
curl -X POST "${BASE_URL}/api/automation/v1/preset/prompt" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "KV Test Automation",
    "prompt": "This is a test automation to verify KV store functionality. List all KV keys.",
    "trigger": {"type": "cron", "schedule": "0 0 1 1 *", "timezone": "UTC"}
  }'
```

**Expected Result:**
- HTTP 201 response
- Response includes `enable_kv_store` field (check default value)

**Verification:**
```bash
# Get the automation to verify enable_kv_store field
curl "${BASE_URL}/api/automation/v1/{automation_id}" \
  -H "Authorization: Bearer ${API_KEY}"
```

---

### TC-1.2: Create automation with explicit `enable_kv_store: true` (raw API)

**Steps:**
```bash
# First create an upload or use a valid tarball path
curl -X POST "${BASE_URL}/api/automation/v1" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "KV Enabled Automation",
    "trigger": {"type": "cron", "schedule": "0 0 1 1 *"},
    "tarball_path": "https://example.com/test.tar.gz",
    "entrypoint": "uv run main.py",
    "enable_kv_store": true
  }'
```

**Expected Result:**
- HTTP 201 response
- `enable_kv_store: true` in response

---

### TC-1.3: Create automation with `enable_kv_store: false`

**Steps:**
```bash
curl -X POST "${BASE_URL}/api/automation/v1" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "KV Disabled Automation",
    "trigger": {"type": "cron", "schedule": "0 0 1 1 *"},
    "tarball_path": "https://example.com/test.tar.gz",
    "entrypoint": "uv run main.py",
    "enable_kv_store": false
  }'
```

**Expected Result:**
- HTTP 201 response
- `enable_kv_store: false` in response

---

### TC-1.4: Update automation to enable/disable KV store

**Steps:**
```bash
# Enable KV store on existing automation
curl -X PATCH "${BASE_URL}/api/automation/v1/{automation_id}" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"enable_kv_store": true}'
```

**Expected Result:**
- HTTP 200 response
- `enable_kv_store` field updated accordingly

---

## Test Category 2: KV Store Authentication

> **Testing Method:** TC-2.1 through TC-2.3 can be tested directly via curl (external). TC-2.4 requires sandbox-based testing.

### TC-2.1: Access KV API without token ✅ EXTERNALLY TESTABLE

**Steps:**
```bash
curl -X GET "${BASE_URL}/api/automation/v1/kv" \
  -H "Content-Type: application/json"
```

**Expected Result:**
- HTTP 422 with "Field required" for authorization header

---

### TC-2.2: Access KV API with invalid token ✅ EXTERNALLY TESTABLE

**Steps:**
```bash
curl -X GET "${BASE_URL}/api/automation/v1/kv" \
  -H "Authorization: Bearer invalid-token-12345"
```

**Expected Result:**
- HTTP 401/403 with "Invalid token" error

---

### TC-2.3: Access KV API with automation API key (should fail) ✅ EXTERNALLY TESTABLE

**Steps:**
```bash
curl -X GET "${BASE_URL}/api/automation/v1/kv" \
  -H "Authorization: Bearer ${API_KEY}"
```

**Expected Result:**
- HTTP 401/403 (KV requires special JWT token, not the regular API key)

---

### TC-2.4: Verify KV token is scoped to specific automation 🔒 SANDBOX TESTING

**Method:** Use Approach 4 (Cross-Automation Isolation Test) from the Verification Strategy section.

**Steps:**
1. Create automation A with KV enabled, set key "test" = "A"
2. Create automation B with KV enabled, set key "test" = "B"  
3. Run automation A again and read key "test"

**Expected Result:**
- Automation A should read "A" (not B's value) - each automation has isolated namespace

---

## Test Category 3: Basic KV Operations (GET, SET, DELETE)

> 🔒 **SANDBOX TESTING REQUIRED:** All tests in Categories 3-10 require a valid KV token that's only available inside the automation sandbox. Use one of the verification approaches documented in the "Verification Strategy" section.

**Note:** Tests in this category require a valid KV token. For manual testing, you may need to:
1. Create an automation with `enable_kv_store: true`
2. Dispatch a run
3. Use SDK client or extract token from run logs

For testing purposes, let's assume we have a valid KV_TOKEN:
```bash
export KV_TOKEN="<valid-kv-token-from-run>"
```

### TC-3.1: Set a simple string value

**Steps:**
```bash
curl -X PUT "${BASE_URL}/api/automation/v1/kv/test-key" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '"hello world"'
```

**Expected Result:**
- HTTP 200 response
- Response: `{"key": "test-key", "value": "hello world", "created": true, "updated_at": "..."}`

---

### TC-3.2: Set a JSON object value

**Steps:**
```bash
curl -X PUT "${BASE_URL}/api/automation/v1/kv/config" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"database": {"host": "localhost", "port": 5432}, "debug": true}'
```

**Expected Result:**
- HTTP 200 response
- Response includes the stored JSON object

---

### TC-3.3: Get a value by key

**Steps:**
```bash
curl -X GET "${BASE_URL}/api/automation/v1/kv/test-key" \
  -H "Authorization: Bearer ${KV_TOKEN}"
```

**Expected Result:**
- HTTP 200 response
- Response: `{"key": "test-key", "value": "hello world"}`

---

### TC-3.4: Get value with metadata

**Steps:**
```bash
curl -X GET "${BASE_URL}/api/automation/v1/kv/test-key?meta=true" \
  -H "Authorization: Bearer ${KV_TOKEN}"
```

**Expected Result:**
- HTTP 200 response
- Response includes `created_at` and `updated_at` fields

---

### TC-3.5: Get nested path from JSON value

**Steps:**
```bash
curl -X GET "${BASE_URL}/api/automation/v1/kv/config?path=database.port" \
  -H "Authorization: Bearer ${KV_TOKEN}"
```

**Expected Result:**
- HTTP 200 response
- Response: `{"key": "config", "path": "database.port", "value": 5432}`

---

### TC-3.6: Get non-existent key

**Steps:**
```bash
curl -X GET "${BASE_URL}/api/automation/v1/kv/nonexistent-key" \
  -H "Authorization: Bearer ${KV_TOKEN}"
```

**Expected Result:**
- HTTP 404 Not Found

---

### TC-3.7: Get non-existent nested path

**Steps:**
```bash
curl -X GET "${BASE_URL}/api/automation/v1/kv/config?path=nonexistent.path" \
  -H "Authorization: Bearer ${KV_TOKEN}"
```

**Expected Result:**
- HTTP 404 Not Found or appropriate error

---

### TC-3.8: Delete a key

**Steps:**
```bash
curl -X DELETE "${BASE_URL}/api/automation/v1/kv/test-key" \
  -H "Authorization: Bearer ${KV_TOKEN}"
```

**Expected Result:**
- HTTP 200 response
- Response: `{"key": "test-key", "deleted": true}`

---

### TC-3.9: Delete non-existent key

**Steps:**
```bash
curl -X DELETE "${BASE_URL}/api/automation/v1/kv/nonexistent-key" \
  -H "Authorization: Bearer ${KV_TOKEN}"
```

**Expected Result:**
- HTTP 200 response
- Response: `{"key": "nonexistent-key", "deleted": false}`

---

### TC-3.10: List all keys

**Steps:**
```bash
curl -X GET "${BASE_URL}/api/automation/v1/kv" \
  -H "Authorization: Bearer ${KV_TOKEN}"
```

**Expected Result:**
- HTTP 200 response
- Response: `{"keys": ["config", ...], "count": N}`

---

## Test Category 4: Conditional SET Operations (NX/XX)

### TC-4.1: SET with NX flag (only if NOT exists) - key doesn't exist

**Steps:**
```bash
curl -X PUT "${BASE_URL}/api/automation/v1/kv/new-key?nx=true" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '"new value"'
```

**Expected Result:**
- HTTP 200 response
- `"created": true`

---

### TC-4.2: SET with NX flag - key already exists

**Steps:**
```bash
# First set the key
curl -X PUT "${BASE_URL}/api/automation/v1/kv/existing-key" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '"original"'

# Try to set with NX
curl -X PUT "${BASE_URL}/api/automation/v1/kv/existing-key?nx=true" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '"new value"'
```

**Expected Result:**
- HTTP 200 response
- `"created": false` and `"error": "..."` indicating key exists

---

### TC-4.3: SET with XX flag (only if EXISTS) - key exists

**Steps:**
```bash
curl -X PUT "${BASE_URL}/api/automation/v1/kv/existing-key?xx=true" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '"updated value"'
```

**Expected Result:**
- HTTP 200 response
- Value updated successfully

---

### TC-4.4: SET with XX flag - key doesn't exist

**Steps:**
```bash
curl -X PUT "${BASE_URL}/api/automation/v1/kv/nonexistent-xx?xx=true" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '"value"'
```

**Expected Result:**
- HTTP 200 response
- `"created": false` and `"error": "..."` indicating key doesn't exist

---

### TC-4.5: SET with both NX and XX flags (should be invalid)

**Steps:**
```bash
curl -X PUT "${BASE_URL}/api/automation/v1/kv/test?nx=true&xx=true" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '"value"'
```

**Expected Result:**
- HTTP 400 or 422 validation error (cannot use both)

---

## Test Category 5: PATCH Operations (Nested Path Updates)

### TC-5.1: Update nested path in existing object

**Steps:**
```bash
# First set a JSON object
curl -X PUT "${BASE_URL}/api/automation/v1/kv/settings" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"app": {"theme": "light", "language": "en"}}'

# Patch a nested value
curl -X PATCH "${BASE_URL}/api/automation/v1/kv/settings" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"path": "app.theme", "value": "dark"}'
```

**Expected Result:**
- HTTP 200 response
- Response: `{"key": "settings", "path": "app.theme", "value": "dark"}`

---

### TC-5.2: Verify patched value persisted correctly

**Steps:**
```bash
curl -X GET "${BASE_URL}/api/automation/v1/kv/settings" \
  -H "Authorization: Bearer ${KV_TOKEN}"
```

**Expected Result:**
- Response shows `{"app": {"theme": "dark", "language": "en"}}`

---

### TC-5.3: Patch non-existent key

**Steps:**
```bash
curl -X PATCH "${BASE_URL}/api/automation/v1/kv/nonexistent" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"path": "some.path", "value": "value"}'
```

**Expected Result:**
- HTTP 404 Not Found

---

### TC-5.4: Patch with invalid path (parent doesn't exist)

**Steps:**
```bash
curl -X PATCH "${BASE_URL}/api/automation/v1/kv/settings" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"path": "nonexistent.deep.path", "value": "value"}'
```

**Expected Result:**
- HTTP 400 or appropriate error indicating path is invalid

---

## Test Category 6: Atomic Increment/Decrement Operations

### TC-6.1: INCR on non-existent key (should initialize to 1)

**Steps:**
```bash
curl -X POST "${BASE_URL}/api/automation/v1/kv/counter/incr" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json"
```

**Expected Result:**
- HTTP 200 response
- Response: `{"key": "counter", "value": 1}`

---

### TC-6.2: INCR on existing numeric key

**Steps:**
```bash
curl -X POST "${BASE_URL}/api/automation/v1/kv/counter/incr" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json"
```

**Expected Result:**
- HTTP 200 response
- Response: `{"key": "counter", "value": 2}`

---

### TC-6.3: INCR with custom increment value

**Steps:**
```bash
curl -X POST "${BASE_URL}/api/automation/v1/kv/counter/incr" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"by": 5}'
```

**Expected Result:**
- HTTP 200 response
- Value incremented by 5

---

### TC-6.4: DECR on existing key

**Steps:**
```bash
curl -X POST "${BASE_URL}/api/automation/v1/kv/counter/decr" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json"
```

**Expected Result:**
- HTTP 200 response
- Value decremented by 1

---

### TC-6.5: DECR on non-existent key (should initialize to -1)

**Steps:**
```bash
curl -X POST "${BASE_URL}/api/automation/v1/kv/new-counter/decr" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json"
```

**Expected Result:**
- HTTP 200 response
- Response: `{"key": "new-counter", "value": -1}`

---

### TC-6.6: INCR on non-numeric value (should error)

**Steps:**
```bash
# First set a string value
curl -X PUT "${BASE_URL}/api/automation/v1/kv/string-val" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '"hello"'

# Try to increment
curl -X POST "${BASE_URL}/api/automation/v1/kv/string-val/incr" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json"
```

**Expected Result:**
- HTTP 400 or 422 error indicating value is not numeric

---

## Test Category 7: List Operations

### TC-7.1: LPUSH to create new list

**Steps:**
```bash
curl -X POST "${BASE_URL}/api/automation/v1/kv/mylist/lpush" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"value": "first"}'
```

**Expected Result:**
- HTTP 200 response
- Response: `{"key": "mylist", "length": 1}`

---

### TC-7.2: LPUSH to existing list (adds to front)

**Steps:**
```bash
curl -X POST "${BASE_URL}/api/automation/v1/kv/mylist/lpush" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"value": "second"}'
```

**Expected Result:**
- HTTP 200 response
- Response: `{"key": "mylist", "length": 2}`

---

### TC-7.3: RPUSH to list (adds to back)

**Steps:**
```bash
curl -X POST "${BASE_URL}/api/automation/v1/kv/mylist/rpush" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"value": "third"}'
```

**Expected Result:**
- HTTP 200 response
- Response: `{"key": "mylist", "length": 3}`

---

### TC-7.4: GET list length

**Steps:**
```bash
curl -X GET "${BASE_URL}/api/automation/v1/kv/mylist/len" \
  -H "Authorization: Bearer ${KV_TOKEN}"
```

**Expected Result:**
- HTTP 200 response
- Response: `{"key": "mylist", "length": 3}`

---

### TC-7.5: LPOP from list (removes from front)

**Steps:**
```bash
curl -X POST "${BASE_URL}/api/automation/v1/kv/mylist/lpop" \
  -H "Authorization: Bearer ${KV_TOKEN}"
```

**Expected Result:**
- HTTP 200 response
- Response: `{"key": "mylist", "value": "second"}` (the item added via LPUSH last)

---

### TC-7.6: RPOP from list (removes from back)

**Steps:**
```bash
curl -X POST "${BASE_URL}/api/automation/v1/kv/mylist/rpop" \
  -H "Authorization: Bearer ${KV_TOKEN}"
```

**Expected Result:**
- HTTP 200 response
- Response: `{"key": "mylist", "value": "third"}`

---

### TC-7.7: LPOP from empty list

**Steps:**
```bash
# Pop all remaining items first
curl -X POST "${BASE_URL}/api/automation/v1/kv/mylist/lpop" \
  -H "Authorization: Bearer ${KV_TOKEN}"

# Try to pop from empty list
curl -X POST "${BASE_URL}/api/automation/v1/kv/mylist/lpop" \
  -H "Authorization: Bearer ${KV_TOKEN}"
```

**Expected Result:**
- HTTP 200 response
- Response: `{"key": "mylist", "value": null}`

---

### TC-7.8: List operations on non-list value

**Steps:**
```bash
# First set a non-list value
curl -X PUT "${BASE_URL}/api/automation/v1/kv/notalist" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '"just a string"'

# Try to LPUSH
curl -X POST "${BASE_URL}/api/automation/v1/kv/notalist/lpush" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"value": "item"}'
```

**Expected Result:**
- HTTP 400 or 422 error indicating value is not a list

---

### TC-7.9: LEN on non-existent key

**Steps:**
```bash
curl -X GET "${BASE_URL}/api/automation/v1/kv/nonexistent-list/len" \
  -H "Authorization: Bearer ${KV_TOKEN}"
```

**Expected Result:**
- HTTP 404 Not Found or `{"key": "nonexistent-list", "length": 0}`

---

## Test Category 8: Key Validation and Edge Cases

### TC-8.1: Key with special characters

**Steps:**
```bash
curl -X PUT "${BASE_URL}/api/automation/v1/kv/key-with-dashes_and_underscores" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '"value"'
```

**Expected Result:**
- HTTP 200 response
- Key stored successfully

---

### TC-8.2: Key with URL-encoded characters

**Steps:**
```bash
curl -X PUT "${BASE_URL}/api/automation/v1/kv/key%20with%20spaces" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '"value"'
```

**Expected Result:**
- Either stored with decoded key name or returns appropriate error

---

### TC-8.3: Empty key name

**Steps:**
```bash
curl -X PUT "${BASE_URL}/api/automation/v1/kv/" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '"value"'
```

**Expected Result:**
- HTTP 404 or 422 validation error

---

### TC-8.4: Very long key name

**Steps:**
```bash
LONG_KEY=$(python3 -c "print('k' * 1000)")
curl -X PUT "${BASE_URL}/api/automation/v1/kv/${LONG_KEY}" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '"value"'
```

**Expected Result:**
- Either stored successfully or returns validation error with length limit

---

### TC-8.5: Very large value

**Steps:**
```bash
LARGE_VALUE=$(python3 -c "import json; print(json.dumps({'data': 'x' * 100000}))")
curl -X PUT "${BASE_URL}/api/automation/v1/kv/large-value" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "${LARGE_VALUE}"
```

**Expected Result:**
- Either stored successfully or returns error with size limit

---

### TC-8.6: Store null value

**Steps:**
```bash
curl -X PUT "${BASE_URL}/api/automation/v1/kv/null-key" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d 'null'
```

**Expected Result:**
- HTTP 200 or appropriate handling of null values

---

### TC-8.7: Store various JSON types

**Steps:**
```bash
# Array
curl -X PUT "${BASE_URL}/api/automation/v1/kv/array-key" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '[1, 2, 3, "four", true]'

# Number
curl -X PUT "${BASE_URL}/api/automation/v1/kv/number-key" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '42.5'

# Boolean
curl -X PUT "${BASE_URL}/api/automation/v1/kv/bool-key" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d 'true'
```

**Expected Result:**
- All types stored and retrieved correctly

---

## Test Category 9: Isolation and Security

### TC-9.1: Verify KV data is not accessible from disabled automation

**Steps:**
1. Create automation with `enable_kv_store: false`
2. Dispatch a run
3. Verify `AUTOMATION_KV_TOKEN` is NOT present in the run environment

**Expected Result:**
- Token should not be provided for automations without KV enabled

---

### TC-9.2: Cross-automation isolation

**Steps:**
1. Create automation A with KV enabled, store key "shared-name"
2. Create automation B with KV enabled, store key "shared-name"
3. Get "shared-name" from automation A
4. Verify it returns automation A's value (not B's)

**Expected Result:**
- Each automation has completely isolated key namespace

---

### TC-9.3: Token expiration (if applicable)

**Steps:**
1. Get a KV token from a run
2. Wait beyond expected expiration (if documented)
3. Try to use the token

**Expected Result:**
- Token should be rejected after expiration

---

## Test Category 10: End-to-End Integration Tests

### TC-10.1: Full automation workflow with KV store

**Steps:**
1. Create a prompt automation with state tracking logic:
```bash
curl -X POST "${BASE_URL}/api/automation/v1/preset/prompt" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Counter Automation",
    "prompt": "Increment a counter stored in KV store under key \"run_count\". Print the current count.",
    "trigger": {"type": "cron", "schedule": "0 0 1 1 *", "timezone": "UTC"}
  }'
```

2. Dispatch the automation multiple times
3. Verify the counter persists between runs

**Expected Result:**
- Each run should see and increment the counter

---

### TC-10.2: Verify data persists across service restarts

**Steps:**
1. Store data via KV API
2. (Request service restart if possible in staging)
3. Retrieve the stored data

**Expected Result:**
- Data persists across restarts (stored in PostgreSQL)

---

### TC-10.3: Concurrent access handling

**Steps:**
1. Use multiple parallel INCR operations on same key:
```bash
for i in {1..10}; do
  curl -X POST "${BASE_URL}/api/automation/v1/kv/concurrent-counter/incr" \
    -H "Authorization: Bearer ${KV_TOKEN}" &
done
wait
```
2. Check final counter value

**Expected Result:**
- Counter should equal 10 (atomic operations)

---

## Test Category 11: Error Handling

### TC-11.1: Invalid JSON body

**Steps:**
```bash
curl -X PUT "${BASE_URL}/api/automation/v1/kv/test" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d 'not valid json'
```

**Expected Result:**
- HTTP 422 with clear error message

---

### TC-11.2: Missing required fields in PATCH

**Steps:**
```bash
curl -X PATCH "${BASE_URL}/api/automation/v1/kv/test" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Expected Result:**
- HTTP 422 with validation error about missing `path` and `value`

---

### TC-11.3: Invalid Content-Type header

**Steps:**
```bash
curl -X PUT "${BASE_URL}/api/automation/v1/kv/test" \
  -H "Authorization: Bearer ${KV_TOKEN}" \
  -H "Content-Type: text/plain" \
  -d 'plain text'
```

**Expected Result:**
- HTTP 415 or 422 indicating wrong content type

---

## Test Results Summary

| Test ID | Description | Status | Notes |
|---------|-------------|--------|-------|
| TC-1.1 | Create automation with KV store | ✅ | `enable_kv_store` defaults to `false` |
| TC-1.2 | Create with explicit enable_kv_store=true | ⬜ | Raw API not tested |
| TC-1.3 | Create with enable_kv_store=false | ⬜ | |
| TC-1.4 | Update automation KV flag | ✅ | PATCH works correctly |
| TC-2.1 | Access without token | ✅ | Returns 422 "Field required" |
| TC-2.2 | Access with invalid token | ✅ | Returns "Invalid token: Not enough segments" |
| TC-2.3 | Access with API key (not KV token) | ✅ | Correctly rejected |
| TC-2.4 | Cross-automation token scoping | ⚠️ | BLOCKED - token not injected |
| TC-3.1 | Set simple string | ⚠️ | BLOCKED - no token |
| TC-3.2 | Set JSON object | ⚠️ | BLOCKED - no token |
| TC-3.3 | Get value | ⚠️ | BLOCKED - no token |
| TC-3.4 | Get with metadata | ⚠️ | BLOCKED - no token |
| TC-3.5 | Get nested path | ⚠️ | BLOCKED - no token |
| TC-3.6 | Get non-existent key | ⚠️ | BLOCKED - no token |
| TC-3.7 | Get non-existent nested path | ⚠️ | BLOCKED - no token |
| TC-3.8 | Delete key | ⚠️ | BLOCKED - no token |
| TC-3.9 | Delete non-existent key | ⚠️ | BLOCKED - no token |
| TC-3.10 | List keys | ⚠️ | BLOCKED - no token |
| TC-4.1 | SET NX - key doesn't exist | ⚠️ | BLOCKED - no token |
| TC-4.2 | SET NX - key exists | ⚠️ | BLOCKED - no token |
| TC-4.3 | SET XX - key exists | ⚠️ | BLOCKED - no token |
| TC-4.4 | SET XX - key doesn't exist | ⚠️ | BLOCKED - no token |
| TC-4.5 | SET NX+XX (invalid) | ⚠️ | BLOCKED - no token |
| TC-5.1 | PATCH nested path | ⚠️ | BLOCKED - no token |
| TC-5.2 | Verify patched value | ⚠️ | BLOCKED - no token |
| TC-5.3 | PATCH non-existent key | ⚠️ | BLOCKED - no token |
| TC-5.4 | PATCH invalid path | ⚠️ | BLOCKED - no token |
| TC-6.1 | INCR new key | ⚠️ | BLOCKED - no token |
| TC-6.2 | INCR existing key | ⚠️ | BLOCKED - no token |
| TC-6.3 | INCR with custom value | ⚠️ | BLOCKED - no token |
| TC-6.4 | DECR existing key | ⚠️ | BLOCKED - no token |
| TC-6.5 | DECR new key | ⚠️ | BLOCKED - no token |
| TC-6.6 | INCR non-numeric | ⚠️ | BLOCKED - no token |
| TC-7.1 | LPUSH new list | ⚠️ | BLOCKED - no token |
| TC-7.2 | LPUSH existing list | ⚠️ | BLOCKED - no token |
| TC-7.3 | RPUSH | ⚠️ | BLOCKED - no token |
| TC-7.4 | LEN | ⚠️ | BLOCKED - no token |
| TC-7.5 | LPOP | ⚠️ | BLOCKED - no token |
| TC-7.6 | RPOP | ⚠️ | BLOCKED - no token |
| TC-7.7 | LPOP empty list | ⚠️ | BLOCKED - no token |
| TC-7.8 | List op on non-list | ⚠️ | BLOCKED - no token |
| TC-7.9 | LEN non-existent | ⚠️ | BLOCKED - no token |
| TC-8.1 | Special characters in key | ⚠️ | BLOCKED - no token |
| TC-8.2 | URL-encoded key | ⚠️ | BLOCKED - no token |
| TC-8.3 | Empty key | ⚠️ | BLOCKED - no token |
| TC-8.4 | Long key | ⚠️ | BLOCKED - no token |
| TC-8.5 | Large value | ⚠️ | BLOCKED - no token |
| TC-8.6 | Null value | ⚠️ | BLOCKED - no token |
| TC-8.7 | Various JSON types | ⚠️ | BLOCKED - no token |
| TC-9.1 | KV disabled automation | ⚠️ | BLOCKED - no token |
| TC-9.2 | Cross-automation isolation | ⚠️ | BLOCKED - no token |
| TC-9.3 | Token expiration | ⚠️ | BLOCKED - no token |
| TC-10.1 | Full workflow | ⚠️ | BLOCKED - no token |
| TC-10.2 | Data persistence | ⚠️ | BLOCKED - no token |
| TC-10.3 | Concurrent access | ⚠️ | BLOCKED - no token |
| TC-11.1 | Invalid JSON | ⚠️ | BLOCKED - no token |
| TC-11.2 | Missing required fields | ⚠️ | BLOCKED - no token |
| TC-11.3 | Invalid Content-Type | ⚠️ | BLOCKED - no token |

**Legend:** ⬜ Not Tested | ✅ Pass | ❌ Fail | ⚠️ Blocked

---

## Initial Validation Results

The following tests were executed during test plan creation to validate the API is functional:

### TC-1.1: Create automation with prompt preset ✅
**Response:**
```json
{
  "id": "efad83bf-b717-4512-b117-f5ebea9d9d44",
  "name": "KV Test Automation",
  "enable_kv_store": false,  // Default value confirmed
  "enabled": true,
  ...
}
```
**Finding:** The `enable_kv_store` field defaults to `false` for prompt preset automations.

### TC-1.4: Update automation to enable KV store ✅
**Response:**
```json
{
  "id": "efad83bf-b717-4512-b117-f5ebea9d9d44",
  "enable_kv_store": true,
  "updated_at": "2026-04-25T01:35:28.416446Z"
}
```
**Finding:** PATCH endpoint correctly updates the `enable_kv_store` flag.

### TC-2.1: Access KV API without token ✅
**Response:**
```json
{
  "detail": [{"type": "missing", "loc": ["header", "authorization"], "msg": "Field required"}]
}
```
**Finding:** API correctly rejects requests without authorization header (HTTP 422).

### TC-2.2: Access KV API with invalid token ✅
**Response:**
```json
{"detail": "Invalid token: Not enough segments"}
```
**Finding:** API correctly rejects malformed tokens.

### TC-2.3: Access KV API with API key (not KV token) ✅
**Response:**
```json
{"detail": "Invalid token: Not enough segments"}
```
**Finding:** API correctly rejects regular API keys - KV store requires specific JWT tokens generated during automation runs.

### Dispatch and Run ✅
Successfully dispatched a run which transitioned from `PENDING` to `RUNNING` status.

---

## Verification Strategy

Since the KV API requires a special JWT token (`AUTOMATION_KV_TOKEN`) that's **only available inside the automation sandbox**, direct curl testing of KV operations is not possible from outside. Instead, use these verification approaches:

### Approach 1: Prompt-Based Testing (Recommended)

Create automations with prompts that instruct the agent to perform KV operations and report results. The agent has access to the KV token via environment variable.

**Example Test Automation Prompts:**

```bash
# TC-3.1 through TC-3.10: Basic CRUD Operations
curl -X POST "${BASE_URL}/api/automation/v1/preset/prompt" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "KV Basic CRUD Test",
    "prompt": "Test the KV store API by performing these operations and reporting all results:\n\n1. SET key \"test-string\" with value \"hello world\"\n2. GET key \"test-string\" and print the response\n3. SET key \"test-json\" with value {\"name\": \"test\", \"count\": 42}\n4. GET key \"test-json\" and print the response\n5. GET key \"test-json\" with path=\"name\" and print the response\n6. GET key \"test-json\" with meta=true and print the response\n7. LIST all keys and print the response\n8. DELETE key \"test-string\" and print the response\n9. GET key \"test-string\" (should return 404)\n10. GET key \"nonexistent\" (should return 404)\n\nUse the AUTOMATION_KV_TOKEN environment variable for authentication. Print the full HTTP response (status code and body) for each operation.",
    "trigger": {"type": "cron", "schedule": "0 0 1 1 *", "timezone": "UTC"}
  }'

# Then enable KV and dispatch
curl -X PATCH "${BASE_URL}/api/automation/v1/{id}" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"enable_kv_store": true}'

curl -X POST "${BASE_URL}/api/automation/v1/{id}/dispatch" \
  -H "Authorization: Bearer ${API_KEY}"
```

**Verification:** Check the conversation/run logs in OpenHands UI for the operation results.

---

### Approach 2: Custom SDK Script Testing

For more controlled testing, create a custom automation with a Python script that performs KV operations:

**Step 1: Create test script (main.py)**
```python
import os
import httpx
import json

BASE_URL = os.environ.get("AUTOMATION_SERVICE_URL", "https://au-pr-69.staging.all-hands.dev")
KV_TOKEN = os.environ["AUTOMATION_KV_TOKEN"]

headers = {"Authorization": f"Bearer {KV_TOKEN}", "Content-Type": "application/json"}

def test_kv_operations():
    results = []
    
    # TC-3.1: SET string value
    r = httpx.put(f"{BASE_URL}/api/automation/v1/kv/test-key", headers=headers, json="hello world")
    results.append(f"TC-3.1 SET string: {r.status_code} - {r.json()}")
    
    # TC-3.2: SET JSON object
    r = httpx.put(f"{BASE_URL}/api/automation/v1/kv/config", headers=headers, 
                  json={"database": {"host": "localhost", "port": 5432}})
    results.append(f"TC-3.2 SET JSON: {r.status_code} - {r.json()}")
    
    # TC-3.3: GET value
    r = httpx.get(f"{BASE_URL}/api/automation/v1/kv/test-key", headers=headers)
    results.append(f"TC-3.3 GET: {r.status_code} - {r.json()}")
    
    # TC-3.4: GET with metadata
    r = httpx.get(f"{BASE_URL}/api/automation/v1/kv/test-key?meta=true", headers=headers)
    results.append(f"TC-3.4 GET meta: {r.status_code} - {r.json()}")
    
    # TC-3.5: GET nested path
    r = httpx.get(f"{BASE_URL}/api/automation/v1/kv/config?path=database.port", headers=headers)
    results.append(f"TC-3.5 GET path: {r.status_code} - {r.json()}")
    
    # TC-3.10: LIST keys
    r = httpx.get(f"{BASE_URL}/api/automation/v1/kv", headers=headers)
    results.append(f"TC-3.10 LIST: {r.status_code} - {r.json()}")
    
    # TC-6.1: INCR new key
    r = httpx.post(f"{BASE_URL}/api/automation/v1/kv/counter/incr", headers=headers)
    results.append(f"TC-6.1 INCR new: {r.status_code} - {r.json()}")
    
    # TC-6.2: INCR existing
    r = httpx.post(f"{BASE_URL}/api/automation/v1/kv/counter/incr", headers=headers)
    results.append(f"TC-6.2 INCR existing: {r.status_code} - {r.json()}")
    
    # TC-7.1: LPUSH
    r = httpx.post(f"{BASE_URL}/api/automation/v1/kv/mylist/lpush", headers=headers, json={"value": "first"})
    results.append(f"TC-7.1 LPUSH: {r.status_code} - {r.json()}")
    
    # Print all results
    print("\\n=== KV TEST RESULTS ===")
    for result in results:
        print(result)
    print("=== END RESULTS ===\\n")

if __name__ == "__main__":
    test_kv_operations()
```

**Step 2:** Package as tarball, upload, and create automation with `enable_kv_store: true`.

---

### Approach 3: State Persistence Verification (Multi-Run)

To verify data persists between runs:

```bash
# Create automation that reads/writes a counter
curl -X POST "${BASE_URL}/api/automation/v1/preset/prompt" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "KV Persistence Test",
    "prompt": "1. Read the current value of KV key \"run_counter\" (may not exist on first run)\n2. If it exists, print the value. If not, print \"First run - no counter yet\"\n3. Increment the counter using INCR operation\n4. Print the new counter value\n5. Print \"Test complete - run this automation again to verify persistence\"",
    "trigger": {"type": "cron", "schedule": "0 0 1 1 *", "timezone": "UTC"}
  }'

# Enable KV store
curl -X PATCH "${BASE_URL}/api/automation/v1/{id}" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"enable_kv_store": true}'

# Dispatch run 1
curl -X POST "${BASE_URL}/api/automation/v1/{id}/dispatch" \
  -H "Authorization: Bearer ${API_KEY}"
# Expected: "First run - no counter yet", then counter = 1

# Dispatch run 2
curl -X POST "${BASE_URL}/api/automation/v1/{id}/dispatch" \
  -H "Authorization: Bearer ${API_KEY}"
# Expected: Previous value = 1, new counter = 2

# Dispatch run 3
curl -X POST "${BASE_URL}/api/automation/v1/{id}/dispatch" \
  -H "Authorization: Bearer ${API_KEY}"
# Expected: Previous value = 2, new counter = 3
```

**Verification:** Check each run's conversation logs to confirm counter increments correctly.

---

### Approach 4: Cross-Automation Isolation Test

```bash
# Create Automation A
curl -X POST "${BASE_URL}/api/automation/v1/preset/prompt" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Isolation Test A",
    "prompt": "Set KV key \"shared-name\" to value \"I am Automation A\". Then read and print it.",
    "trigger": {"type": "cron", "schedule": "0 0 1 1 *"}
  }'
# Enable KV, dispatch, note the automation_id as A_ID

# Create Automation B  
curl -X POST "${BASE_URL}/api/automation/v1/preset/prompt" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Isolation Test B",
    "prompt": "Set KV key \"shared-name\" to value \"I am Automation B\". Then read and print it.",
    "trigger": {"type": "cron", "schedule": "0 0 1 1 *"}
  }'
# Enable KV, dispatch, note the automation_id as B_ID

# Run A again
curl -X POST "${BASE_URL}/api/automation/v1/{A_ID}/dispatch" \
  -H "Authorization: Bearer ${API_KEY}"
# Verification: Should still print "I am Automation A" (not B's value)
```

---

## Notes for Testers

1. **Token is Sandbox-Only:** The `AUTOMATION_KV_TOKEN` env var is injected into the sandbox at runtime. You cannot extract it externally - all KV testing must happen through automation runs.

2. **Preset vs Raw API:** The prompt preset (`/preset/prompt`) does not expose `enable_kv_store` - use PATCH to enable it after creation.

3. **Token Scope:** Each token is scoped to a specific automation ID for strict isolation.

4. **Checking Results:** View run results in the OpenHands UI conversation view, or query the runs API for status/errors.

---

## Appendix A: Consolidated Test Automation

This single automation runs most KV test cases and reports results. Create it, enable KV, and dispatch:

```bash
curl -X POST "${BASE_URL}/api/automation/v1/preset/prompt" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "KV Store Comprehensive Test Suite",
    "prompt": "Execute the following KV store test cases using the AUTOMATION_KV_TOKEN environment variable. For each test, print the test ID, operation, HTTP status code, and response body.\n\n## Basic Operations\n1. [TC-3.1] PUT /kv/test-string with body: \"hello world\"\n2. [TC-3.2] PUT /kv/config with body: {\"database\": {\"host\": \"localhost\", \"port\": 5432}, \"debug\": true}\n3. [TC-3.3] GET /kv/test-string\n4. [TC-3.4] GET /kv/test-string?meta=true\n5. [TC-3.5] GET /kv/config?path=database.port\n6. [TC-3.6] GET /kv/nonexistent-key (expect 404)\n7. [TC-3.10] GET /kv (list all keys)\n\n## Conditional Operations\n8. [TC-4.1] PUT /kv/nx-test?nx=true with body: \"first\"\n9. [TC-4.2] PUT /kv/nx-test?nx=true with body: \"second\" (should fail - key exists)\n10. [TC-4.4] PUT /kv/xx-test?xx=true with body: \"value\" (should fail - key does not exist)\n11. [TC-4.3] PUT /kv/nx-test?xx=true with body: \"updated\" (should succeed)\n\n## PATCH Operations\n12. [TC-5.1] PATCH /kv/config with body: {\"path\": \"database.port\", \"value\": 5433}\n13. [TC-5.2] GET /kv/config (verify port changed to 5433)\n14. [TC-5.3] PATCH /kv/nonexistent with body: {\"path\": \"x\", \"value\": 1} (expect 404)\n\n## Increment/Decrement\n15. [TC-6.1] POST /kv/counter/incr (new key, expect value: 1)\n16. [TC-6.2] POST /kv/counter/incr (expect value: 2)\n17. [TC-6.3] POST /kv/counter/incr with body: {\"by\": 5} (expect value: 7)\n18. [TC-6.4] POST /kv/counter/decr (expect value: 6)\n19. [TC-6.5] POST /kv/new-counter/decr (new key, expect value: -1)\n20. [TC-6.6] POST /kv/test-string/incr (expect error - not numeric)\n\n## List Operations\n21. [TC-7.1] POST /kv/mylist/lpush with body: {\"value\": \"first\"} (expect length: 1)\n22. [TC-7.2] POST /kv/mylist/lpush with body: {\"value\": \"second\"} (expect length: 2)\n23. [TC-7.3] POST /kv/mylist/rpush with body: {\"value\": \"third\"} (expect length: 3)\n24. [TC-7.4] GET /kv/mylist/len (expect length: 3)\n25. [TC-7.5] POST /kv/mylist/lpop (expect value: \"second\")\n26. [TC-7.6] POST /kv/mylist/rpop (expect value: \"third\")\n27. [TC-7.7] POST /kv/mylist/lpop then POST /kv/mylist/lpop again (second should return null)\n28. [TC-7.8] POST /kv/test-string/lpush with body: {\"value\": \"x\"} (expect error - not a list)\n\n## Cleanup\n29. [TC-3.8] DELETE /kv/test-string\n30. [TC-3.9] DELETE /kv/nonexistent-key (expect deleted: false)\n\n## Final Summary\nPrint a summary table with Pass/Fail for each test case.\n\nBase URL for KV API: Use the automation service URL + /api/automation/v1/kv",
    "trigger": {"type": "cron", "schedule": "0 0 1 1 *", "timezone": "UTC"}
  }'

# Save the automation ID, then:
AUTOMATION_ID="<id-from-response>"

# Enable KV store
curl -X PATCH "${BASE_URL}/api/automation/v1/${AUTOMATION_ID}" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"enable_kv_store": true}'

# Dispatch the test run
curl -X POST "${BASE_URL}/api/automation/v1/${AUTOMATION_ID}/dispatch" \
  -H "Authorization: Bearer ${API_KEY}"

# Check results in OpenHands UI conversation view
```

---

## Appendix B: Quick Reference Commands

### Create automation with KV enabled (prompt preset)
```bash
curl -X POST "${BASE_URL}/api/automation/v1/preset/prompt" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test Automation",
    "prompt": "Test prompt",
    "trigger": {"type": "cron", "schedule": "0 0 1 1 *"}
  }'
```

### Dispatch automation run
```bash
curl -X POST "${BASE_URL}/api/automation/v1/{automation_id}/dispatch" \
  -H "Authorization: Bearer ${API_KEY}"
```

### List runs
```bash
curl "${BASE_URL}/api/automation/v1/{automation_id}/runs" \
  -H "Authorization: Bearer ${API_KEY}"
```

### Delete automation
```bash
curl -X DELETE "${BASE_URL}/api/automation/v1/{automation_id}" \
  -H "Authorization: Bearer ${API_KEY}"
```
