"""Click-based CLI entry point."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import click

from . import __version__
from .backup import (
    BackupOrchestrator,
    BackupResult,
    BackupSummary,
    PruneOrchestrator,
    SkipCause,
)
from .config import (
    CREDENTIALS_PATH,
    LEGACY_CONFIG_PATH,
    Credentials,
    CredentialsError,
    get_hostname,
    load_credentials,
    read_legacy_config,
)
from .connections import ConnectionProblem, Connections, Target
from .lock import EXIT_ALREADY_RUNNING, AlreadyRunning, run_lock
from .metrics import report_metrics
from .notify import format_summary_for_notification, get_notification_config, send_notification
from .scheduler import format_last_backup, format_time_delta, is_backup_due, time_until_due
from .zfs import (
    ALL_PROPERTIES,
    CHANGE_DETECTION_MODES,
    PROP_BACKUP,
    PROP_FINGERPRINT,
    PROP_ID_SEPARATOR,
    PROP_KEYFILE,
    PROP_REPOSITORY,
    Dataset,
    InvalidPropertyError,
    discover_datasets,
    enabled_datasets,
    get_all_datasets,
    get_dataset,
    inherit_all_properties,
    inherit_property,
    set_property,
    validate_property_value,
)


def _load_connections() -> Connections:
    """Read the credentials file and return the per-repository connections."""
    try:
        credentials = load_credentials()
    except CredentialsError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    return Connections(credentials)


def _last_backup(
    connections: Connections, dataset: Dataset, hostname: str
) -> tuple[datetime | None, str | None]:
    """Return (newest backup time, problem) for a dataset.

    The problem is set, and the time None, when the dataset has no usable
    connection; nothing is contacted in that case.
    """
    try:
        client = connections.reachable(connections.resolve(dataset))
    except ConnectionProblem as e:
        return None, str(e)

    backup_id = dataset.get_backup_id(hostname)
    namespace = dataset.namespace or dataset.get_auto_namespace(hostname)
    return client.get_last_backup_time(backup_id, namespace), None


@click.group()
@click.version_option(version=__version__)
def main() -> None:
    """ZFS property-driven Proxmox Backup Server backup tool.

    Automatically discovers ZFS datasets with zpbs:backup=true and backs them
    up to PBS. Configuration is stored entirely in ZFS properties.
    """
    pass


@main.command()
@click.option("-o", "--orphans", is_flag=True, help="Show orphaned backups in PBS")
@click.option("-j", "--json", "json_output", is_flag=True, help="Output as JSON")
@click.option(
    "--connections",
    "show_connections",
    is_flag=True,
    help="Show each backup-enabled dataset's effective connection settings",
)
def status(orphans: bool, json_output: bool, show_connections: bool) -> None:
    """Show backup status for all discovered datasets."""
    if show_connections:
        _show_connections(json_output)
        return

    connections = _load_connections()
    hostname = get_hostname()

    # Get all datasets with any zpbs property
    datasets = get_all_datasets()

    if json_output:
        output_data = []
        for ds in datasets:
            errors = ds.property_errors()
            readable = not errors
            container = ds.backup_enabled and ds.container
            last_backup, problem = (
                _last_backup(connections, ds, hostname)
                if ds.backup_enabled and readable and not container
                else (None, None)
            )
            namespace = (
                (ds.namespace or ds.get_auto_namespace(hostname)) if ds.backup_enabled else None
            )

            output_data.append({
                "dataset": ds.name,
                "backup_enabled": ds.backup_enabled,
                "container": container,
                "schedule": ds.schedule.value if readable else None,
                "priority": ds.priority if readable else None,
                "change_detection": ds.change_detection_mode if readable else None,
                "retention": ds.retention,
                "namespace": namespace,
                "last_backup": last_backup.isoformat() if last_backup else None,
                "backup_due": (
                    is_backup_due(ds.schedule, last_backup)
                    if ds.backup_enabled and readable and not container and problem is None
                    else None
                ),
                "connection_problem": problem,
                "property_errors": [str(e) for e in errors],
                "unknown_properties": ds.unknown_properties,
            })

        click.echo(json.dumps(output_data, indent=2))
        return

    # Table output
    if not datasets:
        click.echo("No datasets with zpbs properties found.")
        click.echo("")
        click.echo("To enable backup for a dataset:")
        click.echo("  zpbs-backup set backup=true <dataset>")
        return

    # Find column widths
    name_width = max(len(ds.name) for ds in datasets)
    name_width = max(name_width, 7)  # "DATASET" header

    click.echo(f"{'DATASET':<{name_width}}  BACKUP  SCHEDULE  PRIORITY  LAST BACKUP     STATUS")
    click.echo("-" * (name_width + 60))

    property_errors = []
    connection_problems = []
    unknown = {ds.name: ds.unknown_properties for ds in datasets if ds.unknown_properties}

    for ds in datasets:
        backup = "yes" if ds.backup_enabled else "no"
        errors = ds.property_errors()
        property_errors.extend(errors)

        if errors:
            schedule = "invalid"
            priority = "invalid"
            last_str = "-"
            status_str = "misconfigured"
        elif ds.backup_enabled and ds.container:
            schedule = "-"
            priority = "-"
            last_str = "-"
            status_str = "container"
        elif ds.backup_enabled:
            schedule = ds.schedule.value
            priority = str(ds.priority)
            last_backup, problem = _last_backup(connections, ds, hostname)

            if problem is not None:
                connection_problems.append(problem)
                last_str = "-"
                status_str = "no connection"
            else:
                last_str = format_last_backup(last_backup)
                if is_backup_due(ds.schedule, last_backup):
                    status_str = "due"
                else:
                    until = time_until_due(ds.schedule, last_backup)
                    status_str = f"in {format_time_delta(until)}"
        else:
            schedule = "-"
            priority = "-"
            last_str = "-"
            status_str = "disabled"

        click.echo(
            f"{ds.name:<{name_width}}  {backup:<6}  {schedule:<8}  {priority:<8}  "
            f"{last_str:<14}  {status_str}"
        )

    if property_errors:
        click.echo("")
        click.echo("Invalid properties:")
        for error in property_errors:
            click.echo(f"  {error}")

    if connection_problems:
        click.echo("")
        click.echo("Connection problems:")
        for problem_text in connection_problems:
            click.echo(f"  {problem_text}")

    if unknown:
        click.echo("")
        click.echo("Unknown properties (ignored by zpbs-backup):")
        for name, props in unknown.items():
            click.echo(f"  {name}: {', '.join(props)}")

    if orphans:
        click.echo("")
        _show_orphans(connections, datasets, hostname)


def _property_view(
    dataset: Dataset, prop: str, default: str | None = None
) -> dict[str, str | None]:
    """A property's raw value and where it came from, for the connection view."""
    value = dataset.properties.get(prop)
    if value is not None and value.is_set:
        return {"value": value.value, "source": value.source}
    return {"value": default, "source": "default" if default is not None else "not set"}


