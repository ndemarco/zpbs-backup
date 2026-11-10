"""Tests for zpbs_backup.metrics module."""

from __future__ import annotations

import os
import types
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from zpbs_backup import metrics as metrics_mod
from zpbs_backup.backup import SkipCause
from zpbs_backup.metrics import (
    MetricsConfig,
    _render_metrics,
    get_metrics_config,
    push_to_gateway,
    report_metrics,
    write_textfile,
)

METRIC_NAMES = (
    "zpbs_backup_last_run_timestamp_seconds",
    "zpbs_backup_dataset_last_backup_timestamp_seconds",
    "zpbs_backup_dataset_last_current_timestamp_seconds",
    "zpbs_backup_unknown_properties",
    "zpbs_backup_duration_seconds",
    "zpbs_backup_datasets_successful",
    "zpbs_backup_datasets_failed",
    "zpbs_backup_datasets_skipped",
    "zpbs_backup_datasets_skipped_by_cause",
)


def _summary(
    successful=0,
    failed=0,
    skipped=0,
    duration=0.0,
    skipped_by_cause=None,
    last_backup_times=None,
    last_current_times=None,
    unknown_properties=None,
):
    """Build a minimal stand-in for BackupSummary (metrics only reads these fields)."""
    counts = {cause: 0 for cause in SkipCause}
    counts.update(skipped_by_cause or {})

    return types.SimpleNamespace(
        successful=successful,
        failed=failed,
        skipped=skipped,
        duration_seconds=duration,
        skipped_by_cause=counts,
        last_backup_times=last_backup_times or {},
        last_current_times=last_current_times or {},
        unknown_properties=unknown_properties or {},
    )


class TestPushToGateway:
    def test_noop_when_url_missing(self):
        summary = _summary(successful=1)
        # No exception, no attempt to open urllib — url is falsy.
        push_to_gateway(_render_metrics(summary), "host1", None)
        push_to_gateway(_render_metrics(summary), "host1", "")

    def test_puts_every_metric_to_gateway_url(self):
        summary = _summary(successful=2, failed=1, skipped=0, duration=5.0)
        captured = {}

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=5):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = req.data.decode()
            return _FakeResponse()

        with mock.patch("zpbs_backup.metrics.urllib.request.urlopen", side_effect=fake_urlopen):
            push_to_gateway(_render_metrics(summary), "storage-server", "http://10.0.16.16:9091")

        assert captured["url"] == (
            "http://10.0.16.16:9091/metrics/job/zpbs_backup/instance/storage-server"
        )
        assert captured["method"] == "PUT"
        for name in METRIC_NAMES:
            assert name in captured["body"]
        assert "zpbs_backup_datasets_successful 2" in captured["body"]
        assert "zpbs_backup_datasets_failed 1" in captured["body"]

    def test_push_failure_logs_and_does_not_raise(self, capsys):
        summary = _summary(successful=1)
        with mock.patch(
            "zpbs_backup.metrics.urllib.request.urlopen",
            side_effect=OSError("connection refused"),
        ):
            push_to_gateway(_render_metrics(summary), "host1", "http://10.0.16.16:9091")
        assert "zpbs-backup metrics: push failed" in capsys.readouterr().err


class TestWriteTextfile:
    def test_noop_when_dir_missing(self, tmp_path):
        summary = _summary(successful=1)
        write_textfile(_render_metrics(summary), None)
        write_textfile(_render_metrics(summary), "")
        assert list(tmp_path.iterdir()) == []

    def test_writes_the_same_metrics_as_pushgateway(self, tmp_path):
        summary = _summary(successful=3, failed=0, skipped=1, duration=12.5)
        write_textfile(_render_metrics(summary), str(tmp_path))

        dest = tmp_path / "zpbs_backup.prom"
        assert dest.exists()
        content = dest.read_text()
        for name in METRIC_NAMES:
            assert name in content
        assert "zpbs_backup_datasets_successful 3" in content
        assert "zpbs_backup_datasets_skipped 1" in content
        assert "zpbs_backup_duration_seconds 12.5" in content

    def test_write_is_atomic_via_tempfile_and_replace(self, tmp_path):
        summary = _summary(successful=1)

        with mock.patch(
            "zpbs_backup.metrics.os.replace", wraps=os.replace
        ) as mock_replace:
            write_textfile(_render_metrics(summary), str(tmp_path))

        assert mock_replace.call_count == 1
        src, dst = mock_replace.call_args[0]
        assert Path(src).parent == tmp_path
        assert Path(src).name != "zpbs_backup.prom"
        assert dst == tmp_path / "zpbs_backup.prom"

        # No temp file left behind after a successful write.
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "zpbs_backup.prom"]
        assert leftovers == []

    def test_file_is_world_readable(self, tmp_path):
        write_textfile(_render_metrics(_summary(successful=1)), str(tmp_path))

        mode = (tmp_path / "zpbs_backup.prom").stat().st_mode & 0o777
        assert mode == 0o644

    def test_missing_dir_logs_error_and_does_not_raise(self, tmp_path, capsys):
        summary = _summary(successful=1)
        missing_dir = tmp_path / "does-not-exist"

        write_textfile(_render_metrics(summary), str(missing_dir))

        assert not missing_dir.exists()
        assert "zpbs-backup metrics: textfile write failed" in capsys.readouterr().err

    def test_unwritable_dir_logs_error_and_does_not_raise(self, tmp_path, capsys):
        summary = _summary(successful=1)

        with mock.patch(
            "zpbs_backup.metrics.tempfile.mkstemp",
            side_effect=PermissionError("Permission denied"),
        ):
            write_textfile(_render_metrics(summary), str(tmp_path))

        assert "zpbs-backup metrics: textfile write failed" in capsys.readouterr().err
        assert list(tmp_path.iterdir()) == []


