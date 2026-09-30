"""Unit tests for git-sync retry backoff and due-scheduling.

Pure functions over a lightweight stand-in for the org config row -- no DB and
no git. The integration side (counter increments/resets across a real cycle)
lives in ``test_git_sync.py``.
"""

from datetime import timedelta
from types import SimpleNamespace

from openhands.automation.git_sync.loop import _is_due, _retry_backoff_seconds
from openhands.automation.utils import utcnow


class TestRetryBackoffSeconds:
    def test_no_failures_is_the_plain_interval(self):
        assert _retry_backoff_seconds(60, 0, 3600.0) == 60.0

    def test_grows_exponentially_with_the_failure_streak(self):
        assert _retry_backoff_seconds(60, 1, 3600.0) == 120.0
        assert _retry_backoff_seconds(60, 2, 3600.0) == 240.0
        assert _retry_backoff_seconds(60, 3, 3600.0) == 480.0

    def test_is_capped(self):
        # 60 * 2**10 = 61440, well over the cap.
        assert _retry_backoff_seconds(60, 10, 3600.0) == 3600.0

    def test_huge_streak_stays_capped_and_does_not_overflow(self):
        # The shift is bounded so 2**failures can't blow up; result is the cap.
        assert _retry_backoff_seconds(60, 10_000, 3600.0) == 3600.0

    def test_cap_below_interval_never_shortens_a_healthy_wait(self):
        # No failures always returns the interval, even if the cap is smaller.
        assert _retry_backoff_seconds(60, 0, 10.0) == 60.0


def _org(consecutive_failures: int, *, last_error_at=None, last_run_at=None):
    return SimpleNamespace(
        consecutive_failures=consecutive_failures,
        last_error_at=last_error_at,
        last_run_at=last_run_at,
    )


class TestIsDue:
    def test_never_attempted_is_due(self):
        assert _is_due(_org(0), 60, utcnow(), 3600.0) is True

    def test_healthy_org_due_after_one_interval(self):
        now = utcnow()
        just_before = _org(0, last_run_at=now - timedelta(seconds=59))
        just_after = _org(0, last_run_at=now - timedelta(seconds=61))
        assert _is_due(just_before, 60, now, 3600.0) is False
        assert _is_due(just_after, 60, now, 3600.0) is True

    def test_failing_org_waits_the_backed_off_interval(self):
        now = utcnow()
        # One failure -> wait doubles to 120s.
        org = _org(1, last_error_at=now - timedelta(seconds=90))
        assert _is_due(org, 60, now, 3600.0) is False
        org_later = _org(1, last_error_at=now - timedelta(seconds=121))
        assert _is_due(org_later, 60, now, 3600.0) is True

    def test_backoff_is_capped_so_a_broken_org_still_gets_reprobed(self):
        now = utcnow()
        # A long streak would back off past an hour, but the cap holds it at
        # 3600s: still due once the cap elapses (this is what self-heals).
        org = _org(20, last_error_at=now - timedelta(seconds=3601))
        assert _is_due(org, 60, now, 3600.0) is True
        not_yet = _org(20, last_error_at=now - timedelta(seconds=3599))
        assert _is_due(not_yet, 60, now, 3600.0) is False

    def test_latest_of_run_or_error_is_the_reference_point(self):
        now = utcnow()
        org = _org(
            0,
            last_run_at=now - timedelta(seconds=120),
            last_error_at=now - timedelta(seconds=10),
        )
        # The more recent attempt (the error, 10s ago) gates it, so not due.
        assert _is_due(org, 60, now, 3600.0) is False
