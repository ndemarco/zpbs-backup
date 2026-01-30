"""Backup schedule evaluation."""

from __future__ import annotations

from datetime import datetime, timedelta

from .zfs import Schedule


# Schedule intervals
SCHEDULE_INTERVALS = {
    Schedule.DAILY: timedelta(hours=24),
    Schedule.WEEKLY: timedelta(days=7),
    Schedule.MONTHLY: timedelta(days=30),
}


def is_backup_due(
    schedule: Schedule,
    last_backup: datetime | None,
    now: datetime | None = None,
) -> bool:
    """Determine if a backup is due based on schedule and last backup time.

    Args:
        schedule: The backup schedule (daily, weekly, monthly)
        last_backup: Timestamp of last backup, or None if never backed up
        now: Current time (defaults to datetime.now())

    Returns:
        True if a backup should be run
    """
    if now is None:
        now = datetime.now()

    # Never backed up = definitely due
    if last_backup is None:
        return True

    interval = SCHEDULE_INTERVALS.get(schedule, SCHEDULE_INTERVALS[Schedule.DAILY])
    next_due = last_backup + interval

    return now >= next_due


def time_until_due(
    schedule: Schedule,
    last_backup: datetime | None,
    now: datetime | None = None,
) -> timedelta | None:
    """Calculate time until the next backup is due.

    Args:
        schedule: The backup schedule
        last_backup: Timestamp of last backup, or None if never backed up
        now: Current time (defaults to datetime.now())

    Returns:
        Time until due, or None if already due/overdue
    """
    if now is None:
        now = datetime.now()

    if last_backup is None:
        return None  # Already due

    interval = SCHEDULE_INTERVALS.get(schedule, SCHEDULE_INTERVALS[Schedule.DAILY])
    next_due = last_backup + interval

    if now >= next_due:
        return None  # Already due

    return next_due - now


def format_time_delta(delta: timedelta | None) -> str:
    """Format a timedelta for human-readable display.

    Args:
        delta: The timedelta to format, or None

    Returns:
        Formatted string like "2h 30m" or "due now"
    """
    if delta is None:
        return "due now"

    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "due now"

    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 and days == 0:  # Only show minutes if less than a day
        parts.append(f"{minutes}m")

    return " ".join(parts) if parts else "due now"


def format_last_backup(last_backup: datetime | None, now: datetime | None = None) -> str:
    """Format last backup time for display.

    Args:
        last_backup: Timestamp of last backup, or None
        now: Current time (defaults to datetime.now())

    Returns:
        Formatted string like "2h ago" or "never"
    """
    if last_backup is None:
        return "never"

    if now is None:
        now = datetime.now()

    delta = now - last_backup
    total_seconds = int(delta.total_seconds())

    if total_seconds < 0:
        return "future?"  # Shouldn't happen

    if total_seconds < 60:
        return "just now"

    if total_seconds < 3600:
        minutes = total_seconds // 60
        return f"{minutes}m ago"

    if total_seconds < 86400:
        hours = total_seconds // 3600
        return f"{hours}h ago"

    days = total_seconds // 86400
    if days == 1:
        return "1 day ago"
    if days < 30:
        return f"{days} days ago"

    months = days // 30
    if months == 1:
        return "1 month ago"
    return f"{months} months ago"
