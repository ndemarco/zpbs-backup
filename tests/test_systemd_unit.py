"""Tests that the packaged systemd unit can write what the tool writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from zpbs_backup.lock import EXIT_ALREADY_RUNNING, LOCK_PATH
from zpbs_backup.metrics import STATE_FILE

UNIT_PATH = Path(__file__).resolve().parent.parent / "systemd" / "zpbs-backup.service"
STATE_DIR = Path("/var/lib/zpbs-backup")


@pytest.fixture(scope="module")
def service() -> dict[str, list[str]]:
    """Parse the [Service] section.

    Hand-rolled rather than via configparser: systemd allows a key to repeat
    and takes all of them (ReadWritePaths does exactly that), while
    configparser keeps only the last.
    """
    values: dict[str, list[str]] = {}
    in_section = False

    for raw in UNIT_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_section = line == "[Service]"
            continue
        if in_section and "=" in line:
            key, _, value = line.partition("=")
            values.setdefault(key.strip(), []).append(value.strip())

    return values


class TestUnitCanWriteItsOutputs:
    """ProtectSystem=strict makes every writable path an explicit decision."""

    def test_hardening_is_still_in_force(self, service):
        """These tests only matter while the unit is actually locked down."""
        assert service["ProtectSystem"] == ["strict"]

    def test_the_state_directory_is_declared(self, service):
        """StateDirectory creates and unlocks /var/lib/zpbs-backup."""
        assert service["StateDirectory"] == ["zpbs-backup"]

    def test_the_state_file_lives_under_the_state_directory(self):
        assert STATE_FILE.parent == STATE_DIR

    def test_the_lock_lives_under_the_state_directory(self):
        assert LOCK_PATH.parent == STATE_DIR

    def test_the_default_textfile_collector_directory_is_writable(self, service):
        paths = service["ReadWritePaths"]
        assert "-/var/lib/node_exporter/textfile_collector" in paths

    def test_zfs_stays_writable(self, service):
        assert "/dev/zfs" in service["ReadWritePaths"]


class TestUnitConcurrencyGuard:
    """The pgrep race is gone; the application lock replaces it."""

    def test_the_pgrep_guard_is_gone(self):
        assert "pgrep" not in UNIT_PATH.read_text()

    def test_a_deferred_run_is_not_reported_as_a_failure(self, service):
        assert service["SuccessExitStatus"] == [str(EXIT_ALREADY_RUNNING)]
