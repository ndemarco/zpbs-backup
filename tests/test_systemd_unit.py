"""Tests that the packaged systemd unit can write what the tool writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from zpbs_backup.lock import EXIT_ALREADY_RUNNING, LOCK_PATH
from zpbs_backup.metrics import STATE_FILE

UNIT_PATH = Path(__file__).resolve().parent.parent / "systemd" / "zpbs-backup.service"
STATE_DIR = Path("/var/lib/zpbs-backup")


def _parse_section(path: Path, section: str) -> dict[str, list[str]]:
    """Parse one section of a unit file into key -> list of values.

    Hand-rolled rather than via configparser: systemd allows a key to repeat
    and takes all of them (ReadWritePaths does exactly that), while
    configparser keeps only the last.
    """
    values: dict[str, list[str]] = {}
    in_section = False

    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_section = line == section
            continue
        if in_section and "=" in line:
            key, _, value = line.partition("=")
            values.setdefault(key.strip(), []).append(value.strip())

    return values


@pytest.fixture(scope="module")
def service() -> dict[str, list[str]]:
    return _parse_section(UNIT_PATH, "[Service]")


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


PRUNE_UNIT_PATH = UNIT_PATH.parent / "zpbs-backup-prune.service"
PRUNE_TIMER_PATH = UNIT_PATH.parent / "zpbs-backup-prune.timer"
NFPM_PATH = UNIT_PATH.parent.parent / "packaging" / "nfpm.yaml"
POSTINSTALL_PATH = (
    UNIT_PATH.parent.parent / "packaging" / "scripts" / "postinstall.sh"
)
PREREMOVE_PATH = UNIT_PATH.parent.parent / "packaging" / "scripts" / "preremove.sh"


@pytest.fixture(scope="module")
def prune_service() -> dict[str, list[str]]:
    return _parse_section(PRUNE_UNIT_PATH, "[Service]")


class TestPruneUnit:
    """The prune unit must be able to do its job, and share the run lock."""

    def test_it_runs_prune(self, prune_service):
        assert prune_service["ExecStart"] == ["/usr/local/bin/zpbs-backup prune"]

    def test_it_shares_the_state_directory_holding_the_lock(self, prune_service):
        """A lock only works if both units reach the same file."""
        assert prune_service["StateDirectory"] == ["zpbs-backup"]

    def test_zfs_stays_writable(self, prune_service):
        assert "/dev/zfs" in prune_service["ReadWritePaths"]

    def test_hardening_matches_the_backup_unit(self, prune_service):
        assert prune_service["ProtectSystem"] == ["strict"]

    def test_a_deferred_prune_is_not_reported_as_a_failure(self, prune_service):
        assert prune_service["SuccessExitStatus"] == [str(EXIT_ALREADY_RUNNING)]


class TestPruneTimerShipsDisabled:
    """Prune deletes real backups, so enabling it is the operator's call."""

    def test_the_timer_exists_and_is_installable(self):
        timer = _parse_section(PRUNE_TIMER_PATH, "[Install]")
        assert timer["WantedBy"] == ["timers.target"]

    def test_the_timer_avoids_the_backup_window(self):
        timer = _parse_section(PRUNE_TIMER_PATH, "[Timer]")
        assert timer["OnCalendar"] == ["*-*-* 05:00:00"]

    def test_both_prune_files_are_packaged(self):
        packaged = NFPM_PATH.read_text()
        assert "/lib/systemd/system/zpbs-backup-prune.service" in packaged
        assert "/lib/systemd/system/zpbs-backup-prune.timer" in packaged

    def test_postinstall_does_not_enable_the_prune_timer(self):
        """The whole point: shipped installed, left disabled."""
        # Only lines that actually invoke systemctl, not the echoed
        # instructions telling the operator how to opt in.
        enabled = [
            line
            for line in POSTINSTALL_PATH.read_text().splitlines()
            if line.strip().startswith("systemctl enable")
        ]
        assert any("zpbs-backup.timer" in line for line in enabled)
        assert not any("zpbs-backup-prune.timer" in line for line in enabled)

    def test_postinstall_tells_the_operator_how_to_opt_in(self):
        text = POSTINSTALL_PATH.read_text()
        assert "prune --dry-run" in text
        assert "enable --now zpbs-backup-prune.timer" in text

    def test_removal_disables_the_prune_timer(self):
        assert "disable zpbs-backup-prune.timer" in PREREMOVE_PATH.read_text()


class TestPruneReadme:
    """The opt-in has to be documented where an operator will find it."""

    def test_readme_documents_enabling_after_a_dry_run(self):
        readme = (UNIT_PATH.parent.parent / "README.md").read_text()
        assert "systemctl enable --now zpbs-backup-prune.timer" in readme
        assert "zpbs-backup prune --dry-run" in readme
