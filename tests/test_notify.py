"""Tests for how a backup failure is reported to the notification hook and syslog."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from zpbs_backup import notify as notify_mod
from zpbs_backup.backup import BackupResult, BackupSummary
from zpbs_backup.notify import (
    NotificationConfig,
    _send_to_syslog,
    _syslog_error,
    format_summary_for_notification,
    send_notification,
)
from zpbs_backup.zfs import PROP_BACKUP, Dataset, PropertyValue

CLIENT_ERROR = "Error: unable to open chunk store\nCaused by: permission denied"


def _ds(name: str = "tank/data") -> Dataset:
    return Dataset(
        name=name,
        properties={PROP_BACKUP: PropertyValue(value="true", source="local")},
        mountpoint=f"/{name}",
        mounted=True,
    )


def _failed_summary(error: str | None = CLIENT_ERROR) -> BackupSummary:
    summary = BackupSummary()
    summary.results.append(BackupResult(dataset=_ds(), success=False, error=error))
    summary.end_time = summary.start_time
    return summary


class TestSyslogError:
    """Tests for _syslog_error."""

    def test_flattens_a_multiline_error_to_one_line(self):
        flattened = _syslog_error(CLIENT_ERROR)
        assert "\n" not in flattened
        assert "unable to open chunk store" in flattened
        assert "permission denied" in flattened

    def test_strips_quotes_that_would_break_the_field(self):
        assert '"' not in _syslog_error('bad "path" given')

    def test_missing_error_is_still_readable(self):
        assert _syslog_error(None).strip()
        assert _syslog_error("   ").strip()
        assert "None" not in _syslog_error(None)


class TestSyslogRecord:
    """The syslog line for a failure carries the client's text."""

    def test_failure_line_quotes_the_client_error(self):
        with patch.object(notify_mod, "syslog") as syslog:
            syslog.LOG_ERR = 3
            syslog.LOG_INFO = 6
            syslog.LOG_LOCAL0 = 128
            assert _send_to_syslog(_failed_summary(), "storage-server") is True

        logged = " ".join(str(c) for c in syslog.syslog.call_args_list)
        assert "unable to open chunk store" in logged
        assert 'error="None"' not in logged


class TestNotificationBody:
    """The notification body carries the client's text."""

    def test_failed_dataset_line_shows_the_error(self):
        _subject, body = format_summary_for_notification(_failed_summary(), "storage-server")

        assert "unable to open chunk store" in body
        assert "permission denied" in body
        assert "tank/data: None" not in body

    def test_missing_error_does_not_print_none(self):
        _subject, body = format_summary_for_notification(_failed_summary(None), "storage-server")

        assert "tank/data: None" not in body
        assert "no error text reported" in body


class TestSyslogIsIndependentOfTheHook:
    """ZPBS_SYSLOG controls syslog; ZPBS_NOTIFY and --no-notify control the hook."""

    @pytest.mark.parametrize("syslog_on", [True, False])
    @pytest.mark.parametrize("notify_on", [True, False])
    @pytest.mark.parametrize("run_hook", [True, False])
    def test_each_combination(self, syslog_on, notify_on, run_hook):
        config = NotificationConfig(
            enabled=notify_on,
            external_script="/usr/local/bin/zpbs-send-notification",
            syslog_enabled=syslog_on,
        )
        with patch.object(notify_mod, "_send_to_syslog") as to_syslog, \
             patch.object(notify_mod, "_send_via_external_script", return_value=True) as hook:
            assert send_notification(_failed_summary(), "host", config, run_hook=run_hook)

        assert to_syslog.called is syslog_on
        assert hook.called is (notify_on and run_hook)

    def test_unknown_properties_are_warned_to_syslog(self):
        summary = _failed_summary()
        summary.unknown_properties = {"tank/typo": ["zpbs:backups"]}
        with patch.object(notify_mod, "syslog") as syslog:
            syslog.LOG_WARNING = 4
            assert _send_to_syslog(summary, "host") is True

        warned = [c for c in syslog.syslog.call_args_list if c.args[0] == 4]
        assert len(warned) == 1
        assert "dataset=tank/typo" in warned[0].args[1]
        assert "property=zpbs:backups" in warned[0].args[1]
