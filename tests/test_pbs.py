"""Tests for PBS client wrapper."""

from datetime import datetime

import pytest

from zpbs_backup.pbs import BackupGroup, BackupSnapshot, sanitize_notes


class TestBackupSnapshot:
    """Tests for BackupSnapshot class."""

    def test_from_dict(self):
        data = {
            "backup-type": "host",
            "backup-id": "myhost-tank-data",
            "backup-time": 1704067200,  # 2024-01-01 00:00:00 UTC
            "size": 1024000,
        }
        snapshot = BackupSnapshot.from_dict(data)

        assert snapshot.backup_type == "host"
        assert snapshot.backup_id == "myhost-tank-data"
        assert snapshot.size == 1024000
        assert snapshot.timestamp.year == 2024

    def test_from_dict_minimal(self):
        data = {
            "backup-time": 1704067200,
        }
        snapshot = BackupSnapshot.from_dict(data)

        assert snapshot.backup_type == "host"
        assert snapshot.backup_id == ""
        assert snapshot.size is None

    def test_from_dict_missing_backup_time(self):
        data = {
            "backup-type": "host",
            "backup-id": "myhost-tank-data",
        }
        snapshot = BackupSnapshot.from_dict(data)
        assert snapshot.timestamp is None

    def test_from_dict_zero_backup_time(self):
        data = {
            "backup-type": "host",
            "backup-id": "myhost-tank-data",
            "backup-time": 0,
        }
        snapshot = BackupSnapshot.from_dict(data)
        assert snapshot.timestamp is None

    def test_from_dict_iso_string_timestamp(self):
        data = {
            "backup-type": "host",
            "backup-id": "myhost-tank-data",
            "backup-time": "2024-06-15T10:30:00",
        }
        snapshot = BackupSnapshot.from_dict(data)
        assert snapshot.timestamp is not None
        assert snapshot.timestamp.year == 2024
        assert snapshot.timestamp.month == 6
        assert snapshot.timestamp.day == 15

    def test_from_dict_invalid_string_timestamp(self):
        data = {
            "backup-type": "host",
            "backup-id": "myhost-tank-data",
            "backup-time": "not-a-date",
        }
        snapshot = BackupSnapshot.from_dict(data)
        assert snapshot.timestamp is None

    def test_from_dict_epoch_before_2000_treated_as_none(self):
        data = {
            "backup-type": "host",
            "backup-id": "myhost-tank-data",
            "backup-time": 100,  # 1970-01-01 00:01:40
        }
        snapshot = BackupSnapshot.from_dict(data)
        assert snapshot.timestamp is None

    def test_from_dict_last_backup_field(self):
        """PBS list returns groups with 'last-backup' instead of 'backup-time'."""
        data = {
            "backup-type": "host",
            "backup-id": "myhost-tank-data",
            "last-backup": 1704067200,  # 2024-01-01 00:00:00 UTC
        }
        snapshot = BackupSnapshot.from_dict(data)
        assert snapshot.timestamp is not None
        assert snapshot.timestamp.year == 2024

    def test_from_dict_backup_time_preferred_over_last_backup(self):
        """backup-time takes precedence when both fields are present."""
        data = {
            "backup-type": "host",
            "backup-id": "myhost-tank-data",
            "backup-time": 1704067200,  # 2024-01-01
            "last-backup": 1706745600,  # 2024-02-01
        }
        snapshot = BackupSnapshot.from_dict(data)
        assert snapshot.timestamp.month == 1  # backup-time wins

    def test_from_dict_stores_epoch(self):
        data = {
            "backup-type": "host",
            "backup-id": "myhost-tank-data",
            "backup-time": 1704067200,
        }
        snapshot = BackupSnapshot.from_dict(data)
        assert snapshot.backup_time_epoch == 1704067200

    def test_from_dict_epoch_none_when_missing(self):
        data = {"backup-type": "host", "backup-id": "myhost-tank-data"}
        snapshot = BackupSnapshot.from_dict(data)
        assert snapshot.backup_time_epoch is None

    def test_from_dict_epoch_none_when_pre_2000(self):
        data = {
            "backup-type": "host",
            "backup-id": "myhost-tank-data",
            "backup-time": 100,
        }
        snapshot = BackupSnapshot.from_dict(data)
        assert snapshot.backup_time_epoch is None

    def test_from_dict_epoch_from_iso_string(self):
        data = {
            "backup-type": "host",
            "backup-id": "myhost-tank-data",
            "backup-time": "2024-06-15T10:30:00",
        }
        snapshot = BackupSnapshot.from_dict(data)
        assert snapshot.backup_time_epoch is not None
        assert isinstance(snapshot.backup_time_epoch, int)


class TestSanitizeNotes:
    """Tests for sanitize_notes function."""

    def test_plain_text_unchanged(self):
        assert sanitize_notes("hello world") == "hello world"

    def test_strips_newlines_and_tabs(self):
        assert sanitize_notes("hello\nworld\ttab\r\n") == "hello world tab"

    def test_strips_html_tags(self):
        assert sanitize_notes("<b>bold</b> text") == "bold text"

    def test_strips_script_tags(self):
        assert sanitize_notes("<script>alert(1)</script>") == "alert(1)"

    def test_strips_javascript_uri(self):
        assert sanitize_notes("javascript:alert(1)") == "alert(1)"

    def test_strips_event_handlers(self):
        assert sanitize_notes("onclick=alert(1)") == "alert(1)"

    def test_truncates_at_256(self):
        long = "a" * 300
        result = sanitize_notes(long)
        assert len(result) == 256
        assert result.endswith("...")

    def test_short_text_not_truncated(self):
        text = "a" * 256
        assert sanitize_notes(text) == text

    def test_collapses_multiple_spaces(self):
        assert sanitize_notes("hello    world") == "hello world"

    def test_empty_string(self):
        assert sanitize_notes("") == ""


class TestBackupGroup:
    """Tests for BackupGroup class."""

    def test_defaults(self):
        group = BackupGroup(backup_type="host", backup_id="myhost-tank")
        assert group.last_backup is None
        assert group.snapshot_count == 0
