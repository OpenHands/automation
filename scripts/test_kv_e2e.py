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
"""KV store test script - runs inside sandbox."""

import json
import os
import sys

# Use urllib since requests may not be installed
from urllib.request import Request, urlopen
from urllib.error import HTTPError


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


def test_set_get():
    """Test basic SET and GET operations."""
    print("\\n[TEST] SET and GET")
    
    # SET
    status, resp = api_call("PUT", "/test_key", {"message": "hello", "count": 42})
    print(f"  PUT /test_key: {status}")
    if status not in (200, 201):
        print(f"  FAIL: {resp}")
        return False
    print(f"  Response: {resp}")
    
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


def test_incr_decr():
    """Test INCR and DECR operations."""
    print("\\n[TEST] INCR and DECR")
    
    # Set initial value
    api_call("PUT", "/counter", 10)
    
    # INCR
    status, resp = api_call("POST", "/counter/incr", {"by": 5})
    print(f"  INCR by 5: {status}, value={resp.get('value')}")
    if resp.get("value") != 15:
        print(f"  FAIL: Expected 15, got {resp.get('value')}")
        return False
    
    # DECR
    status, resp = api_call("POST", "/counter/decr", {"by": 3})
    print(f"  DECR by 3: {status}, value={resp.get('value')}")
    if resp.get("value") != 12:
        print(f"  FAIL: Expected 12, got {resp.get('value')}")
        return False
    
    print("  PASS")
    return True


def test_list_operations():
    """Test list push/pop operations."""
    print("\\n[TEST] List operations (RPUSH, LPUSH, LPOP, RPOP)")
    
    # Initialize empty list
    api_call("PUT", "/my_list", [])
    
    # RPUSH
    for val in ["a", "b", "c"]:
        status, _ = api_call("POST", "/my_list/rpush", {"value": val})
        print(f"  RPUSH '{val}': {status}")
    
    # Check list
    status, resp = api_call("GET", "/my_list")
    if resp.get("value") != ["a", "b", "c"]:
        print(f"  FAIL: Expected ['a', 'b', 'c'], got {resp.get('value')}")
        return False
    
    # LPUSH
    status, resp = api_call("POST", "/my_list/lpush", {"value": "z"})
    print(f"  LPUSH 'z': {status}")
    
    # Check
    status, resp = api_call("GET", "/my_list")
    if resp.get("value") != ["z", "a", "b", "c"]:
        print(f"  FAIL: Expected ['z', 'a', 'b', 'c'], got {resp.get('value')}")
        return False
    
    # LPOP
    status, resp = api_call("POST", "/my_list/lpop")
    print(f"  LPOP: {status}, popped={resp.get('value')}")
    if resp.get("value") != "z":
        print(f"  FAIL: Expected 'z', got {resp.get('value')}")
        return False
    
    # RPOP
    status, resp = api_call("POST", "/my_list/rpop")
    print(f"  RPOP: {status}, popped={resp.get('value')}")
    if resp.get("value") != "c":
        print(f"  FAIL: Expected 'c', got {resp.get('value')}")
        return False
    
    # LEN
    status, resp = api_call("GET", "/my_list/len")
    print(f"  LEN: {status}, length={resp.get('length')}")
    if resp.get("length") != 2:
        print(f"  FAIL: Expected 2, got {resp.get('length')}")
        return False
    
    print("  PASS")
    return True


def test_nested_path():
    """Test nested path operations (PATCH and GET with path)."""
    print("\\n[TEST] Nested path operations")
    
    # Set complex object
    config = {
        "database": {"host": "localhost", "port": 5432},
        "cache": {"enabled": True}
    }
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
        print(f"  FAIL: Expected 5433, got {resp.get('value')}")
        return False
    
    # Verify full object
    status, resp = api_call("GET", "/config")
    expected_port = resp.get("value", {}).get("database", {}).get("port")
    if expected_port != 5433:
        print(f"  FAIL: Full object check failed, port={expected_port}")
        return False
    
    print("  PASS")
    return True


def test_conditional_set():
    """Test conditional SET operations (nx and xx flags)."""
    print("\\n[TEST] Conditional SET (nx, xx)")
    
    # Delete key if exists
    api_call("DELETE", "/cond_key")
    
    # SET with nx=true (should succeed - key doesn't exist)
    status, resp = api_call("PUT", "/cond_key?nx=true", "first")
    print(f"  PUT with nx=true (new): {status}")
    if status != 201:
        print(f"  FAIL: Expected 201, got {status}")
        return False
    
    # SET with nx=true again (should fail - key exists)
    status, resp = api_call("PUT", "/cond_key?nx=true", "second")
    print(f"  PUT with nx=true (exists): {status}")
    if status != 409:
        print(f"  FAIL: Expected 409 Conflict, got {status}")
        return False
    
    # Verify value unchanged
    status, resp = api_call("GET", "/cond_key")
    if resp.get("value") != "first":
        print(f"  FAIL: Value should be 'first', got {resp.get('value')}")
        return False
    
    # SET with xx=true (should succeed - key exists)
    status, resp = api_call("PUT", "/cond_key?xx=true", "updated")
    print(f"  PUT with xx=true (exists): {status}")
    if status != 200:
        print(f"  FAIL: Expected 200, got {status}")
        return False
    
    # Delete and try xx=true (should fail - key doesn't exist)
    api_call("DELETE", "/cond_key")
    status, resp = api_call("PUT", "/cond_key?xx=true", "new")
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
