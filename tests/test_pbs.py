"""Tests for PBS client wrapper."""

import io
import subprocess
import sys
import unittest.mock as umock
from datetime import datetime, timedelta, timezone

import pytest

from zpbs_backup import pbs as pbs_mod
from zpbs_backup.config import PBSConfig
from zpbs_backup.pbs import (
    ERROR_TAIL_LINES,
    BackupGroup,
    BackupSnapshot,
    PBSClient,
    _parse_server_address,
    run_streaming,
)


class TestBackupSnapshot:
    """Tests for BackupSnapshot class."""

    def test_from_dict(self):
        data = {
            "backup-type": "host",
            "backup-id": "myhost-tank-data",
            "backup-time": 1704067200,  # 2024-01-01 00:00:00 UTC
            "size": 1024000,
        }
        snapshot = BackupSnapshot.from_dict(data)

        assert snapshot.backup_type == "host"
        assert snapshot.backup_id == "myhost-tank-data"
        assert snapshot.size == 1024000
        assert snapshot.timestamp is not None
        assert snapshot.timestamp.timestamp() == 1704067200

    def test_from_dict_minimal(self):
        data = {
            "backup-time": 1704067200,
        }
        snapshot = BackupSnapshot.from_dict(data)

        assert snapshot.backup_type == "host"
        assert snapshot.backup_id == ""
        assert snapshot.size is None

    def test_from_dict_missing_backup_time(self):
        data = {
            "backup-type": "host",
            "backup-id": "myhost-tank-data",
        }
        snapshot = BackupSnapshot.from_dict(data)
        assert snapshot.timestamp is None

    def test_from_dict_zero_backup_time(self):
        data = {
            "backup-type": "host",
            "backup-id": "myhost-tank-data",
            "backup-time": 0,
        }
        snapshot = BackupSnapshot.from_dict(data)
        assert snapshot.timestamp is None

    def test_from_dict_iso_string_timestamp(self):
        data = {
            "backup-type": "host",
            "backup-id": "myhost-tank-data",
            "backup-time": "2024-06-15T10:30:00",
        }
        snapshot = BackupSnapshot.from_dict(data)
        assert snapshot.timestamp is not None
        assert snapshot.timestamp.year == 2024
        assert snapshot.timestamp.month == 6
        assert snapshot.timestamp.day == 15

    def test_from_dict_invalid_string_timestamp(self):
        data = {
            "backup-type": "host",
            "backup-id": "myhost-tank-data",
            "backup-time": "not-a-date",
        }
        snapshot = BackupSnapshot.from_dict(data)
        assert snapshot.timestamp is None

    def test_from_dict_epoch_before_2000_treated_as_none(self):
        data = {
            "backup-type": "host",
            "backup-id": "myhost-tank-data",
            "backup-time": 100,  # 1970-01-01 00:01:40
        }
        snapshot = BackupSnapshot.from_dict(data)
        assert snapshot.timestamp is None

    def test_from_dict_last_backup_field(self):
        """PBS list returns groups with 'last-backup' instead of 'backup-time'."""
        data = {
            "backup-type": "host",
            "backup-id": "myhost-tank-data",
            "last-backup": 1704067200,  # 2024-01-01 00:00:00 UTC
        }
        snapshot = BackupSnapshot.from_dict(data)
        assert snapshot.timestamp is not None
        assert snapshot.timestamp.timestamp() == 1704067200

    def test_from_dict_backup_time_preferred_over_last_backup(self):
        """backup-time takes precedence when both fields are present."""
        data = {
            "backup-type": "host",
            "backup-id": "myhost-tank-data",
            "backup-time": 1704067200,  # 2024-01-01
            "last-backup": 1706745600,  # 2024-02-01
        }
        snapshot = BackupSnapshot.from_dict(data)
        assert snapshot.timestamp is not None
        assert snapshot.timestamp.timestamp() == 1704067200  # backup-time wins


class TestBackupGroup:
    """Tests for BackupGroup class."""

    def test_defaults(self):
        group = BackupGroup(backup_type="host", backup_id="myhost-tank")
        assert group.last_backup is None
        assert group.snapshot_count == 0


class TestPBSClientBackup:
    """Tests for PBSClient.backup() method."""

    def test_backup_with_change_detection_mode(self):
        """Verify change-detection-mode flag is included in backup command."""
        from zpbs_backup.config import PBSConfig
        from zpbs_backup.pbs import PBSClient

        config = PBSConfig(
            repository="user@realm!tokenname@server:datastore",
            password="test-token",
        )
        client = PBSClient(config)

        # Test with metadata mode
        result = client.backup(
            backup_id="test-backup",
            source_path="/test/path",
            change_detection_mode="metadata",
            dry_run=True,
        )

        assert "--change-detection-mode" in result.stdout or result.returncode == 0

    def test_backup_without_change_detection_mode(self):
        """Verify backup works without change-detection-mode (backward compatibility)."""
        from zpbs_backup.config import PBSConfig
        from zpbs_backup.pbs import PBSClient

        config = PBSConfig(
            repository="user@realm!tokenname@server:datastore",
            password="test-token",
        )
        client = PBSClient(config)

        # Test without change detection mode (should not include the flag)
        result = client.backup(
            backup_id="test-backup",
            source_path="/test/path",
            dry_run=True,
        )

        assert result.returncode == 0

    @pytest.mark.parametrize("with_encryption", [False, True])
    def test_backup_passes_right_arguments_to_backup_client(self, with_encryption, monkeypatch):
        from zpbs_backup.config import PBSConfig
        from zpbs_backup.pbs import PBSClient

        config = PBSConfig(
            repository="user@realm!tokenname@server:datastore",
            password="test-token",
            keyfile="/path/to/key.enc" if with_encryption else None
        )
        client = PBSClient(config)

        monkeypatch.setattr("os.environ", {"USERS_ENV_VAR1": "VALUE1", "USERS_ENV_VAR2": "VALUE2"})

        with umock.patch.object(pbs_mod, "run_streaming") as streamed:
            client.backup(
                backup_id="test-backup",
                source_path="/test/path",
            )
            assert streamed.call_args == umock.call(
                [
                    "proxmox-backup-client",
                    "backup",
                    "root.pxar:/test/path",
                    "--backup-id",
                    "test-backup",
                ] + (["--keyfile", "/path/to/key.enc"] if with_encryption else []),
                env={
                    "PBS_REPOSITORY": "user@realm!tokenname@server:datastore",
                    "PBS_PASSWORD": "test-token",
                    "USERS_ENV_VAR1": "VALUE1",
                    "USERS_ENV_VAR2": "VALUE2",
                },
                output=client.output,
                timeout=None,
            )


