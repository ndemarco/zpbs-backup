"""Tests for ZFS operations."""

import pytest

from zpbs_backup.zfs import (
    Dataset,
    PropertyValue,
    Schedule,
    _parse_dataset_output,
    validate_property_value,
    PROP_BACKUP,
    PROP_COMMENT,
    PROP_SCHEDULE,
    PROP_RETENTION,
    PROP_NAMESPACE,
    PROP_PRIORITY,
)


class TestPropertyValue:
    """Tests for PropertyValue class."""

    def test_is_set_true(self):
        prop = PropertyValue(value="true", source="local")
        assert prop.is_set

    def test_is_set_false(self):
        prop = PropertyValue(value="-", source="-")
        assert not prop.is_set

    def test_is_local(self):
        prop = PropertyValue(value="true", source="local")
        assert prop.is_local

    def test_is_inherited(self):
        prop = PropertyValue(value="true", source="inherited from tank")
        assert prop.is_inherited
        assert not prop.is_local


class TestDataset:
    """Tests for Dataset class."""

    def test_backup_enabled_true(self):
        ds = Dataset(
            name="tank/data",
            properties={
                PROP_BACKUP: PropertyValue(value="true", source="local"),
            },
        )
        assert ds.backup_enabled

    def test_backup_enabled_false(self):
        ds = Dataset(
            name="tank/data",
            properties={
                PROP_BACKUP: PropertyValue(value="false", source="local"),
            },
        )
        assert not ds.backup_enabled

    def test_backup_enabled_unset(self):
        ds = Dataset(name="tank/data", properties={})
        assert not ds.backup_enabled

    def test_schedule_default(self):
        ds = Dataset(name="tank/data", properties={})
        assert ds.schedule == Schedule.DAILY

    def test_schedule_set(self):
        ds = Dataset(
            name="tank/data",
            properties={
                PROP_SCHEDULE: PropertyValue(value="weekly", source="local"),
            },
        )
        assert ds.schedule == Schedule.WEEKLY

    def test_schedule_invalid_falls_back_to_default(self):
        ds = Dataset(
            name="tank/data",
            properties={
                PROP_SCHEDULE: PropertyValue(value="hourly", source="local"),
            },
        )
        assert ds.schedule == Schedule.DAILY

    def test_priority_default(self):
        ds = Dataset(name="tank/data", properties={})
        assert ds.priority == 50

    def test_priority_set(self):
        ds = Dataset(
            name="tank/data",
            properties={
                PROP_PRIORITY: PropertyValue(value="10", source="local"),
            },
        )
        assert ds.priority == 10

    def test_pool(self):
        ds = Dataset(name="tank/data/files", properties={})
        assert ds.pool == "tank"

    def test_relative_path(self):
        ds = Dataset(name="tank/data/files", properties={})
        assert ds.relative_path == "data/files"

    def test_relative_path_root_dataset(self):
        ds = Dataset(name="tank", properties={})
        assert ds.relative_path == ""

    def test_get_backup_id(self):
        ds = Dataset(name="tank/data/files", properties={})
        assert ds.get_backup_id("myhost") == "myhost-tank-data-files"

    def test_get_auto_namespace(self):
        ds = Dataset(name="tank/data/files", properties={})
        assert ds.get_auto_namespace("myhost") == "myhost/tank/data/files"

    def test_get_auto_namespace_root_dataset(self):
        ds = Dataset(name="tank", properties={})
        assert ds.get_auto_namespace("myhost") == "myhost/tank"


    def test_comment_local(self):
        ds = Dataset(
            name="tank/data",
            properties={
                PROP_COMMENT: PropertyValue(value="my backup", source="local"),
            },
        )
        assert ds.comment == "my backup"

    def test_comment_inherited_returns_none(self):
        ds = Dataset(
            name="tank/data",
            properties={
                PROP_COMMENT: PropertyValue(
                    value="parent comment", source="inherited from tank"
                ),
            },
        )
        assert ds.comment is None

    def test_comment_unset_returns_none(self):
        ds = Dataset(name="tank/data", properties={})
        assert ds.comment is None

    def test_comment_dash_returns_none(self):
        ds = Dataset(
            name="tank/data",
            properties={
                PROP_COMMENT: PropertyValue(value="-", source="-"),
            },
        )
        assert ds.comment is None

    def test_mounted_defaults_true(self):
        ds = Dataset(name="tank/data", properties={})
        assert ds.mounted is True

    def test_canmount_defaults_true(self):
        ds = Dataset(name="tank/data", properties={})
        assert ds.canmount is True

    def test_mounted_false(self):
        ds = Dataset(name="tank/data", properties={}, mounted=False)
        assert ds.mounted is False

    def test_canmount_false(self):
        ds = Dataset(name="tank/data", properties={}, canmount=False)
        assert ds.canmount is False


