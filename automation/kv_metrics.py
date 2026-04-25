"""Prometheus metrics for KV store operations.

Provides observability into KV store performance and health:
- Operation latency by type
- Lock wait time
- Conflict (409) rate
- Version mismatch rate
- State document size

Label Design:
- We use 'automation_name' instead of 'automation_id' to reduce cardinality.
- If name is unavailable, we use a truncated hash of the automation_id.
- This keeps the metric time series manageable at scale.

Usage:
    from automation.kv_metrics import (
        kv_operation_duration,
        kv_conflict_total,
        record_operation,
    )

    # Record operation duration
    with record_operation("set", automation_id):
        await do_operation()

    # Or manually:
    with kv_operation_duration.labels(operation="get", automation="my-auto").time():
        await do_operation()
"""

import time
from collections.abc import Generator
from contextlib import contextmanager

from prometheus_client import Counter, Histogram


# --- Metrics Definitions ---

# Operation latency histogram
# Buckets optimized for typical KV operation times (10ms to 5s)
kv_operation_duration = Histogram(
    "kv_operation_duration_seconds",
    "Duration of KV store operations",
    ["operation"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

# Lock wait time histogram
# Separate from operation duration to isolate contention from processing time
kv_lock_wait_duration = Histogram(
    "kv_lock_wait_duration_seconds",
    "Time spent waiting for row lock in KV operations",
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0],
)

# Lock/statement timeout conflicts (409 responses)
kv_conflict_total = Counter(
    "kv_conflict_total",
    "Number of KV store lock conflicts (409 responses)",
    ["reason"],  # "lock_timeout" or "version_mismatch"
)

# State document size histogram
# Buckets aligned with PostgreSQL TOAST thresholds (see config.py)
kv_state_size_bytes = Histogram(
    "kv_state_size_bytes",
    "Size of encrypted state documents in bytes",
    buckets=[100, 500, 1000, 2000, 8000, 16000, 32000, 64000, 128000, 256000],
)


# --- Helper Functions ---


@contextmanager
def record_operation(operation: str) -> Generator[None, None, None]:
    """Context manager to record operation duration.

    Usage:
        with record_operation("set"):
            await do_set_operation()
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        duration = time.perf_counter() - start
        kv_operation_duration.labels(operation=operation).observe(duration)


@contextmanager
def record_lock_wait() -> Generator[None, None, None]:
    """Context manager to record time spent waiting for row lock."""
    start = time.perf_counter()
    try:
        yield
    finally:
        duration = time.perf_counter() - start
        kv_lock_wait_duration.observe(duration)


def record_conflict(reason: str = "lock_timeout") -> None:
    """Record a conflict (409) response.

    Args:
        reason: Either "lock_timeout" or "version_mismatch"
    """
    kv_conflict_total.labels(reason=reason).inc()


def record_state_size(size_bytes: int) -> None:
    """Record the size of an encrypted state document."""
    kv_state_size_bytes.observe(size_bytes)
