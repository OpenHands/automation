"""Tests for KV store concurrency controls.

Tests cover:
- Statement timeout (safety net for runaway operations)
- Retry-After header on 409 responses
- Configurable lock timeout per-automation
- KV token claims with lock_timeout_ms
- Metrics recording
"""

import uuid

import pytest

from automation.kv_metrics import (
    kv_conflict_total,
    record_conflict,
    record_lock_wait,
    record_operation,
    record_state_size,
)
from automation.kv_router import (
    _is_lock_timeout_error,
    _raise_lock_conflict,
    _raise_version_conflict,
)
from automation.utils.kv import (
    DEFAULT_LOCK_TIMEOUT_MS,
    KVTokenClaims,
    create_kv_token,
    verify_kv_token,
)


# --- Test Constants ---
TEST_SECRET = "test-secret-key-for-testing-only"
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


class TestKVTokenClaims:
    """Tests for KV token with lock_timeout_ms claim."""

    def test_create_token_with_default_timeout(self):
        """Token created with default lock timeout."""
        token = create_kv_token(
            secret=TEST_SECRET,
            automation_id=TEST_AUTOMATION_ID,
            run_id=TEST_RUN_ID,
        )

        claims = verify_kv_token(TEST_SECRET, token)
        assert isinstance(claims, KVTokenClaims)
        assert claims.automation_id == TEST_AUTOMATION_ID
        assert claims.lock_timeout_ms == DEFAULT_LOCK_TIMEOUT_MS

    def test_create_token_with_custom_timeout(self):
        """Token created with custom lock timeout."""
        token = create_kv_token(
            secret=TEST_SECRET,
            automation_id=TEST_AUTOMATION_ID,
            run_id=TEST_RUN_ID,
            lock_timeout_ms=2000,
        )

        claims = verify_kv_token(TEST_SECRET, token)
        assert claims.lock_timeout_ms == 2000

    def test_verify_token_backward_compatible(self):
        """Old tokens without lock_timeout_ms use default."""
        from datetime import UTC, datetime, timedelta

        import jwt

        # Create a token manually without lock_timeout_ms (simulating old token)
        payload = {
            "automation_id": str(TEST_AUTOMATION_ID),
            "run_id": str(TEST_RUN_ID),
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(hours=24),
        }
        old_token = jwt.encode(payload, TEST_SECRET, algorithm="HS256")

        claims = verify_kv_token(TEST_SECRET, old_token)
        assert claims.automation_id == TEST_AUTOMATION_ID
        # Should use default when claim is missing
        assert claims.lock_timeout_ms == DEFAULT_LOCK_TIMEOUT_MS

    def test_verify_token_invalid_timeout_uses_default(self):
        """Invalid lock_timeout_ms in token uses default."""
        from datetime import UTC, datetime, timedelta

        import jwt

        # Create a token with invalid timeout
        payload = {
            "automation_id": str(TEST_AUTOMATION_ID),
            "run_id": str(TEST_RUN_ID),
            "lock_timeout_ms": "not_a_number",
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(hours=24),
        }
        token = jwt.encode(payload, TEST_SECRET, algorithm="HS256")

        claims = verify_kv_token(TEST_SECRET, token)
        assert claims.lock_timeout_ms == DEFAULT_LOCK_TIMEOUT_MS

    def test_verify_token_too_small_timeout_uses_default(self):
        """Lock timeout < 100ms uses default."""
        from datetime import UTC, datetime, timedelta

        import jwt

        payload = {
            "automation_id": str(TEST_AUTOMATION_ID),
            "run_id": str(TEST_RUN_ID),
            "lock_timeout_ms": 50,  # Below minimum
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(hours=24),
        }
        token = jwt.encode(payload, TEST_SECRET, algorithm="HS256")

        claims = verify_kv_token(TEST_SECRET, token)
        assert claims.lock_timeout_ms == DEFAULT_LOCK_TIMEOUT_MS


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


class TestLockTimeoutValidation:
    """Tests for kv_lock_timeout_ms validation in schemas."""

    def test_create_automation_default_timeout(self):
        """CreateAutomationRequest has default lock timeout."""
        from automation.schemas import CronTrigger, CreateAutomationRequest

        req = CreateAutomationRequest(
            name="test",
            trigger=CronTrigger(schedule="0 9 * * *"),
            tarball_path="gs://bucket/path.tar.gz",
            entrypoint="python run.py",
        )
        assert req.kv_lock_timeout_ms == 5000

    def test_create_automation_custom_timeout(self):
        """CreateAutomationRequest accepts custom lock timeout."""
        from automation.schemas import CronTrigger, CreateAutomationRequest

        req = CreateAutomationRequest(
            name="test",
            trigger=CronTrigger(schedule="0 9 * * *"),
            tarball_path="gs://bucket/path.tar.gz",
            entrypoint="python run.py",
            kv_lock_timeout_ms=2000,
        )
        assert req.kv_lock_timeout_ms == 2000

    def test_create_automation_timeout_min_validation(self):
        """CreateAutomationRequest rejects timeout < 100ms."""
        from pydantic import ValidationError

        from automation.schemas import CreateAutomationRequest, CronTrigger

        with pytest.raises(ValidationError) as exc_info:
            CreateAutomationRequest(
                name="test",
                trigger=CronTrigger(schedule="0 9 * * *"),
                tarball_path="gs://bucket/path.tar.gz",
                entrypoint="python run.py",
                kv_lock_timeout_ms=50,  # Too low
            )

        assert "kv_lock_timeout_ms" in str(exc_info.value)

    def test_create_automation_timeout_max_validation(self):
        """CreateAutomationRequest rejects timeout > 30000ms."""
        from pydantic import ValidationError

        from automation.schemas import CreateAutomationRequest, CronTrigger

        with pytest.raises(ValidationError) as exc_info:
            CreateAutomationRequest(
                name="test",
                trigger=CronTrigger(schedule="0 9 * * *"),
                tarball_path="gs://bucket/path.tar.gz",
                entrypoint="python run.py",
                kv_lock_timeout_ms=60000,  # Too high
            )

        assert "kv_lock_timeout_ms" in str(exc_info.value)

    def test_update_automation_timeout(self):
        """UpdateAutomationRequest accepts optional lock timeout."""
        from automation.schemas import UpdateAutomationRequest

        req = UpdateAutomationRequest(kv_lock_timeout_ms=10000)
        assert req.kv_lock_timeout_ms == 10000

    def test_update_automation_timeout_validation(self):
        """UpdateAutomationRequest validates timeout bounds."""
        from pydantic import ValidationError

        from automation.schemas import UpdateAutomationRequest

        with pytest.raises(ValidationError):
            UpdateAutomationRequest(kv_lock_timeout_ms=99)  # Too low


class TestAutomationModelTimeout:
    """Tests for kv_lock_timeout_ms in Automation model."""

    def test_model_has_default_timeout(self):
        """Automation model has default lock timeout."""
        from automation.models import Automation

        # Check column default
        col = Automation.__table__.columns["kv_lock_timeout_ms"]
        assert col.default.arg == 5000


class TestResponseSchema:
    """Tests for kv_lock_timeout_ms in response schemas."""

    def test_automation_response_includes_timeout(self):
        """AutomationResponse includes kv_lock_timeout_ms."""
        from automation.schemas import AutomationResponse

        # Check field exists in model
        assert "kv_lock_timeout_ms" in AutomationResponse.model_fields
