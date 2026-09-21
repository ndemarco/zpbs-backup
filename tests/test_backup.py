"""Tests for BackupOrchestrator skip-unchanged and preflight skew logic."""

from __future__ import annotations

import io
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from zpbs_backup import backup as backup_mod
from zpbs_backup.backup import (
    SKEW_MARGIN_SECONDS,
    BackupOrchestrator,
    PruneOrchestrator,
    failure_message,
    get_retention_policy,
)
from zpbs_backup.config import PBSConfig
from zpbs_backup.retention import DEFAULT_RETENTION
from zpbs_backup.zfs import (
    Dataset,
    InvalidPropertyError,
    PropertyValue,
    PROP_BACKUP,
    PROP_PRIORITY,
    PROP_RETENTION,
    PROP_SCHEDULE,
)


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


class TestPlanScheduleCheck:
    """Tests for the schedule gate in BackupOrchestrator.plan."""

    def test_force_bypasses_the_schedule_check(self):
        """--force backs up regardless of when the last backup ran."""
        orch = _orch(force=True)
        ds = _ds()
        with patch.object(backup_mod, "is_backup_due") as is_due, \
             patch.object(orch.client, "get_last_backup_time") as last_backup:
            plan = orch.plan([ds])
        is_due.assert_not_called()
        last_backup.assert_not_called()
        assert plan == [(ds, True, None)]

    def test_daily_dataset_stamped_after_midnight_is_planned_next_day(self):
        """A dataset a long run stamped at 03:20 is planned the following day."""
        orch = _orch()
        ds = _ds()
        stamped_late = (datetime.now() - timedelta(days=1)).replace(hour=3, minute=20)
        with patch.object(orch.client, "get_last_backup_time", return_value=stamped_late), \
             patch.object(backup_mod, "get_written_bytes", return_value=12345), \
             patch.object(backup_mod, "get_latest_snapshot_creation", return_value=None):
            plan = orch.plan([ds])
        assert plan == [(ds, True, None)]

    def test_daily_dataset_already_backed_up_today_is_skipped(self):
        """A dataset backed up earlier the same day is not due again."""
        orch = _orch()
        ds = _ds()
        with patch.object(orch.client, "get_last_backup_time", return_value=datetime.now()):
            plan = orch.plan([ds])
        assert len(plan) == 1
        planned_ds, should_backup, reason = plan[0]
        assert planned_ds is ds
        assert should_backup is False
        assert reason is not None and "not due" in reason


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


class TestFailureMessage:
    """Tests for failure_message."""

    def test_prefers_stderr(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="progress", stderr="  Error: auth failed\n"
        )
        assert failure_message(result) == "Error: auth failed"

    def test_falls_back_to_stdout_when_stderr_is_none(self):
        """Output streamed to the terminal leaves stderr None, not empty."""
        result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="Error: unable to open chunk store", stderr=None
        )
        assert failure_message(result) == "Error: unable to open chunk store"

    def test_reports_exit_status_when_the_client_said_nothing(self):
        result = subprocess.CompletedProcess(args=[], returncode=7, stdout=None, stderr=None)
        message = failure_message(result)
        assert message.strip()
        assert "7" in message

    def test_blank_streams_are_treated_as_nothing_said(self):
        result = subprocess.CompletedProcess(args=[], returncode=7, stdout="  \n", stderr="")
        assert failure_message(result).strip()


