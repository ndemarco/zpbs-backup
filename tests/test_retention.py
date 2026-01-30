"""Tests for retention policy parsing."""

import pytest

from zpbs_backup.retention import (
    RetentionPolicy,
    format_retention,
    parse_retention,
)


class TestParseRetention:
    """Tests for parse_retention function."""

    def test_parse_daily_only(self):
        policy = parse_retention("7d")
        assert policy.keep_daily == 7
        assert policy.keep_weekly is None
        assert policy.keep_monthly is None
        assert policy.keep_yearly is None

    def test_parse_full_policy(self):
        policy = parse_retention("7d,4w,6m,1y")
        assert policy.keep_daily == 7
        assert policy.keep_weekly == 4
        assert policy.keep_monthly == 6
        assert policy.keep_yearly == 1

    def test_parse_partial_policy(self):
        policy = parse_retention("14d,2m")
        assert policy.keep_daily == 14
        assert policy.keep_weekly is None
        assert policy.keep_monthly == 2
        assert policy.keep_yearly is None

    def test_parse_with_spaces(self):
        policy = parse_retention("7d, 4w, 6m")
        assert policy.keep_daily == 7
        assert policy.keep_weekly == 4
        assert policy.keep_monthly == 6

    def test_parse_uppercase(self):
        policy = parse_retention("7D,4W")
        assert policy.keep_daily == 7
        assert policy.keep_weekly == 4

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="Invalid retention format"):
            parse_retention("7days")

    def test_invalid_empty(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            parse_retention("")

    def test_duplicate_type(self):
        with pytest.raises(ValueError, match="Duplicate retention type"):
            parse_retention("7d,5d")

    def test_negative_count(self):
        # Regex won't match negative numbers, so it's an invalid format
        with pytest.raises(ValueError, match="Invalid retention format"):
            parse_retention("-1d")

    def test_very_high_count(self):
        with pytest.raises(ValueError, match="seems too high"):
            parse_retention("9999d")


class TestFormatRetention:
    """Tests for format_retention function."""

    def test_format_full_policy(self):
        policy = RetentionPolicy(
            keep_daily=7,
            keep_weekly=4,
            keep_monthly=6,
            keep_yearly=1,
        )
        assert format_retention(policy) == "7d,4w,6m,1y"

    def test_format_partial_policy(self):
        policy = RetentionPolicy(keep_daily=14, keep_monthly=3)
        assert format_retention(policy) == "14d,3m"

    def test_format_empty_policy(self):
        policy = RetentionPolicy()
        assert format_retention(policy) == ""


class TestRetentionPolicy:
    """Tests for RetentionPolicy class."""

    def test_is_empty_true(self):
        policy = RetentionPolicy()
        assert policy.is_empty()

    def test_is_empty_false(self):
        policy = RetentionPolicy(keep_daily=7)
        assert not policy.is_empty()

    def test_to_pbs_args(self):
        policy = RetentionPolicy(
            keep_daily=7,
            keep_weekly=4,
            keep_monthly=6,
            keep_yearly=1,
        )
        args = policy.to_pbs_args()
        assert "--keep-daily" in args
        assert "7" in args
        assert "--keep-weekly" in args
        assert "4" in args
        assert "--keep-monthly" in args
        assert "6" in args
        assert "--keep-yearly" in args
        assert "1" in args

    def test_to_pbs_args_partial(self):
        policy = RetentionPolicy(keep_daily=7)
        args = policy.to_pbs_args()
        assert args == ["--keep-daily", "7"]
