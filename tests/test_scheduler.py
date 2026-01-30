"""Tests for schedule evaluation."""

from datetime import datetime, timedelta

import pytest

from zpbs_backup.scheduler import (
    format_last_backup,
    format_time_delta,
    is_backup_due,
    time_until_due,
)
from zpbs_backup.zfs import Schedule


class TestIsBackupDue:
    """Tests for is_backup_due function."""

    def test_never_backed_up(self):
        """Dataset that has never been backed up is always due."""
        assert is_backup_due(Schedule.DAILY, None)
        assert is_backup_due(Schedule.WEEKLY, None)
        assert is_backup_due(Schedule.MONTHLY, None)

    def test_daily_just_backed_up(self):
        """Dataset backed up just now is not due."""
        now = datetime.now()
        last_backup = now - timedelta(hours=1)
        assert not is_backup_due(Schedule.DAILY, last_backup, now=now)

    def test_daily_overdue(self):
        """Dataset backed up over 24 hours ago is due."""
        now = datetime.now()
        last_backup = now - timedelta(hours=25)
        assert is_backup_due(Schedule.DAILY, last_backup, now=now)

    def test_daily_exactly_due(self):
        """Dataset backed up exactly 24 hours ago is due."""
        now = datetime.now()
        last_backup = now - timedelta(hours=24)
        assert is_backup_due(Schedule.DAILY, last_backup, now=now)

    def test_weekly_not_due(self):
        """Dataset backed up 3 days ago on weekly schedule is not due."""
        now = datetime.now()
        last_backup = now - timedelta(days=3)
        assert not is_backup_due(Schedule.WEEKLY, last_backup, now=now)

    def test_weekly_due(self):
        """Dataset backed up 8 days ago on weekly schedule is due."""
        now = datetime.now()
        last_backup = now - timedelta(days=8)
        assert is_backup_due(Schedule.WEEKLY, last_backup, now=now)

    def test_monthly_not_due(self):
        """Dataset backed up 15 days ago on monthly schedule is not due."""
        now = datetime.now()
        last_backup = now - timedelta(days=15)
        assert not is_backup_due(Schedule.MONTHLY, last_backup, now=now)

    def test_monthly_due(self):
        """Dataset backed up 31 days ago on monthly schedule is due."""
        now = datetime.now()
        last_backup = now - timedelta(days=31)
        assert is_backup_due(Schedule.MONTHLY, last_backup, now=now)


class TestTimeUntilDue:
    """Tests for time_until_due function."""

    def test_never_backed_up(self):
        """Never backed up returns None (already due)."""
        assert time_until_due(Schedule.DAILY, None) is None

    def test_already_due(self):
        """Overdue returns None."""
        now = datetime.now()
        last_backup = now - timedelta(hours=25)
        assert time_until_due(Schedule.DAILY, last_backup, now=now) is None

    def test_not_yet_due(self):
        """Returns time remaining until due."""
        now = datetime.now()
        last_backup = now - timedelta(hours=12)
        remaining = time_until_due(Schedule.DAILY, last_backup, now=now)
        assert remaining is not None
        # Should be approximately 12 hours remaining
        assert timedelta(hours=11) < remaining < timedelta(hours=13)


class TestFormatTimeDelta:
    """Tests for format_time_delta function."""

    def test_none(self):
        assert format_time_delta(None) == "due now"

    def test_zero(self):
        assert format_time_delta(timedelta(0)) == "due now"

    def test_minutes_only(self):
        assert format_time_delta(timedelta(minutes=30)) == "30m"

    def test_hours_and_minutes(self):
        assert format_time_delta(timedelta(hours=2, minutes=15)) == "2h 15m"

    def test_days_only(self):
        assert format_time_delta(timedelta(days=3)) == "3d"

    def test_days_and_hours(self):
        assert format_time_delta(timedelta(days=2, hours=5)) == "2d 5h"

    def test_days_hours_minutes_no_minutes(self):
        # When days > 0, minutes are hidden
        result = format_time_delta(timedelta(days=1, hours=2, minutes=30))
        assert "30m" not in result


class TestFormatLastBackup:
    """Tests for format_last_backup function."""

    def test_never(self):
        assert format_last_backup(None) == "never"

    def test_just_now(self):
        now = datetime.now()
        last = now - timedelta(seconds=30)
        assert format_last_backup(last, now=now) == "just now"

    def test_minutes_ago(self):
        now = datetime.now()
        last = now - timedelta(minutes=15)
        assert format_last_backup(last, now=now) == "15m ago"

    def test_hours_ago(self):
        now = datetime.now()
        last = now - timedelta(hours=5)
        assert format_last_backup(last, now=now) == "5h ago"

    def test_one_day_ago(self):
        now = datetime.now()
        last = now - timedelta(days=1)
        assert format_last_backup(last, now=now) == "1 day ago"

    def test_days_ago(self):
        now = datetime.now()
        last = now - timedelta(days=5)
        assert format_last_backup(last, now=now) == "5 days ago"

    def test_one_month_ago(self):
        now = datetime.now()
        last = now - timedelta(days=35)
        assert format_last_backup(last, now=now) == "1 month ago"

    def test_months_ago(self):
        now = datetime.now()
        last = now - timedelta(days=90)
        assert format_last_backup(last, now=now) == "3 months ago"