class TestBackupDatasetFailureReporting:
    """A failed backup must surface the client's own words."""

    @contextmanager
    def _failing_orchestrator(self, stdout, stderr=None, returncode=2):
        orch = _orch()
        completed = subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=stderr
        )
        with patch.object(orch.client, "backup", return_value=completed), \
             patch.object(orch.client, "create_namespace", return_value=True):
            yield orch

    def test_error_is_the_client_text_not_none(self):
        with self._failing_orchestrator("Error: unable to open chunk store") as orch:
            result = orch.backup_dataset(_ds())

        assert result.success is False
        assert result.error == "Error: unable to open chunk store"
        assert result.error != "None"

    def test_error_reaches_the_run_output(self):
        with self._failing_orchestrator("Error: unable to open chunk store") as orch:
            orch.backup_dataset(_ds())
            printed = orch.output.getvalue()

        assert "FAILED: Error: unable to open chunk store" in printed
        assert "FAILED: None" not in printed

    def test_error_is_never_empty_even_with_a_silent_client(self):
        with self._failing_orchestrator(None, returncode=9) as orch:
            result = orch.backup_dataset(_ds())

        assert result.success is False
        assert result.error and result.error.strip()


def _ds_with(prop: str, value: str, name: str = "tank/data") -> Dataset:
    return Dataset(
        name=name,
        properties={
            PROP_BACKUP: PropertyValue(value="true", source="local"),
            prop: PropertyValue(value=value, source="local"),
        },
        mountpoint=f"/{name}",
        mounted=True,
    )


class TestMisconfiguredDatasetsFailLoudly:
    """A malformed zpbs property is reported, never quietly defaulted."""

    def _run_with(self, datasets):
        orch = _orch()
        with patch.object(backup_mod, "discover_datasets", return_value=datasets), \
             patch.object(orch, "_preflight_skew_check"), \
             patch.object(orch.client, "get_last_backup_time", return_value=None), \
             patch.object(orch.client, "create_namespace", return_value=True), \
             patch.object(orch.client, "backup") as client_backup:
            client_backup.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            summary = orch.run()
        return orch, summary, client_backup

    @pytest.mark.parametrize(
        "prop,value",
        [
            (PROP_SCHEDULE, "hourly"),
            (PROP_PRIORITY, "high"),
            (PROP_RETENTION, "7 days"),
        ],
    )
    def test_invalid_property_is_a_failure_naming_dataset_and_property(self, prop, value):
        orch, summary, client_backup = self._run_with([_ds_with(prop, value)])

        assert summary.failed == 1
        assert summary.successful == 0
        client_backup.assert_not_called()

        error = summary.results[0].error
        assert error and "tank/data" in error and prop in error
        assert "tank/data" in orch.output.getvalue()

    def test_one_bad_dataset_does_not_stop_the_others(self):
        good = _ds_with(PROP_SCHEDULE, "weekly", name="tank/good")
        bad = _ds_with(PROP_SCHEDULE, "hourly", name="tank/bad")

        _orch_used, summary, client_backup = self._run_with([bad, good])

        assert summary.failed == 1
        assert summary.successful == 1
        assert client_backup.call_count == 1

    def test_a_clean_dataset_is_untouched_by_the_check(self):
        _orch_used, summary, client_backup = self._run_with(
            [_ds_with(PROP_RETENTION, "7d,4w")]
        )

        assert summary.failed == 0
        assert client_backup.call_count == 1


class TestRetentionPolicyResolution:
    """Tests for get_retention_policy."""

    def test_unset_retention_takes_the_default(self):
        ds = Dataset(name="tank/data", properties={})
        assert get_retention_policy(ds) is DEFAULT_RETENTION

    def test_invalid_retention_raises_rather_than_defaulting(self):
        with pytest.raises(InvalidPropertyError) as raised:
            get_retention_policy(_ds_with(PROP_RETENTION, "7 days"))

        message = str(raised.value)
        assert "tank/data" in message
        assert PROP_RETENTION in message

    def test_prune_refuses_a_dataset_with_invalid_retention(self):
        """Pruning against a default policy would delete to a policy nobody set."""
        pruner = PruneOrchestrator(
            config=PBSConfig(repository="u@p!t@srv:store", server="srv"),
            output=io.StringIO(),
        )

        with patch.object(pruner.client, "prune") as prune_call:
            assert pruner.prune_dataset(_ds_with(PROP_RETENTION, "7 days")) is False

        prune_call.assert_not_called()
        assert "Refusing to prune tank/data" in pruner.output.getvalue()