def _show_connections(json_output: bool) -> None:
    """Show each backup-enabled dataset's effective connection settings.

    Reads properties and the credentials file only; contacts no server and
    never prints a secret.
    """
    credentials: Credentials | None = None
    credentials_error: str | None = None
    try:
        credentials = load_credentials()
    except CredentialsError as e:
        credentials_error = str(e)

    rows = []
    for ds in enabled_datasets(get_all_datasets()):
        repository = _property_view(ds, PROP_REPOSITORY)
        if credentials is None:
            has_credentials = "unknown"
        elif repository["value"] and credentials.has_entry(repository["value"]):
            has_credentials = "yes"
        else:
            has_credentials = "no"

        rows.append({
            "dataset": ds.name,
            "repository": repository,
            "fingerprint": _property_view(ds, PROP_FINGERPRINT),
            "keyfile": _property_view(ds, PROP_KEYFILE),
            "id_separator": _property_view(ds, PROP_ID_SEPARATOR, default="-"),
            "credentials": has_credentials,
        })

    if json_output:
        click.echo(json.dumps(
            {"credentials_error": credentials_error, "datasets": rows}, indent=2
        ))
        return

    if credentials_error:
        click.echo(f"Credentials: {credentials_error}", err=True)

    if not rows:
        click.echo("No backup-enabled datasets.")
        return

    for row in rows:
        click.echo(str(row["dataset"]))
        for label, key in (
            ("repository", "repository"),
            ("fingerprint", "fingerprint"),
            ("keyfile", "keyfile"),
            ("id-separator", "id_separator"),
        ):
            view = row[key]
            assert isinstance(view, dict)
            click.echo(f"  {label:<13}{view['value'] or '-'}  ({view['source']})")
        click.echo(f"  {'credentials':<13}{row['credentials']}")


