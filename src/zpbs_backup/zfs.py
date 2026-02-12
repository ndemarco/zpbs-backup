"""ZFS dataset and property operations."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from enum import Enum


class Schedule(Enum):
    """Backup schedule types."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


# Property names
PROP_BACKUP = "zpbs:backup"
PROP_SCHEDULE = "zpbs:schedule"
PROP_RETENTION = "zpbs:retention"
PROP_NAMESPACE = "zpbs:namespace"
PROP_PRIORITY = "zpbs:priority"

ALL_PROPERTIES = [
    PROP_BACKUP,
    PROP_SCHEDULE,
    PROP_RETENTION,
    PROP_NAMESPACE,
    PROP_PRIORITY,
]

# Default values
DEFAULT_SCHEDULE = Schedule.DAILY
DEFAULT_PRIORITY = 50


@dataclass
class PropertyValue:
    """A ZFS property value with its source."""

    value: str
    source: str  # 'local', 'inherited from <dataset>', 'default', '-'

    @property
    def is_set(self) -> bool:
        """Return True if the property has a value (not '-')."""
        return self.value != "-"

    @property
    def is_local(self) -> bool:
        """Return True if the property is set locally."""
        return self.source == "local"

    @property
    def is_inherited(self) -> bool:
        """Return True if the property is inherited."""
        return self.source.startswith("inherited from ")


@dataclass
class Dataset:
    """A ZFS dataset with zpbs properties."""

    name: str
    properties: dict[str, PropertyValue] = field(default_factory=dict)
    mountpoint: str | None = None  # None if mountpoint=none/legacy/-
    mounted: bool = True
    canmount: bool = True

    @property
    def backup_enabled(self) -> bool:
        """Return True if backup is enabled for this dataset."""
        prop = self.properties.get(PROP_BACKUP)
        return prop is not None and prop.value == "true"

    @property
    def schedule(self) -> Schedule:
        """Return the backup schedule for this dataset."""
        prop = self.properties.get(PROP_SCHEDULE)
        if prop and prop.is_set:
            try:
                return Schedule(prop.value)
            except ValueError:
                pass
        return DEFAULT_SCHEDULE

    @property
    def retention(self) -> str | None:
        """Return the retention policy string, or None if not set."""
        prop = self.properties.get(PROP_RETENTION)
        if prop and prop.is_set:
            return prop.value
        return None

    @property
    def namespace(self) -> str | None:
        """Return the explicit namespace, or None to use auto-derived."""
        prop = self.properties.get(PROP_NAMESPACE)
        if prop and prop.is_set:
            return prop.value
        return None

    @property
    def priority(self) -> int:
        """Return the backup priority (lower = first)."""
        prop = self.properties.get(PROP_PRIORITY)
        if prop and prop.is_set:
            try:
                return int(prop.value)
            except ValueError:
                pass
        return DEFAULT_PRIORITY

    @property
    def pool(self) -> str:
        """Return the pool name for this dataset."""
        return self.name.split("/")[0]

    @property
    def relative_path(self) -> str:
        """Return the dataset path relative to the pool."""
        parts = self.name.split("/", 1)
        return parts[1] if len(parts) > 1 else ""

    def get_backup_id(self, hostname: str) -> str:
        """Generate the backup ID for this dataset.

        Format: {hostname}-{dataset-name-with-slashes-replaced}
        """
        dataset_part = self.name.replace("/", "-")
        return f"{hostname}-{dataset_part}"

    def get_auto_namespace(self, hostname: str) -> str:
        """Generate the auto-derived namespace.

        Format: {hostname}/{pool}/{dataset-path}
        """
        if self.relative_path:
            return f"{hostname}/{self.pool}/{self.relative_path}"
        return f"{hostname}/{self.pool}"


