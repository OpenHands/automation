#!/usr/bin/env python3
"""End-to-end test for KV store functionality with full stdout/stderr capture.

This script:
1. Creates a real automation via API (with enable_kv_store=true)
2. Generates a KV token for that automation
3. Uses run_automation() to execute a test script with full output capture
4. Cleans up the automation

Usage:
    export OPENHANDS_API_KEY="sk-oh-..."
    export AUTOMATION_KV_SECRET="<same-as-staging>"  # Required for token generation
    python scripts/test_kv_e2e.py

    # Optional: specify staging URL
    export OPENHANDS_API_URL="https://staging.all-hands.dev"
"""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import httpx

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from automation.execution import build_tarball, run_automation
from automation.utils.kv import create_kv_token


# ---------------------------------------------------------------------------
# Test script that runs inside the sandbox
# ---------------------------------------------------------------------------

KV_TEST_SCRIPT = '''
"""KV store test script - runs inside sandbox.

Supports two modes:
  --quick     Smoke test: one test per operation type (~8 tests)
  --thorough  Full coverage: all edge cases and error paths (~40 tests)

Default is --quick if no argument provided.
"""

import json
import os
import sys

# Use urllib since requests may not be installed
from urllib.request import Request, urlopen
from urllib.error import HTTPError


# Test registry
QUICK_TESTS = []
THOROUGH_TESTS = []


def quick(fn):
    """Decorator to mark a test as part of quick suite."""
    QUICK_TESTS.append(fn)
    THOROUGH_TESTS.append(fn)
    return fn


def thorough(fn):
    """Decorator to mark a test as thorough-only."""
    THOROUGH_TESTS.append(fn)
    return fn


def api_call(method, path, body=None, headers=None):
    """Make an HTTP request to the KV API."""
    url = f"{API_URL}/api/automation/v1/kv{path}"
    req_headers = {"Authorization": f"Bearer {KV_TOKEN}"}
    if headers:
        req_headers.update(headers)
    
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    
    req = Request(url, data=data, headers=req_headers, method=method)
    
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {"error": str(e)}
        return e.code, body


def api_call_raw(method, path, body=None, headers=None, auth=True):
    """Make HTTP request with optional auth control (for auth tests)."""
    url = f"{API_URL}/api/automation/v1/kv{path}"
    req_headers = {}
    if auth:
        req_headers["Authorization"] = f"Bearer {KV_TOKEN}"
    if headers:
        req_headers.update(headers)
    
    data = None
    if body is not None:
        if isinstance(body, bytes):
            data = body
        else:
            data = json.dumps(body).encode("utf-8")
            if "Content-Type" not in req_headers:
                req_headers["Content-Type"] = "application/json"
    
    req = Request(url, data=data, headers=req_headers, method=method)
    
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except HTTPError as e:
        return e.code, e.read().decode("utf-8")


# ===========================================================================
# QUICK TESTS - Core functionality smoke tests
# ===========================================================================

@quick
def test_set_get():
    """[TC-3.1/3.3] Basic SET and GET operations."""
    print("\\n[TEST] SET and GET")
    
    # SET
    status, resp = api_call("PUT", "/test_key", {"message": "hello", "count": 42})
    print(f"  PUT /test_key: {status}")
    if status not in (200, 201):
        print(f"  FAIL: {resp}")
        return False
    
    # GET
    status, resp = api_call("GET", "/test_key")
    print(f"  GET /test_key: {status}")
    if status != 200:
        print(f"  FAIL: {resp}")
        return False
    
    expected = {"message": "hello", "count": 42}
    if resp.get("value") != expected:
        print(f"  FAIL: Expected {expected}, got {resp.get('value')}")
        return False
    
    print("  PASS")
    return True


@quick
def test_delete():
    """[TC-3.8] DELETE operation."""
    print("\\n[TEST] DELETE")
    
    api_call("PUT", "/to_delete", "bye")
    
    status, resp = api_call("DELETE", "/to_delete")
    print(f"  DELETE /to_delete: {status}")
    if status != 200:
        print(f"  FAIL: Expected 200, got {status}")
        return False
    
    # Verify gone
    status, resp = api_call("GET", "/to_delete")
    print(f"  GET after delete: {status}")
    if status != 404:
        print(f"  FAIL: Expected 404, got {status}")
        return False
    
    print("  PASS")
    return True


@quick
def test_incr_decr():
    """[TC-6.2/6.4] INCR and DECR on existing key."""
    print("\\n[TEST] INCR and DECR")
    
    api_call("PUT", "/counter", 10)
    
    status, resp = api_call("POST", "/counter/incr", {"by": 5})
    print(f"  INCR by 5: {status}, value={resp.get('value')}")
    if resp.get("value") != 15:
        print(f"  FAIL: Expected 15, got {resp.get('value')}")
        return False
    
    status, resp = api_call("POST", "/counter/decr", {"by": 3})
    print(f"  DECR by 3: {status}, value={resp.get('value')}")
    if resp.get("value") != 12:
        print(f"  FAIL: Expected 12, got {resp.get('value')}")
        return False
    
    print("  PASS")
    return True


@quick
def test_list_operations():
    """[TC-7.1-7.6] List RPUSH, LPUSH, LPOP, RPOP, LEN."""
    print("\\n[TEST] List operations")
    
    api_call("DELETE", "/my_list")
    
    # RPUSH to create list
    status, resp = api_call("POST", "/my_list/rpush", {"value": "a"})
    print(f"  RPUSH 'a': {status}, length={resp.get('length')}")
    if resp.get("length") != 1:
        print(f"  FAIL: Expected length 1")
        return False
    
    api_call("POST", "/my_list/rpush", {"value": "b"})
    api_call("POST", "/my_list/rpush", {"value": "c"})
    
    # LPUSH
    status, resp = api_call("POST", "/my_list/lpush", {"value": "z"})
    print(f"  LPUSH 'z': {status}, length={resp.get('length')}")
    
    # Verify order: [z, a, b, c]
    status, resp = api_call("GET", "/my_list")
    if resp.get("value") != ["z", "a", "b", "c"]:
        print(f"  FAIL: Expected ['z', 'a', 'b', 'c'], got {resp.get('value')}")
        return False
    
    # LPOP
    status, resp = api_call("POST", "/my_list/lpop")
    print(f"  LPOP: {status}, value={resp.get('value')}")
    if resp.get("value") != "z":
        print(f"  FAIL: Expected 'z'")
        return False
    
    # RPOP
    status, resp = api_call("POST", "/my_list/rpop")
    print(f"  RPOP: {status}, value={resp.get('value')}")
    if resp.get("value") != "c":
        print(f"  FAIL: Expected 'c'")
        return False
    
    # LEN
    status, resp = api_call("GET", "/my_list/len")
    print(f"  LEN: {status}, length={resp.get('length')}")
    if resp.get("length") != 2:
        print(f"  FAIL: Expected 2")
        return False
    
    print("  PASS")
    return True


@quick
def test_nested_path():
    """[TC-3.5/5.1] Nested path GET and PATCH."""
    print("\\n[TEST] Nested path operations")
    
    config = {"database": {"host": "localhost", "port": 5432}, "cache": {"enabled": True}}
    api_call("PUT", "/config", config)
    
    # PATCH nested value
    status, resp = api_call("PATCH", "/config", {"path": "database.port", "value": 5433})
    print(f"  PATCH database.port=5433: {status}")
    if status != 200:
        print(f"  FAIL: {resp}")
        return False
    
    # GET with path
    status, resp = api_call("GET", "/config?path=database.port")
    print(f"  GET with path: {status}, value={resp.get('value')}")
    if resp.get("value") != 5433:
        print(f"  FAIL: Expected 5433")
        return False
    
    print("  PASS")
    return True


@quick
def test_conditional_set():
    """[TC-4.1/4.2] Conditional SET with NX flag."""
    print("\\n[TEST] Conditional SET (nx)")
    
    api_call("DELETE", "/cond_key")
    
    # NX when key doesn't exist - should succeed
    status, resp = api_call("PUT", "/cond_key?nx=true", "first")
    print(f"  PUT with nx=true (new): {status}")
    if status != 201:
        print(f"  FAIL: Expected 201, got {status}")
        return False
    
    # NX when key exists - should fail
    status, resp = api_call("PUT", "/cond_key?nx=true", "second")
    print(f"  PUT with nx=true (exists): {status}")
    if status != 409:
        print(f"  FAIL: Expected 409, got {status}")
        return False
    
    # Verify value unchanged
    status, resp = api_call("GET", "/cond_key")
    if resp.get("value") != "first":
        print(f"  FAIL: Value should be 'first'")
        return False
    
    print("  PASS")
    return True


@quick
def test_list_keys():
    """[TC-3.10] List all keys."""
    print("\\n[TEST] List keys")
    
    api_call("PUT", "/list_test_a", "a")
    api_call("PUT", "/list_test_b", "b")
    
    status, resp = api_call("GET", "")
    print(f"  GET /kv: {status}, count={resp.get('count')}")
    if status != 200:
        print(f"  FAIL: {resp}")
        return False
    
    keys = resp.get("keys", [])
    if "list_test_a" not in keys or "list_test_b" not in keys:
        print(f"  FAIL: Expected keys to include list_test_a and list_test_b")
        return False
    
    print("  PASS")
    return True


@quick
def test_get_with_meta():
    """[TC-3.4] GET with metadata."""
    print("\\n[TEST] GET with metadata")
    
    api_call("PUT", "/meta_test", "value")
    
    status, resp = api_call("GET", "/meta_test?meta=true")
    print(f"  GET with meta=true: {status}")
    
    if "created_at" not in resp or "updated_at" not in resp:
        print(f"  FAIL: Missing timestamps")
        return False
    
    print(f"  created_at: {resp.get('created_at')}")
    print("  PASS")
    return True


# ===========================================================================
# THOROUGH TESTS - Edge cases, error paths, security
# ===========================================================================

@thorough
def test_get_nonexistent_key():
    """[TC-3.6] GET non-existent key returns 404."""
    print("\\n[TEST] GET non-existent key")
    
    status, resp = api_call("GET", "/definitely_does_not_exist_12345")
    print(f"  GET /nonexistent: {status}")
    if status != 404:
        print(f"  FAIL: Expected 404, got {status}")
        return False
    
    print("  PASS")
    return True


@thorough
def test_get_nonexistent_path():
    """[TC-3.7] GET non-existent nested path."""
    print("\\n[TEST] GET non-existent nested path")
    
    api_call("PUT", "/path_test", {"a": {"b": 1}})
    
    status, resp = api_call("GET", "/path_test?path=a.c.d")
    print(f"  GET with invalid path: {status}")
    # Should return 404 or null value
    if status not in (200, 404):
        print(f"  FAIL: Expected 200 or 404, got {status}")
        return False
    
    if status == 200 and resp.get("value") is not None:
        print(f"  FAIL: Expected null value for missing path")
        return False
    
    print("  PASS")
    return True


@thorough
def test_delete_nonexistent():
    """[TC-3.9] DELETE non-existent key."""
    print("\\n[TEST] DELETE non-existent key")
    
    status, resp = api_call("DELETE", "/never_existed_xyz")
    print(f"  DELETE /nonexistent: {status}, deleted={resp.get('deleted')}")
    
    # Should succeed but indicate nothing was deleted
    if status != 200:
        print(f"  FAIL: Expected 200, got {status}")
        return False
    
    if resp.get("deleted") is not False:
        print(f"  FAIL: Expected deleted=false")
        return False
    
    print("  PASS")
    return True


@thorough
def test_conditional_set_xx():
    """[TC-4.3/4.4] Conditional SET with XX flag."""
    print("\\n[TEST] Conditional SET (xx)")
    
    api_call("DELETE", "/xx_test")
    
    # XX when key doesn't exist - should fail
    status, resp = api_call("PUT", "/xx_test?xx=true", "value")
    print(f"  PUT with xx=true (missing): {status}")
    if status not in (404, 412):  # Either 404 Not Found or 412 Precondition Failed
        print(f"  FAIL: Expected 404 or 412, got {status}")
        return False
    
    # Create key first
    api_call("PUT", "/xx_test", "original")
    
    # XX when key exists - should succeed
    status, resp = api_call("PUT", "/xx_test?xx=true", "updated")
    print(f"  PUT with xx=true (exists): {status}")
    if status != 200:
        print(f"  FAIL: Expected 200, got {status}")
        return False
    
    print("  PASS")
    return True


@thorough
def test_patch_nonexistent():
    """[TC-5.3] PATCH non-existent key returns 404."""
    print("\\n[TEST] PATCH non-existent key")
    
    status, resp = api_call("PATCH", "/nonexistent_patch", {"path": "x", "value": 1})
    print(f"  PATCH /nonexistent: {status}")
    if status != 404:
        print(f"  FAIL: Expected 404, got {status}")
        return False
    
    print("  PASS")
    return True


@thorough
def test_incr_new_key():
    """[TC-6.1] INCR on non-existent key initializes to 1."""
    print("\\n[TEST] INCR new key")
    
    api_call("DELETE", "/new_incr_counter")
    
    status, resp = api_call("POST", "/new_incr_counter/incr")
    print(f"  INCR new key: {status}, value={resp.get('value')}")
    if resp.get("value") != 1:
        print(f"  FAIL: Expected 1, got {resp.get('value')}")
        return False
    
    print("  PASS")
    return True


@thorough
def test_decr_new_key():
    """[TC-6.5] DECR on non-existent key initializes to -1."""
    print("\\n[TEST] DECR new key")
    
    api_call("DELETE", "/new_decr_counter")
    
    status, resp = api_call("POST", "/new_decr_counter/decr")
    print(f"  DECR new key: {status}, value={resp.get('value')}")
    if resp.get("value") != -1:
        print(f"  FAIL: Expected -1, got {resp.get('value')}")
        return False
    
    print("  PASS")
    return True


@thorough
def test_incr_non_numeric():
    """[TC-6.6] INCR on non-numeric value returns error."""
    print("\\n[TEST] INCR non-numeric")
    
    api_call("PUT", "/string_val", "hello")
    
    status, resp = api_call("POST", "/string_val/incr")
    print(f"  INCR string value: {status}")
    if status != 400:
        print(f"  FAIL: Expected 400, got {status}")
        return False
    
    print("  PASS")
    return True


@thorough
def test_lpop_empty_list():
    """[TC-7.7] LPOP from empty list returns null."""
    print("\\n[TEST] LPOP empty list")
    
    api_call("PUT", "/empty_list", [])
    
    status, resp = api_call("POST", "/empty_list/lpop")
    print(f"  LPOP empty: {status}, value={resp.get('value')}")
    if resp.get("value") is not None:
        print(f"  FAIL: Expected null, got {resp.get('value')}")
        return False
    
    print("  PASS")
    return True


@thorough
def test_lpop_nonexistent():
    """[TC-7.7b] LPOP from non-existent key returns null."""
    print("\\n[TEST] LPOP non-existent key")
    
    api_call("DELETE", "/no_such_list")
    
    status, resp = api_call("POST", "/no_such_list/lpop")
    print(f"  LPOP nonexistent: {status}, value={resp.get('value')}")
    if resp.get("value") is not None:
        print(f"  FAIL: Expected null")
        return False
    
    print("  PASS")
    return True


@thorough
def test_push_to_non_list():
    """[TC-7.8] RPUSH to non-list value returns error."""
    print("\\n[TEST] RPUSH to non-list")
    
    api_call("PUT", "/not_a_list", {"key": "value"})
    
    status, resp = api_call("POST", "/not_a_list/rpush", {"value": "item"})
    print(f"  RPUSH to dict: {status}")
    if status != 400:
        print(f"  FAIL: Expected 400, got {status}")
        return False
    
    print("  PASS")
    return True


@thorough
def test_len_nonexistent():
    """[TC-7.9] LEN on non-existent key returns 404."""
    print("\\n[TEST] LEN non-existent key")
    
    api_call("DELETE", "/no_such_list_len")
    
    status, resp = api_call("GET", "/no_such_list_len/len")
    print(f"  LEN nonexistent: {status}")
    if status != 404:
        print(f"  FAIL: Expected 404, got {status}")
        return False
    
    print("  PASS")
    return True


@thorough
def test_special_characters_in_key():
    """[TC-8.1] Key with special characters."""
    print("\\n[TEST] Special characters in key")
    
    # Test with dashes, underscores, numbers
    key = "test-key_123"
    status, resp = api_call("PUT", f"/{key}", "value")
    print(f"  PUT /{key}: {status}")
    if status not in (200, 201):
        print(f"  FAIL: {resp}")
        return False
    
    status, resp = api_call("GET", f"/{key}")
    if resp.get("value") != "value":
        print(f"  FAIL: Value mismatch")
        return False
    
    print("  PASS")
    return True


@thorough
def test_null_value():
    """[TC-8.6] Store null value."""
    print("\\n[TEST] Store null value")
    
    status, resp = api_call("PUT", "/null_test", None)
    print(f"  PUT null: {status}")
    if status not in (200, 201):
        print(f"  FAIL: {resp}")
        return False
    
    status, resp = api_call("GET", "/null_test")
    if resp.get("value") is not None:
        print(f"  FAIL: Expected null, got {resp.get('value')}")
        return False
    
    print("  PASS")
    return True


@thorough
def test_various_json_types():
    """[TC-8.7] Store various JSON types."""
    print("\\n[TEST] Various JSON types")
    
    test_cases = [
        ("string_type", "hello"),
        ("number_int", 42),
        ("number_float", 3.14),
        ("boolean_true", True),
        ("boolean_false", False),
        ("array_type", [1, 2, 3]),
        ("nested_obj", {"a": {"b": {"c": 1}}}),
    ]
    
    for key, value in test_cases:
        status, _ = api_call("PUT", f"/type_{key}", value)
        if status not in (200, 201):
            print(f"  FAIL: PUT {key} returned {status}")
            return False
        
        status, resp = api_call("GET", f"/type_{key}")
        if resp.get("value") != value:
            print(f"  FAIL: {key} value mismatch: {resp.get('value')} != {value}")
            return False
        print(f"  {key}: OK")
    
    print("  PASS")
    return True


@thorough
def test_auth_missing_token():
    """[TC-2.1] Access without token returns 401."""
    print("\\n[TEST] Auth - missing token")
    
    global KV_TOKEN
    saved_token = KV_TOKEN
    KV_TOKEN = ""
    
    status, _ = api_call_raw("GET", "/test", auth=False)
    print(f"  GET without token: {status}")
    
    KV_TOKEN = saved_token
    
    if status not in (401, 403):
        print(f"  FAIL: Expected 401 or 403, got {status}")
        return False
    
    print("  PASS")
    return True


@thorough
def test_auth_invalid_token():
    """[TC-2.2] Access with invalid token returns 401."""
    print("\\n[TEST] Auth - invalid token")
    
    status, _ = api_call_raw("GET", "/test", headers={"Authorization": "Bearer invalid.token.here"})
    print(f"  GET with invalid token: {status}")
    
    if status not in (401, 403):
        print(f"  FAIL: Expected 401 or 403, got {status}")
        return False
    
    print("  PASS")
    return True


@thorough  
def test_invalid_json_body():
    """[TC-11.1] Invalid JSON body returns 400."""
    print("\\n[TEST] Invalid JSON body")
    
    status, _ = api_call_raw(
        "PUT", "/bad_json",
        body=b"not valid json {",
        headers={"Content-Type": "application/json"}
    )
    print(f"  PUT invalid JSON: {status}")
    
    if status != 400 and status != 422:
        print(f"  FAIL: Expected 400 or 422, got {status}")
        return False
    
    print("  PASS")
    return True


def main():
    global API_URL, KV_TOKEN
    
    API_URL = os.environ.get("OPENHANDS_CLOUD_API_URL", "").rstrip("/")
    KV_TOKEN = os.environ.get("AUTOMATION_KV_TOKEN", "")
    
    # Parse mode from command line
    mode = "quick"
    if len(sys.argv) > 1:
        if sys.argv[1] == "--thorough":
            mode = "thorough"
        elif sys.argv[1] == "--quick":
            mode = "quick"
    
    tests = QUICK_TESTS if mode == "quick" else THOROUGH_TESTS
    
    print("=" * 60)
    print(f"KV STORE E2E TEST ({mode.upper()} MODE)")
    print(f"Running {len(tests)} tests")
    print("=" * 60)
    print(f"API URL: {API_URL}")
    print(f"KV Token: {'present (' + str(len(KV_TOKEN)) + ' chars)' if KV_TOKEN else 'MISSING'}")
    
    if not API_URL:
        print("\\nFAIL: OPENHANDS_CLOUD_API_URL not set")
        sys.exit(1)
    
    if not KV_TOKEN:
        print("\\nFAIL: AUTOMATION_KV_TOKEN not set")
        sys.exit(1)
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            if test():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    
    print("\\n" + "=" * 60)
    print(f"RESULTS ({mode.upper()}): {passed} passed, {failed} failed")
    print("=" * 60)
    
    if failed == 0:
        print("\\nKV_STORE_ALL_TESTS_PASSED")
        sys.exit(0)
    else:
        print("\\nKV_STORE_TESTS_FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
'''