def _show_orphans(connections: Connections, datasets: list[Dataset], hostname: str) -> None:
    """Show orphaned backup groups in PBS, repository by repository."""
    click.echo("Checking for orphaned backups...")

    groups, problems = connections.group(enabled_datasets(datasets))
    for _dataset, problem in problems:
        click.echo(f"  Cannot check: {problem}")

    found = 0
    for target in groups:
        try:
            client = connections.reachable(target)
        except ConnectionProblem as e:
            click.echo(f"  Cannot check {target.repository}: {e}")
            continue

        expected_ids = {
            ds.get_backup_id(hostname)
            for other, others in groups.items()
            if other.repository == target.repository
            for ds in others
        }
        for namespace, group in client.list_all_backup_groups():
            if group.backup_id not in expected_ids:
                found += 1
                ns_str = f" (ns: {namespace})" if namespace else ""
                click.echo(
                    f"  - {group.backup_type}/{group.backup_id}{ns_str} [{target.repository}]"
                )

    if found == 0:
        click.echo("No orphaned backups found.")
    else:
        click.echo(f"Found {found} orphaned backup group(s).")


SYSTEMD_UNIT = "zpbs-backup.service"


@main.command()
@click.option("-n", "--dry-run", is_flag=True, help="Show what would be backed up without running")
@click.option("-d", "--dataset", "pattern", help="Only backup datasets matching pattern")
@click.option("-f", "--force", is_flag=True, help="Bypass schedule check")
@click.option("--no-notify", is_flag=True, help="Disable the notification hook")
@click.option("-b", "--bg", is_flag=True, help="Run in background via systemd")
@click.option(
    "--change-detection-mode",
    type=click.Choice(CHANGE_DETECTION_MODES),
    default=None,
    help=(
        "Mode to detect file changes since last backup, overriding each "
        "dataset's change-detection property for this run"
    ),
)
def run(
    dry_run: bool,
    pattern: str | None,
    force: bool,
    no_notify: bool,
    bg: bool,
    change_detection_mode: str | None,
) -> None:
    """Run backups for all due datasets."""
    if bg:
        _run_via_systemd()
        return

    connections = _load_connections()
    hostname = get_hostname()

    orchestrator = BackupOrchestrator(
        connections=connections,
        dry_run=dry_run,
        force=force,
        change_detection_mode=change_detection_mode,
    )

    if dry_run:
        # A dry run writes nothing, so making it contend for the lock would
        # only block a real run.
        summary = orchestrator.run(pattern)
    else:
        try:
            with run_lock():
                summary = orchestrator.run(pattern)
        except AlreadyRunning as e:
            click.echo(f"Not starting: {e}", err=True)
            sys.exit(EXIT_ALREADY_RUNNING)
        except OSError as e:
            click.echo(f"Error: cannot take the run lock: {e}", err=True)
            sys.exit(1)

    if not dry_run:
        # Metrics are independent of notifications: --no-notify silences
        # the hook, not Prometheus.
        report_metrics(summary, hostname)

        # --no-notify silences the hook only; syslog follows ZPBS_SYSLOG.
        send_notification(summary, hostname, run_hook=not no_notify)

    # Exit with error if any failures
    if summary.failed > 0:
        sys.exit(1)


