"""Tests for PBS client wrapper."""

from datetime import datetime

import pytest

from zpbs_backup.pbs import BackupGroup, BackupSnapshot


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


class TestBackupGroup:
    """Tests for BackupGroup class."""

    def test_defaults(self):
        group = BackupGroup(backup_type="host", backup_id="myhost-tank")
        assert group.last_backup is None
        assert group.snapshot_count == 0