# Legacy test script reference (keeping for backwards compatibility)
KV_TEST_SCRIPT_QUICK = KV_TEST_SCRIPT.replace('mode = "quick"', 'mode = "quick"')
KV_TEST_SCRIPT_THOROUGH = KV_TEST_SCRIPT.replace('mode = "quick"', 'mode = "thorough"')


async def create_automation(client: httpx.AsyncClient, api_url: str, api_key: str) -> str:
    """Create a test automation with KV store enabled. Returns automation_id."""
    print("Creating automation with enable_kv_store=true...")
    
    resp = await client.post(
        f"{api_url}/api/automation/v1/preset/prompt",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "name": f"KV Store Test {uuid.uuid4().hex[:8]}",
            "prompt": "This is a test automation for KV store verification.",
            "trigger": {
                "type": "cron",
                "schedule": "0 0 1 1 *",  # Once a year (won't actually trigger)
                "timezone": "UTC",
            },
            "enable_kv_store": True,
        },
    )
    
    if resp.status_code != 201:
        print(f"Failed to create automation: {resp.status_code}")
        print(resp.text)
        sys.exit(1)
    
    data = resp.json()
    automation_id = data["id"]
    print(f"Created automation: {automation_id}")
    return automation_id


async def delete_automation(client: httpx.AsyncClient, api_url: str, api_key: str, automation_id: str):
    """Delete the test automation."""
    print(f"\nCleaning up automation {automation_id}...")
    resp = await client.delete(
        f"{api_url}/api/automation/v1/{automation_id}",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    if resp.status_code == 204:
        print("Automation deleted.")
    else:
        print(f"Warning: Failed to delete automation: {resp.status_code}")


async def main():
    # --- Configuration ---
    api_key = os.environ.get("OPENHANDS_API_KEY")
    kv_secret = os.environ.get("AUTOMATION_KV_SECRET")
    api_url = os.environ.get("OPENHANDS_API_URL", "https://staging.all-hands.dev").rstrip("/")
    
    # Parse mode from command line
    mode = "quick"
    if "--thorough" in sys.argv:
        mode = "thorough"
    
    print("=" * 70)
    print(f"KV STORE E2E TEST RUNNER ({mode.upper()} MODE)")
    print("=" * 70)
    print(f"API URL: {api_url}")
    print(f"API Key: {'present' if api_key else 'MISSING'}")
    print(f"KV Secret: {'present' if kv_secret else 'MISSING'}")
    print()
    
    if not api_key:
        print("ERROR: Set OPENHANDS_API_KEY environment variable")
        sys.exit(1)
    
    if not kv_secret:
        print("ERROR: Set AUTOMATION_KV_SECRET environment variable")
        print("       (Must match the secret configured in staging)")
        sys.exit(1)
    
    # Select test script based on mode
    test_script = KV_TEST_SCRIPT
    entrypoint = f"python main.py --{mode}"
    
    # --- Create automation via API ---
    automation_id = None
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            automation_id = await create_automation(client, api_url, api_key)
            automation_uuid = uuid.UUID(automation_id)
            
            # --- Generate KV token ---
            run_id = uuid.uuid4()
            kv_token = create_kv_token(
                secret=kv_secret,
                automation_id=automation_uuid,
                run_id=run_id,
            )
            print(f"Generated KV token for run_id={run_id}")
            
            # --- Build tarball ---
            print("\nBuilding test tarball...")
            tarball = build_tarball({
                "main.py": test_script,
            })
            print(f"Tarball size: {len(tarball)} bytes")
            
            # --- Run automation ---
            print("\n" + "-" * 70)
            print(f"EXECUTING IN SANDBOX ({mode.upper()} MODE)")
            print("-" * 70)
            
            result = await run_automation(
                api_url=api_url,
                api_key=api_key,
                entrypoint=entrypoint,
                tarball_source=tarball,
                env_vars={
                    "OPENHANDS_API_KEY": api_key,
                    "OPENHANDS_CLOUD_API_URL": api_url,
                    "AUTOMATION_KV_TOKEN": kv_token,
                    "AUTOMATION_ENABLE_KV_STORE": "true",
                },
                timeout=600 if mode == "thorough" else 300,
                keep_sandbox=False,
            )
            
            # --- Display results ---
            print("\n" + "=" * 70)
            print("EXECUTION RESULT")
            print("=" * 70)
            print(f"Success: {result.success}")
            print(f"Exit code: {result.exit_code}")
            print(f"Sandbox ID: {result.sandbox_id}")
            
            if result.stdout:
                print("\n" + "-" * 70)
                print("STDOUT")
                print("-" * 70)
                print(result.stdout)
            
            if result.stderr:
                print("\n" + "-" * 70)
                print("STDERR (last 3000 chars)")
                print("-" * 70)
                print(result.stderr[-3000:])
            
            if result.error:
                print("\n" + "-" * 70)
                print("ERROR")
                print("-" * 70)
                print(result.error)
            
            # --- Final verdict ---
            print("\n" + "=" * 70)
            if result.success and "KV_STORE_ALL_TESTS_PASSED" in result.stdout:
                print(f"✅ KV STORE E2E TEST PASSED ({mode.upper()} MODE)")
                print("=" * 70)
                return 0
            else:
                print(f"❌ KV STORE E2E TEST FAILED ({mode.upper()} MODE)")
                print("=" * 70)
                return 1
                
        finally:
            # --- Cleanup ---
            if automation_id:
                await delete_automation(client, api_url, api_key, automation_id)d_key?xx=true", "new")
    print(f"  PUT with xx=true (deleted): {status}")
    if status != 404:
        print(f"  FAIL: Expected 404, got {status}")
        return False
    
    print("  PASS")
    return True


def test_list_keys():
    """Test listing all keys."""
    print("\\n[TEST] List keys")
    
    # Create some known keys
    api_call("PUT", "/list_test_a", "a")
    api_call("PUT", "/list_test_b", "b")
    
    status, resp = api_call("GET", "")
    print(f"  GET /kv: {status}")
    
    keys = resp.get("keys", [])
    print(f"  Keys found: {len(keys)}")
    
    if "list_test_a" not in keys or "list_test_b" not in keys:
        print(f"  FAIL: Expected list_test_a and list_test_b in {keys}")
        return False
    
    print("  PASS")
    return True


def test_delete():
    """Test DELETE operation."""
    print("\\n[TEST] DELETE")
    
    # Create key
    api_call("PUT", "/to_delete", "bye")
    
    # Delete
    status, resp = api_call("DELETE", "/to_delete")
    print(f"  DELETE /to_delete: {status}")
    if status != 200:
        print(f"  FAIL: Expected 200, got {status}")
        return False
    
    # Verify gone
    status, resp = api_call("GET", "/to_delete")
    print(f"  GET after delete: {status}")
    if status != 404:
        print(f"  FAIL: Expected 404, got {status}")
        return False
    
    print("  PASS")
    return True


def test_get_with_meta():
    """Test GET with meta=true."""
    print("\\n[TEST] GET with metadata")
    
    api_call("PUT", "/meta_test", "value")
    
    status, resp = api_call("GET", "/meta_test?meta=true")
    print(f"  GET with meta=true: {status}")
    
    if "created_at" not in resp or "updated_at" not in resp:
        print(f"  FAIL: Missing timestamps in {resp}")
        return False
    
    print(f"  created_at: {resp.get('created_at')}")
    print(f"  updated_at: {resp.get('updated_at')}")
    print("  PASS")
    return True


def main():
    global API_URL, KV_TOKEN
    
    API_URL = os.environ.get("OPENHANDS_CLOUD_API_URL", "").rstrip("/")
    KV_TOKEN = os.environ.get("AUTOMATION_KV_TOKEN", "")
    
    print("=" * 60)
    print("KV STORE END-TO-END TEST")
    print("=" * 60)
    print(f"API URL: {API_URL}")
    print(f"KV Token: {'present (' + str(len(KV_TOKEN)) + ' chars)' if KV_TOKEN else 'MISSING'}")
    
    if not API_URL:
        print("\\nFAIL: OPENHANDS_CLOUD_API_URL not set")
        sys.exit(1)
    
    if not KV_TOKEN:
        print("\\nFAIL: AUTOMATION_KV_TOKEN not set")
        print("This means enable_kv_store is not enabled or KV secret is not configured")
        sys.exit(1)
    
    # Run all tests
    tests = [
        test_set_get,
        test_incr_decr,
        test_list_operations,
        test_nested_path,
        test_conditional_set,
        test_list_keys,
        test_delete,
        test_get_with_meta,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            if test():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            failed += 1
    
    print("\\n" + "=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("=" * 60)
    
    if failed == 0:
        print("\\nKV_STORE_ALL_TESTS_PASSED")
        sys.exit(0)
    else:
        print("\\nKV_STORE_TESTS_FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
'''


async def create_automation(client: httpx.AsyncClient, api_url: str, api_key: str) -> str:
    """Create a test automation with KV store enabled. Returns automation_id."""
    print("Creating automation with enable_kv_store=true...")
    
    resp = await client.post(
        f"{api_url}/api/automation/v1/preset/prompt",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "name": f"KV Store Test {uuid.uuid4().hex[:8]}",
            "prompt": "This is a test automation for KV store verification.",
            "trigger": {
                "type": "cron",
                "schedule": "0 0 1 1 *",  # Once a year (won't actually trigger)
                "timezone": "UTC",
            },
            "enable_kv_store": True,
        },
    )
    
    if resp.status_code != 201:
        print(f"Failed to create automation: {resp.status_code}")
        print(resp.text)
        sys.exit(1)
    
    data = resp.json()
    automation_id = data["id"]
    print(f"Created automation: {automation_id}")
    return automation_id


async def delete_automation(client: httpx.AsyncClient, api_url: str, api_key: str, automation_id: str):
    """Delete the test automation."""
    print(f"\nCleaning up automation {automation_id}...")
    resp = await client.delete(
        f"{api_url}/api/automation/v1/{automation_id}",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    if resp.status_code == 204:
        print("Automation deleted.")
    else:
        print(f"Warning: Failed to delete automation: {resp.status_code}")


async def main():
    # --- Configuration ---
    api_key = os.environ.get("OPENHANDS_API_KEY")
    kv_secret = os.environ.get("AUTOMATION_KV_SECRET")
    api_url = os.environ.get("OPENHANDS_API_URL", "https://staging.all-hands.dev").rstrip("/")
    
    print("=" * 70)
    print("KV STORE E2E TEST RUNNER")
    print("=" * 70)
    print(f"API URL: {api_url}")
    print(f"API Key: {'present' if api_key else 'MISSING'}")
    print(f"KV Secret: {'present' if kv_secret else 'MISSING'}")
    print()
    
    if not api_key:
        print("ERROR: Set OPENHANDS_API_KEY environment variable")
        sys.exit(1)
    
    if not kv_secret:
        print("ERROR: Set AUTOMATION_KV_SECRET environment variable")
        print("       (Must match the secret configured in staging)")
        sys.exit(1)
    
    # --- Create automation via API ---
    automation_id = None
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            automation_id = await create_automation(client, api_url, api_key)
            automation_uuid = uuid.UUID(automation_id)
            
            # --- Generate KV token ---
            run_id = uuid.uuid4()
            kv_token = create_kv_token(
                secret=kv_secret,
                automation_id=automation_uuid,
                run_id=run_id,
            )
            print(f"Generated KV token for run_id={run_id}")
            
            # --- Build tarball ---
            print("\nBuilding test tarball...")
            tarball = build_tarball({
                "main.py": KV_TEST_SCRIPT,
            })
            print(f"Tarball size: {len(tarball)} bytes")
            
            # --- Run automation ---
            print("\n" + "-" * 70)
            print("EXECUTING IN SANDBOX")
            print("-" * 70)
            
            result = await run_automation(
                api_url=api_url,
                api_key=api_key,
                entrypoint="python main.py",
                tarball_source=tarball,
                env_vars={
                    "OPENHANDS_API_KEY": api_key,
                    "OPENHANDS_CLOUD_API_URL": api_url,
                    "AUTOMATION_KV_TOKEN": kv_token,
                    "AUTOMATION_ENABLE_KV_STORE": "true",
                },
                timeout=300,
                keep_sandbox=False,
            )
            
            # --- Display results ---
            print("\n" + "=" * 70)
            print("EXECUTION RESULT")
            print("=" * 70)
            print(f"Success: {result.success}")
            print(f"Exit code: {result.exit_code}")
            print(f"Sandbox ID: {result.sandbox_id}")
            
            if result.stdout:
                print("\n" + "-" * 70)
                print("STDOUT")
                print("-" * 70)
                print(result.stdout)
            
            if result.stderr:
                print("\n" + "-" * 70)
                print("STDERR (last 3000 chars)")
                print("-" * 70)
                print(result.stderr[-3000:])
            
            if result.error:
                print("\n" + "-" * 70)
                print("ERROR")
                print("-" * 70)
                print(result.error)
            
            # --- Final verdict ---
            print("\n" + "=" * 70)
            if result.success and "KV_STORE_ALL_TESTS_PASSED" in result.stdout:
                print("✅ KV STORE E2E TEST PASSED")
                print("=" * 70)
                return 0
            else:
                print("❌ KV STORE E2E TEST FAILED")
                print("=" * 70)
                return 1
                
        finally:
            # --- Cleanup ---
            if automation_id:
                await delete_automation(client, api_url, api_key, automation_id)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
