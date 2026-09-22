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
- **Skip-unchanged**: Uses ZFS `written` and snapshot creation time to skip datasets that are byte-identical to their last backup — no PBS round-trip for dormant data
- **Priority ordering**: Back up critical data first
- **Retention policies**: Per-dataset retention settings
- **Dry-run mode**: See what would happen without making changes
- **Audit mode**: Find orphaned backups and never-backed-up datasets

## Installation

<details open>
<summary><strong>📦 Debian / Ubuntu / Proxmox VE (.deb)</strong></summary>

Download the latest `.deb` from the [Releases](https://github.com/ndemarco/zpbs-backup/releases) page:

```bash
sudo dpkg -i zpbs-backup_<version>_amd64.deb
sudo apt-get install -f  # Install any missing dependencies
```

This installs everything: CLI, systemd timer, and config directory. The timer is enabled automatically -- configure PBS credentials at `/etc/zpbs-backup/pbs.conf`, then start the timer:

```bash
sudo systemctl start zpbs-backup.timer
```

</details>

<details>
<summary><strong>📦 RHEL / Rocky / Alma (.rpm)</strong></summary>

Download the latest `.rpm` from the [Releases](https://github.com/ndemarco/zpbs-backup/releases) page:

```bash
sudo rpm -i zpbs-backup-<version>.x86_64.rpm
```

Requires `python3.11` from AppStream:

```bash
sudo dnf install python3.11
```

Configure PBS credentials at `/etc/zpbs-backup/pbs.conf`, then start the timer:

```bash
sudo systemctl start zpbs-backup.timer
```

</details>

<details>
<summary><strong>🐍 PyPI (pip / pipx)</strong></summary>

```bash
pip install zpbs-backup
# or
pipx install zpbs-backup
```

When installing via pip/pipx, systemd units are not installed automatically. After installation:

1. Configure PBS credentials (environment variables or config file)
2. See [Systemd Integration](#systemd-integration) below for manual setup

</details>

<details>
<summary><strong>📝 From Source</strong></summary>

```bash
git clone https://github.com/ndemarco/zpbs-backup.git
cd zpbs-backup
pip install .
```

Then configure PBS credentials and set up systemd units as described in [Systemd Integration](#systemd-integration).

</details>

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

# Omit following if you don't want encryption enabled.
export PBS_ENCRYPTION_KEYFILE="/path/to/key.enc"

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

# Omit following if you don't want encryption enabled.
PBS_ENCRYPTION_KEYFILE="/path/to/key.enc"

# Shell variable interpolation is supported:
# PBS_REPOSITORY="${PBS_USER}!${PBS_API_TOKEN_NAME}@${PBS_SERVER}:${PBS_DATASTORE}"
```

> **Do not mix environment variables and the configuration file.** If
> `PBS_REPOSITORY` (or its individual parts) is set in the environment, the
> configuration file is ignored entirely. Define all variables in one place.

### Encryption

Setting `PBS_ENCRYPTION_KEYFILE` passes `--keyfile` to `proxmox-backup-client`,
so every backup is encrypted client-side with that key.

- **Keep a copy of the key off this host.** Without it, encrypted backups
  cannot be restored. `proxmox-backup-client key paperkey` prints a
  recoverable paper copy.
- **Scheduled runs cannot prompt for a passphrase.** Either create the key
  without one (`proxmox-backup-client key create --kdf none /path/to/key.enc`)
  or set `PBS_ENCRYPTION_PASSWORD` in the systemd unit's environment. The
  config file does not pass `PBS_ENCRYPTION_PASSWORD` through.

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

Datasets with `canmount=off` (used for organizational hierarchy — they hold no data themselves) are automatically skipped during backup. Their child datasets with their own mountpoints are backed up normally.

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
zpbs-backup run -n                   # Show what would happen (--dry-run)
zpbs-backup run -f                   # Ignore schedule, run all (--force)
zpbs-backup run -d 'tank/*'         # Only matching datasets (--dataset)
zpbs-backup run --no-notify          # Skip email notification
zpbs-backup run -b                   # Run in background via systemd (--bg)
```

A run skips any due dataset where ZFS reports `written=0` and the most recent
snapshot is at least 60s older than the last successful backup. The skip is
disabled automatically if a pre-flight check finds clock skew between this host
and PBS larger than the 60s safety margin; `--force` bypasses both checks.

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

`zpbs:retention` is not enforced until something runs `prune`. The package
ships a timer for it, **installed but disabled**, because prune deletes real
backups and opting in is your decision, not the installer's.

See what it would delete before enabling anything:

```bash
zpbs-backup prune --dry-run
```

Then opt in:

```bash
systemctl enable --now zpbs-backup-prune.timer
systemctl list-timers zpbs-backup-prune.timer
```

The timer runs daily at 05:00 with up to 15 minutes of jitter, well clear of
the 02:00 backup window. Prune takes the same exclusive lock as a backup run,
so it will not delete from a group currently being written to; if a backup is
still running it defers with status 75, which the unit treats as success
rather than a failure. `prune --dry-run` neither takes the lock nor waits on
it.

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
zpbs-backup run -n
```

5. Run a manual backup:

```bash
zpbs-backup run -b  # background via systemd
```

6. Once confident, switch timers:

```bash
sudo systemctl disable pbs-backup.timer
sudo systemctl enable zpbs-backup.timer
sudo systemctl start zpbs-backup.timer
```

## Backup ID Format

Backup IDs are generated as `{hostname}{sep}{dataset-with-slashes-replaced}`,
where `{sep}` is the configurable **backup-id separator** (default `-`):
- Dataset `tank/files/downloads` on host `storage-server`
- Backup ID: `storage-server-tank-files-downloads`

This default maintains compatibility with existing backup IDs from the bash
scripts.

### Reversible IDs (`ZPBS_BACKUP_ID_SEPARATOR`)

Because the default separator (`-`) is also a legal character in ZFS dataset
names, the default ID is **not reversible**: a `-` in the ID may be a path
separator or part of a name (`pool/a-b` and `pool/a/b` both flatten to
`pool-a-b`). If you need self-describing, unambiguous IDs — e.g. to recover the
dataset layout from PBS group names alone during disaster recovery — set a
multi-character separator that your dataset names do not contain:

```bash
# environment or /etc/zpbs-backup/pbs.conf
export ZPBS_BACKUP_ID_SEPARATOR="--"
```

- Dataset `local-hdd/encrypted/device-backups` on host `s1-nas01`
- Backup ID: `s1-nas01--local-hdd--encrypted--device-backups`
- Recover the path: strip the host, then split the remainder on `--`.

The separator joins the hostname prefix too, so the whole ID splits uniformly.
It must be non-empty and use only PBS-backup-id characters (`[A-Za-z0-9_.-]`).
Reversibility holds only while no dataset name component contains the separator
or leads/trails with `-`. **Changing the separator changes every ID**, so
existing backup groups become orphans (prune them with
`proxmox-backup-client` and re-seed).

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

## Metrics

Run metrics (last run timestamp, last success timestamp, duration, and
successful/failed/skipped dataset counts, with skips broken down by cause)
can be reported two ways, either or both:

- Environment: `ZPBS_PUSHGATEWAY=http://10.0.16.16:9091` — push to a
  Prometheus Pushgateway after each run.
- Environment: `ZPBS_TEXTFILE_DIR=/var/lib/node_exporter/textfile_collector`
  — write `zpbs_backup.prom` to a node_exporter textfile collector
  directory (atomic write: temp file plus `os.replace`) for hosts that can't
  reach a Pushgateway.

Both are unset by default (no-op). If neither is set, no metrics are
reported.

Metrics and notifications are independent. `ZPBS_NOTIFY=false` and
`zpbs-backup run --no-notify` silence email only; metrics are still
reported. Likewise, reporting no metrics does not affect email.

Under the packaged systemd unit, `ProtectSystem=strict` means every writable
path is declared explicitly. node_exporter's default collector directory,
`/var/lib/node_exporter/textfile_collector`, is already allowed. A collector
directory elsewhere needs a drop-in:

```ini
# /etc/systemd/system/zpbs-backup.service.d/textfile.conf
[Service]
ReadWritePaths=-/srv/metrics/textfile_collector
```

## Concurrent runs

A run takes an exclusive lock on `/var/lib/zpbs-backup/run.lock`, covering
both the timer and a manual `zpbs-backup run`. A second run exits
immediately with status 75 and backs up nothing; the systemd unit treats
that status as success, so an overlap is not reported as a failed backup.

The lock is held by the process, not the file, so it is released even if the
holder is killed outright, and a leftover lock file blocks nothing.
`--dry-run` neither takes the lock nor waits on it.

### Series

| Metric | Type | Meaning |
| --- | --- | --- |
| `zpbs_backup_last_run_timestamp_seconds` | gauge | When the most recent run ended |
| `zpbs_backup_last_success_timestamp_seconds` | gauge | When the last fully successful run ended |
| `zpbs_backup_duration_seconds` | gauge | How long the run took |
| `zpbs_backup_datasets_successful` | gauge | Datasets backed up |
| `zpbs_backup_datasets_failed` | gauge | Datasets that failed |
| `zpbs_backup_datasets_skipped` | gauge | Datasets skipped, all causes |
| `zpbs_backup_datasets_skipped_by_cause{cause="…"}` | gauge | Datasets skipped, split by cause |

`zpbs_backup_datasets_skipped` counts every skip together, which cannot tell
a dataset that merely is not due yet from one that has silently lost its
mountpoint. `zpbs_backup_datasets_skipped_by_cause` splits that total four
ways, and the two always agree:

| `cause` | Meaning |
| --- | --- |
| `not_due` | The schedule says it is not time yet |
| `unchanged` | Provably identical to the last backup (`written=0`) |
| `no_mountpoint` | `mountpoint` is `none`, `legacy` or unset |
| `not_mounted` | Not mounted, including `canmount=off` |

All four label values are emitted on every run, reporting zero when no
dataset had that cause. A series that disappeared at zero would go stale in
Prometheus rather than read zero, which is the opposite of what an alert on
it needs.

`zpbs_backup_datasets_skipped` keeps its existing unlabelled form, so queries
and alert rules written against it are unaffected.

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=zpbs_backup

# Lint and type check
ruff check .
mypy
```

Every pull request and every branch push runs those three in GitHub Actions
(`.github/workflows/ci.yml`), and all three must pass. `ruff check --fix`
applies the mechanical fixes.

## License

MIT License - see [LICENSE](LICENSE) for details.
