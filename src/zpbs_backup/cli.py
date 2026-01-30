"""Click-based CLI entry point."""

from __future__ import annotations

import json
import sys
from datetime import datetime

import click

from . import __version__
from .backup import BackupOrchestrator, PruneOrchestrator
from .config import get_hostname, load_config
from .notify import send_notification
from .pbs import PBSClient
from .scheduler import format_last_backup, format_time_delta, is_backup_due, time_until_due
from .zfs import (
    ALL_PROPERTIES,
    PROP_BACKUP,
    Dataset,
    discover_datasets,
    get_all_datasets,
    get_dataset,
    inherit_all_properties,
    inherit_property,
    set_property,
    validate_property_value,
)


@click.group()
@click.version_option(version=__version__)
def main() -> None:
    """ZFS property-driven Proxmox Backup Server backup tool.

    Automatically discovers ZFS datasets with zpbs:backup=true and backs them
    up to PBS. Configuration is stored entirely in ZFS properties.
    """
    pass


@main.command()
@click.option("--orphans", is_flag=True, help="Show orphaned backups in PBS")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def status(orphans: bool, json_output: bool) -> None:
    """Show backup status for all discovered datasets."""
    try:
        config = load_config()
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    client = PBSClient(config)
    hostname = get_hostname()

    # Get all datasets with any zpbs property
    datasets = get_all_datasets()

    if json_output:
        output_data = []
        for ds in datasets:
            backup_id = ds.get_backup_id(hostname)
            namespace = ds.namespace or ds.get_auto_namespace(hostname)
            last_backup = client.get_last_backup_time(backup_id, namespace) if ds.backup_enabled else None

            output_data.append({
                "dataset": ds.name,
                "backup_enabled": ds.backup_enabled,
                "schedule": ds.schedule.value,
                "priority": ds.priority,
                "retention": ds.retention,
                "namespace": namespace if ds.backup_enabled else None,
                "last_backup": last_backup.isoformat() if last_backup else None,
                "backup_due": is_backup_due(ds.schedule, last_backup) if ds.backup_enabled else None,
            })

        click.echo(json.dumps(output_data, indent=2))
        return

    # Table output
    if not datasets:
        click.echo("No datasets with zpbs properties found.")
        click.echo("")
        click.echo("To enable backup for a dataset:")
        click.echo("  zpbs-backup set zpbs:backup=true <dataset>")
        return

    # Find column widths
    name_width = max(len(ds.name) for ds in datasets)
    name_width = max(name_width, 7)  # "DATASET" header

    click.echo(f"{'DATASET':<{name_width}}  BACKUP  SCHEDULE  PRIORITY  LAST BACKUP     STATUS")
    click.echo("-" * (name_width + 60))

    for ds in datasets:
        backup = "yes" if ds.backup_enabled else "no"
        schedule = ds.schedule.value if ds.backup_enabled else "-"
        priority = str(ds.priority) if ds.backup_enabled else "-"

        if ds.backup_enabled:
            backup_id = ds.get_backup_id(hostname)
            namespace = ds.namespace or ds.get_auto_namespace(hostname)
            last_backup = client.get_last_backup_time(backup_id, namespace)
            last_str = format_last_backup(last_backup)

            if is_backup_due(ds.schedule, last_backup):
                status_str = "due"
            else:
                until = time_until_due(ds.schedule, last_backup)
                status_str = f"in {format_time_delta(until)}"
        else:
            last_str = "-"
            status_str = "disabled"

        click.echo(
            f"{ds.name:<{name_width}}  {backup:<6}  {schedule:<8}  {priority:<8}  "
            f"{last_str:<14}  {status_str}"
        )

    if orphans:
        click.echo("")
        _show_orphans(client, datasets, hostname)


def _show_orphans(client: PBSClient, datasets: list[Dataset], hostname: str) -> None:
    """Show orphaned backup groups in PBS."""
    click.echo("Checking for orphaned backups...")

    # Get all expected backup IDs
    expected_ids = {ds.get_backup_id(hostname) for ds in datasets if ds.backup_enabled}

    # Get all backup groups from PBS
    all_groups = client.list_all_backup_groups()

    orphans = []
    for namespace, group in all_groups:
        if group.backup_id not in expected_ids:
            orphans.append((namespace, group))

    if not orphans:
        click.echo("No orphaned backups found.")
        return

    click.echo(f"Found {len(orphans)} orphaned backup group(s):")
    for namespace, group in orphans:
        ns_str = f" (ns: {namespace})" if namespace else ""
        click.echo(f"  - {group.backup_type}/{group.backup_id}{ns_str}")


@main.command()
@click.option("--dry-run", is_flag=True, help="Show what would be backed up without running")
@click.option("--dataset", "pattern", help="Only backup datasets matching pattern")
@click.option("--force", is_flag=True, help="Bypass schedule check")
@click.option("--no-notify", is_flag=True, help="Disable email notification")
def run(dry_run: bool, pattern: str | None, force: bool, no_notify: bool) -> None:
    """Run backups for all due datasets."""
    try:
        config = load_config()
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    hostname = get_hostname()
    orchestrator = BackupOrchestrator(
        config=config,
        dry_run=dry_run,
        force=force,
    )

    summary = orchestrator.run(pattern)

    # Send notification
    if not no_notify and not dry_run:
        send_notification(summary, hostname)

    # Exit with error if any failures
    if summary.failed > 0:
        sys.exit(1)


