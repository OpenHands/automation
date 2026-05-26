"""Tests for Pydantic response schema UTC datetime normalisation.

SQLite returns naive datetime objects (no tzinfo). Without explicit
normalisation, Pydantic serialises those as bare ISO 8601 strings such as
"2026-03-23T09:00:00", which JavaScript's Date constructor interprets as
*local* time rather than UTC.  The UtcDatetime annotated type (and the
underlying ensure_utc helper) fix this at the serialisation layer.
"""

import uuid
from datetime import UTC, datetime, timezone, timedelta

import pytest

from openhands.automation.utils.time import ensure_utc
from openhands.automation.schemas import AutomationRunResponse, AutomationResponse
from openhands.automation.schemas import RunStatus


_NAIVE = datetime(2026, 3, 23, 9, 0, 0)  # no tzinfo — simulates SQLite output
_UTC_AWARE = datetime(2026, 3, 23, 9, 0, 0, tzinfo=UTC)
_OTHER_TZ = datetime(2026, 3, 23, 14, 30, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))


class TestEnsureUtc:
    def test_naive_datetime_gets_utc_tzinfo(self):
        result = ensure_utc(_NAIVE)
        assert result.tzinfo is UTC

    def test_naive_datetime_value_is_unchanged(self):
        result = ensure_utc(_NAIVE)
        assert result.replace(tzinfo=None) == _NAIVE

    def test_utc_aware_datetime_is_unchanged(self):
        result = ensure_utc(_UTC_AWARE)
        assert result is _UTC_AWARE

    def test_non_utc_aware_datetime_is_unchanged(self):
        result = ensure_utc(_OTHER_TZ)
        assert result is _OTHER_TZ


class TestAutomationRunResponseUtcSerialisation:
    """AutomationRunResponse must include a UTC offset in all datetime fields."""

    def _make_run(self, **overrides) -> AutomationRunResponse:
        defaults = dict(
            id=uuid.uuid4(),
            automation_id=uuid.uuid4(),
            status=RunStatus.COMPLETED,
            error_detail=None,
            conversation_id=None,
            timeout_at=None,
            keep_alive=False,
            sandbox_id=None,
            bash_command_id=None,
            created_at=_NAIVE,
            started_at=_NAIVE,
            completed_at=_NAIVE,
        )
        defaults.update(overrides)
        return AutomationRunResponse(**defaults)

    def test_naive_created_at_serialises_with_utc_offset(self):
        run = self._make_run()
        data = run.model_dump(mode="json")
        assert data["created_at"].endswith("+00:00") or data["created_at"].endswith("Z")

    def test_naive_started_at_serialises_with_utc_offset(self):
        run = self._make_run()
        data = run.model_dump(mode="json")
        assert data["started_at"].endswith("+00:00") or data["started_at"].endswith("Z")

    def test_naive_completed_at_serialises_with_utc_offset(self):
        run = self._make_run()
        data = run.model_dump(mode="json")
        assert data["completed_at"].endswith("+00:00") or data["completed_at"].endswith("Z")

    def test_none_optional_fields_remain_none(self):
        run = self._make_run(started_at=None, completed_at=None, timeout_at=None)
        data = run.model_dump(mode="json")
        assert data["started_at"] is None
        assert data["completed_at"] is None
        assert data["timeout_at"] is None

    def test_already_utc_aware_datetime_serialises_correctly(self):
        run = self._make_run(created_at=_UTC_AWARE)
        data = run.model_dump(mode="json")
        assert data["created_at"].endswith("+00:00") or data["created_at"].endswith("Z")


class TestAutomationResponseUtcSerialisation:
    """AutomationResponse datetime fields also emit UTC offsets."""

    def _make_automation(self, **overrides) -> AutomationResponse:
        defaults = dict(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            model=None,
            name="Test",
            prompt=None,
            trigger={"type": "cron", "schedule": "0 9 * * 1", "timezone": "UTC"},
            tarball_path="s3://bucket/key.tar.gz",
            setup_script_path=None,
            entrypoint="python main.py",
            timeout=None,
            enabled=True,
            last_triggered_at=_NAIVE,
            created_at=_NAIVE,
            updated_at=_NAIVE,
        )
        defaults.update(overrides)
        return AutomationResponse(**defaults)

    def test_naive_created_at_serialises_with_utc_offset(self):
        automation = self._make_automation()
        data = automation.model_dump(mode="json")
        assert data["created_at"].endswith("+00:00") or data["created_at"].endswith("Z")

    def test_naive_last_triggered_at_serialises_with_utc_offset(self):
        automation = self._make_automation()
        data = automation.model_dump(mode="json")
        assert data["last_triggered_at"].endswith("+00:00") or data["last_triggered_at"].endswith("Z")

    def test_none_last_triggered_at_remains_none(self):
        automation = self._make_automation(last_triggered_at=None)
        data = automation.model_dump(mode="json")
        assert data["last_triggered_at"] is None