class TestMetricsConfig:
    """Tests for get_metrics_config."""

    def test_both_transports_default_to_unset(self, monkeypatch):
        monkeypatch.delenv("ZPBS_PUSHGATEWAY", raising=False)
        monkeypatch.delenv("ZPBS_TEXTFILE_DIR", raising=False)

        config = get_metrics_config()

        assert config.pushgateway_url is None
        assert config.textfile_dir is None

    def test_reads_both_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("ZPBS_PUSHGATEWAY", "http://10.0.16.16:9091")
        monkeypatch.setenv("ZPBS_TEXTFILE_DIR", "/var/lib/node_exporter/textfile_collector")

        config = get_metrics_config()

        assert config.pushgateway_url == "http://10.0.16.16:9091"
        assert config.textfile_dir == "/var/lib/node_exporter/textfile_collector"

    def test_empty_string_counts_as_unset(self, monkeypatch):
        """An exported-but-blank variable must not be treated as a URL."""
        monkeypatch.setenv("ZPBS_PUSHGATEWAY", "")
        monkeypatch.setenv("ZPBS_TEXTFILE_DIR", "")

        config = get_metrics_config()

        assert config.pushgateway_url is None
        assert config.textfile_dir is None


class TestReportMetrics:
    """Tests for report_metrics."""

    def test_drives_both_transports(self, tmp_path):
        summary = _summary(successful=1)
        config = MetricsConfig(
            pushgateway_url="http://10.0.16.16:9091", textfile_dir=str(tmp_path)
        )

        with mock.patch.object(metrics_mod, "push_to_gateway") as push:
            report_metrics(summary, "storage-server", config)

        payload, host, url = push.call_args[0]
        assert (host, url) == ("storage-server", config.pushgateway_url)
        # Rendered once: the textfile holds exactly what the gateway gets.
        assert (tmp_path / "zpbs_backup.prom").read_bytes() == payload

    def test_is_a_noop_when_nothing_is_configured(self, tmp_path):
        report_metrics(_summary(successful=1), "storage-server", MetricsConfig())

        assert list(tmp_path.iterdir()) == []


class TestPerDatasetSeries:
    """Per-dataset freshness and unknown-property series."""

    def test_no_last_success_series_remains(self):
        body = _render_metrics(_summary(successful=1)).decode()

        assert "last_success" not in body

    def test_freshness_has_one_series_per_dataset(self):
        when = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)
        body = _render_metrics(
            _summary(successful=1, last_backup_times={"tank/data": when})
        ).decode()

        assert "# TYPE zpbs_backup_dataset_last_backup_timestamp_seconds gauge" in body
        assert (
            'zpbs_backup_dataset_last_backup_timestamp_seconds{dataset="tank/data"} '
            f"{when.timestamp()}"
        ) in body

    def test_dataset_without_a_snapshot_has_no_series(self):
        body = _render_metrics(_summary(successful=1)).decode()

        assert "# HELP zpbs_backup_dataset_last_backup_timestamp_seconds" in body
        assert "zpbs_backup_dataset_last_backup_timestamp_seconds{" not in body

    def test_current_timestamp_has_one_series_per_dataset(self):
        when = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)
        body = _render_metrics(
            _summary(last_current_times={'tank/a"b': when})
        ).decode()

        assert "# TYPE zpbs_backup_dataset_last_current_timestamp_seconds gauge" in body
        assert (
            'zpbs_backup_dataset_last_current_timestamp_seconds{dataset="tank/a\\"b"} '
            f"{when.timestamp()}"
        ) in body

    def test_never_backed_up_dataset_has_no_current_series(self):
        body = _render_metrics(_summary(successful=1)).decode()

        assert "# HELP zpbs_backup_dataset_last_current_timestamp_seconds" in body
        assert "zpbs_backup_dataset_last_current_timestamp_seconds{" not in body

    def test_unknown_properties_count_only_datasets_that_have_some(self):
        body = _render_metrics(
            _summary(unknown_properties={"tank/odd": ["zpbs:foo", "zpbs:bar"]})
        ).decode()

        assert 'zpbs_backup_unknown_properties{dataset="tank/odd"} 2' in body
        assert body.count("zpbs_backup_unknown_properties{") == 1

    def test_label_values_are_escaped(self):
        body = _render_metrics(
            _summary(unknown_properties={'tank/a"b': ["zpbs:x"]})
        ).decode()

        assert 'dataset="tank/a\\"b"' in body
