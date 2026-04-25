# KV Store Client Guide

This guide covers how to use the automation KV store API for state persistence between runs.

## Overview

The KV store provides a Redis-like key-value interface for automations to persist state between runs. It's designed for small, frequently-accessed data like:

- Counters and cursors
- Configuration flags
- Small caches (< 64KB recommended)
- Run metadata and logs

### When to Use

✅ **Good use cases:**
- Tracking pagination cursors across runs
- Counting events or iterations
- Storing configuration that changes over time
- Caching small computed values

❌ **Not designed for:**
- Large file storage (use object storage)
- High-throughput queues (use proper message queues)
- Relational data (use a database)
- Storing sensitive credentials (use secrets management)

### Limitations

| Limit | Value | Notes |
|-------|-------|-------|
| Max state size | 64 KB | Total size of all keys combined |
| Max key length | 255 chars | Keys are case-sensitive |
| Reserved keys | `$` prefix | System use (e.g., `$version`) |
| Max nesting depth | 32 levels | For nested objects/arrays |

## Authentication

All KV endpoints require a JWT token passed via the `Authorization` header:

```
Authorization: Bearer <AUTOMATION_KV_TOKEN>
```

The token is automatically provided to your automation via the `AUTOMATION_KV_TOKEN` environment variable when `enable_kv_store: true` is set.

## Basic Operations

### Get a Value

```bash
curl -H "Authorization: Bearer $AUTOMATION_KV_TOKEN" \
  "$AUTOMATION_API_URL/v1/kv/mykey"
```

**Response:**
```json
{"key": "mykey", "value": {"foo": "bar"}}
```

### Set a Value

```bash
curl -X PUT \
  -H "Authorization: Bearer $AUTOMATION_KV_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"foo": "bar"}' \
  "$AUTOMATION_API_URL/v1/kv/mykey"
```

**Response (201 Created for new key, 200 OK for update):**
```json
{"key": "mykey", "value": {"foo": "bar"}, "created": true, "updated_at": "2024-01-15T10:00:00Z"}
```

### Delete a Value

```bash
curl -X DELETE \
  -H "Authorization: Bearer $AUTOMATION_KV_TOKEN" \
  "$AUTOMATION_API_URL/v1/kv/mykey"
```

**Response:**
```json
{"key": "mykey", "deleted": true}
```

### List All Keys

```bash
curl -H "Authorization: Bearer $AUTOMATION_KV_TOKEN" \
  "$AUTOMATION_API_URL/v1/kv"
```

**Response:**
```json
{"keys": ["config", "counter", "last_run"], "count": 3}
```

## Advanced Operations

### Nested Paths with PATCH

Update a nested field without replacing the entire value:

```bash
curl -X PATCH \
  -H "Authorization: Bearer $AUTOMATION_KV_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path": "settings.theme", "value": "dark"}' \
  "$AUTOMATION_API_URL/v1/kv/config"
```

### Atomic Counters

**Increment:**
```bash
curl -X POST \
  -H "Authorization: Bearer $AUTOMATION_KV_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"by": 1}' \
  "$AUTOMATION_API_URL/v1/kv/counter/incr"
```

**Response:**
```json
{"key": "counter", "value": 42}
```

**Decrement:**
```bash
curl -X POST \
  -H "Authorization: Bearer $AUTOMATION_KV_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"by": 5}' \
  "$AUTOMATION_API_URL/v1/kv/counter/decr"
```

### List Operations

**Push to front (LPUSH):**
```bash
curl -X POST \
  -H "Authorization: Bearer $AUTOMATION_KV_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"value": "new_item"}' \
  "$AUTOMATION_API_URL/v1/kv/queue/lpush"
```

**Push to back (RPUSH):**
```bash
curl -X POST \
  -H "Authorization: Bearer $AUTOMATION_KV_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"value": "new_item"}' \
  "$AUTOMATION_API_URL/v1/kv/queue/rpush"
```

**Pop from front (LPOP):**
```bash
curl -X POST \
  -H "Authorization: Bearer $AUTOMATION_KV_TOKEN" \
  "$AUTOMATION_API_URL/v1/kv/queue/lpop"
```

