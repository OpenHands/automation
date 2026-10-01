"""Unit tests for git-sync retry backoff and due-scheduling.

Pure functions over a lightweight stand-in for the org config row -- no DB and
no git. The integration side (counter increments/resets across a real cycle)
lives in ``test_git_sync.py``.
"""

import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import cast

from openhands.automation.git_sync.loop import (
    _is_due,
    _jitter_fraction,
    _retry_backoff_seconds,
)
from openhands.automation.models import AutomationGitSyncOrgConfig
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


def _org(
    consecutive_auth_failures: int, *, last_error_at=None, last_run_at=None, org_id=None
) -> AutomationGitSyncOrgConfig:
    # A lightweight stand-in for the ORM row: _is_due only reads these four
    # attributes, so a SimpleNamespace is enough. Cast so the pure-function
    # calls type-check without building a real DB-backed model.
    return cast(
        AutomationGitSyncOrgConfig,
        SimpleNamespace(
            org_id=org_id or uuid.uuid4(),
            consecutive_auth_failures=consecutive_auth_failures,
            last_error_at=last_error_at,
            last_run_at=last_run_at,
        ),
    )


class TestIsDue:
    def test_never_attempted_is_due(self):
        assert _is_due(_org(0), 60, utcnow(), 3600.0) is True

    def test_healthy_org_due_after_one_interval(self):
        # No failures -> no jitter, exact interval boundary.
        now = utcnow()
        just_before = _org(0, last_run_at=now - timedelta(seconds=59))
        just_after = _org(0, last_run_at=now - timedelta(seconds=61))
        assert _is_due(just_before, 60, now, 3600.0) is False
        assert _is_due(just_after, 60, now, 3600.0) is True

    def test_failing_org_waits_at_least_half_the_backed_off_ceiling(self):
        # One failure -> ceiling doubles to 120s; equal jitter puts the actual
        # wait in [60, 120). So: never due below the 60s floor, always due at or
        # past the 120s ceiling, regardless of which org drew which offset.
        now = utcnow()
        for _ in range(50):
            org = _org(1, last_error_at=now - timedelta(seconds=59))
            assert _is_due(org, 60, now, 3600.0) is False
            org = _org(1, last_error_at=now - timedelta(seconds=120))
            assert _is_due(org, 60, now, 3600.0) is True

    def test_backoff_is_capped_so_a_broken_org_still_gets_reprobed(self):
        # A long streak backs off to the 3600s cap; jitter keeps the wait in
        # [1800, 3600). Due once the full cap elapses; never due below half.
        now = utcnow()
        for _ in range(50):
            org = _org(20, last_error_at=now - timedelta(seconds=3600))
            assert _is_due(org, 60, now, 3600.0) is True
            not_yet = _org(20, last_error_at=now - timedelta(seconds=1799))
            assert _is_due(not_yet, 60, now, 3600.0) is False

    def test_failing_org_never_retries_sooner_than_its_interval(self):
        # An interval at or above the cap (hourly, daily) would otherwise have
        # the cap and jitter pull a failing org's wait down to [1800, 3600) --
        # probing a broken repo more often than a healthy one syncs.
        now = utcnow()
        for interval in (3600, 86400):
            for failures in (1, 20):
                for _ in range(50):
                    early = _org(
                        failures,
                        last_error_at=now - timedelta(seconds=interval - 1),
                    )
                    assert _is_due(early, interval, now, 3600.0) is False
                    on_time = _org(
                        failures, last_error_at=now - timedelta(seconds=interval)
                    )
                    assert _is_due(on_time, interval, now, 3600.0) is True

    def test_interval_between_half_cap_and_cap_is_floored_not_just_jittered(self):
        # 2400s: ceiling is the 3600s cap, jitter alone would allow 1800s.
        now = utcnow()
        for _ in range(50):
            org = _org(1, last_error_at=now - timedelta(seconds=2399))
            assert _is_due(org, 2400, now, 3600.0) is False

    def test_jitter_is_stable_within_a_wait_window(self):
        # Same org + same last attempt must give the same answer every tick --
        # otherwise repeated ticks would eventually roll a low offset and fire
        # early, defeating the backoff.
        now = utcnow()
        org = _org(
            5,
            org_id=uuid.uuid4(),
            last_error_at=now - timedelta(seconds=1000),
        )
        assert all(
            _is_due(org, 60, now, 3600.0) == _is_due(org, 60, now, 3600.0)
            for _ in range(20)
        )

    def test_latest_of_run_or_error_is_the_reference_point(self):
        now = utcnow()
        org = _org(
            0,
            last_run_at=now - timedelta(seconds=120),
            last_error_at=now - timedelta(seconds=10),
        )
        # The more recent attempt (the error, 10s ago) gates it, so not due.
        assert _is_due(org, 60, now, 3600.0) is False

    def test_is_due_jitter_varies_by_org_not_a_constant_fraction(self):
        # The anti-herd property lives at the _is_due call site, not just in the
        # _jitter_fraction helper. Pin it here: at a fixed point *inside* the
        # backed-off window, different orgs must reach different due-decisions.
        # One failure -> ceiling 120s; equal jitter puts each org's real wait in
        # [60, 120), so 90s is due for some orgs and not others. A constant
        # fraction (the mutant that drops the per-org jitter) would make every
        # org share one wait and one decision -- exactly the lockstep retry the
        # jitter exists to prevent.
        now = utcnow()
        elapsed = timedelta(seconds=90)  # mid the [60, 120) window for failures=1
        decisions = {
            _is_due(
                _org(1, org_id=uuid.uuid4(), last_error_at=now - elapsed),
                60,
                now,
                3600.0,
            )
            for _ in range(300)
        }
        assert decisions == {True, False}


class TestJitterFraction:
    def test_is_in_the_unit_interval(self):
        now = utcnow()
        for _ in range(200):
            frac = _jitter_fraction(uuid.uuid4(), now)
            assert 0.0 <= frac < 1.0

    def test_is_deterministic_for_the_same_org_and_reference(self):
        org_id = uuid.uuid4()
        now = utcnow()
        assert _jitter_fraction(org_id, now) == _jitter_fraction(org_id, now)

    def test_spreads_orgs_that_share_a_reference(self):
        # The whole point: many orgs knocked out at the same instant must not
        # collapse onto one offset. Over a fleet of orgs the fractions should
        # cover both halves of the window.
        now = utcnow()
        fracs = [_jitter_fraction(uuid.uuid4(), now) for _ in range(200)]
        assert min(fracs) < 0.25
        assert max(fracs) > 0.75

    def test_rerolls_across_attempts(self):
        org_id = uuid.uuid4()
        now = utcnow()
        earlier = now - timedelta(seconds=3600)
        assert _jitter_fraction(org_id, now) != _jitter_fraction(org_id, earlier)
