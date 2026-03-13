"""Tests for cron schedule evaluation."""

from datetime import UTC, datetime

from automation.scheduler import is_cron_due


class TestIsCronDue:
    def test_never_triggered_and_recently_due(self):
        """First-time automation with recent fire time should be due."""
        now = datetime(2026, 3, 13, 9, 0, 30, tzinfo=UTC)  # 09:00:30 Friday
        # "Every Friday at 09:00"
        assert is_cron_due("0 9 * * 5", last_triggered=None, now=now) is True

    def test_never_triggered_but_too_old(self):
        """First-time automation whose fire time is too old should not be due."""
        now = datetime(2026, 3, 13, 12, 0, 0, tzinfo=UTC)  # 3 hours after fire
        assert is_cron_due("0 9 * * 5", last_triggered=None, now=now) is False

    def test_triggered_recently_not_due_yet(self):
        """Automation triggered recently should not fire again."""
        last = datetime(2026, 3, 13, 9, 0, 0, tzinfo=UTC)
        now = datetime(2026, 3, 13, 9, 30, 0, tzinfo=UTC)
        assert is_cron_due("0 9 * * 5", last_triggered=last, now=now) is False

    def test_triggered_last_week_now_due(self):
        """Automation triggered last Friday should fire this Friday."""
        last = datetime(2026, 3, 6, 9, 0, 0, tzinfo=UTC)
        now = datetime(2026, 3, 13, 9, 0, 30, tzinfo=UTC)
        assert is_cron_due("0 9 * * 5", last_triggered=last, now=now) is True

    def test_every_minute_due(self):
        """Every-minute cron should be due after 1 minute."""
        last = datetime(2026, 3, 13, 10, 0, 0, tzinfo=UTC)
        now = datetime(2026, 3, 13, 10, 1, 5, tzinfo=UTC)
        assert is_cron_due("* * * * *", last_triggered=last, now=now) is True

    def test_every_minute_not_yet(self):
        """Every-minute cron should not be due within the same minute."""
        last = datetime(2026, 3, 13, 10, 0, 0, tzinfo=UTC)
        now = datetime(2026, 3, 13, 10, 0, 30, tzinfo=UTC)
        assert is_cron_due("* * * * *", last_triggered=last, now=now) is False

    def test_daily_cron(self):
        """Daily cron at midnight should fire next day."""
        last = datetime(2026, 3, 12, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 3, 13, 0, 0, 30, tzinfo=UTC)
        assert is_cron_due("0 0 * * *", last_triggered=last, now=now) is True
