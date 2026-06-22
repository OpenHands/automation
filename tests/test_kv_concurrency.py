"""Tests for KV store concurrency controls.

Tests cover:
- Statement timeout (safety net for runaway operations)
- Retry-After header on 409 responses
- Metrics recording
"""

import uuid

import pytest

from openhands.automation.kv_metrics import (
    kv_conflict_total,
    record_conflict,
    record_lock_wait,
    record_operation,
    record_state_size,
)
from openhands.automation.kv_router import (
    _is_lock_timeout_error,
    _raise_lock_conflict,
    _raise_version_conflict,
)


# --- Test Constants ---
TEST_AUTOMATION_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TEST_RUN_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


class TestStatementTimeoutDetection:
    """Tests for statement timeout error detection."""

    def test_detects_lock_timeout_55p03(self):
        """Detects lock timeout error code 55P03."""
        exc = Exception("ERROR: canceling statement due to lock timeout (55P03)")
        assert _is_lock_timeout_error(exc) is True

    def test_detects_lock_not_available(self):
        """Detects lock_not_available error."""
        exc = Exception("asyncpg.exceptions.LockNotAvailableError: lock_not_available")
        assert _is_lock_timeout_error(exc) is True

    def test_detects_statement_timeout_57014(self):
        """Detects statement timeout error code 57014."""
        exc = Exception("ERROR: canceling statement due to statement timeout (57014)")
        assert _is_lock_timeout_error(exc) is True

    def test_detects_query_canceled(self):
        """Detects query_canceled error."""
        exc = Exception("asyncpg.exceptions.QueryCanceledError: query_canceled")
        assert _is_lock_timeout_error(exc) is True

    def test_ignores_unrelated_errors(self):
        """Ignores unrelated database errors."""
        exc = Exception("ERROR: duplicate key value violates unique constraint")
        assert _is_lock_timeout_error(exc) is False

    def test_ignores_generic_errors(self):
        """Ignores generic Python errors."""
        exc = ValueError("invalid value")
        assert _is_lock_timeout_error(exc) is False


class TestRetryAfterHeader:
    """Tests for Retry-After header on 409 responses."""

    def test_lock_conflict_includes_retry_after(self):
        """_raise_lock_conflict includes Retry-After header."""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            _raise_lock_conflict()

        exc = exc_info.value
        assert exc.status_code == 409
        assert exc.headers is not None
        assert "Retry-After" in exc.headers
        assert exc.headers["Retry-After"] == "1"

    def test_version_conflict_includes_retry_after(self):
        """_raise_version_conflict includes Retry-After header."""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            _raise_version_conflict(expected=5, actual=6)

        exc = exc_info.value
        assert exc.status_code == 409
        assert exc.headers is not None
        assert "Retry-After" in exc.headers
        assert exc.headers["Retry-After"] == "1"

    def test_version_conflict_includes_versions(self):
        """_raise_version_conflict includes version info in detail."""
        from typing import Any, cast

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            _raise_version_conflict(expected=5, actual=6)

        exc = exc_info.value
        detail = cast(dict[str, Any], exc.detail)
        assert detail["error"] == "version_mismatch"
        assert detail["expected_version"] == 5
        assert detail["actual_version"] == 6


class TestKVMetrics:
    """Tests for KV store Prometheus metrics."""

    def test_record_operation_timing(self):
        """record_operation measures duration."""
        import time

        # Use the context manager
        with record_operation("test_op"):
            time.sleep(0.01)  # 10ms

        # Metric should have been recorded (we can't easily check exact value
        # but we can verify no exceptions)

    def test_record_lock_wait_timing(self):
        """record_lock_wait measures duration."""
        import time

        with record_lock_wait():
            time.sleep(0.001)  # 1ms

    def test_record_conflict_lock_timeout(self):
        """record_conflict increments counter for lock timeout."""
        # Get initial count (if any)
        initial = kv_conflict_total.labels(reason="lock_timeout")._value.get()

        record_conflict("lock_timeout")

        # Should have incremented
        new_value = kv_conflict_total.labels(reason="lock_timeout")._value.get()
        assert new_value == initial + 1

    def test_record_conflict_version_mismatch(self):
        """record_conflict increments counter for version mismatch."""
        initial = kv_conflict_total.labels(reason="version_mismatch")._value.get()

        record_conflict("version_mismatch")

        new_value = kv_conflict_total.labels(reason="version_mismatch")._value.get()
        assert new_value == initial + 1

    def test_record_state_size(self):
        """record_state_size records to histogram."""
        # Just verify it doesn't raise
        record_state_size(1000)
        record_state_size(50000)
