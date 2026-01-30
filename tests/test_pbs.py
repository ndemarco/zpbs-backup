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


class TestBackupGroup:
    """Tests for BackupGroup class."""

    def test_defaults(self):
        group = BackupGroup(backup_type="host", backup_id="myhost-tank")
        assert group.last_backup is None
        assert group.snapshot_count == 0
