"""ZFS dataset and property operations."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum

from .config import (
    DEFAULT_BACKUP_ID_SEPARATOR,
    parse_repository,
    validate_backup_id_separator,
)


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
PROP_CHANGE_DETECTION = "zpbs:change-detection"
PROP_REPOSITORY = "zpbs:repository"
PROP_FINGERPRINT = "zpbs:fingerprint"
PROP_KEYFILE = "zpbs:keyfile"
PROP_ID_SEPARATOR = "zpbs:id-separator"

ALL_PROPERTIES = [
    PROP_BACKUP,
    PROP_SCHEDULE,
    PROP_RETENTION,
    PROP_NAMESPACE,
    PROP_PRIORITY,
    PROP_CHANGE_DETECTION,
    PROP_REPOSITORY,
    PROP_FINGERPRINT,
    PROP_KEYFILE,
    PROP_ID_SEPARATOR,
]

REPOSITORY_FORMAT = "user@realm!tokenname@server:datastore"

# The values proxmox-backup-client accepts for --change-detection-mode.
CHANGE_DETECTION_MODES = ("legacy", "data", "metadata")

# Default values — these apply when a property is UNSET. A property that is
# set but unreadable is an error, never a default: `zpbs-backup set` validates
# its input, but `zfs set` writes whatever it is given, and silently standing
# in a default for a typo means backing up or pruning to a policy nobody chose.
DEFAULT_SCHEDULE = Schedule.DAILY
DEFAULT_PRIORITY = 50

PRIORITY_MIN = 1
PRIORITY_MAX = 100


class InvalidPropertyError(ValueError):
    """A zpbs property is set to a value zpbs-backup cannot act on."""

    def __init__(self, dataset: str, prop: str, value: str, expected: str):
        self.dataset = dataset
        self.prop = prop
        self.value = value
        self.expected = expected
        super().__init__(
            f"{dataset}: {prop}={value!r} is not valid — expected {expected}. "
            f"Set it with: zpbs-backup set {prop.removeprefix('zpbs:')}=<value> {dataset}"
        )


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
        """Return the backup schedule for this dataset.

        Raises:
            InvalidPropertyError: If zpbs:schedule is set to an unknown value
        """
        prop = self.properties.get(PROP_SCHEDULE)
        if prop and prop.is_set:
            try:
                return Schedule(prop.value)
            except ValueError:
                raise InvalidPropertyError(
                    self.name,
                    PROP_SCHEDULE,
                    prop.value,
                    "one of " + ", ".join(s.value for s in Schedule),
                ) from None
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
        """Return the backup priority (lower = first).

        Raises:
            InvalidPropertyError: If zpbs:priority is set to something other
                than an integer in the accepted range
        """
        prop = self.properties.get(PROP_PRIORITY)
        if prop and prop.is_set:
            expected = f"an integer between {PRIORITY_MIN} and {PRIORITY_MAX}"
            try:
                priority = int(prop.value)
            except ValueError:
                raise InvalidPropertyError(
                    self.name, PROP_PRIORITY, prop.value, expected
                ) from None

            if not PRIORITY_MIN <= priority <= PRIORITY_MAX:
                raise InvalidPropertyError(
                    self.name, PROP_PRIORITY, prop.value, expected
                )

            return priority
        return DEFAULT_PRIORITY

    @property
    def change_detection_mode(self) -> str | None:
        """Return the change-detection mode, or None to use the client default.

        Raises:
            InvalidPropertyError: If zpbs:change-detection is set to a mode
                proxmox-backup-client does not accept
        """
        prop = self.properties.get(PROP_CHANGE_DETECTION)
        if prop and prop.is_set:
            if prop.value not in CHANGE_DETECTION_MODES:
                raise InvalidPropertyError(
                    self.name,
                    PROP_CHANGE_DETECTION,
                    prop.value,
                    "one of " + ", ".join(CHANGE_DETECTION_MODES),
                )
            return prop.value
        return None

    def _text(self, prop: str) -> str | None:
        value = self.properties.get(prop)
        return value.value if value and value.is_set else None

    @property
    def repository(self) -> str | None:
        """Return the PBS repository, or None if not set.

        Raises:
            InvalidPropertyError: If zpbs:repository is not
                user@realm!tokenname@server:datastore
        """
        value = self._text(PROP_REPOSITORY)
        if value is not None and parse_repository(value)[0] is None:
            raise InvalidPropertyError(
                self.name, PROP_REPOSITORY, value, REPOSITORY_FORMAT
            )
        return value

    @property
    def fingerprint(self) -> str | None:
        """Return the PBS server certificate fingerprint, or None."""
        return self._text(PROP_FINGERPRINT)

    @property
    def keyfile(self) -> str | None:
        """Return the encryption keyfile path, or None to not encrypt."""
        return self._text(PROP_KEYFILE)

    @property
    def id_separator(self) -> str:
        """Return the backup-id separator (default "-").

        Raises:
            InvalidPropertyError: If zpbs:id-separator holds characters a PBS
                backup-id cannot contain
        """
        value = self._text(PROP_ID_SEPARATOR)
        if value is None:
            return DEFAULT_BACKUP_ID_SEPARATOR
        try:
            return validate_backup_id_separator(value)
        except ValueError:
            raise InvalidPropertyError(
                self.name, PROP_ID_SEPARATOR, value, "one or more of [A-Za-z0-9_.-]"
            ) from None

    @property
    def unknown_properties(self) -> list[str]:
        """Return locally set zpbs properties zpbs-backup does not recognise.

        Only local values count: an inherited one is reported on the dataset
        that sets it, not on every descendant. A typo such as ``zpbs:backups``
        is silently inert, so these are surfaced rather than ignored.
        """
        return sorted(
            name
            for name, prop in self.properties.items()
            if name not in ALL_PROPERTIES and prop.is_local
        )

    def property_errors(self) -> list[InvalidPropertyError]:
        """Return every zpbs property on this dataset that cannot be acted on.

        Lets a caller report and exclude one misconfigured dataset without
        one bad value aborting the whole run.
        """
        from .retention import parse_retention

        errors: list[InvalidPropertyError] = []

        accessors = (
            lambda: self.schedule,
            lambda: self.priority,
            lambda: self.change_detection_mode,
            lambda: self.repository,
            lambda: self.id_separator,
        )
        for accessor in accessors:
            try:
                accessor()
            except InvalidPropertyError as e:
                errors.append(e)

        if self.retention:
            try:
                parse_retention(self.retention)
            except ValueError as e:
                errors.append(
                    InvalidPropertyError(
                        self.name,
                        PROP_RETENTION,
                        self.retention,
                        f"counts with d/w/m/y suffixes such as 7d,4w,6m,1y ({e})",
                    )
                )

        return errors

    @property
    def pool(self) -> str:
        """Return the pool name for this dataset."""
        return self.name.split("/")[0]

    @property
    def relative_path(self) -> str:
        """Return the dataset path relative to the pool."""
        parts = self.name.split("/", 1)
        return parts[1] if len(parts) > 1 else ""

    def get_backup_id(self, hostname: str, separator: str | None = None) -> str:
        """Generate the backup ID for this dataset.

        Format: {hostname}{separator}{dataset-path-with-slashes-replaced}

        ``separator`` replaces every "/" in the dataset path AND joins the
        hostname prefix, so the id splits uniformly on it. The default "-"
        preserves historical ids. A multi-character separator such as "--"
        makes the id REVERSIBLE when dataset name components may themselves
        contain "-" (strip the known hostname, then split on the separator to
        recover pool/dataset paths) — provided no component itself contains
        the separator. Without an argument the dataset's own (inherited)
        zpbs:id-separator applies.
        """
        if separator is None:
            separator = self.id_separator
        dataset_part = self.name.replace("/", separator)
        return f"{hostname}{separator}{dataset_part}"

    def get_auto_namespace(self, hostname: str) -> str:
        """Generate the auto-derived namespace.

        Format: {hostname}/{pool}/{dataset-path}
        """
        if self.relative_path:
            return f"{hostname}/{self.pool}/{self.relative_path}"
        return f"{hostname}/{self.pool}"


def run_zfs_command(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a zfs command and return the result."""
    cmd = ["zfs", *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _parse_dataset_output(stdout: str) -> dict[str, Dataset]:
    """Parse zfs get output into a dict of datasets.

    Keeps every zpbs: property (known or not, so unknown ones can be reported)
    plus mountpoint, mounted and canmount; any other property is ignored.
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
        elif prop.startswith("zpbs:"):
            datasets[name].properties[prop] = PropertyValue(value=value, source=source)

    return datasets


def enabled_datasets(datasets: list[Dataset]) -> list[Dataset]:
    """Select the datasets with backup enabled, sorted by priority.

    An unreadable priority must not abort selection: the orchestrators report
    and exclude such a dataset, and `zpbs-backup status` displays the bad
    value. Ordering it last keeps it out of the way until then.
    """
    enabled = [ds for ds in datasets if ds.backup_enabled]
    enabled.sort(key=_priority_sort_key)
    return enabled


def discover_datasets() -> list[Dataset]:
    """Discover all datasets with zpbs:backup=true (including inherited).

    Returns:
        List of Dataset objects with backup enabled, sorted by priority.
    """
    return enabled_datasets(get_all_datasets())


def _priority_sort_key(dataset: Dataset) -> tuple[int, str]:
    """Sort key that tolerates an unreadable zpbs:priority."""
    try:
        return (dataset.priority, dataset.name)
    except InvalidPropertyError:
        return (PRIORITY_MAX + 1, dataset.name)


def get_all_datasets() -> list[Dataset]:
    """Get all datasets with any zpbs properties, in one zfs call.

    Reads every property so that zpbs: properties outside ALL_PROPERTIES are
    seen too. Used for discovery and for status display.

    Returns:
        List of all Dataset objects that have any zpbs property set.
    """
    result = run_zfs_command(
        ["get", "-t", "filesystem", "-H", "-o", "name,property,value,source", "all"]
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


def get_written_bytes(dataset: str) -> int | None:
    """Return bytes written to the live filesystem since the most recent snapshot.

    Returns 0 if the live FS is byte-identical to the most recent snapshot.
    Returns the dataset's referenced size if no snapshots exist.
    Returns None if the dataset doesn't exist or the property can't be read.
    """
    result = run_zfs_command(
        ["get", "-Hp", "-o", "value", "written", dataset], check=False
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    try:
        return int(value)
    except ValueError:
        return None


def get_latest_snapshot_creation(dataset: str) -> int | None:
    """Return epoch seconds of the most recent snapshot's creation, or None.

    None means: dataset has no snapshots, or the query failed.
    """
    result = run_zfs_command(
        ["list", "-Hp", "-o", "creation", "-t", "snapshot", "-s", "creation",
         "-d", "1", dataset],
        check=False,
    )
    if result.returncode != 0:
        return None
    lines = [line for line in result.stdout.strip().split("\n") if line]
    if not lines:
        return None
    try:
        return int(lines[-1])
    except ValueError:
        return None


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
    """Clear every locally set zpbs property on a dataset.

    Includes properties zpbs-backup does not recognise, not only
    ALL_PROPERTIES, so a stray ``zpbs:foo`` does not outlive ``--clear``.

    Args:
        dataset: The dataset name
        recursive: If True, apply recursively to all descendants

    Raises:
        subprocess.CalledProcessError: If the command fails
    """
    args = ["get", "-H", "-s", "local", "-o", "property"]
    if recursive:
        args.append("-r")
    args.extend(["all", dataset])
    result = run_zfs_command(args)

    local = {line for line in result.stdout.splitlines() if line.startswith("zpbs:")}
    for prop in sorted(local):
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

    if prop not in ALL_PROPERTIES:
        known = ", ".join(p.removeprefix("zpbs:") for p in ALL_PROPERTIES)
        return False, f"Unknown property '{short}'. Valid properties: {known}"

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

    elif prop == PROP_CHANGE_DETECTION:
        if value not in CHANGE_DETECTION_MODES:
            return (
                False,
                f"{short} must be one of {list(CHANGE_DETECTION_MODES)}, got '{value}'",
            )

    elif prop == PROP_REPOSITORY:
        if parse_repository(value)[0] is None:
            return False, f"{short} must be {REPOSITORY_FORMAT}, got '{value}'"

    elif prop == PROP_FINGERPRINT:
        if not re.fullmatch(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2})*", value):
            return False, f"{short} must be colon-separated hex bytes (AA:BB:...), got '{value}'"

    elif prop == PROP_KEYFILE:
        if not value.startswith("/"):
            return False, f"{short} must be an absolute path, got '{value}'"

    elif prop == PROP_ID_SEPARATOR:
        try:
            validate_backup_id_separator(value)
        except ValueError as e:
            return False, str(e)

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
        if not re.match(r"^[a-zA-Z0-9/_-]+$", value):
            return (
                False,
                f"{short} can only contain alphanumeric characters, "
                "dashes, underscores, and slashes",
            )

    return True, ""
