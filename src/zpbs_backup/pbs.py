"""Proxmox Backup Server client wrapper."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .config import PBSConfig


@dataclass
class BackupSnapshot:
    """A backup snapshot in PBS."""

    backup_type: str  # 'host', 'vm', 'ct'
    backup_id: str
    timestamp: Optional[datetime] = None
    size: int | None = None

    @classmethod
    def from_dict(cls, data: dict) -> BackupSnapshot:
        """Create from PBS JSON output."""
        backup_time = data.get("backup-time")
        timestamp: Optional[datetime] = None

        if backup_time:
            if isinstance(backup_time, (int, float)):
                timestamp = datetime.fromtimestamp(backup_time)
            elif isinstance(backup_time, str):
                try:
                    timestamp = datetime.fromisoformat(backup_time)
                except ValueError:
                    timestamp = None

        # Sanity check: discard timestamps before year 2000 (likely epoch junk)
        if timestamp is not None and timestamp.year < 2000:
            timestamp = None

        return cls(
            backup_type=data.get("backup-type", "host"),
            backup_id=data.get("backup-id", ""),
            timestamp=timestamp,
            size=data.get("size"),
        )


@dataclass
class BackupGroup:
    """A backup group (all snapshots with same type/id)."""

    backup_type: str
    backup_id: str
    last_backup: datetime | None = None
    snapshot_count: int = 0


class PBSClient:
    """Wrapper for proxmox-backup-client commands."""

    def __init__(self, config: PBSConfig):
        self.config = config
        self._env: dict[str, str] | None = None

    def _get_env(self) -> dict[str, str]:
        """Get environment variables for PBS commands."""
        if self._env is None:
            self._env = os.environ.copy()
            self._env.update(self.config.get_env())
        return self._env

    def _run(
        self,
        args: list[str],
        check: bool = True,
        capture_output: bool = True,
        timeout: int | None = 30,
    ) -> subprocess.CompletedProcess:
        """Run a proxmox-backup-client command."""
        cmd = ["proxmox-backup-client"] + args
        return subprocess.run(
            cmd,
            env=self._get_env(),
            capture_output=capture_output,
            text=True,
            check=check,
            timeout=timeout,
        )

    def check_connection(self) -> None:
        """Verify PBS server is reachable and credentials are valid.

        Raises:
            ConnectionError: If the server is unreachable or auth fails
        """
        try:
            result = self._run(
                ["list", "--output-format", "json"],
                check=False,
                timeout=10,
            )
        except FileNotFoundError:
            raise ConnectionError(
                "proxmox-backup-client not found. Install the Proxmox Backup client package."
            )
        except subprocess.TimeoutExpired:
            raise ConnectionError(
                "Timed out connecting to PBS server. Check PBS_REPOSITORY and network."
            )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            msg = f"PBS connection failed: {stderr}"
            if "permission check failed" in stderr.lower():
                msg += (
                    "\n\n"
                    "Hint: PBS API tokens use privilege separation by default.\n"
                    "The token's effective permissions are the INTERSECTION of\n"
                    "the user's and the token's permissions. Both the user AND\n"
                    "the token need permissions on the datastore.\n"
                    "\n"
                    "Fix: in the PBS web UI, go to Configuration > Access Control >\n"
                    "Permissions > Add, and grant the user (e.g. zpbs-test@pbs)\n"
                    "at least DatastoreAudit on the datastore path (e.g. /datastore/mystore)."
                )
            raise ConnectionError(msg)

    def list_snapshots(self, namespace: str | None = None) -> list[BackupSnapshot]:
        """List all backup snapshots.

        Args:
            namespace: Optional namespace to list from

        Returns:
            List of BackupSnapshot objects
        """
        args = ["list", "--output-format", "json"]
        if namespace:
            args.extend(["--ns", namespace])

        try:
            result = self._run(args, check=False)
        except subprocess.TimeoutExpired:
            return []
        if result.returncode != 0:
            # Namespace might not exist yet
            return []

        try:
            data = json.loads(result.stdout)
            return [BackupSnapshot.from_dict(item) for item in data]
        except json.JSONDecodeError:
            return []

    def list_groups(self, namespace: str | None = None) -> list[BackupGroup]:
        """List all backup groups.

        Args:
            namespace: Optional namespace to list from

        Returns:
            List of BackupGroup objects
        """
        snapshots = self.list_snapshots(namespace)

        # Group by backup_type and backup_id
        groups: dict[tuple[str, str], BackupGroup] = {}

        for snapshot in snapshots:
            key = (snapshot.backup_type, snapshot.backup_id)
            if key not in groups:
                groups[key] = BackupGroup(
                    backup_type=snapshot.backup_type,
                    backup_id=snapshot.backup_id,
                )

            group = groups[key]
            group.snapshot_count += 1
            if snapshot.timestamp is not None:
                if group.last_backup is None or snapshot.timestamp > group.last_backup:
                    group.last_backup = snapshot.timestamp

        return list(groups.values())

    def get_last_backup_time(
        self, backup_id: str, namespace: str | None = None
    ) -> datetime | None:
        """Get the timestamp of the most recent backup for a given ID.

        Args:
            backup_id: The backup ID to look up
            namespace: Optional namespace

        Returns:
            datetime of last backup, or None if never backed up
        """
        snapshots = self.list_snapshots(namespace)

        matching = [
            s for s in snapshots
            if s.backup_id == backup_id and s.timestamp is not None
        ]
        if not matching:
            return None

        return max(s.timestamp for s in matching)

    def backup(
        self,
        backup_id: str,
        source_path: str,
        archive_name: str = "root.pxar",
        namespace: str | None = None,
        dry_run: bool = False,
    ) -> subprocess.CompletedProcess:
        """Run a backup.

        Args:
            backup_id: The backup ID (e.g., 'storage-server-tank-files')
            source_path: The path to back up (e.g., '/tank/files')
            archive_name: Name of the archive (default: root.pxar)
            namespace: Optional namespace
            dry_run: If True, don't actually run the backup

        Returns:
            CompletedProcess from the backup command
        """
        args = [
            "backup",
            f"{archive_name}:{source_path}",
            "--backup-id",
            backup_id,
        ]

        if namespace:
            args.extend(["--ns", namespace])

        if dry_run:
            # Return a fake successful result for dry-run
            return subprocess.CompletedProcess(
                args=["proxmox-backup-client"] + args,
                returncode=0,
                stdout=f"[DRY-RUN] Would backup {source_path} as {backup_id}",
                stderr="",
            )

        # No timeout for backups — they can run for hours
        return self._run(args, check=False, capture_output=False, timeout=None)

    def create_namespace(self, namespace: str) -> bool:
        """Create a namespace if it doesn't exist.

        Creates parent namespaces as needed for nested paths like
        'storage-server/files-fast/orgs'.

        Args:
            namespace: The namespace to create (e.g., 'storage-server/tank')

        Returns:
            True if created or already exists, False on error
        """
        # Create each level of the namespace hierarchy
        parts = namespace.split("/")
        for i in range(1, len(parts) + 1):
            partial_ns = "/".join(parts[:i])
            result = self._run(["namespace", "create", partial_ns], check=False)
            # Continue even if it already exists
            if result.returncode != 0 and "already exists" not in result.stderr.lower():
                # Real error - but only fail on the final namespace
                if i == len(parts):
                    return False

        return True

    def prune(
        self,
        backup_type: str,
        backup_id: str,
        keep_daily: int | None = None,
        keep_weekly: int | None = None,
        keep_monthly: int | None = None,
        keep_yearly: int | None = None,
        namespace: str | None = None,
        dry_run: bool = False,
    ) -> subprocess.CompletedProcess:
        """Prune old backups according to retention policy.

        Args:
            backup_type: The backup type (e.g., 'host')
            backup_id: The backup ID
            keep_daily: Number of daily backups to keep
            keep_weekly: Number of weekly backups to keep
            keep_monthly: Number of monthly backups to keep
            keep_yearly: Number of yearly backups to keep
            namespace: Optional namespace
            dry_run: If True, show what would be pruned without doing it

        Returns:
            CompletedProcess from the prune command
        """
        args = ["prune", f"{backup_type}/{backup_id}"]

        if namespace:
            args.extend(["--ns", namespace])

        if keep_daily is not None:
            args.extend(["--keep-daily", str(keep_daily)])
        if keep_weekly is not None:
            args.extend(["--keep-weekly", str(keep_weekly)])
        if keep_monthly is not None:
            args.extend(["--keep-monthly", str(keep_monthly)])
        if keep_yearly is not None:
            args.extend(["--keep-yearly", str(keep_yearly)])

        if dry_run:
            args.append("--dry-run")

        return self._run(args, check=False, capture_output=False, timeout=None)

    def list_all_namespaces(self) -> list[str]:
        """List all namespaces in the repository.

        Returns:
            List of namespace paths
        """
        try:
            result = self._run(["namespace", "list", "--output-format", "json"], check=False)
        except subprocess.TimeoutExpired:
            return []
        if result.returncode != 0:
            return []

        try:
            data = json.loads(result.stdout)
            # PBS may return [{"ns": "path"}, ...] or ["path", ...]
            namespaces = []
            for item in data:
                if isinstance(item, dict):
                    ns = item.get("ns", "")
                elif isinstance(item, str):
                    ns = item
                else:
                    continue
                if ns:
                    namespaces.append(ns)
            return namespaces
        except json.JSONDecodeError:
            return []

    def list_all_backup_groups(self) -> list[tuple[str | None, BackupGroup]]:
        """List all backup groups across all namespaces.

        Returns:
            List of (namespace, BackupGroup) tuples
        """
        result: list[tuple[str | None, BackupGroup]] = []

        # Get groups from root namespace
        for group in self.list_groups():
            result.append((None, group))

        # Get groups from all sub-namespaces
        for ns in self.list_all_namespaces():
            for group in self.list_groups(ns):
                result.append((ns, group))

        return result