**Pop from back (RPOP):**
```bash
curl -X POST \
  -H "Authorization: Bearer $AUTOMATION_KV_TOKEN" \
  "$AUTOMATION_API_URL/v1/kv/queue/rpop"
```

### Batch Operations

Execute multiple operations atomically:

```bash
curl -X POST \
  -H "Authorization: Bearer $AUTOMATION_KV_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "operations": [
      {"op": "incr", "key": "counter"},
      {"op": "set", "key": "last_run", "value": "2024-01-15"},
      {"op": "rpush", "key": "log", "value": {"event": "complete"}}
    ]
  }' \
  "$AUTOMATION_API_URL/v1/kv/batch"
```

**Response:**
```json
{
  "version": 5,
  "results": [
    {"op": "incr", "key": "counter", "success": true, "value": 42},
    {"op": "set", "key": "last_run", "success": true, "created": true},
    {"op": "rpush", "key": "log", "success": true, "length": 3}
  ]
}
```

## Concurrency Patterns

### For Scheduled Automations (max_concurrent_runs=1)

When only one instance runs at a time, there's no contention:

```python
import os
import httpx

async def main():
    token = os.environ["AUTOMATION_KV_TOKEN"]
    api_url = os.environ["AUTOMATION_API_URL"]
    
    async with httpx.AsyncClient() as client:
        # Simple read-modify-write, no retry needed
        resp = await client.get(
            f"{api_url}/v1/kv/counter",
            headers={"Authorization": f"Bearer {token}"}
        )
        if resp.status_code == 200:
            counter = resp.json()["value"]
        else:
            counter = 0
        
        # Or just use atomic incr
        resp = await client.post(
            f"{api_url}/v1/kv/counter/incr",
            headers={"Authorization": f"Bearer {token}"},
            json={"by": 1}
        )
```

### For Event Handlers (max_concurrent_runs > 1)

When multiple instances run concurrently, **409 Conflicts are expected**. Always implement retry with exponential backoff:

```python
import asyncio
import os
import random
import httpx

async def kv_set_with_retry(
    client: httpx.AsyncClient,
    key: str,
    value: any,
    max_retries: int = 5
) -> dict:
    """Set a KV value with automatic retry on conflict."""
    token = os.environ["AUTOMATION_KV_TOKEN"]
    api_url = os.environ["AUTOMATION_API_URL"]
    
    for attempt in range(max_retries):
        resp = await client.put(
            f"{api_url}/v1/kv/{key}",
            headers={"Authorization": f"Bearer {token}"},
            json=value
        )
        
        if resp.status_code in (200, 201):
            return resp.json()
        
        if resp.status_code == 409:
            # Get suggested retry delay from header
            retry_after = int(resp.headers.get("Retry-After", 1))
            # Exponential backoff with jitter
            delay = retry_after * (2 ** attempt) + random.uniform(0, 0.5)
            await asyncio.sleep(delay)
            continue
        
        resp.raise_for_status()
    
    raise Exception(f"Failed to set {key} after {max_retries} retries")
```

### Using Optimistic Concurrency

For read-modify-write patterns, use `if_version` to detect concurrent modifications:

```python
async def safe_update(client: httpx.AsyncClient, key: str, transform_fn):
    """Safely update a value using optimistic concurrency."""
    token = os.environ["AUTOMATION_KV_TOKEN"]
    api_url = os.environ["AUTOMATION_API_URL"]
    headers = {"Authorization": f"Bearer {token}"}
    
    for attempt in range(5):
        # Read with version metadata
        resp = await client.get(
            f"{api_url}/v1/kv/{key}",
            headers=headers,
            params={"meta": "true"}
        )
        
        if resp.status_code == 404:
            # Key doesn't exist, create it
            initial_value = transform_fn(None)
            resp = await client.put(
                f"{api_url}/v1/kv/{key}",
                headers=headers,
                json=initial_value,
                params={"nx": "true"}  # Only if not exists
            )
            if resp.status_code in (200, 201):
                return resp.json()
            continue  # Retry if conflict
        
        data = resp.json()
        version = data["version"]
        old_value = data["value"]
        
        # Apply transformation locally
        new_value = transform_fn(old_value)
        
        # Write with version check
        resp = await client.put(
            f"{api_url}/v1/kv/{key}",
            headers=headers,
            json=new_value,
            params={"if_version": version}
        )
        
        if resp.status_code in (200, 201):
            return resp.json()
        
        if resp.status_code == 409:
            # Version changed, retry with backoff
            await asyncio.sleep(0.1 * (2 ** attempt))
            continue
        
        resp.raise_for_status()
    
    raise Exception("Max retries exceeded")


# Usage example
async def increment_counter():
    async with httpx.AsyncClient() as client:
        result = await safe_update(
            client,
            "counter",
            lambda v: (v or 0) + 1
        )
        print(f"Counter is now: {result['value']}")
```