@main.command()
def audit() -> None:
    """Audit PBS backups vs ZFS datasets.

    Reports:
    - Datasets with zpbs:backup=true that have never been backed up
    - Backup groups in PBS with no matching ZFS dataset
    """
    try:
        config = load_config()
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    client = PBSClient(config)
    hostname = get_hostname()

    # Get enabled datasets
    datasets = discover_datasets()
    expected_ids = {ds.get_backup_id(hostname): ds for ds in datasets}

    click.echo(f"Auditing {len(datasets)} dataset(s)...")
    click.echo("")

    # Check for never-backed-up datasets
    never_backed_up = []
    for ds in datasets:
        backup_id = ds.get_backup_id(hostname)
        namespace = ds.namespace or ds.get_auto_namespace(hostname)
        last_backup = client.get_last_backup_time(backup_id, namespace)
        if last_backup is None:
            never_backed_up.append(ds)

    if never_backed_up:
        click.echo("Datasets never backed up:")
        for ds in never_backed_up:
            click.echo(f"  - {ds.name}")
        click.echo("")
    else:
        click.echo("All enabled datasets have been backed up at least once.")
        click.echo("")

    # Check for orphaned PBS groups
    click.echo("Checking for orphaned backup groups...")
    all_groups = client.list_all_backup_groups()

    orphans = []
    for namespace, group in all_groups:
        if group.backup_id not in expected_ids:
            orphans.append((namespace, group))

    if orphans:
        click.echo(f"Found {len(orphans)} orphaned backup group(s):")
        for namespace, group in orphans:
            ns_str = f" (ns: {namespace})" if namespace else ""
            last = group.last_backup.strftime("%Y-%m-%d") if group.last_backup else "unknown"
            click.echo(
                f"  - {group.backup_type}/{group.backup_id}{ns_str} "
                f"({group.snapshot_count} snapshots, last: {last})"
            )
        click.echo("")
        click.echo("To remove orphaned backups, use proxmox-backup-client directly.")
    else:
        click.echo("No orphaned backup groups found.")


@main.command()
@click.option("--dry-run", is_flag=True, help="Show what would be pruned without doing it")
@click.option("--dataset", "pattern", help="Only prune datasets matching pattern")
def prune(dry_run: bool, pattern: str | None) -> None:
    """Apply retention policies to backup snapshots."""
    try:
        config = load_config()
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    orchestrator = PruneOrchestrator(
        config=config,
        dry_run=dry_run,
    )

    success, failed = orchestrator.run(pattern)

    if failed > 0:
        sys.exit(1)


@main.command("get")
@click.argument("property")
@click.argument("dataset")
def get_property_cmd(property: str, dataset: str) -> None:
    """Get a zpbs property value for a dataset.

    PROPERTY can be a specific property (e.g., zpbs:backup) or 'all' to show
    all zpbs properties.
    """
    try:
        ds = get_dataset(dataset)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if property == "all":
        # Show all properties
        click.echo(f"{'PROPERTY':<20}  {'VALUE':<15}  SOURCE")
        click.echo("-" * 60)
        for prop_name in ALL_PROPERTIES:
            prop = ds.properties.get(prop_name)
            if prop:
                click.echo(f"{prop_name:<20}  {prop.value:<15}  {prop.source}")
            else:
                click.echo(f"{prop_name:<20}  -")
    else:
        # Normalize property name
        if not property.startswith("zpbs:"):
            property = f"zpbs:{property}"

        prop = ds.properties.get(property)
        if prop:
            click.echo(f"{prop.value}\t{prop.source}")
        else:
            click.echo(f"-")


@main.command("set")
@click.argument("property_value")
@click.argument("dataset")
@click.option("--clear", is_flag=True, help="When setting backup=false, also clear all properties")
@click.option("-r", "--recursive", is_flag=True, help="Apply recursively to descendants")
def set_property_cmd(property_value: str, dataset: str, clear: bool, recursive: bool) -> None:
    """Set a zpbs property on a dataset.

    PROPERTY_VALUE should be in the form property=value, e.g., zpbs:backup=true.

    Examples:
        zpbs-backup set zpbs:backup=true tank/data
        zpbs-backup set schedule=daily tank/data
        zpbs-backup set backup=false --clear tank/data
    """
    if "=" not in property_value:
        click.echo("Error: Property must be in the form property=value", err=True)
        sys.exit(1)

    property_name, _, value = property_value.partition("=")

    # Normalize property name
    if not property_name.startswith("zpbs:"):
        property_name = f"zpbs:{property_name}"

    # Validate
    valid, error = validate_property_value(property_name, value)
    if not valid:
        click.echo(f"Error: {error}", err=True)
        sys.exit(1)

    try:
        # If clearing all properties when disabling backup
        if clear and property_name == PROP_BACKUP and value == "false":
            inherit_all_properties(dataset, recursive=recursive)
            click.echo(f"Cleared all zpbs properties on {dataset}")
        else:
            set_property(dataset, property_name, value)
            click.echo(f"Set {property_name}={value} on {dataset}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@main.command("inherit")
@click.option("-r", "--recursive", is_flag=True, help="Apply recursively to descendants")
@click.argument("property")
@click.argument("dataset")
def inherit_cmd(recursive: bool, property: str, dataset: str) -> None:
    """Clear a zpbs property (inherit from parent).

    PROPERTY can be a specific property (e.g., zpbs:backup) or 'all' to clear
    all zpbs properties.

    Examples:
        zpbs-backup inherit zpbs:schedule tank/data
        zpbs-backup inherit -r all tank/data
    """
    try:
        if property == "all":
            inherit_all_properties(dataset, recursive=recursive)
            click.echo(f"Cleared all zpbs properties on {dataset}")
        else:
            # Normalize property name
            if not property.startswith("zpbs:"):
                property = f"zpbs:{property}"

            inherit_property(dataset, property, recursive=recursive)
            click.echo(f"Cleared {property} on {dataset}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
