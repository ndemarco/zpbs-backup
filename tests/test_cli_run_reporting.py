"""Tests that `zpbs-backup run` reports metrics and notifications independently."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from zpbs_backup import cli as cli_mod
from zpbs_backup import lock as lock_mod
from zpbs_backup import metrics as metrics_mod
from zpbs_backup import notify as notify_mod
from zpbs_backup.backup import BackupResult, BackupSummary
from zpbs_backup.cli import main
from zpbs_backup.config import PBSConfig
from zpbs_backup.lock import EXIT_ALREADY_RUNNING, run_lock
from zpbs_backup.metrics import MetricsConfig
from zpbs_backup.notify import NotificationConfig
from zpbs_backup.zfs import Dataset

SCRIPT_HOOK = "/usr/local/bin/zpbs-send-notification"


def _run(args, metrics_config, notify_config, lock_path, summary=None):
    """Invoke `run`, stubbing everything but the reporting decision."""
    runner = CliRunner()
    pbs_config = PBSConfig(repository="u@p!t@srv:store", server="srv")

    with patch.object(lock_mod, "LOCK_PATH", lock_path), \
         patch.object(cli_mod, "load_config", return_value=pbs_config), \
         patch.object(cli_mod, "_check_pbs_connection"), \
         patch.object(cli_mod, "get_hostname", return_value="storage-server"), \
         patch.object(
             cli_mod.BackupOrchestrator, "run", return_value=summary or BackupSummary()
         ), \
         patch.object(metrics_mod, "get_metrics_config", return_value=metrics_config), \
         patch.object(notify_mod, "get_notification_config", return_value=notify_config), \
         patch.object(metrics_mod, "push_to_gateway") as push, \
         patch.object(metrics_mod, "write_textfile") as textfile, \
         patch.object(notify_mod, "_send_via_external_script", return_value=True) as hook:
        result = runner.invoke(main, ["run", *args])

    return result, push, textfile, hook


@pytest.fixture
def metrics_on() -> MetricsConfig:
    return MetricsConfig(
        pushgateway_url="http://10.0.16.16:9091",
        textfile_dir="/var/lib/node_exporter/textfile_collector",
    )


@pytest.fixture
def notify_on() -> NotificationConfig:
    return NotificationConfig(
        enabled=True, external_script=SCRIPT_HOOK, syslog_enabled=False
    )


class TestMetricsAndNotificationsAreIndependent:
    """Turning one off must not turn the other off."""

    def test_no_notify_still_reports_metrics(self, metrics_on, notify_on, tmp_path):
        result, push, textfile, hook = _run(
            ["--no-notify"], metrics_on, notify_on, tmp_path / "run.lock"
        )

        assert result.exit_code == 0
        push.assert_called_once()
        textfile.assert_called_once()
        hook.assert_not_called()

    def test_notify_disabled_by_environment_still_reports_metrics(self, metrics_on, tmp_path):
        off = NotificationConfig(enabled=False, external_script=SCRIPT_HOOK)
        result, push, textfile, hook = _run([], metrics_on, off, tmp_path / "run.lock")

        assert result.exit_code == 0
        push.assert_called_once()
        textfile.assert_called_once()
        hook.assert_not_called()

    def test_notification_is_sent_when_no_metrics_are_configured(self, notify_on, tmp_path):
        result, push, textfile, hook = _run(
            [], MetricsConfig(), notify_on, tmp_path / "run.lock"
        )

        assert result.exit_code == 0
        hook.assert_called_once()
        # Both transports are still invoked; each no-ops on its own unset value.
        assert push.call_args[0][2] is None
        assert textfile.call_args[0][1] is None

    def test_dry_run_reports_neither(self, metrics_on, notify_on, tmp_path):
        result, push, textfile, hook = _run(
            ["--dry-run"], metrics_on, notify_on, tmp_path / "run.lock"
        )

        assert result.exit_code == 0
        push.assert_not_called()
        textfile.assert_not_called()
        hook.assert_not_called()


class TestExitCodeReflectsFailures:
    """`run` exits non-zero when any dataset failed, independent of reporting."""

    def test_a_failed_dataset_causes_a_nonzero_exit(self, notify_on, tmp_path):
        failed = BackupSummary()
        failed.results.append(
            BackupResult(
                dataset=Dataset(name="tank/data", properties={}),
                success=False,
                error="boom",
            )
        )

        result, _push, _textfile, _hook = _run(
            [], MetricsConfig(), notify_on, tmp_path / "run.lock", summary=failed
        )

        assert result.exit_code == 1


class TestRunLocking:
    """`run` must not start while another run holds the lock."""

    def test_a_second_run_is_refused_with_the_deferral_code(
        self, metrics_on, notify_on, tmp_path
    ):
        lock_path = tmp_path / "run.lock"

        with run_lock(lock_path), patch.object(lock_mod, "LOCK_PATH", lock_path):
            result, push, textfile, hook = _run(
                [], metrics_on, notify_on, lock_path
            )

        assert result.exit_code == EXIT_ALREADY_RUNNING
        assert "Not starting" in result.output
        push.assert_not_called()
        textfile.assert_not_called()
        hook.assert_not_called()

    def test_the_lock_is_released_for_the_next_run(self, metrics_on, notify_on, tmp_path):
        lock_path = tmp_path / "run.lock"

        first, _push, _textfile, _hook = _run([], metrics_on, notify_on, lock_path)
        second, _push2, _textfile2, _hook2 = _run([], metrics_on, notify_on, lock_path)

        assert first.exit_code == 0
        assert second.exit_code == 0

    def test_a_dry_run_does_not_contend_for_the_lock(self, metrics_on, notify_on, tmp_path):
        """A dry run writes nothing, so it must not block a real run."""
        lock_path = tmp_path / "run.lock"

        with run_lock(lock_path):
            result, _push, _textfile, _hook = _run(
                ["--dry-run"], metrics_on, notify_on, lock_path
            )

        assert result.exit_code == 0
