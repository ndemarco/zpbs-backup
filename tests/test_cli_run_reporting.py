"""Tests that `zpbs-backup run` reports metrics and email independently."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from zpbs_backup import cli as cli_mod
from zpbs_backup import lock as lock_mod
from zpbs_backup import metrics as metrics_mod
from zpbs_backup import notify as notify_mod
from zpbs_backup.backup import BackupSummary
from zpbs_backup.cli import main
from zpbs_backup.config import PBSConfig
from zpbs_backup.lock import EXIT_ALREADY_RUNNING, run_lock
from zpbs_backup.metrics import MetricsConfig
from zpbs_backup.notify import NotificationConfig


def _run(args, metrics_config, notify_config, lock_path):
    """Invoke `run`, stubbing everything but the reporting decision."""
    runner = CliRunner()
    pbs_config = PBSConfig(repository="u@p!t@srv:store", server="srv")

    with patch.object(lock_mod, "LOCK_PATH", lock_path), \
         patch.object(cli_mod, "load_config", return_value=pbs_config), \
         patch.object(cli_mod, "_check_pbs_connection"), \
         patch.object(cli_mod, "get_hostname", return_value="storage-server"), \
         patch.object(cli_mod.BackupOrchestrator, "run", return_value=BackupSummary()), \
         patch.object(metrics_mod, "get_metrics_config", return_value=metrics_config), \
         patch.object(notify_mod, "get_notification_config", return_value=notify_config), \
         patch.object(metrics_mod, "push_to_gateway") as push, \
         patch.object(metrics_mod, "write_textfile") as textfile, \
         patch.object(notify_mod, "_send_via_mail", return_value=True) as mail:
        result = runner.invoke(main, ["run"] + args)

    return result, push, textfile, mail


@pytest.fixture
def metrics_on() -> MetricsConfig:
    return MetricsConfig(
        pushgateway_url="http://10.0.16.16:9091",
        textfile_dir="/var/lib/node_exporter/textfile_collector",
    )


@pytest.fixture
def email_on() -> NotificationConfig:
    return NotificationConfig(
        enabled=True, recipient="admin@example.com", syslog_enabled=False
    )


class TestMetricsAndNotificationsAreIndependent:
    """Turning one off must not turn the other off."""

    def test_no_notify_still_reports_metrics(self, metrics_on, email_on, tmp_path):
        result, push, textfile, mail = _run(
            ["--no-notify"], metrics_on, email_on, tmp_path / "run.lock"
        )

        assert result.exit_code == 0
        push.assert_called_once()
        textfile.assert_called_once()
        mail.assert_not_called()

    def test_notify_disabled_by_environment_still_reports_metrics(self, metrics_on, tmp_path):
        off = NotificationConfig(enabled=False, recipient="admin@example.com")
        result, push, textfile, mail = _run([], metrics_on, off, tmp_path / "run.lock")

        assert result.exit_code == 0
        push.assert_called_once()
        textfile.assert_called_once()
        mail.assert_not_called()

    def test_email_is_sent_when_no_metrics_are_configured(self, email_on, tmp_path):
        result, push, textfile, mail = _run(
            [], MetricsConfig(), email_on, tmp_path / "run.lock"
        )

        assert result.exit_code == 0
        mail.assert_called_once()
        # Both transports are still invoked; each no-ops on its own unset value.
        assert push.call_args[0][2] is None
        assert textfile.call_args[0][1] is None

    def test_dry_run_reports_neither(self, metrics_on, email_on, tmp_path):
        result, push, textfile, mail = _run(
            ["--dry-run"], metrics_on, email_on, tmp_path / "run.lock"
        )

        assert result.exit_code == 0
        push.assert_not_called()
        textfile.assert_not_called()
        mail.assert_not_called()


class TestRunLocking:
    """`run` must not start while another run holds the lock."""

    def test_a_second_run_is_refused_with_the_deferral_code(
        self, metrics_on, email_on, tmp_path
    ):
        lock_path = tmp_path / "run.lock"

        with run_lock(lock_path):
            with patch.object(lock_mod, "LOCK_PATH", lock_path):
                result, push, textfile, mail = _run(
                    [], metrics_on, email_on, lock_path
                )

        assert result.exit_code == EXIT_ALREADY_RUNNING
        assert "Not starting" in result.output
        push.assert_not_called()
        textfile.assert_not_called()
        mail.assert_not_called()

    def test_the_lock_is_released_for_the_next_run(self, metrics_on, email_on, tmp_path):
        lock_path = tmp_path / "run.lock"

        first, _push, _textfile, _mail = _run([], metrics_on, email_on, lock_path)
        second, _push2, _textfile2, _mail2 = _run([], metrics_on, email_on, lock_path)

        assert first.exit_code == 0
        assert second.exit_code == 0

    def test_a_dry_run_does_not_contend_for_the_lock(self, metrics_on, email_on, tmp_path):
        """A dry run writes nothing, so it must not block a real run."""
        lock_path = tmp_path / "run.lock"

        with run_lock(lock_path):
            result, _push, _textfile, _mail = _run(
                ["--dry-run"], metrics_on, email_on, lock_path
            )

        assert result.exit_code == 0
