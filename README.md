# zpbs-backup

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

```bash
# Install with pip
pip install .

# Or install in development mode
pip install -e ".[dev]"
```

## Quick Start

1. Configure PBS connection:

```bash
# Option 1: Environment variables
export PBS_REPOSITORY="user@pbs!token@server:datastore"
export PBS_PASSWORD="your-api-token-secret"
export PBS_FINGERPRINT="..."

# Option 2: Config file (/etc/zpbs-backup/pbs.conf)
PBS_REPOSITORY="user@pbs!token@server:datastore"
PBS_PASSWORD="your-api-token-secret"
PBS_FINGERPRINT="..."
```

2. Enable backup on datasets:

```bash
# Enable backup for a dataset
zpbs-backup set zpbs:backup=true tank/important-data

# Set schedule (optional, default is daily)
zpbs-backup set zpbs:schedule=weekly tank/less-important

# Set retention policy (optional)
zpbs-backup set zpbs:retention=7d,4w,6m,1y tank/important-data

# Set priority (optional, lower = first, default 50)
zpbs-backup set zpbs:priority=10 tank/critical-data
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

Properties are inherited through the ZFS dataset hierarchy. Set `zpbs:backup=true` on a parent dataset to enable backup for all children, then selectively disable with `zpbs:backup=false`.

```bash
# Enable backup for entire pool
zpbs-backup set zpbs:backup=true tank

# Disable for specific dataset
zpbs-backup set zpbs:backup=false tank/scratch

# Check inheritance
zpbs-backup get all tank/data
```

## CLI Commands

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
- Datasets with `zpbs:backup=true` that have never been backed up
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
zpbs-backup get zpbs:backup tank/data
zpbs-backup get all tank/data

# Set properties
zpbs-backup set zpbs:backup=true tank/data
zpbs-backup set schedule=weekly tank/data   # zpbs: prefix optional

# Clear properties (inherit from parent)
zpbs-backup inherit zpbs:schedule tank/data
zpbs-backup inherit -r all tank/data  # Recursive, clear all
```

## Systemd Integration

Install the systemd units for scheduled operation:

```bash
# Copy units
sudo cp systemd/zpbs-backup.service /etc/systemd/system/
sudo cp systemd/zpbs-backup.timer /etc/systemd/system/

# Enable and start timer
sudo systemctl daemon-reload
sudo systemctl enable zpbs-backup.timer
sudo systemctl start zpbs-backup.timer

# Check status
systemctl list-timers zpbs-backup.timer
```

The timer runs daily at 2:00 AM with a random delay up to 15 minutes.

## Migration from pbs-backup-all

If you're migrating from the bash-based `pbs-backup-all` scripts:

1. Install zpbs-backup alongside existing scripts
2. Set ZFS properties on datasets that were in your `DATASETS_*` arrays:

```bash
# For each dataset in DATASETS_FAST/DATASETS_BULK
zpbs-backup set zpbs:backup=true tank/files
zpbs-backup set zpbs:priority=10 tank/files  # Lower for "fast" datasets
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

Override with explicit property:

```bash
zpbs-backup set zpbs:namespace=production/data tank/files
```

## Notifications

Email notifications are sent on backup completion. Configure via:

- Environment: `ZPBS_NOTIFY_EMAIL=admin@example.com`
- External script: `/usr/local/bin/pbs-send-notification` (for compatibility)

Disable notifications:

```bash
zpbs-backup run --no-notify
export ZPBS_NOTIFY=false
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
