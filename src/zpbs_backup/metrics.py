"""Prometheus metrics reporting for zpbs-backup (Pushgateway push and textfile)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backup import BackupSummary

STATE_FILE = Path("/var/lib/zpbs-backup/state.json")


@dataclass
class MetricsConfig:
    """Where run metrics are reported, if anywhere.

    Kept separate from NotificationConfig on purpose: metrics and email are
    independent knobs, and they were not once, which meant ZPBS_NOTIFY=false
    silently turned off Prometheus reporting too.
    """

    # Prometheus Pushgateway base URL (e.g. http://10.0.16.16:9091)
    pushgateway_url: str | None = None
    # node_exporter textfile collector directory
    # (e.g. /var/lib/node_exporter/textfile_collector)
    textfile_dir: str | None = None


def get_metrics_config() -> MetricsConfig:
    """Load metrics configuration from the environment.

    Both transports are unset by default, making metrics reporting a no-op.
    """
    return MetricsConfig(
        pushgateway_url=os.environ.get("ZPBS_PUSHGATEWAY") or None,
        textfile_dir=os.environ.get("ZPBS_TEXTFILE_DIR") or None,
    )


def report_metrics(
    summary: BackupSummary,
    hostname: str,
    config: MetricsConfig | None = None,
) -> None:
    """Report run metrics through every configured transport.

    Best-effort throughout: a metrics failure logs and never raises, because
    it must not fail a backup run that otherwise succeeded.

    Args:
        summary: Completed backup summary.
        hostname: Instance label value (short hostname).
        config: Optional metrics config (loaded from the environment if not
            provided).
    """
    if config is None:
        config = get_metrics_config()

    push_to_gateway(summary, hostname, config.pushgateway_url)
    write_textfile(summary, config.textfile_dir)


def _read_last_success() -> float | None:
    """Read persisted last-success timestamp from state file."""
    try:
        data = json.loads(STATE_FILE.read_text())
        return float(data["last_success_timestamp_seconds"])
    except Exception:
        return None


def _write_last_success(ts: float) -> None:
    """Persist last-success timestamp to state file."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"last_success_timestamp_seconds": ts}))
    except Exception as exc:
        print(f"zpbs-backup metrics: could not write state file: {exc}", file=sys.stderr)


def _render_metrics(summary: BackupSummary) -> bytes:
    """Render the zpbs_backup metrics in Prometheus text exposition format.

    Shared by both the Pushgateway and textfile transports so their output
    (metric names, help/type lines, values) is identical.

    zpbs_backup_datasets_skipped stays an unlabelled total so existing
    queries and alert rules keep working; the per-cause breakdown is a
    separate series rather than a label added to that name.
    """
    run_end_ts = time.time()

    # Duration: prefer actual measured value, fall back to wall-clock estimate.
    duration = summary.duration_seconds if summary.duration_seconds is not None else 0.0

    # Determine last-success timestamp.
    if summary.failed == 0 and summary.successful > 0:
        # This run succeeded — persist and use current time.
        _write_last_success(run_end_ts)
        last_success_ts = run_end_ts
    else:
        # This run failed/partial — read persisted value without overwriting.
        last_success_ts = _read_last_success() or 0.0

    lines = [
        "# HELP zpbs_backup_last_run_timestamp_seconds "
        "Unix timestamp of most recent backup run end",
        "# TYPE zpbs_backup_last_run_timestamp_seconds gauge",
        f"zpbs_backup_last_run_timestamp_seconds {run_end_ts}",
        "# HELP zpbs_backup_last_success_timestamp_seconds "
        "Unix timestamp of last fully successful backup run",
        "# TYPE zpbs_backup_last_success_timestamp_seconds gauge",
        f"zpbs_backup_last_success_timestamp_seconds {last_success_ts}",
        "# HELP zpbs_backup_duration_seconds Duration of the backup run in seconds",
        "# TYPE zpbs_backup_duration_seconds gauge",
        f"zpbs_backup_duration_seconds {duration}",
        "# HELP zpbs_backup_datasets_successful Number of datasets backed up successfully",
        "# TYPE zpbs_backup_datasets_successful gauge",
        f"zpbs_backup_datasets_successful {summary.successful}",
        "# HELP zpbs_backup_datasets_failed Number of datasets that failed to back up",
        "# TYPE zpbs_backup_datasets_failed gauge",
        f"zpbs_backup_datasets_failed {summary.failed}",
        "# HELP zpbs_backup_datasets_skipped Number of datasets skipped",
        "# TYPE zpbs_backup_datasets_skipped gauge",
        f"zpbs_backup_datasets_skipped {summary.skipped}",
        "# HELP zpbs_backup_datasets_skipped_by_cause Number of datasets skipped, by cause",
        "# TYPE zpbs_backup_datasets_skipped_by_cause gauge",
    ]

    for cause, count in summary.skipped_by_cause.items():
        lines.append(
            f'zpbs_backup_datasets_skipped_by_cause{{cause="{cause.value}"}} {count}'
        )

    lines.append("")
    return "\n".join(lines).encode("utf-8")


def push_to_gateway(
    summary: BackupSummary,
    hostname: str,
    pushgateway_url: str | None,
) -> None:
    """Push backup metrics to a Prometheus Pushgateway.

    Args:
        summary: Completed backup summary.
        hostname: Instance label value (short hostname).
        pushgateway_url: Base URL of the Pushgateway (e.g. ``http://10.0.16.16:9091``).
            If ``None`` or empty, this function returns immediately (no-op).
    """
    if not pushgateway_url:
        return

    payload = _render_metrics(summary)
    url = f"{pushgateway_url.rstrip('/')}/metrics/job/zpbs_backup/instance/{hostname}"

    try:
        req = urllib.request.Request(
            url,
            data=payload,
            method="PUT",
            headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as exc:
        print(f"zpbs-backup metrics: push failed: {exc}", file=sys.stderr)


def write_textfile(
    summary: BackupSummary,
    textfile_dir: str | None,
) -> None:
    """Write backup metrics to a node_exporter textfile collector file.

    Writes the same six metrics as :func:`push_to_gateway` to
    ``<textfile_dir>/zpbs_backup.prom``. The write is atomic (temp file in the
    same directory, then ``os.replace``) so node_exporter never observes a
    partial file.

    Args:
        summary: Completed backup summary.
        textfile_dir: Directory scanned by node_exporter's textfile collector
            (e.g. ``/var/lib/node_exporter/textfile_collector``). If ``None``
            or empty, this function returns immediately (no-op). A missing or
            unwritable directory logs an error and never raises — a metrics
            failure must not fail the backup run.
    """
    if not textfile_dir:
        return

    dest_dir = Path(textfile_dir)
    dest = dest_dir / "zpbs_backup.prom"
    payload = _render_metrics(summary)

    try:
        fd, tmp_path = tempfile.mkstemp(dir=dest_dir, prefix=".zpbs_backup.", suffix=".tmp")
        try:
            # mkstemp creates the file 0600; node_exporter runs as its own
            # unprivileged user and must be able to read it.
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            os.replace(tmp_path, dest)
        except Exception:
            os.unlink(tmp_path)
            raise
    except Exception as exc:
        print(f"zpbs-backup metrics: textfile write failed: {exc}", file=sys.stderr)