def _run_via_systemd() -> None:
    """Trigger backup via systemd and return immediately."""
    try:
        result = subprocess.run(
            ["systemctl", "start", "--no-block", SYSTEMD_UNIT],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        click.echo("Error: systemctl not found", err=True)
        sys.exit(1)

    if result.returncode != 0:
        click.echo(f"Error: {result.stderr.strip()}", err=True)
        sys.exit(1)

    click.echo(f"Backup started in background via {SYSTEMD_UNIT}")
    click.echo(f"  Follow logs:  journalctl -u {SYSTEMD_UNIT} -f")
    click.echo(f"  Check status: systemctl is-active {SYSTEMD_UNIT}")


@main.command()
def audit() -> None:
    """Audit PBS backups vs ZFS datasets.

    Reports:
    - Datasets with zpbs:backup=true that have never been backed up
    - Backup groups in PBS with no matching ZFS dataset
    """
    connections = _load_connections()
    hostname = get_hostname()

    datasets = [ds for ds in discover_datasets() if not ds.container]
    groups, problems = connections.group(datasets)

    click.echo(f"Auditing {len(datasets)} dataset(s)...")
    click.echo("")

    for _dataset, problem in problems:
        click.echo(f"Cannot audit: {problem}")

    for target, members in groups.items():
        click.echo(f"Repository {target.repository}")
        try:
            client = connections.reachable(target)
        except ConnectionProblem as e:
            click.echo(f"  Cannot audit: {e}")
            continue

        expected_ids = {ds.get_backup_id(hostname) for ds in members}

        never_backed_up = [
            ds
            for ds in members
            if client.get_last_backup_time(
                ds.get_backup_id(hostname),
                ds.namespace or ds.get_auto_namespace(hostname),
            )
            is None
        ]

        if never_backed_up:
            click.echo("  Datasets never backed up:")
            for ds in never_backed_up:
                click.echo(f"    - {ds.name}")
        else:
            click.echo("  All enabled datasets have been backed up at least once.")

        orphans = [
            (group_ns, group)
            for group_ns, group in client.list_all_backup_groups()
            if group.backup_id not in expected_ids
        ]

        if orphans:
            click.echo(f"  Found {len(orphans)} orphaned backup group(s):")
            for group_ns, group in orphans:
                ns_str = f" (ns: {group_ns})" if group_ns else ""
                last = group.last_backup.strftime("%Y-%m-%d") if group.last_backup else "unknown"
                click.echo(
                    f"    - {group.backup_type}/{group.backup_id}{ns_str} "
                    f"({group.snapshot_count} snapshots, last: {last})"
                )
            click.echo("  To remove orphaned backups, use proxmox-backup-client directly.")
        else:
            click.echo("  No orphaned backup groups found.")
        click.echo("")


@main.command()
def check() -> None:
    """Log in once to each repository a backup-enabled dataset uses.

    A repository with no entry in the credentials file is reported as failed
    and not contacted. Exits non-zero if anything fails.
    """
    connections = _load_connections()
    datasets = discover_datasets()

    if not datasets:
        click.echo("No backup-enabled datasets.")
        return

    targets: dict[Target, list[str]] = {}
    failed = 0

    for ds in datasets:
        try:
            repository = ds.repository
        except InvalidPropertyError as e:
            click.echo(f"INVALID        {e}")
            failed += 1
            continue

        if repository is None:
            click.echo(f"NO REPOSITORY  {ds.name}: zpbs:repository is not set")
            failed += 1
            continue

        targets.setdefault(Target(repository, ds.fingerprint), []).append(ds.name)

    for target, names in targets.items():
        count = f"{len(names)} dataset(s)"
        if not connections.credentials.has_entry(target.repository):
            click.echo(f"NO CREDENTIAL  {target.repository} ({count})")
            failed += 1
            continue

        try:
            connections.reachable(target)
        except ConnectionProblem as e:
            click.echo(f"REJECTED       {target.repository} ({count}): {e}")
            failed += 1
        else:
            click.echo(f"OK             {target.repository} ({count})")

    if failed:
        sys.exit(1)


REPORTING_UNIT = "zpbs-backup.service"


@main.command("migrate-config")
@click.option(
    "--pbs-conf",
    "pbs_conf",
    type=click.Path(path_type=Path),
    default=LEGACY_CONFIG_PATH,
    show_default=True,
    help="The 0.11.0 configuration file to read",
)
@click.option(
    "--credentials",
    "credentials_path",
    type=click.Path(path_type=Path),
    default=CREDENTIALS_PATH,
    show_default=True,
    help="Credentials file to create (never overwritten)",
)
@click.option("--apply", is_flag=True, help="Run the printed zfs set commands")
def migrate_config(pbs_conf: Path, credentials_path: Path, apply: bool) -> None:
    """Convert a 0.11.0 pbs.conf into properties plus the credentials file.

    Writes the credentials file unless one exists, prints the zfs set
    commands for the pool roots (and runs them only with --apply), and prints
    ZPBS_* reporting settings as Environment= lines. pbs.conf stays in place.
    """
    try:
        legacy = read_legacy_config(pbs_conf)
    except OSError as e:
        click.echo(f"Error: cannot read {pbs_conf}: {e.strerror}", err=True)
        sys.exit(1)

    if not legacy.repository:
        click.echo(f"Error: {pbs_conf} has no PBS_REPOSITORY (or user/token/server/datastore).",
                   err=True)
        sys.exit(1)

    click.echo(f"Read {pbs_conf}")

    # Credentials
    click.echo("")
    section = f"[{legacy.repository}]"
    if not legacy.secret:
        click.echo(f"Credentials: pbs.conf holds no token secret; add it to {credentials_path}:")
        click.echo(f"  {section}")
        click.echo("  secret = <token secret>")
    elif _write_credentials(credentials_path, legacy.repository, legacy.secret):
        click.echo(f"Credentials: wrote {credentials_path} (mode 0600)")
    else:
        click.echo(f"Credentials: {credentials_path} exists and was not changed. Add:")
        click.echo(f"  {section}")
        click.echo("  secret = <the token secret from pbs.conf>")

    # Properties
    properties = [(PROP_REPOSITORY, legacy.repository)]
    if legacy.fingerprint:
        properties.append((PROP_FINGERPRINT, legacy.fingerprint))
    if legacy.keyfile:
        properties.append((PROP_KEYFILE, legacy.keyfile))
    if legacy.separator:
        properties.append((PROP_ID_SEPARATOR, legacy.separator))

    pools = sorted({ds.pool for ds in enabled_datasets(get_all_datasets())})
    click.echo("")
    click.echo("Properties (pool roots; datasets inherit):")
    for pool in pools or ["<pool>"]:
        for prop, value in properties:
            click.echo(f"  zfs set {shlex.quote(f'{prop}={value}')} {pool}")
    if not pools:
        click.echo("  No backup-enabled datasets found; replace <pool> with your pool names.")

    # Reporting settings
    if legacy.reporting:
        click.echo("")
        click.echo(f"Reporting settings, for a drop-in (systemctl edit {REPORTING_UNIT}):")
        click.echo("  [Service]")
        for key, value in legacy.reporting.items():
            click.echo(f"  Environment={shlex.quote(f'{key}={value}')}")

    click.echo("")
    click.echo(f"{pbs_conf} was left in place; delete it once the above is applied.")

    if not apply:
        click.echo("Run again with --apply to set the properties.")
        return

    if not pools:
        click.echo("Error: --apply needs at least one backup-enabled dataset.", err=True)
        sys.exit(1)

    for pool in pools:
        for prop, value in properties:
            try:
                set_property(pool, prop, value)
            except subprocess.CalledProcessError as e:
                stderr = e.stderr.strip() if e.stderr else ""
                click.echo(f"Error: zfs set {prop} on {pool} failed: {stderr}", err=True)
                sys.exit(1)
    click.echo(f"Applied {len(properties)} propert(ies) to {', '.join(pools)}.")


def _write_credentials(path: Path, repository: str, secret: str) -> bool:
    """Create the credentials file mode 0600. Returns False if it already exists."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(f"[{repository}]\nsecret = {secret}\n")
    return True


@main.command()
@click.option("-n", "--dry-run", is_flag=True, help="Show what would be pruned without doing it")
@click.option("-d", "--dataset", "pattern", help="Only prune datasets matching pattern")
def prune(dry_run: bool, pattern: str | None) -> None:
    """Apply retention policies to backup snapshots."""
    orchestrator = PruneOrchestrator(
        connections=_load_connections(),
        dry_run=dry_run,
    )

    if dry_run:
        # Deletes nothing, so it need not wait on a running backup.
        _success, failed = orchestrator.run(pattern)
    else:
        try:
            # The same lock a backup run takes: never prune a backup group
            # that is being written to.
            with run_lock():
                _success, failed = orchestrator.run(pattern)
        except AlreadyRunning as e:
            click.echo(f"Not starting: {e}", err=True)
            sys.exit(EXIT_ALREADY_RUNNING)
        except OSError as e:
            click.echo(f"Error: cannot take the run lock: {e}", err=True)
            sys.exit(1)

    if failed > 0:
        sys.exit(1)


@main.command("get")
@click.argument("dataset")
@click.argument("property", default="all")
def get_property_cmd(dataset: str, property: str) -> None:
    """Get zpbs properties for a dataset.

    Shows all properties by default, or a specific one.

    \b
    Examples:
        zpbs-backup get tank/data
        zpbs-backup get tank/data backup
        zpbs-backup get tank/data schedule
    """
    try:
        ds = get_dataset(dataset)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if property == "all":
        # Show all properties
        click.echo(f"{'PROPERTY':<16}  {'VALUE':<15}  SOURCE")
        click.echo("-" * 54)
        for prop_name in ALL_PROPERTIES:
            short_name = prop_name.removeprefix("zpbs:")
            prop = ds.properties.get(prop_name)
            if prop:
                click.echo(f"{short_name:<16}  {prop.value:<15}  {prop.source}")
            else:
                click.echo(f"{short_name:<16}  -")
    else:
        # Normalize property name
        if not property.startswith("zpbs:"):
            property = f"zpbs:{property}"

        prop = ds.properties.get(property)
        if prop:
            click.echo(f"{prop.value}\t{prop.source}")
        else:
            click.echo("-")


@main.command("set")
@click.argument("property_value")
@click.argument("dataset")
@click.option(
    "-c",
    "--clear",
    is_flag=True,
    help="When setting backup=false, also clear every local zpbs property",
)
@click.option("-r", "--recursive", is_flag=True, help="Apply recursively to descendants")
def set_property_cmd(property_value: str, dataset: str, clear: bool, recursive: bool) -> None:
    """Set a zpbs property on a dataset.

    PROPERTY_VALUE should be in the form property=value.

    \b
    Examples:
        zpbs-backup set backup=true tank/data
        zpbs-backup set schedule=daily tank/data
        zpbs-backup set change-detection=metadata tank/data
        zpbs-backup set repository=user@realm!token@server:datastore tank
        zpbs-backup set backup=false --clear tank/data
    """
    if "=" not in property_value:
        click.echo("Error: Property must be in the form property=value", err=True)
        sys.exit(1)

    property_name, _, value = property_value.partition("=")

    # Normalize property name
    if not property_name.startswith("zpbs:"):
        property_name = f"zpbs:{property_name}"
    short_name = property_name.removeprefix("zpbs:")

    # Validate
    valid, error = validate_property_value(property_name, value)
    if not valid:
        click.echo(f"Error: {error}", err=True)
        sys.exit(1)

    try:
        # If clearing all properties when disabling backup
        if clear and property_name == PROP_BACKUP and value == "false":
            inherit_all_properties(dataset, recursive=recursive)
            click.echo(f"Cleared all properties on {dataset}")
        else:
            set_property(dataset, property_name, value)
            click.echo(f"Set {short_name}={value} on {dataset}")
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else ""
        if "permission denied" in stderr.lower() or e.returncode == 255:
            click.echo(
                f"Error: Permission denied. Setting ZFS properties requires root.\n"
                f"  Try: sudo zpbs-backup set {short_name}={value} {dataset}",
                err=True,
            )
        else:
            click.echo(
                f"Error: Failed to set {short_name}={value} on {dataset}: {stderr}",
                err=True,
            )
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@main.command("inherit")
@click.option("-r", "--recursive", is_flag=True, help="Apply recursively to descendants")
@click.argument("property")
@click.argument("dataset")
def inherit_cmd(recursive: bool, property: str, dataset: str) -> None:
    """Clear a zpbs property (inherit from parent).

    PROPERTY can be a property name (e.g., schedule) or 'all'.

    \b
    Examples:
        zpbs-backup inherit schedule tank/data
        zpbs-backup inherit -r all tank/data
    """
    try:
        if property == "all":
            inherit_all_properties(dataset, recursive=recursive)
            click.echo(f"Cleared all properties on {dataset}")
        else:
            # Normalize property name
            if not property.startswith("zpbs:"):
                property = f"zpbs:{property}"
            short_name = property.removeprefix("zpbs:")

            inherit_property(dataset, property, recursive=recursive)
            click.echo(f"Cleared {short_name} on {dataset}")
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else ""
        if "permission denied" in stderr.lower() or e.returncode == 255:
            click.echo(
                f"Error: Permission denied. Modifying ZFS properties requires root.\n"
                f"  Try: sudo zpbs-backup inherit {property.removeprefix('zpbs:')} {dataset}",
                err=True,
            )
        else:
            click.echo(f"Error: {stderr or e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@main.command("send-test-notification")
@click.option("-s", "--show-only", is_flag=True, help="Only show the message, don't send")
def send_test_notification(show_only: bool) -> None:
    """Send a test notification to verify configuration.

    Creates a sample backup summary and sends it using the configured
    notification method.
    """
    _do_send_test_notification(show_only)


def _do_send_test_notification(show_only: bool) -> None:
    """Shared implementation for send-test-notification."""
    hostname = get_hostname()
    config = get_notification_config()

    # Create a sample summary
    now = datetime.now()
    sample_results = [
        BackupResult(
            dataset=Dataset(name="tank/data", properties={}),
            success=True,
            skipped=False,
            duration_seconds=45.2,
        ),
        BackupResult(
            dataset=Dataset(name="tank/media", properties={}),
            success=True,
            skipped=False,
            duration_seconds=120.5,
        ),
        BackupResult(
            dataset=Dataset(name="tank/archive", properties={}),
            success=False,
            skipped=False,
            error="Connection timeout (test error)",
            duration_seconds=30.0,
        ),
        BackupResult(
            dataset=Dataset(name="tank/scratch", properties={}),
            success=False,
            skipped=True,
            skip_reason="Not due yet",
            skip_cause=SkipCause.NOT_DUE,
            duration_seconds=0.0,
        ),
    ]

    sample_summary = BackupSummary(
        start_time=now,
        end_time=now,
        results=sample_results,
    )

    subject, body = format_summary_for_notification(sample_summary, hostname)

    click.echo(f"Subject: {subject}")
    click.echo("")
    click.echo(body)

    if show_only:
        return

    click.echo("-" * 40)
    click.echo("Notification config:")
    click.echo(f"  Enabled: {config.enabled}")
    click.echo(f"  External script: {config.external_script or '(not found)'}")
    click.echo("")

    if not config.enabled:
        click.echo("Notifications are disabled (ZPBS_NOTIFY=false)")
        return

    if not config.external_script:
        click.echo("No notification method configured.")
        click.echo("Install a script at:")
        click.echo("  /usr/local/bin/zpbs-send-notification")
        sys.exit(1)

    click.echo("Sending test notification...")
    success = send_notification(sample_summary, hostname, config)

    if success:
        click.echo("Test notification sent successfully.")
    else:
        click.echo("Failed to send notification.", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
