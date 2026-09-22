"""Backup orchestration logic."""

from __future__ import annotations

import fnmatch
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import NamedTuple, TextIO

from .config import PBSConfig, get_hostname
from .pbs import PBSClient
from .retention import DEFAULT_RETENTION, RetentionPolicy, parse_retention
from .scheduler import format_last_backup, is_backup_due
from .zfs import (
    PROP_RETENTION,
    Dataset,
    InvalidPropertyError,
    discover_datasets,
    get_latest_snapshot_creation,
    get_written_bytes,
)

SKEW_MARGIN_SECONDS = 60


def failure_message(result: subprocess.CompletedProcess[str]) -> str:
    """Describe why a proxmox-backup-client command failed.

    Prefers what the client itself said. A CompletedProcess always has a
    stderr attribute, so testing for one tells you nothing about whether it
    holds text — the value is None whenever output was not captured. When
    the client printed nothing at all, fall back to the exit status, which
    is still more use than an empty string.

    Args:
        result: The CompletedProcess of the failed command

    Returns:
        A non-empty, human-readable error message
    """
    for stream in (result.stderr, result.stdout):
        if stream and stream.strip():
            return stream.strip()

    return f"proxmox-backup-client exited {result.returncode} with no output"


class SkipCause(Enum):
    """Why a dataset was not backed up on this run.

    A single skipped count cannot tell a dataset that merely is not due yet
    from one that has silently lost its mountpoint. The label value is what
    reaches Prometheus, so it is part of the metric contract — rename one
    and the dashboards and alert rules reading it go blind.
    """

    NOT_DUE = "not_due"
    UNCHANGED = "unchanged"
    NO_MOUNTPOINT = "no_mountpoint"
    NOT_MOUNTED = "not_mounted"


@dataclass
class BackupResult:
    """Result of a single backup operation."""

    dataset: Dataset
    success: bool
    skipped: bool = False
    skip_reason: str | None = None
    skip_cause: SkipCause | None = None
    error: str | None = None
    duration_seconds: float = 0.0


class PlannedDataset(NamedTuple):
    """One dataset's place in a run: back it up, or skip it and why."""

    dataset: Dataset
    should_backup: bool
    skip_reason: str | None = None
    skip_cause: SkipCause | None = None


@dataclass
class BackupSummary:
    """Summary of a backup run."""

    results: list[BackupResult] = field(default_factory=list)
    start_time: datetime = field(default_factory=datetime.now)
    end_time: datetime | None = None

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def successful(self) -> int:
        return sum(1 for r in self.results if r.success and not r.skipped)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.success and not r.skipped)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.skipped)

    @property
    def skipped_by_cause(self) -> dict[SkipCause, int]:
        """Count skipped datasets per cause, every cause always present.

        Causes with no datasets still report zero: a series that vanishes
        when its count drops to nothing goes stale in Prometheus rather
        than reading zero, which is the opposite of what an alert needs.
        """
        counts = {cause: 0 for cause in SkipCause}
        for result in self.results:
            if result.skipped and result.skip_cause is not None:
                counts[result.skip_cause] += 1
        return counts

    @property
    def duration_seconds(self) -> float:
        if self.end_time is None:
            return 0.0
        return (self.end_time - self.start_time).total_seconds()


