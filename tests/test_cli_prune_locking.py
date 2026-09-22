"""Tests that `zpbs-backup prune` shares the backup run lock."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from zpbs_backup import cli as cli_mod
from zpbs_backup import lock as lock_mod
from zpbs_backup.cli import main
from zpbs_backup.config import PBSConfig
from zpbs_backup.lock import EXIT_ALREADY_RUNNING, run_lock


def _prune(args, lock_path):
    runner = CliRunner()
    pbs_config = PBSConfig(repository="u@p!t@srv:store", server="srv")

    with patch.object(lock_mod, "LOCK_PATH", lock_path), \
         patch.object(cli_mod, "load_config", return_value=pbs_config), \
         patch.object(cli_mod.PruneOrchestrator, "run", return_value=(1, 0)) as pruned:
        result = runner.invoke(main, ["prune"] + args)

    return result, pruned


class TestPruneRespectsTheRunLock:
    """Pruning a group while it is being written to must not happen."""

    def test_prune_is_refused_while_a_backup_holds_the_lock(self, tmp_path):
        lock_path = tmp_path / "run.lock"

        with run_lock(lock_path):
            result, pruned = _prune([], lock_path)

        assert result.exit_code == EXIT_ALREADY_RUNNING
        assert "Not starting" in result.output
        pruned.assert_not_called()

    def test_prune_runs_when_nothing_holds_the_lock(self, tmp_path):
        result, pruned = _prune([], tmp_path / "run.lock")

        assert result.exit_code == 0
        pruned.assert_called_once()

    def test_prune_releases_the_lock_afterwards(self, tmp_path):
        lock_path = tmp_path / "run.lock"

        _prune([], lock_path)

        with run_lock(lock_path):
            pass  # acquired, so prune did not leave it held

    def test_a_dry_run_prune_does_not_contend(self, tmp_path):
        """It deletes nothing, so it must not wait on a running backup."""
        lock_path = tmp_path / "run.lock"

        with run_lock(lock_path):
            result, pruned = _prune(["--dry-run"], lock_path)

        assert result.exit_code == 0
        pruned.assert_called_once()