class TestParseDatasetOutput:
    """Tests for _parse_dataset_output with mounted/canmount."""

    def test_mounted_yes(self):
        output = "tank/data\tmounted\tyes\t-\n"
        datasets = _parse_dataset_output(output)
        assert datasets["tank/data"].mounted is True

    def test_mounted_no(self):
        output = "tank/data\tmounted\tno\t-\n"
        datasets = _parse_dataset_output(output)
        assert datasets["tank/data"].mounted is False

    def test_canmount_on(self):
        output = "tank/data\tcanmount\ton\t-\n"
        datasets = _parse_dataset_output(output)
        assert datasets["tank/data"].canmount is True

    def test_canmount_off(self):
        output = "tank/data\tcanmount\toff\t-\n"
        datasets = _parse_dataset_output(output)
        assert datasets["tank/data"].canmount is False

    def test_canmount_noauto(self):
        output = "tank/data\tcanmount\tnoauto\t-\n"
        datasets = _parse_dataset_output(output)
        assert datasets["tank/data"].canmount is False


class TestValidatePropertyValue:
    """Tests for validate_property_value function."""

    def test_backup_valid_true(self):
        valid, error = validate_property_value(PROP_BACKUP, "true")
        assert valid
        assert error == ""

    def test_backup_valid_false(self):
        valid, error = validate_property_value(PROP_BACKUP, "false")
        assert valid

    def test_backup_invalid(self):
        valid, error = validate_property_value(PROP_BACKUP, "yes")
        assert not valid
        assert "true" in error and "false" in error

    def test_schedule_valid(self):
        valid, _ = validate_property_value(PROP_SCHEDULE, "daily")
        assert valid
        valid, _ = validate_property_value(PROP_SCHEDULE, "weekly")
        assert valid
        valid, _ = validate_property_value(PROP_SCHEDULE, "monthly")
        assert valid

    def test_schedule_invalid(self):
        valid, error = validate_property_value(PROP_SCHEDULE, "hourly")
        assert not valid
        assert "must be one of" in error

    def test_priority_valid(self):
        valid, _ = validate_property_value(PROP_PRIORITY, "1")
        assert valid
        valid, _ = validate_property_value(PROP_PRIORITY, "50")
        assert valid
        valid, _ = validate_property_value(PROP_PRIORITY, "100")
        assert valid

    def test_priority_invalid_range(self):
        valid, error = validate_property_value(PROP_PRIORITY, "0")
        assert not valid
        assert "between 1 and 100" in error

        valid, error = validate_property_value(PROP_PRIORITY, "101")
        assert not valid

    def test_priority_invalid_type(self):
        valid, error = validate_property_value(PROP_PRIORITY, "high")
        assert not valid
        assert "must be an integer" in error

    def test_retention_valid(self):
        valid, _ = validate_property_value(PROP_RETENTION, "7d,4w,6m,1y")
        assert valid

    def test_retention_invalid(self):
        valid, error = validate_property_value(PROP_RETENTION, "invalid")
        assert not valid

    def test_namespace_valid(self):
        valid, _ = validate_property_value(PROP_NAMESPACE, "myhost/tank/data")
        assert valid

    def test_namespace_empty(self):
        valid, error = validate_property_value(PROP_NAMESPACE, "")
        assert not valid
        assert "cannot be empty" in error

    def test_namespace_invalid_chars(self):
        valid, error = validate_property_value(PROP_NAMESPACE, "my namespace")
        assert not valid
        assert "alphanumeric" in error

    def test_comment_valid(self):
        valid, _ = validate_property_value(PROP_COMMENT, "My backup note")
        assert valid

    def test_comment_empty(self):
        valid, error = validate_property_value(PROP_COMMENT, "")
        assert not valid
        assert "cannot be empty" in error

    def test_comment_whitespace_only(self):
        valid, error = validate_property_value(PROP_COMMENT, "   ")
        assert not valid
        assert "cannot be empty" in error

    def test_comment_too_long(self):
        valid, error = validate_property_value(PROP_COMMENT, "a" * 257)
        assert not valid
        assert "256" in error

    def test_comment_max_length_ok(self):
        valid, _ = validate_property_value(PROP_COMMENT, "a" * 256)
        assert valid

    def test_unknown_property(self):
        valid, error = validate_property_value("other:prop", "value")
        assert not valid
        assert "Unknown property" in error
