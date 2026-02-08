# zpbs-backup

[![PyPI version](https://badge.fury.io/py/zpbs-backup.svg)](https://badge.fury.io/py/zpbs-backup)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

ZFS property-driven Proxmox Backup Server backup tool.

Automatically discovers ZFS datasets with `zpbs:backup=true` and backs them up to PBS. Configuration is stored entirely in ZFS properties - no config files for dataset selection.

## Features

- **Auto-discovery**: Finds datasets to back up via ZFS custom properties
- **Inheritance**: Child datasets inherit backup settings from parents
- **Schedule-aware**: Only runs backups when due (daily/weekly/monthly)
- **Priority ordering**: Back up critical data first
- **Retention policies**: Per-dataset retention settings
- **Dry-run mode**: See what would happen without making changes
- **Audit mode**: Find orphaned backups and never-backed-up datasets

## Installation

### Debian / Ubuntu / Proxmox VE (.deb)

Download the latest `.deb` from the [Releases](https://github.com/ndemarco/zpbs-backup/releases) page:

```bash
sudo dpkg -i zpbs-backup_<version>_amd64.deb
sudo apt-get install -f  # Install any missing dependencies
```

This installs everything: CLI, systemd timer, and config directory. The timer is enabled automatically -- configure PBS credentials at `/etc/zpbs-backup/pbs.conf`, then start the timer:

```bash
sudo systemctl start zpbs-backup.timer
```

### RHEL / Rocky / Alma (.rpm)

```bash
sudo rpm -i zpbs-backup-<version>.x86_64.rpm
```

Requires `python3.11` from AppStream (`dnf install python3.11`).

### From PyPI

```bash
pip install zpbs-backup
# or
pipx install zpbs-backup
```

When installing via pip/pipx, systemd units are not installed automatically. See [Systemd Integration](#systemd-integration) below for manual setup.

### From Source

```bash
git clone https://github.com/ndemarco/zpbs-backup.git
cd zpbs-backup
pip install .
```

### System Requirements

- Python 3.11 or newer
- ZFS utilities (`zfs`, `zpool` commands)
- Proxmox Backup Server client (`proxmox-backup-client`)
- Linux operating system

**Note:** ZFS and PBS client cannot be installed via pip and must be installed through your system package manager. The `.deb` and `.rpm` packages list these as recommended dependencies.

## Quick Start

1. Create a PBS API token with **DatastoreAdmin** permission, then configure the connection:

> **Important: PBS privilege separation.** By default, a token's effective permissions
> are the *intersection* of the user's and the token's permissions. You must grant
> permissions on the datastore to **both** the user (e.g. `backup@pbs`) **and** the
> token (e.g. `backup@pbs!mytoken`). In the PBS web UI: Configuration > Access Control >
> Permissions > Add — grant **DatastoreAdmin** on `/datastore/yourstore` to both.

```bash
# Option 1: Environment variables (individual parts — recommended)
export PBS_USER="backup@pbs"
export PBS_API_TOKEN_NAME="mytoken"
export PBS_SERVER="pbs.example.com"
export PBS_DATASTORE="backups"
export PBS_API_TOKEN_SECRET="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
export PBS_FINGERPRINT="AA:BB:CC:..."

# Option 2: Config file
# Config files are checked in priority order:
#   1. ~/.config/zpbs-backup/pbs.conf  (per-user)
#   2. /etc/zpbs-backup/pbs.conf       (system-wide)
```

Example `/etc/zpbs-backup/pbs.conf`:

```bash
# PBS API token configuration
PBS_USER="backup@pbs"
PBS_API_TOKEN_NAME="mytoken"
PBS_SERVER="pbs.example.com"
PBS_DATASTORE="backups"
PBS_API_TOKEN_SECRET="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
PBS_FINGERPRINT="AA:BB:CC:..."

# Shell variable interpolation is supported:
# PBS_REPOSITORY="${PBS_USER}!${PBS_API_TOKEN_NAME}@${PBS_SERVER}:${PBS_DATASTORE}"
```

Verify your configuration:

```bash
zpbs-backup show-config
```

2. Enable backup on datasets:

```bash
# Enable backup for a dataset
zpbs-backup set backup=true tank/important-data

# Set schedule (optional, default is daily)
zpbs-backup set schedule=weekly tank/less-important

# Set retention policy (optional)
zpbs-backup set retention=7d,4w,6m,1y tank/important-data

# Set priority (optional, lower = first, default 50)
zpbs-backup set priority=10 tank/critical-data
```

3. Run backups:

```bash
# See what would be backed up
zpbs-backup run --dry-run

# Run actual backups
zpbs-backup run

# Force backup regardless of schedule
zpbs-backup run --force
```

## ZFS Properties

| Property | Values | Default | Description |
|----------|--------|---------|-------------|
| `zpbs:backup` | `true` / `false` | inherit | Enable/disable backup |
| `zpbs:schedule` | `daily` / `weekly` / `monthly` | `daily` | Backup frequency |
| `zpbs:retention` | `7d,4w,6m,1y` | `7d,4w,6m,1y` | Retention policy |
| `zpbs:namespace` | string | auto-derived | PBS namespace |
| `zpbs:priority` | 1-100 | 50 | Lower = backup first |

### Inheritance

Properties are inherited through the ZFS dataset hierarchy. Set `backup=true` on a parent dataset to enable backup for all children, then selectively disable with `backup=false`.

```bash
# Enable backup for entire pool
zpbs-backup set backup=true tank

# Disable for specific dataset
zpbs-backup set backup=false tank/scratch

# Check inheritance
zpbs-backup get tank/data
```

## CLI Commands

### Show Config

Display PBS connection configuration, source, and verify connectivity:

```bash
zpbs-backup show-config              # Show active config + connection check
zpbs-backup show-config --verbose    # Show all config sources in priority order
zpbs-backup show-config --json       # Machine-parseable JSON (for automation)
```

### Status

Show backup status for all discovered datasets:

```bash
zpbs-backup status
zpbs-backup status --orphans  # Also show orphaned PBS backups
zpbs-backup status --json     # JSON output
```

### Run

Run backups for due datasets:

```bash
zpbs-backup run                      # Run all due backups
zpbs-backup run --dry-run            # Show what would happen
zpbs-backup run --force              # Ignore schedule, run all
zpbs-backup run --dataset 'tank/*'   # Only matching datasets
zpbs-backup run --no-notify          # Skip email notification
```

### Audit

Compare PBS backups with ZFS datasets:

```bash
zpbs-backup audit
```

Reports:
- Datasets with `backup=true` that have never been backed up
- Backup groups in PBS with no matching ZFS dataset (orphans)

### Prune

Apply retention policies:

```bash
zpbs-backup prune
zpbs-backup prune --dry-run
zpbs-backup prune --dataset 'tank/*'
```

### Property Management

```bash
# Get properties
zpbs-backup get tank/data
zpbs-backup get tank/data backup

# Set properties
zpbs-backup set backup=true tank/data
zpbs-backup set schedule=weekly tank/data

# Clear properties (inherit from parent)
zpbs-backup inherit schedule tank/data
zpbs-backup inherit -r all tank/data  # Recursive, clear all
```

### Test Notifications

```bash
zpbs-backup send-test-notification              # Send a test notification
zpbs-backup send-test-notification --show-only  # Preview without sending
```

## Systemd Integration

If you installed via `.deb` or `.rpm`, systemd units are already in place. Just start the timer after configuring PBS:

```bash
sudo systemctl start zpbs-backup.timer
systemctl list-timers zpbs-backup.timer
```

For pip/source installs, copy the units manually:

```bash
sudo cp systemd/zpbs-backup.service /etc/systemd/system/
sudo cp systemd/zpbs-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zpbs-backup.timer
```

The timer runs daily at 2:00 AM with a random delay up to 15 minutes.

## Migration from pbs-backup-all

If you're migrating from the bash-based `pbs-backup-all` scripts:

1. Install zpbs-backup alongside existing scripts
2. Set ZFS properties on datasets that were in your `DATASETS_*` arrays:

```bash
# For each dataset in DATASETS_FAST/DATASETS_BULK
zpbs-backup set backup=true tank/files
zpbs-backup set priority=10 tank/files  # Lower for "fast" datasets
```

3. Verify discovery matches:

```bash
zpbs-backup status
```

4. Test with dry-run:

```bash
zpbs-backup run --dry-run
```

5. Run a manual backup:

```bash
zpbs-backup run
```

6. Once confident, switch timers:

```bash
sudo systemctl disable pbs-backup.timer
sudo systemctl enable zpbs-backup.timer
sudo systemctl start zpbs-backup.timer
```

## Backup ID Format

Backup IDs are generated as `{hostname}-{dataset-with-slashes-replaced}`:
- Dataset `tank/files/downloads` on host `storage-server`
- Backup ID: `storage-server-tank-files-downloads`

This maintains compatibility with existing backup IDs from the bash scripts.

## Namespace Strategy

By default, namespaces are auto-derived as `{hostname}/{pool}/{dataset-path}`:
- Dataset `tank/files/downloads` on host `storage-server`
- Namespace: `storage-server/tank/files/downloads`

The API token needs `DatastoreAdmin` permission to create namespaces automatically.

Override with explicit property:

```bash
zpbs-backup set namespace=production/data tank/files
```

## Notifications

Email notifications are sent on backup completion. Configure via:

- Environment: `ZPBS_NOTIFY_EMAIL=admin@example.com`
- External script: `/usr/local/bin/pbs-send-notification` (for compatibility)

Test and manage notifications:

```bash
zpbs-backup send-test-notification   # Verify notification delivery
zpbs-backup run --no-notify          # Skip notification for this run
export ZPBS_NOTIFY=false             # Disable notifications globally
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=zpbs_backup
```

## License

MIT License - see [LICENSE](LICENSE) for details.
