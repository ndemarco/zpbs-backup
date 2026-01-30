"""Retention policy parsing and application."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class RetentionPolicy:
    """Parsed retention policy."""

    keep_daily: int | None = None
    keep_weekly: int | None = None
    keep_monthly: int | None = None
    keep_yearly: int | None = None

    def is_empty(self) -> bool:
        """Return True if no retention values are set."""
        return all(
            v is None
            for v in [
                self.keep_daily,
                self.keep_weekly,
                self.keep_monthly,
                self.keep_yearly,
            ]
        )

    def to_pbs_args(self) -> list[str]:
        """Convert to proxmox-backup-client prune arguments."""
        args = []
        if self.keep_daily is not None:
            args.extend(["--keep-daily", str(self.keep_daily)])
        if self.keep_weekly is not None:
            args.extend(["--keep-weekly", str(self.keep_weekly)])
        if self.keep_monthly is not None:
            args.extend(["--keep-monthly", str(self.keep_monthly)])
        if self.keep_yearly is not None:
            args.extend(["--keep-yearly", str(self.keep_yearly)])
        return args


# Regex pattern for retention values
RETENTION_PATTERN = re.compile(r"^(\d+)([dwmy])$", re.IGNORECASE)

# Mapping of suffixes to retention types
SUFFIX_MAP = {
    "d": "keep_daily",
    "w": "keep_weekly",
    "m": "keep_monthly",
    "y": "keep_yearly",
}


def parse_retention(value: str) -> RetentionPolicy:
    """Parse a retention string into a RetentionPolicy.

    Format: "7d,4w,6m,1y"
    - d = daily
    - w = weekly
    - m = monthly
    - y = yearly

    Args:
        value: The retention string to parse

    Returns:
        RetentionPolicy object

    Raises:
        ValueError: If the format is invalid
    """
    policy = RetentionPolicy()

    if not value or not value.strip():
        raise ValueError("Retention value cannot be empty")

    parts = value.split(",")
    seen_types = set()

    for part in parts:
        part = part.strip()
        if not part:
            continue

        match = RETENTION_PATTERN.match(part)
        if not match:
            raise ValueError(
                f"Invalid retention format '{part}'. "
                f"Expected format like '7d', '4w', '6m', or '1y'"
            )

        count = int(match.group(1))
        suffix = match.group(2).lower()

        if count < 0:
            raise ValueError(f"Retention count cannot be negative: {part}")

        if count > 1000:
            raise ValueError(f"Retention count seems too high: {part}")

        if suffix in seen_types:
            raise ValueError(f"Duplicate retention type: {suffix}")

        seen_types.add(suffix)
        attr_name = SUFFIX_MAP[suffix]
        setattr(policy, attr_name, count)

    if policy.is_empty():
        raise ValueError("At least one retention value is required")

    return policy


def format_retention(policy: RetentionPolicy) -> str:
    """Format a RetentionPolicy back to string format.

    Args:
        policy: The retention policy to format

    Returns:
        Formatted string like "7d,4w,6m,1y"
    """
    parts = []
    if policy.keep_daily is not None:
        parts.append(f"{policy.keep_daily}d")
    if policy.keep_weekly is not None:
        parts.append(f"{policy.keep_weekly}w")
    if policy.keep_monthly is not None:
        parts.append(f"{policy.keep_monthly}m")
    if policy.keep_yearly is not None:
        parts.append(f"{policy.keep_yearly}y")
    return ",".join(parts)


# Default retention policy if none specified
DEFAULT_RETENTION = RetentionPolicy(
    keep_daily=7,
    keep_weekly=4,
    keep_monthly=6,
    keep_yearly=1,
)
