"""Tests for how `zpbs-backup status` reports a malformed zpbs property."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from zpbs_backup import cli as cli_mod
from zpbs_backup.cli import main
from zpbs_backup.config import PBSConfig
from zpbs_backup.zfs import PROP_BACKUP, PROP_SCHEDULE, Dataset, PropertyValue


@pytest.fixture
def bad_dataset() -> Dataset:
    return Dataset(
        name="tank/data",
        properties={
            PROP_BACKUP: PropertyValue(value="true", source="local"),
            PROP_SCHEDULE: PropertyValue(value="hourly", source="local"),
        },
        mountpoint="/tank/data",
        mounted=True,
    )


def _invoke(args, dataset):
    runner = CliRunner()
    config = PBSConfig(repository="u@p!t@srv:store", server="srv")

    with patch.object(cli_mod, "load_config", return_value=config), \
         patch.object(cli_mod, "_check_pbs_connection"), \
         patch.object(cli_mod, "get_hostname", return_value="storage-server"), \
         patch.object(cli_mod, "get_all_datasets", return_value=[dataset]), \
         patch.object(cli_mod.PBSClient, "get_last_backup_time", return_value=None):
        return runner.invoke(main, args)


class TestStatusWithAnInvalidProperty:
    """status must report the bad value, not crash and not hide it."""

    def test_table_marks_the_dataset_and_explains_why(self, bad_dataset):
        result = _invoke(["status"], bad_dataset)

        assert result.exit_code == 0
        assert "misconfigured" in result.output
        assert "Invalid properties:" in result.output
        assert "zpbs:schedule" in result.output
        assert "hourly" in result.output

    def test_json_reports_the_error_and_withholds_the_derived_values(self, bad_dataset):
        result = _invoke(["status", "--json"], bad_dataset)

        assert result.exit_code == 0
        entry = json.loads(result.output)[0]
        assert entry["schedule"] is None
        assert entry["priority"] is None
        assert entry["backup_due"] is None
        assert any("zpbs:schedule" in e for e in entry["property_errors"])
