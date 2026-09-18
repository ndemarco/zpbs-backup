"""Prometheus metrics reporting for zpbs-backup (Pushgateway push and textfile)."""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backup import BackupSummary

STATE_FILE = Path("/var/lib/zpbs-backup/state.json")


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
    """Render the six zpbs_backup metrics in Prometheus text exposition format.

    Shared by both the Pushgateway and textfile transports so their output
    (metric names, help/type lines, values) is identical.
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
        "# HELP zpbs_backup_last_run_timestamp_seconds Unix timestamp of most recent backup run end",
        "# TYPE zpbs_backup_last_run_timestamp_seconds gauge",
        f"zpbs_backup_last_run_timestamp_seconds {run_end_ts}",
        "# HELP zpbs_backup_last_success_timestamp_seconds Unix timestamp of last fully successful backup run",
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
        "",
    ]
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
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            os.replace(tmp_path, dest)
        except Exception:
            os.unlink(tmp_path)
            raise
    except Exception as exc:
        print(f"zpbs-backup metrics: textfile write failed: {exc}", file=sys.stderr)
