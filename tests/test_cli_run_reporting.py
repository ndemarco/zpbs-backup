"""Tests that `zpbs-backup run` reports metrics and email independently."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from zpbs_backup import cli as cli_mod
from zpbs_backup import metrics as metrics_mod
from zpbs_backup import notify as notify_mod
from zpbs_backup.backup import BackupSummary
from zpbs_backup.cli import main
from zpbs_backup.config import PBSConfig
from zpbs_backup.metrics import MetricsConfig
from zpbs_backup.notify import NotificationConfig


def _run(args, metrics_config, notify_config):
    """Invoke `run`, stubbing everything but the reporting decision."""
    runner = CliRunner()
    pbs_config = PBSConfig(repository="u@p!t@srv:store", server="srv")

    with patch.object(cli_mod, "load_config", return_value=pbs_config), \
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

    def test_no_notify_still_reports_metrics(self, metrics_on, email_on):
        result, push, textfile, mail = _run(["--no-notify"], metrics_on, email_on)

        assert result.exit_code == 0
        push.assert_called_once()
        textfile.assert_called_once()
        mail.assert_not_called()

    def test_notify_disabled_by_environment_still_reports_metrics(self, metrics_on):
        off = NotificationConfig(enabled=False, recipient="admin@example.com")
        result, push, textfile, mail = _run([], metrics_on, off)

        assert result.exit_code == 0
        push.assert_called_once()
        textfile.assert_called_once()
        mail.assert_not_called()

    def test_email_is_sent_when_no_metrics_are_configured(self, email_on):
        result, push, textfile, mail = _run([], MetricsConfig(), email_on)

        assert result.exit_code == 0
        mail.assert_called_once()
        # Both transports are still invoked; each no-ops on its own unset value.
        assert push.call_args[0][2] is None
        assert textfile.call_args[0][1] is None

    def test_dry_run_reports_neither(self, metrics_on, email_on):
        result, push, textfile, mail = _run(["--dry-run"], metrics_on, email_on)

        assert result.exit_code == 0
        push.assert_not_called()
        textfile.assert_not_called()
        mail.assert_not_called()
