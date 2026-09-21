"""Tests for the exclusive run lock."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from zpbs_backup.lock import EXIT_ALREADY_RUNNING, AlreadyRunning, run_lock


def _holder_process(lock_path, src_dir) -> subprocess.Popen:
    """Start a separate process that takes the lock and then waits."""
    script = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(src_dir)!r})
        from pathlib import Path
        from zpbs_backup.lock import run_lock

        with run_lock(Path({str(lock_path)!r})):
            print("locked", flush=True)
            time.sleep(60)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "locked"
    return proc


@pytest.fixture
def src_dir():
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")


class TestRunLock:
    """A run must not start while another holds the lock."""

    def test_a_second_acquisition_in_one_process_is_refused(self, tmp_path):
        lock_path = tmp_path / "run.lock"

        with run_lock(lock_path):
            with pytest.raises(AlreadyRunning):
                with run_lock(lock_path):
                    pass

    def test_a_second_process_is_refused(self, tmp_path, src_dir):
        """The timer-driven and manual cases are two processes, not one."""
        lock_path = tmp_path / "run.lock"
        holder = _holder_process(lock_path, src_dir)

        try:
            with pytest.raises(AlreadyRunning):
                with run_lock(lock_path):
                    pass
        finally:
            holder.kill()
            holder.wait(timeout=10)

    def test_the_lock_is_released_after_the_holder_is_killed(self, tmp_path, src_dir):
        """SIGKILL leaves no chance to clean up, so the kernel must do it."""
        lock_path = tmp_path / "run.lock"
        holder = _holder_process(lock_path, src_dir)

        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=10)

        with run_lock(lock_path):
            pass  # acquired, so the dead holder's lock is gone

    def test_the_lock_is_released_after_a_normal_exit(self, tmp_path):
        lock_path = tmp_path / "run.lock"

        with run_lock(lock_path):
            pass

        with run_lock(lock_path):
            pass

    def test_a_leftover_lock_file_does_not_block_a_run(self, tmp_path):
        """The file is not the lock; only a live holder is."""
        lock_path = tmp_path / "run.lock"
        lock_path.write_text("12345\n")

        with run_lock(lock_path) as held:
            assert held == lock_path

    def test_the_parent_directory_is_created(self, tmp_path):
        lock_path = tmp_path / "state" / "run.lock"

        with run_lock(lock_path):
            assert lock_path.exists()

    def test_the_holding_pid_is_recorded(self, tmp_path):
        lock_path = tmp_path / "run.lock"

        with run_lock(lock_path):
            assert lock_path.read_text().strip() == str(os.getpid())

    def test_the_descriptor_is_not_inherited_by_children(self, tmp_path, src_dir):
        """A child outliving the run must not keep the lock held."""
        lock_path = tmp_path / "run.lock"

        with run_lock(lock_path):
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

        try:
            time.sleep(0.1)
            with run_lock(lock_path):
                pass  # the surviving child does not hold it
        finally:
            child.kill()
            child.wait(timeout=10)


def test_the_already_running_exit_code_is_not_a_generic_failure():
    """The unit lists this in SuccessExitStatus, so it must not be 1."""
    assert EXIT_ALREADY_RUNNING == 75