class TestParseServerAddress:
    """Tests for _parse_server_address."""

    def test_host_only_defaults_to_8007(self):
        assert _parse_server_address("pbs.example.com") == ("pbs.example.com", 8007)

    def test_host_and_port(self):
        assert _parse_server_address("pbs.example.com:8443") == ("pbs.example.com", 8443)

    def test_invalid_port_falls_back_to_default(self):
        assert _parse_server_address("pbs.example.com:notaport") == (
            "pbs.example.com:notaport",
            8007,
        )


class TestClockSkew:
    """Tests for clock skew probe."""

    def _client(self) -> PBSClient:
        return PBSClient(
            PBSConfig(repository="u@p!t@pbs.example.com:store", server="pbs.example.com")
        )

    def test_skew_zero_when_server_matches_local(self):
        now = datetime.now(timezone.utc)
        with umock.patch.object(PBSClient, "get_server_time", return_value=now):
            skew = self._client().get_clock_skew_seconds()
        assert skew is not None
        assert abs(skew) < 1.0

    def test_skew_positive_when_server_ahead(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        with umock.patch.object(PBSClient, "get_server_time", return_value=future):
            skew = self._client().get_clock_skew_seconds()
        assert skew is not None
        assert 119 < skew < 121

    def test_skew_negative_when_server_behind(self):
        past = datetime.now(timezone.utc) - timedelta(seconds=90)
        with umock.patch.object(PBSClient, "get_server_time", return_value=past):
            skew = self._client().get_clock_skew_seconds()
        assert skew is not None
        assert -91 < skew < -89

    def test_skew_none_when_probe_fails(self):
        with umock.patch.object(PBSClient, "get_server_time", return_value=None):
            assert self._client().get_clock_skew_seconds() is None

    def test_get_server_time_returns_none_when_no_server_configured(self):
        client = PBSClient(PBSConfig(repository=""))
        assert client.get_server_time() is None


class TestRunStreaming:
    """Tests for run_streaming."""

    def test_echoes_output_live_and_retains_it(self):
        """The child's output reaches the sink and the retained tail."""
        sink = io.StringIO()
        result = run_streaming(
            [
                sys.executable,
                "-c",
                "import sys; print('starting'); "
                "print('Error: unable to open chunk store', file=sys.stderr); "
                "sys.exit(3)",
            ],
            output=sink,
        )

        assert result.returncode == 3
        assert "starting" in sink.getvalue()
        assert "unable to open chunk store" in sink.getvalue()
        assert "unable to open chunk store" in result.stdout

    def test_tail_is_bounded(self):
        """A long run keeps only the last ERROR_TAIL_LINES lines."""
        total = ERROR_TAIL_LINES * 3
        sink = io.StringIO()
        result = run_streaming(
            [sys.executable, "-c", f"[print(i) for i in range({total})]"],
            output=sink,
        )

        lines = result.stdout.splitlines()
        assert result.returncode == 0
        assert len(lines) == ERROR_TAIL_LINES
        assert lines[-1] == str(total - 1)

    def test_carriage_return_progress_is_echoed_but_kept_out_of_the_tail(self):
        """Progress redrawn with \\r is transient, so only the real line is kept."""
        sink = io.StringIO()
        result = run_streaming(
            [
                sys.executable,
                "-c",
                r"import sys; sys.stdout.write('10%\r50%\rfailed: broken pipe\n')",
            ],
            output=sink,
        )

        assert "50%" in sink.getvalue()
        assert result.stdout == "failed: broken pipe"

    def test_progress_is_kept_when_it_is_all_there_was(self):
        """A command that only ever redrew progress still explains itself."""
        sink = io.StringIO()
        result = run_streaming(
            [sys.executable, "-c", r"import sys; sys.stdout.write('stalled at 10%\r')"],
            output=sink,
        )

        assert result.stdout == "stalled at 10%"

    def test_backup_streams_and_returns_the_tail(self):
        """PBSClient.backup routes through run_streaming with its own sink."""
        sink = io.StringIO()
        client = PBSClient(PBSConfig(repository="u@p!t@srv:store", server="srv"), output=sink)
        completed = subprocess.CompletedProcess(args=[], returncode=2, stdout="boom", stderr="")

        with umock.patch.object(pbs_mod, "run_streaming", return_value=completed) as streamed:
            result = client.backup(backup_id="host-tank-data", source_path="/tank/data")

        assert result.stdout == "boom"
        assert streamed.call_args.kwargs["output"] is sink
        assert streamed.call_args.args[0][0] == "proxmox-backup-client"
