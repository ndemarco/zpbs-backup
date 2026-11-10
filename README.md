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

This installs everything: CLI, systemd timer, and config directory. The timer is enabled automatically -- set the repository property and credentials file (see Quick Start), then start the timer:

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

Set the repository properties and credentials file (see Quick Start), then start the timer:

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
> Permissions > Add -- grant **DatastoreAdmin** on `/datastore/yourstore` to both.

See [Connection Settings](#connection-settings). Upgrading from 0.11.0: run `sudo zpbs-backup migrate-config`.

Set the repository and fingerprint once, on the pool root. Every dataset in the pool inherits them:

```bash
sudo zpbs-backup set repository=backup@pbs!mytoken@pbs.example.com:backups tank
sudo zpbs-backup set fingerprint=AA:BB:CC:... tank
```

Put the token secret in the root-only credentials file `/etc/zpbs-backup/credentials` (mode `0600`), bound to that exact repository:

```ini
[backup@pbs!mytoken@pbs.example.com:backups]
secret = xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

### Encryption

Set `zpbs:keyfile` to the path of a key file and every backup of the dataset
(and of its descendants, which inherit it) is encrypted client-side with that
key. Unset means no encryption.

```bash
sudo zpbs-backup set keyfile=/etc/zpbs-backup/key.enc tank
```

- **Keep a copy of the key off this host.** Without it, encrypted backups
  cannot be restored. `proxmox-backup-client key paperkey` prints a
  recoverable paper copy.
- **Scheduled runs cannot prompt for a passphrase.** Either create the key
  without one (`proxmox-backup-client key create --kdf none /etc/zpbs-backup/key.enc`)
  or set `PBS_ENCRYPTION_PASSWORD` in the systemd unit's environment
  (`Environment=` in a drop-in). zpbs-backup passes the environment through
  to `proxmox-backup-client` unchanged.

Verify your configuration:

```bash
zpbs-backup status --connections
zpbs-backup check
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
| `zpbs:change-detection` | `legacy` / `data` / `metadata` | client default (`legacy`) | How `proxmox-backup-client` finds changed files |
| `zpbs:repository` | `user@realm!token@server:datastore` | none (required) | PBS repository the dataset backs up to; inherited |
| `zpbs:fingerprint` | `AA:BB:...` | none | PBS server certificate fingerprint; inherited |
| `zpbs:keyfile` | absolute path | none (no encryption) | Encryption key file; inherited |
| `zpbs:id-separator` | `[A-Za-z0-9_.-]+` | `-` | Backup ID separator; inherited. See [Backup ID Format](#backup-id-format) |

`zpbs-backup set` accepts only these names and rejects any other with an error
listing the valid ones. A `zpbs:` property set by other means (for example a
typo made with raw `zfs set`, such as `zpbs:backups`) does nothing, so each run
reports it: a warning in the run output and in syslog naming the dataset and
property, and the `zpbs_backup_unknown_properties` metric. Only properties set
locally on a dataset count, and `zpbs-backup status` lists them per dataset.

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
zpbs-backup status --connections     # Effective repository, fingerprint, keyfile, separator per dataset
zpbs-backup check                    # One login per repository in use
sudo zpbs-backup migrate-config      # Convert a 0.11.0 pbs.conf
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
zpbs-backup run --no-notify          # Skip the notification hook
zpbs-backup run -b                   # Run in background via systemd (--bg)
zpbs-backup run --change-detection-mode metadata  # Override every dataset's mode
```

`zpbs:change-detection` chooses how `proxmox-backup-client` finds what changed
since the previous backup. `legacy` reads and hashes every file on every run.
`metadata` compares file metadata against the previous snapshot and rereads
only files that differ, which is much faster on large, rarely modified
datasets; `data` writes the same split archive format but still reads
everything. The first `metadata` run after switching has no split-archive
snapshot to compare with, so it reads everything once. Set it per dataset so
scheduled runs use it:

```bash
zpbs-backup set change-detection=metadata tank/media
```

`--change-detection-mode` on `run` overrides the property for that run only.

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
zpbs-backup set change-detection=metadata tank/data

# Clear properties (inherit from parent)
zpbs-backup inherit schedule tank/data
zpbs-backup inherit -r all tank/data  # Recursive, clear all
zpbs-backup set backup=false --clear tank/data  # Disable and clear all
```

`inherit all` and `set backup=false --clear` remove every `zpbs:` property
set locally on the dataset, including unknown ones. With `-r` they also
cover its descendants.

### Test Notifications

```bash
zpbs-backup send-test-notification              # Send a test notification
zpbs-backup send-test-notification --show-only  # Preview without sending
```

## Connection Settings

Dataset configuration lives in ZFS properties; secrets live in one root-only file.

### Properties

`zpbs:repository` (`user@realm!tokenname@server:datastore`, the string `proxmox-backup-client` takes as `PBS_REPOSITORY`), `zpbs:fingerprint`, `zpbs:keyfile` and `zpbs:id-separator` are inherited like every other property, normally set on the pool root. Pools or datasets may point at different datastores. `run` and `prune` group datasets by repository and handle each group with one connection.

A backup-enabled dataset with no `zpbs:repository` counts as failed (in the run results, the exit code and the metrics) and no server is contacted for it.

### Credentials file

Secrets cannot be ZFS properties: any local user can read them with `zfs get all`. They go in `/etc/zpbs-backup/credentials`, INI format, owned by root, mode `0600`. Each section name is a repository string exactly as it appears in `zpbs:repository`:

```ini
[backup@pbs!mytoken@pbs.example.com:backups]
secret = xxxxxxxx-xxxx-xxxx-xxxxxxxxxxxx

[backup@pbs!other@pbs2.example.com:offsite]
secret = yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy
```

zpbs-backup refuses to read the file if it is not owned by root or is group- or world-accessible.

**Binding rule.** A secret is used only for the exact repository string it is listed under (string comparison, no normalisation). ZFS can delegate property writes to non-root users (`zfs allow`); without the binding, such a user could set `zpbs:repository` to their own server and receive the host's token. A dataset whose repository has no entry **fails closed**: it is reported as failed and no server is contacted.

### Reporting settings

`ZPBS_PUSHGATEWAY`, `ZPBS_TEXTFILE_DIR`, `ZPBS_SYSLOG` and `ZPBS_NOTIFY` are systemd environment settings (`Environment=` in a drop-in, `systemctl edit zpbs-backup.service`). The packaged units do not read `pbs.conf`. `PBS_REPOSITORY`, `PBS_PASSWORD` and the other `PBS_*` connection variables are not read by zpbs-backup.

### Commands

```bash
zpbs-backup status --connections   # per dataset: effective repository, fingerprint, keyfile, separator, each with its source dataset, and whether a credentials entry exists (never the secret)
zpbs-backup check                  # one login per referenced repository; reports ok / no credential / rejected
sudo zpbs-backup migrate-config    # convert pbs.conf into the credentials file plus properties
```

- `check` logs in once per repository used by a backup-enabled dataset. A repository with no credentials entry is reported as failed and not contacted. Exit status is non-zero if anything fails.
- `migrate-config` reads `/etc/zpbs-backup/pbs.conf` (`--pbs-conf` for another path), writes the credentials file (never overwriting an existing one), and prints the `zfs set` commands for the pool roots. It runs them only with `--apply`. `ZPBS_*` reporting settings it finds are printed as `Environment=` lines for a drop-in. It leaves `pbs.conf` in place; delete it once the properties are set.

### Backup ID separator

`zpbs:id-separator` replaces `ZPBS_BACKUP_ID_SEPARATOR`. Changing it would rename every backup group on the server, so a property edit alone does not do that. On each backup zpbs-backup records the separator in use in the snapshot's PBS notes (`zpbs:id-separator=<value>`). On the next run, if the newest snapshot's recorded separator differs from the dataset's effective separator, that dataset fails with a message naming both values, and nothing else happens. Set the property back to clear it. Snapshots with no recorded separator (made before 0.12.0) are accepted, and the new snapshot records its separator. There is no command to rename existing groups in 0.12.0.

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

### Reversible IDs (`zpbs:id-separator`)

Because the default separator (`-`) is also a legal character in ZFS dataset
names, the default ID is **not reversible**: a `-` in the ID may be a path
separator or part of a name (`pool/a-b` and `pool/a/b` both flatten to
`pool-a-b`). If you need self-describing, unambiguous IDs — e.g. to recover the
dataset layout from PBS group names alone during disaster recovery — set a
multi-character separator that your dataset names do not contain:

```bash
# property; inherited, set on the pool root
zpbs-backup set id-separator=-- tank
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

zpbs-backup doesn't send mail. It tells you three ways, and you wire up whichever suits the box:

- **syslog** — every run, to the journal. `journalctl -u zpbs-backup.service`.
- **exit status** — non-zero when a dataset failed, so a systemd `OnFailure=` drop-in catches it. (Exit 75 means another run held the lock. That's a deferral, and the unit already treats it as success.)
- **your own script** at `/usr/local/bin/zpbs-send-notification` — gets the summary on stdin, plus `ZPBS_SUBJECT`, `ZPBS_SUCCESSFUL`, `ZPBS_FAILED`, `ZPBS_SKIPPED` and `ZPBS_DURATION` in the environment. Send it wherever you like.

syslog is controlled only by `ZPBS_SYSLOG` (default `true`). `ZPBS_NOTIFY=false` and `run --no-notify` silence the hook script only; syslog still logs the run. Properties zpbs-backup does not recognise are logged to syslog as warnings, one per dataset and property. `zpbs-backup send-test-notification` still exercises it.

### Coming from `ZPBS_NOTIFY_EMAIL`

zpbs-backup emailing was out of scope and has been removed. The new way to email is via the hook:

```sh
#!/bin/sh
# /usr/local/bin/zpbs-send-notification
exec msmtp -t <<EOF
To: admin@example.com
Subject: $ZPBS_SUBJECT

$(cat)
EOF
```

One thing worth knowing: `OnFailure=` fires when a run ran and failed. Nothing here catches a run that never started — a masked timer, a wedged host. For that you want something watching from outside, which is what the metrics below are for.

## Metrics

Run metrics (last run timestamp, duration, successful/failed/skipped dataset
counts with skips broken down by cause, and per-dataset freshness) can be reported two ways, either or both:

- Environment: `ZPBS_PUSHGATEWAY=http://10.0.16.16:9091` — push to a
  Prometheus Pushgateway after each run.
- Environment: `ZPBS_TEXTFILE_DIR=/var/lib/node_exporter/textfile_collector`
  — write `zpbs_backup.prom` to a node_exporter textfile collector
  directory (atomic write: temp file plus `os.replace`) for hosts that can't
  reach a Pushgateway.

Both are unset by default (no-op). If neither is set, no metrics are
reported.

Metrics and notifications are independent. `ZPBS_NOTIFY=false` and
`zpbs-backup run --no-notify` silence the notification hook only; metrics
are still reported. Likewise, reporting no metrics does not affect
notifications.

Under the packaged systemd unit, `ProtectSystem=strict` means every writable
path is declared explicitly. node_exporter's default collector directory,
`/var/lib/node_exporter/textfile_collector`, is already allowed. A collector
directory elsewhere needs a drop-in:

```ini
# /etc/systemd/system/zpbs-backup.service.d/textfile.conf
[Service]
ReadWritePaths=-/srv/metrics/textfile_collector
```

### Series

| Metric | Type | Meaning |
| --- | --- | --- |
| `zpbs_backup_last_run_timestamp_seconds` | gauge | When the most recent run ended |
| `zpbs_backup_duration_seconds` | gauge | How long the run took |
| `zpbs_backup_datasets_successful` | gauge | Datasets backed up |
| `zpbs_backup_datasets_failed` | gauge | Datasets that failed |
| `zpbs_backup_datasets_skipped` | gauge | Datasets skipped, all causes |
| `zpbs_backup_datasets_skipped_by_cause{cause="…"}` | gauge | Datasets skipped, split by cause |
| `zpbs_backup_dataset_last_backup_timestamp_seconds{dataset="…"}` | gauge | Unix time of the dataset's newest snapshot on the backup server, after this run. Absent for a dataset with no snapshot |
| `zpbs_backup_unknown_properties{dataset="…"}` | gauge | Number of unrecognised `zpbs:` properties set on the dataset. Emitted only for datasets with at least one |

To alert on a stale backup, use the per-dataset series, for example
`time() - zpbs_backup_dataset_last_backup_timestamp_seconds > 2 * 86400`
(allow longer for `weekly` and `monthly` datasets), and pair it with an
`absent(zpbs_backup_last_run_timestamp_seconds)` alert for runs that stop
happening.
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

## Concurrent runs

A run takes an exclusive lock on `/var/lib/zpbs-backup/run.lock`, covering
both the timer and a manual `zpbs-backup run`. A second run exits
immediately with status 75 and backs up nothing; the systemd unit treats
that status as success, so an overlap is not reported as a failed backup.

The lock is held by the process, not the file, so it is released even if the
holder is killed outright, and a leftover lock file blocks nothing.
`--dry-run` neither takes the lock nor waits on it.

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
