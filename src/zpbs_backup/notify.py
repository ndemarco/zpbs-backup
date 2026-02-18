"""Email and syslog notification support."""

from __future__ import annotations

import os
import shutil
import subprocess
import syslog
from dataclasses import dataclass
from datetime import datetime

from .backup import BackupSummary


@dataclass
class NotificationConfig:
    """Configuration for notifications."""

    enabled: bool = True
    recipient: str | None = None
    # Path to external notification script (for compatibility)
    external_script: str | None = None
    # Syslog notification (for centralized logging)
    syslog_enabled: bool = True
    # Prometheus Pushgateway base URL (e.g. http://10.0.16.16:9091)
    pushgateway_url: str | None = None


def get_notification_config() -> NotificationConfig:
    """Load notification configuration.

    Checks environment variables and common locations for config.
    """
    enabled = os.environ.get("ZPBS_NOTIFY", "true").lower() == "true"
    recipient = os.environ.get("ZPBS_NOTIFY_EMAIL")
    syslog_enabled = os.environ.get("ZPBS_SYSLOG", "true").lower() == "true"
    pushgateway_url = os.environ.get("ZPBS_PUSHGATEWAY") or None

    # Check for external notification script
    external_script = None
    script_paths = [
        "/usr/local/bin/pbs-send-notification",
        "/usr/local/bin/zpbs-send-notification",
    ]
    for path in script_paths:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            external_script = path
            break

    return NotificationConfig(
        enabled=enabled,
        recipient=recipient,
        external_script=external_script,
        syslog_enabled=syslog_enabled,
        pushgateway_url=pushgateway_url,
    )


def format_summary_for_email(summary: BackupSummary, hostname: str) -> tuple[str, str]:
    """Format a backup summary for email notification.

    Args:
        summary: The backup summary
        hostname: The hostname

    Returns:
        Tuple of (subject, body)
    """
    status = "SUCCESS" if summary.failed == 0 else "FAILURE"
    subject = f"[zpbs-backup] {hostname}: {status}"

    lines = [
        f"Backup Summary for {hostname}",
        "=" * 40,
        "",
        f"Start time: {summary.start_time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"End time:   {summary.end_time.strftime('%Y-%m-%d %H:%M:%S') if summary.end_time else 'N/A'}",
        f"Duration:   {summary.duration_seconds:.1f}s",
        "",
        "Results:",
        f"  Successful: {summary.successful}",
        f"  Failed:     {summary.failed}",
        f"  Skipped:    {summary.skipped}",
        "",
    ]

    if summary.failed > 0:
        lines.append("Failed datasets:")
        for result in summary.results:
            if not result.success and not result.skipped:
                lines.append(f"  - {result.dataset.name}: {result.error}")
        lines.append("")

    if summary.successful > 0:
        lines.append("Successful datasets:")
        for result in summary.results:
            if result.success and not result.skipped:
                lines.append(
                    f"  - {result.dataset.name} ({result.duration_seconds:.1f}s)"
                )
        lines.append("")

    if summary.skipped > 0:
        lines.append("Skipped datasets:")
        for result in summary.results:
            if result.skipped:
                lines.append(f"  - {result.dataset.name}: {result.skip_reason}")
        lines.append("")

    body = "\n".join(lines)
    return subject, body


def _send_to_syslog(summary: BackupSummary, hostname: str) -> bool:
    """Send backup summary to syslog for centralized logging.

    Logs a summary line plus individual dataset results.
    Uses LOG_INFO for success, LOG_ERR for failures.
    """
    try:
        syslog.openlog(ident="zpbs-backup", facility=syslog.LOG_LOCAL0)

        status = "SUCCESS" if summary.failed == 0 else "FAILURE"
        priority = syslog.LOG_INFO if summary.failed == 0 else syslog.LOG_ERR

        # Summary line
        syslog.syslog(
            priority,
            f"backup_complete host={hostname} status={status} "
            f"successful={summary.successful} failed={summary.failed} "
            f"skipped={summary.skipped} duration={summary.duration_seconds:.1f}s"
        )

        # Log failed datasets with error details
        for result in summary.results:
            if not result.success and not result.skipped:
                syslog.syslog(
                    syslog.LOG_ERR,
                    f"backup_failed dataset={result.dataset.name} error=\"{result.error}\""
                )

        # Log successful datasets
        for result in summary.results:
            if result.success and not result.skipped:
                syslog.syslog(
                    syslog.LOG_INFO,
                    f"backup_success dataset={result.dataset.name} "
                    f"duration={result.duration_seconds:.1f}s"
                )

        syslog.closelog()
        return True
    except Exception:
        return False


def send_notification(
    summary: BackupSummary,
    hostname: str,
    config: NotificationConfig | None = None,
) -> bool:
    """Send a notification about the backup result.

    Args:
        summary: The backup summary
        hostname: The hostname
        config: Optional notification config (loaded automatically if not provided)

    Returns:
        True if notification was sent successfully
    """
    if config is None:
        config = get_notification_config()

    if not config.enabled:
        return True

    success = True

    # Always send to syslog if enabled (for centralized logging)
    if config.syslog_enabled:
        _send_to_syslog(summary, hostname)

    # Push metrics to Pushgateway (best-effort, never raises)
    from .metrics import push_to_gateway
    push_to_gateway(summary, hostname, config.pushgateway_url)

    subject, body = format_summary_for_email(summary, hostname)

    # Try external script first (for compatibility with existing setups)
    if config.external_script:
        success = _send_via_external_script(config.external_script, subject, body, summary)
    # Fall back to sendmail/mail
    elif config.recipient:
        success = _send_via_mail(config.recipient, subject, body)

    return success


def _send_via_external_script(
    script: str, subject: str, body: str, summary: BackupSummary
) -> bool:
    """Send notification via external script.

    The script is called with environment variables providing context.
    """
    env = os.environ.copy()
    env.update(
        {
            "ZPBS_SUBJECT": subject,
            "ZPBS_SUCCESSFUL": str(summary.successful),
            "ZPBS_FAILED": str(summary.failed),
            "ZPBS_SKIPPED": str(summary.skipped),
            "ZPBS_DURATION": str(int(summary.duration_seconds)),
        }
    )

    try:
        result = subprocess.run(
            [script],
            input=body,
            text=True,
            env=env,
            capture_output=True,
            timeout=30,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _send_via_mail(recipient: str, subject: str, body: str) -> bool:
    """Send notification via sendmail or mail command."""
    # Try sendmail first
    sendmail = shutil.which("sendmail")
    if sendmail:
        message = f"To: {recipient}\nSubject: {subject}\n\n{body}"
        try:
            result = subprocess.run(
                [sendmail, "-t"],
                input=message,
                text=True,
                capture_output=True,
                timeout=30,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            pass

    # Fall back to mail command
    mail = shutil.which("mail")
    if mail:
        try:
            result = subprocess.run(
                [mail, "-s", subject, recipient],
                input=body,
                text=True,
                capture_output=True,
                timeout=30,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            pass

    return False