class BackupOrchestrator:
    """Orchestrates backup operations for discovered datasets."""

    def __init__(
        self,
        config: PBSConfig,
        dry_run: bool = False,
        force: bool = False,
        change_detection_mode: str | None = None,
        output: TextIO | None = None,
    ):
        self.config = config
        self.output = output or sys.stdout
        self.client = PBSClient(config, output=self.output)
        self.hostname = get_hostname()
        self.dry_run = dry_run
        self.force = force
        self.change_detection_mode = change_detection_mode
        self._progress_callback: Callable[[str], None] | None = None
        self.skip_unchanged_safe: bool = True

    def set_progress_callback(self, callback: Callable[[str], None]) -> None:
        """Set a callback for progress updates."""
        self._progress_callback = callback

    def _log(self, message: str) -> None:
        """Log a message to output."""
        print(message, file=self.output)
        if self._progress_callback:
            self._progress_callback(message)

    def discover(self, pattern: str | None = None) -> list[Dataset]:
        """Discover datasets to back up.

        Args:
            pattern: Optional glob pattern to filter datasets

        Returns:
            List of datasets sorted by priority
        """
        datasets = discover_datasets()

        if pattern:
            datasets = [ds for ds in datasets if fnmatch.fnmatch(ds.name, pattern)]

        return datasets

    def _reject_misconfigured(
        self, datasets: list[Dataset], summary: BackupSummary
    ) -> list[Dataset]:
        """Drop datasets whose zpbs properties cannot be acted on.

        Each one is recorded as a failure naming the dataset and the
        property, so it reaches the run output, syslog and the notification
        email. The remaining datasets still run: one typo must not cost a
        night of backups.

        Args:
            datasets: The discovered datasets
            summary: The run summary, extended with a failure per rejection

        Returns:
            The datasets safe to proceed with
        """
        usable = []

        for dataset in datasets:
            errors = dataset.property_errors()
            if not errors:
                usable.append(dataset)
                continue

            message = "; ".join(str(e) for e in errors)
            self._log(f"Skipping {dataset.name}: {message}")
            summary.results.append(
                BackupResult(dataset=dataset, success=False, error=message)
            )

        return usable

    def plan(self, datasets: list[Dataset]) -> list[PlannedDataset]:
        """Plan which datasets need to be backed up.

        Args:
            datasets: List of datasets to consider

        Returns:
            One PlannedDataset per dataset, carrying the skip cause when it
            is not being backed up
        """
        plan = []

        for ds in datasets:
            if self.force:
                plan.append(PlannedDataset(ds, True))
                continue

            backup_id = ds.get_backup_id(self.hostname, self.config.backup_id_separator)
            namespace = ds.namespace or ds.get_auto_namespace(self.hostname)
            last_backup = self.client.get_last_backup_time(backup_id, namespace)

            if not is_backup_due(ds.schedule, last_backup):
                reason = f"not due (last: {format_last_backup(last_backup)})"
                plan.append(PlannedDataset(ds, False, reason, SkipCause.NOT_DUE))
                continue

            unchanged_reason = self._unchanged_skip_reason(ds, last_backup)
            if unchanged_reason is not None:
                plan.append(
                    PlannedDataset(ds, False, unchanged_reason, SkipCause.UNCHANGED)
                )
                continue

            plan.append(PlannedDataset(ds, True))

        return plan

    def _unchanged_skip_reason(
        self, ds: Dataset, last_backup: datetime | None
    ) -> str | None:
        """Return a skip reason if the dataset is provably unchanged since last backup.

        Skip when:
          - clock skew with PBS is within the safety margin (set in run()),
          - a previous successful backup exists,
          - `zfs get written` is 0 (live FS identical to most recent snapshot),
          - the most recent snapshot was created at least SKEW_MARGIN_SECONDS
            before the last backup (so it must have been captured by that backup).
        """
        if not self.skip_unchanged_safe:
            return None
        if last_backup is None:
            return None
        written = get_written_bytes(ds.name)
        snap_creation = get_latest_snapshot_creation(ds.name)
        if written != 0 or snap_creation is None:
            return None
        if snap_creation + SKEW_MARGIN_SECONDS > last_backup.timestamp():
            return None
        snap_str = datetime.fromtimestamp(snap_creation).strftime("%Y-%m-%d %H:%M")
        return f"unchanged (written=0, latest snap @ {snap_str})"

    def _preflight_skew_check(self) -> None:
        """Measure clock skew against PBS and decide if skip-unchanged is safe.

        On error or excessive skew, disables skip-unchanged for this run and falls
        back to schedule-only behavior. Never blocks the run.
        """
        if self.force:
            return
        skew = self.client.get_clock_skew_seconds()
        if skew is None:
            self._log(
                "Warning: could not measure clock skew with PBS; "
                "disabling skip-unchanged optimization for this run"
            )
            self.skip_unchanged_safe = False
            return
        abs_skew = abs(skew)
        direction = "ahead of" if skew > 0 else "behind"
        if abs_skew < 5:
            return
        if abs_skew < SKEW_MARGIN_SECONDS:
            self._log(
                f"Clock skew: PBS is {abs_skew:.1f}s {direction} local "
                f"(within {SKEW_MARGIN_SECONDS}s safety margin)"
            )
            return
        self._log(
            f"Warning: clock skew of {abs_skew:.1f}s ({direction} local) exceeds "
            f"{SKEW_MARGIN_SECONDS}s safety margin — disabling skip-unchanged "
            f"optimization for this run. Check NTP on both hosts."
        )
        self.skip_unchanged_safe = False

    def backup_dataset(self, dataset: Dataset) -> BackupResult:
        """Back up a single dataset.

        Args:
            dataset: The dataset to back up

        Returns:
            BackupResult with outcome
        """
        start_time = datetime.now()
        backup_id = dataset.get_backup_id(self.hostname, self.config.backup_id_separator)
        namespace = dataset.namespace or dataset.get_auto_namespace(self.hostname)
        mountpoint = dataset.mountpoint

        self._log(f"Backing up {dataset.name} -> {backup_id}")

        if not mountpoint:
            self._log("  Skipped: no mountpoint (mountpoint=none or legacy)")
            return BackupResult(
                dataset=dataset,
                success=True,
                skipped=True,
                skip_reason="no mountpoint",
                skip_cause=SkipCause.NO_MOUNTPOINT,
            )

        if not dataset.mounted:
            reason = "not mounted"
            if not dataset.canmount:
                reason += " (canmount=off)"
            self._log(f"  Skipped: {reason}")
            return BackupResult(
                dataset=dataset,
                success=True,
                skipped=True,
                skip_reason=reason,
                skip_cause=SkipCause.NOT_MOUNTED,
            )

        if self.dry_run:
            self._log(f"  [DRY-RUN] Would backup {mountpoint}")
            return BackupResult(
                dataset=dataset,
                success=True,
                skipped=False,
                duration_seconds=0.0,
            )

        # Ensure namespace exists
        if namespace:
            self.client.create_namespace(namespace)

        # Run backup
        result = self.client.backup(
            backup_id=backup_id,
            source_path=mountpoint,
            namespace=namespace,
            change_detection_mode=self.change_detection_mode,
            dry_run=self.dry_run,
        )

        duration = (datetime.now() - start_time).total_seconds()

        if result.returncode == 0:
            self._log(f"  Completed in {duration:.1f}s")
            return BackupResult(
                dataset=dataset,
                success=True,
                duration_seconds=duration,
            )
        else:
            error = failure_message(result)
            self._log(f"  FAILED: {error}")
            return BackupResult(
                dataset=dataset,
                success=False,
                error=error,
                duration_seconds=duration,
            )

    def run(self, pattern: str | None = None) -> BackupSummary:
        """Run backups for all discovered datasets.

        Args:
            pattern: Optional glob pattern to filter datasets

        Returns:
            BackupSummary with results
        """
        summary = BackupSummary(start_time=datetime.now())

        datasets = self.discover(pattern)
        if not datasets:
            self._log("No datasets found with zpbs:backup=true")
            summary.end_time = datetime.now()
            return summary

        datasets = self._reject_misconfigured(datasets, summary)

        self._preflight_skew_check()

        plan = self.plan(datasets)

        self._log(f"Found {len(datasets)} dataset(s) to process")
        if self.dry_run:
            self._log("[DRY-RUN MODE]")

        for dataset, should_backup, skip_reason, skip_cause in plan:
            if not should_backup:
                self._log(f"Skipping {dataset.name}: {skip_reason}")
                summary.results.append(
                    BackupResult(
                        dataset=dataset,
                        success=True,
                        skipped=True,
                        skip_reason=skip_reason,
                        skip_cause=skip_cause,
                    )
                )
            else:
                result = self.backup_dataset(dataset)
                summary.results.append(result)

        summary.end_time = datetime.now()

        # Log summary
        self._log("")
        self._log(
            f"Backup complete: {summary.successful} succeeded, "
            f"{summary.failed} failed, {summary.skipped} skipped"
        )
        self._log(f"Total duration: {summary.duration_seconds:.1f}s")

        return summary


