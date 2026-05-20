"""Tests for BackupOrchestrator skip-unchanged and preflight skew logic."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from zpbs_backup import backup as backup_mod
from zpbs_backup.backup import SKEW_MARGIN_SECONDS, BackupOrchestrator
from zpbs_backup.config import PBSConfig
from zpbs_backup.zfs import Dataset, PropertyValue, PROP_BACKUP


def _ds(name: str = "tank/data") -> Dataset:
    return Dataset(
        name=name,
        properties={PROP_BACKUP: PropertyValue(value="true", source="local")},
        mountpoint=f"/{name}",
        mounted=True,
    )


def _orch(force: bool = False) -> BackupOrchestrator:
    config = PBSConfig(repository="u@p!t@srv:store", server="srv")
    return BackupOrchestrator(config=config, force=force, output=io.StringIO())


class TestUnchangedSkipReason:
    """Tests for BackupOrchestrator._unchanged_skip_reason."""

    def test_skips_when_written_zero_and_snap_old_enough(self):
        orch = _orch()
        last_backup = datetime.fromtimestamp(2_000_000, tz=timezone.utc)
        snap_creation = int(last_backup.timestamp()) - SKEW_MARGIN_SECONDS - 1
        with patch.object(backup_mod, "get_written_bytes", return_value=0), \
             patch.object(backup_mod, "get_latest_snapshot_creation", return_value=snap_creation):
            reason = orch._unchanged_skip_reason(_ds(), last_backup)
        assert reason is not None
        assert "unchanged" in reason
        assert "written=0" in reason

    def test_no_skip_when_written_nonzero(self):
        orch = _orch()
        last_backup = datetime.fromtimestamp(2_000_000, tz=timezone.utc)
        snap_creation = int(last_backup.timestamp()) - SKEW_MARGIN_SECONDS - 1
        with patch.object(backup_mod, "get_written_bytes", return_value=12345), \
             patch.object(backup_mod, "get_latest_snapshot_creation", return_value=snap_creation):
            assert orch._unchanged_skip_reason(_ds(), last_backup) is None

    def test_no_skip_when_snap_within_safety_margin(self):
        orch = _orch()
        last_backup = datetime.fromtimestamp(2_000_000, tz=timezone.utc)
        # Snap is exactly at the margin boundary — must NOT skip
        snap_creation = int(last_backup.timestamp()) - SKEW_MARGIN_SECONDS + 1
        with patch.object(backup_mod, "get_written_bytes", return_value=0), \
             patch.object(backup_mod, "get_latest_snapshot_creation", return_value=snap_creation):
            assert orch._unchanged_skip_reason(_ds(), last_backup) is None

    def test_no_skip_when_snap_newer_than_last_backup(self):
        orch = _orch()
        last_backup = datetime.fromtimestamp(2_000_000, tz=timezone.utc)
        snap_creation = int(last_backup.timestamp()) + 100  # after last backup
        with patch.object(backup_mod, "get_written_bytes", return_value=0), \
             patch.object(backup_mod, "get_latest_snapshot_creation", return_value=snap_creation):
            assert orch._unchanged_skip_reason(_ds(), last_backup) is None

    def test_no_skip_when_no_previous_backup(self):
        orch = _orch()
        with patch.object(backup_mod, "get_written_bytes", return_value=0), \
             patch.object(backup_mod, "get_latest_snapshot_creation", return_value=1_000_000):
            assert orch._unchanged_skip_reason(_ds(), None) is None

    def test_no_skip_when_no_snapshots_exist(self):
        orch = _orch()
        last_backup = datetime.fromtimestamp(2_000_000, tz=timezone.utc)
        with patch.object(backup_mod, "get_written_bytes", return_value=0), \
             patch.object(backup_mod, "get_latest_snapshot_creation", return_value=None):
            assert orch._unchanged_skip_reason(_ds(), last_backup) is None

    def test_no_skip_when_skew_check_failed(self):
        orch = _orch()
        orch.skip_unchanged_safe = False
        last_backup = datetime.fromtimestamp(2_000_000, tz=timezone.utc)
        snap_creation = int(last_backup.timestamp()) - 1000
        with patch.object(backup_mod, "get_written_bytes", return_value=0), \
             patch.object(backup_mod, "get_latest_snapshot_creation", return_value=snap_creation):
            assert orch._unchanged_skip_reason(_ds(), last_backup) is None


class TestPreflightSkewCheck:
    """Tests for BackupOrchestrator._preflight_skew_check."""

    def test_skipped_when_force(self):
        orch = _orch(force=True)
        with patch.object(orch.client, "get_clock_skew_seconds") as probe:
            orch._preflight_skew_check()
        probe.assert_not_called()
        assert orch.skip_unchanged_safe is True

    def test_silent_under_5s(self):
        orch = _orch()
        with patch.object(orch.client, "get_clock_skew_seconds", return_value=2.0):
            orch._preflight_skew_check()
        assert orch.skip_unchanged_safe is True

    def test_warns_but_allows_within_margin(self):
        orch = _orch()
        with patch.object(orch.client, "get_clock_skew_seconds", return_value=30.0):
            orch._preflight_skew_check()
        assert orch.skip_unchanged_safe is True
        assert "within" in orch.output.getvalue()

    def test_disables_when_skew_exceeds_margin(self):
        orch = _orch()
        with patch.object(orch.client, "get_clock_skew_seconds", return_value=120.0):
            orch._preflight_skew_check()
        assert orch.skip_unchanged_safe is False
        assert "exceeds" in orch.output.getvalue()

    def test_disables_when_skew_negative_exceeds_margin(self):
        orch = _orch()
        with patch.object(orch.client, "get_clock_skew_seconds", return_value=-90.0):
            orch._preflight_skew_check()
        assert orch.skip_unchanged_safe is False

    def test_disables_when_probe_fails(self):
        orch = _orch()
        with patch.object(orch.client, "get_clock_skew_seconds", return_value=None):
            orch._preflight_skew_check()
        assert orch.skip_unchanged_safe is False
        assert "could not measure" in orch.output.getvalue()