## Best Practices

### DO ✅

- **Use atomic operations** (`incr`, `push`, `pop`) when possible - they're conflict-free
- **Keep state small** (< 64KB total, ideally < 8KB for best performance)
- **Design for idempotency** - operations may be retried
- **Use batch endpoint** for multiple updates in one operation
- **Implement proper retry logic** for concurrent event handlers
- **Set appropriate `kv_lock_timeout_ms`** based on your use case

### DON'T ❌

- **Read state, sleep, then write** - maximizes contention
- **Store large blobs** - use object storage instead
- **Ignore 409 errors** - always handle with retry
- **Use KV as a queue** - use proper message queues for high-throughput
- **Rely on ordering** across concurrent writes

### Lock Timeout Configuration

Configure `kv_lock_timeout_ms` based on your automation type:

| Use Case | Recommended Timeout | Rationale |
|----------|---------------------|-----------|
| High-throughput event handlers | 2000ms | Fail fast, retry quickly |
| Standard scheduled jobs | 5000ms (default) | Balanced wait/fail |
| Long-running batch jobs | 10000ms | Allow more contention |
| Critical single-run ops | 500-1000ms | Immediate feedback |

Set via automation config:
```json
{
  "name": "my-automation",
  "enable_kv_store": true,
  "kv_lock_timeout_ms": 2000,
  ...
}
```

## Error Handling

| Status | Meaning | Action |
|--------|---------|--------|
| 200 | Success (update) | - |
| 201 | Success (create) | - |
| 400 | Bad request | Fix request (invalid key, etc.) |
| 401 | Unauthorized | Check token |
| 404 | Key not found | Handle missing key |
| 409 | Conflict | Retry with backoff (see `Retry-After` header) |
| 413 | Payload too large | Reduce state size |
| 503 | Service unavailable | KV store not configured |

### 409 Conflict Types

The 409 response can indicate:

1. **Lock timeout** (`kv_store_busy`): Another operation is holding the lock
   ```json
   {"detail": "kv_store_busy: another operation is in progress, please retry"}
   ```

2. **Version mismatch** (`version_mismatch`): State changed since your read
   ```json
   {
     "detail": {
       "error": "version_mismatch",
       "expected_version": 5,
       "actual_version": 6
     }
   }
   ```

Both include a `Retry-After: 1` header suggesting initial backoff.

## Debugging

### Common Issues

**Frequent 409s:**
- Too much concurrent access
- Solutions:
  - Reduce `max_concurrent_runs`
  - Use atomic operations instead of read-modify-write
  - Lower `kv_lock_timeout_ms` to fail faster

**Slow operations:**
- State document too large
- Solutions:
  - Split into multiple keys
  - Store large data externally
  - Clean up old/unused keys

**Version mismatches:**
- Concurrent modifications
- Solutions:
  - Use atomic operations
  - Implement proper retry loop
  - Reconsider if you need concurrent access

### Metrics

If Prometheus metrics are enabled, monitor:

- `kv_operation_duration_seconds`: Operation latency
- `kv_lock_wait_duration_seconds`: Time waiting for row lock
- `kv_conflict_total{reason="lock_timeout|version_mismatch"}`: Conflict rate
- `kv_state_size_bytes`: State document size

High lock wait times or conflict rates indicate contention that may need architectural changes.

## API Reference

See the [KV Store Design Document](kv-store-design.md) for full API specification and implementation details.