def get_retention_policy(dataset: Dataset) -> RetentionPolicy:
    """Get the retention policy for a dataset.

    An unset zpbs:retention takes the built-in default. A value that is set
    but unparseable is an error: pruning deletes snapshots, so falling back
    to a default here would apply a policy nobody chose.

    Args:
        dataset: The dataset

    Returns:
        RetentionPolicy for this dataset

    Raises:
        InvalidPropertyError: If zpbs:retention is set but cannot be parsed
    """
    if dataset.retention:
        try:
            return parse_retention(dataset.retention)
        except ValueError as e:
            raise InvalidPropertyError(
                dataset.name,
                PROP_RETENTION,
                dataset.retention,
                f"counts with d/w/m/y suffixes such as 7d,4w,6m,1y ({e})",
            ) from None
    return DEFAULT_RETENTION


class PruneOrchestrator:
    """Orchestrates prune operations."""

    def __init__(
        self,
        config: PBSConfig,
        dry_run: bool = False,
        output: TextIO | None = None,
    ):
        self.config = config
        self.output = output or sys.stdout
        self.client = PBSClient(config, output=self.output)
        self.hostname = get_hostname()
        self.dry_run = dry_run

    def _log(self, message: str) -> None:
        print(message, file=self.output)

    def prune_dataset(self, dataset: Dataset) -> bool:
        """Apply retention policy to a dataset's backups.

        Args:
            dataset: The dataset to prune

        Returns:
            True if successful
        """
        backup_id = dataset.get_backup_id(self.hostname, self.config.backup_id_separator)
        namespace = dataset.namespace or dataset.get_auto_namespace(self.hostname)

        try:
            policy = get_retention_policy(dataset)
        except InvalidPropertyError as e:
            self._log(f"Refusing to prune {dataset.name}: {e}")
            return False

        self._log(f"Pruning {dataset.name} ({backup_id})")

        result = self.client.prune(
            backup_type="host",
            backup_id=backup_id,
            keep_daily=policy.keep_daily,
            keep_weekly=policy.keep_weekly,
            keep_monthly=policy.keep_monthly,
            keep_yearly=policy.keep_yearly,
            namespace=namespace,
            dry_run=self.dry_run,
        )

        return result.returncode == 0

    def run(self, pattern: str | None = None) -> tuple[int, int]:
        """Prune all discovered datasets.

        Args:
            pattern: Optional glob pattern to filter datasets

        Returns:
            Tuple of (success_count, failure_count)
        """
        datasets = discover_datasets()

        if pattern:
            datasets = [ds for ds in datasets if fnmatch.fnmatch(ds.name, pattern)]

        if not datasets:
            self._log("No datasets found with zpbs:backup=true")
            return 0, 0

        self._log(f"Found {len(datasets)} dataset(s) to prune")
        if self.dry_run:
            self._log("[DRY-RUN MODE]")

        success = 0
        failed = 0

        for dataset in datasets:
            if self.prune_dataset(dataset):
                success += 1
            else:
                failed += 1

        self._log("")
        self._log(f"Prune complete: {success} succeeded, {failed} failed")

        return success, failed