def run_zfs_command(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a zfs command and return the result."""
    cmd = ["zfs"] + args
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _parse_dataset_output(stdout: str) -> dict[str, Dataset]:
    """Parse zfs get output into a dict of datasets.

    Handles both zpbs: properties and the standard mountpoint property.
    """
    datasets: dict[str, Dataset] = {}

    for line in stdout.strip().split("\n"):
        if not line:
            continue

        parts = line.split("\t")
        if len(parts) != 4:
            continue

        name, prop, value, source = parts

        if name not in datasets:
            datasets[name] = Dataset(name=name)

        if prop == "mountpoint":
            # none, legacy, and - mean no usable mountpoint
            if value not in ("none", "legacy", "-"):
                datasets[name].mountpoint = value
        elif prop == "mounted":
            datasets[name].mounted = value == "yes"
        elif prop == "canmount":
            datasets[name].canmount = value == "on"
        else:
            datasets[name].properties[prop] = PropertyValue(value=value, source=source)

    return datasets


def discover_datasets() -> list[Dataset]:
    """Discover all datasets with zpbs:backup=true (including inherited).

    Returns:
        List of Dataset objects with backup enabled, sorted by priority.
    """
    # Get all zpbs properties plus mountpoint for all filesystems
    props = ",".join(ALL_PROPERTIES) + ",mountpoint,mounted,canmount"
    result = run_zfs_command(
        ["get", "-t", "filesystem", "-H", "-o", "name,property,value,source", props]
    )

    datasets = _parse_dataset_output(result.stdout)

    # Filter to datasets with backup enabled and sort by priority
    enabled = [ds for ds in datasets.values() if ds.backup_enabled]
    enabled.sort(key=lambda ds: (ds.priority, ds.name))

    return enabled


def get_all_datasets() -> list[Dataset]:
    """Get all datasets with any zpbs properties (for status display).

    Returns:
        List of all Dataset objects that have any zpbs property set.
    """
    props = ",".join(ALL_PROPERTIES) + ",mountpoint,mounted,canmount"
    result = run_zfs_command(
        ["get", "-t", "filesystem", "-H", "-o", "name,property,value,source", props]
    )

    datasets = _parse_dataset_output(result.stdout)

    # Return all datasets that have at least one property set
    return [
        ds
        for ds in datasets.values()
        if any(p.is_set for p in ds.properties.values())
    ]


def get_dataset(name: str) -> Dataset:
    """Get a single dataset with its zpbs properties.

    Args:
        name: The dataset name

    Returns:
        Dataset object

    Raises:
        subprocess.CalledProcessError: If the dataset doesn't exist
    """
    props = ",".join(ALL_PROPERTIES) + ",mountpoint,mounted,canmount"
    result = run_zfs_command(
        ["get", "-H", "-o", "name,property,value,source", props, name]
    )

    datasets = _parse_dataset_output(result.stdout)
    return datasets.get(name, Dataset(name=name))


def set_property(dataset: str, prop: str, value: str) -> None:
    """Set a zpbs property on a dataset.

    Args:
        dataset: The dataset name
        prop: The property name (e.g., 'zpbs:backup')
        value: The property value

    Raises:
        subprocess.CalledProcessError: If the command fails
    """
    run_zfs_command(["set", f"{prop}={value}", dataset])


def inherit_property(dataset: str, prop: str, recursive: bool = False) -> None:
    """Clear a zpbs property on a dataset (inherit from parent).

    Args:
        dataset: The dataset name
        prop: The property name (e.g., 'zpbs:backup')
        recursive: If True, apply recursively to all descendants

    Raises:
        subprocess.CalledProcessError: If the command fails
    """
    args = ["inherit"]
    if recursive:
        args.append("-r")
    args.extend([prop, dataset])
    run_zfs_command(args)


def inherit_all_properties(dataset: str, recursive: bool = False) -> None:
    """Clear all zpbs properties on a dataset.

    Args:
        dataset: The dataset name
        recursive: If True, apply recursively to all descendants

    Raises:
        subprocess.CalledProcessError: If the command fails
    """
    for prop in ALL_PROPERTIES:
        inherit_property(dataset, prop, recursive=recursive)


def validate_property_value(prop: str, value: str) -> tuple[bool, str]:
    """Validate a property value.

    Args:
        prop: The property name
        value: The proposed value

    Returns:
        Tuple of (is_valid, error_message)
    """
    short = prop.removeprefix("zpbs:")

    if prop == PROP_BACKUP:
        if value not in ("true", "false"):
            return False, f"{short} must be 'true' or 'false', got '{value}'"

    elif prop == PROP_SCHEDULE:
        valid_schedules = [s.value for s in Schedule]
        if value not in valid_schedules:
            return False, f"{short} must be one of {valid_schedules}, got '{value}'"

    elif prop == PROP_PRIORITY:
        try:
            priority = int(value)
            if not 1 <= priority <= 100:
                return False, f"{short} must be between 1 and 100, got {priority}"
        except ValueError:
            return False, f"{short} must be an integer, got '{value}'"

    elif prop == PROP_RETENTION:
        # Basic validation - check format like "7d,4w,6m,1y"
        from .retention import parse_retention

        try:
            parse_retention(value)
        except ValueError as e:
            return False, str(e)

    elif prop == PROP_NAMESPACE:
        # Namespace can be any string, but shouldn't contain invalid characters
        if not value:
            return False, f"{short} cannot be empty"
        # PBS namespace rules: alphanumeric, dash, underscore, slash
        import re

        if not re.match(r"^[a-zA-Z0-9/_-]+$", value):
            return (
                False,
                f"{short} can only contain alphanumeric characters, "
                "dashes, underscores, and slashes",
            )

    elif not prop.startswith("zpbs:"):
        return False, f"Unknown property '{prop}'"

    return True, ""
