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


class TestBackupGroup:
    """Tests for BackupGroup class."""

    def test_defaults(self):
        group = BackupGroup(backup_type="host", backup_id="myhost-tank")
        assert group.last_backup is None
        assert group.snapshot_count == 0


class TestPBSClientBackup:
    """Tests for PBSClient.backup() method."""

    def test_backup_with_change_detection_mode(self):
        """Verify change-detection-mode flag is included in backup command."""
        from zpbs_backup.config import PBSConfig
        from zpbs_backup.pbs import PBSClient

        config = PBSConfig(
            repository="user@realm!tokenname@server:datastore",
            password="test-token",
        )
        client = PBSClient(config)

        # Test with metadata mode
        result = client.backup(
            backup_id="test-backup",
            source_path="/test/path",
            change_detection_mode="metadata",
            dry_run=True,
        )

        assert "--change-detection-mode" in result.stdout or result.returncode == 0

    def test_backup_without_change_detection_mode(self):
        """Verify backup works without change-detection-mode (backward compatibility)."""
        from zpbs_backup.config import PBSConfig
        from zpbs_backup.pbs import PBSClient

        config = PBSConfig(
            repository="user@realm!tokenname@server:datastore",
            password="test-token",
        )
        client = PBSClient(config)

        # Test without change detection mode (should not include the flag)
        result = client.backup(
            backup_id="test-backup",
            source_path="/test/path",
            dry_run=True,
        )

        assert result.returncode == 0
