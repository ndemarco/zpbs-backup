# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.12.0] - 2026-11-09

### Breaking
- The PBS connection is no longer read from `pbs.conf`, `~/.config/zpbs-backup/pbs.conf` or `PBS_*` environment variables. Repository, fingerprint, key file and ID separator are inherited ZFS properties; the token secret is in `/etc/zpbs-backup/credentials`. Run `zpbs-backup migrate-config` before upgrading (see Migration).
- The packaged systemd units no longer load `pbs.conf` as an `EnvironmentFile`. `ZPBS_PUSHGATEWAY`, `ZPBS_TEXTFILE_DIR`, `ZPBS_SYSLOG` and `ZPBS_NOTIFY` are set with `Environment=` in a systemd drop-in.
- A backup-enabled dataset with no `zpbs:repository`, or whose repository has no credentials entry, is a failed dataset (results, exit code, metrics). No server is contacted for it.

### Added
- `zpbs:repository` (`user@realm!tokenname@server:datastore`), `zpbs:fingerprint`, `zpbs:keyfile` and `zpbs:id-separator` ZFS properties, inherited, normally set on a pool root. All four are accepted and validated by `zpbs-backup set`.
- Credentials file `/etc/zpbs-backup/credentials` (INI; section name is the exact repository string, key `secret`). It is refused unless owned by root and not group- or world-accessible. A secret is used only for the exact repository it is listed under, so a user with delegated `zfs allow` property rights cannot redirect the host's token to their own server.
- Pools and datasets may target different repositories. `run` and `prune` group datasets by repository and use one client per repository; an unreachable repository fails only its own datasets.
- `zpbs-backup status --connections` (with `--json`): per backup-enabled dataset, the effective repository, fingerprint, key file and separator with the dataset each is inherited from, and whether a credentials entry exists. It never prints a secret and contacts no server.
- `zpbs-backup check`: one login per repository used by a backup-enabled dataset. A repository with no credentials entry is reported as failed and not contacted. Non-zero exit if anything fails.
- `zpbs-backup migrate-config`: reads `pbs.conf`, writes the credentials file (never overwriting an existing one), prints `zfs set` commands for the pool roots (runs them only with `--apply`), prints `ZPBS_*` reporting settings as `Environment=` lines, and leaves `pbs.conf` in place.
- Separator-change guard: each backup records its `zpbs:id-separator` in the snapshot's PBS notes. If the newest snapshot's recorded separator differs from the dataset's effective one, that dataset fails with an error naming both values until the property is reverted. Snapshots without a record (made before 0.12.0) are accepted. There is no command to rename existing groups in 0.12.0.

### Removed
- `pbs.conf` loading (`/etc/zpbs-backup/pbs.conf`, `~/.config/zpbs-backup/pbs.conf`) and the `PBS_*` connection variables: `PBS_REPOSITORY`, `PBS_USER`, `PBS_API_TOKEN_NAME`, `PBS_SERVER`, `PBS_DATASTORE`, `PBS_API_TOKEN_SECRET`, `PBS_PASSWORD`, `PBS_FINGERPRINT`, `PBS_ENCRYPTION_KEYFILE`, and the legacy `REPOSITORY`, `PASSWORD`, `FINGERPRINT` aliases.
- `ZPBS_BACKUP_ID_SEPARATOR`; use `zpbs:id-separator`.
- `show-config`; use `status --connections` and `check`.

### Migration from 0.11.0
1. On each host, before upgrading the package: `sudo zpbs-backup migrate-config`. It writes `/etc/zpbs-backup/credentials` and prints the `zfs set` commands for your pool roots.
2. Apply the printed commands, or rerun with `--apply`: `sudo zpbs-backup migrate-config --apply`.
3. If it printed `Environment=` lines (`ZPBS_PUSHGATEWAY` and the like), put them under `[Service]` in `sudo systemctl edit zpbs-backup.service`.
4. Upgrade the package, then verify: `zpbs-backup status --connections` and `zpbs-backup check`.
5. Delete `/etc/zpbs-backup/pbs.conf` once the check passes.
6. If you used `ZPBS_BACKUP_ID_SEPARATOR`, `migrate-config` sets `zpbs:id-separator` to the same value, so existing backup groups keep their IDs.

## [0.11.0] - 2026-11-09

### Added
- `zpbs:change-detection` ZFS property (`legacy`, `data` or `metadata`), passed to `proxmox-backup-client` as `--change-detection-mode` for that dataset. Scheduled runs now honour a change-detection mode; before this, the mode could be set only with `run --change-detection-mode`, which the systemd timer never passes ([#1](https://github.com/ndemarco/zpbs-backup/issues/1)). The command-line flag still works and overrides the property for that run. A malformed value set with raw `zfs set` is reported and that dataset excluded, like the other zpbs properties. `status --json` gains a `change_detection` field.
- `zpbs_backup_dataset_last_backup_timestamp_seconds{dataset="<zfs dataset>"}`: Unix time of each dataset's newest snapshot on the backup server (after this run's backup if it succeeded, otherwise the newest existing one). A dataset with no snapshot has no series. `run --force` now reads the newest snapshot too, so the metric is present on forced runs.
- Unknown-property detection: each run logs a syslog warning (dataset and property name) for every locally set `zpbs:*` property zpbs-backup does not recognise, and exports `zpbs_backup_unknown_properties{dataset="..."}` with the count, only for datasets that have at least one. `status` lists them per dataset and `status --json` gains `unknown_properties`.
- Publishing to PyPI from the release workflow via trusted publishing, after `twine check` on the built wheel and sdist.

### Changed
- syslog is now controlled only by `ZPBS_SYSLOG`. Before, `ZPBS_NOTIFY=false` and `run --no-notify` also silenced syslog; they now silence only the hook script.
- `zpbs-backup set` rejects a property name it does not know, with an error listing the valid names. Before, any `zpbs:*` name was accepted.
- `set backup=false --clear` and `inherit all` clear every locally set `zpbs:*` property on the dataset, not only the known ones.
- The run's metrics are rendered once; the Pushgateway and the textfile receive identical content.

### Removed
- **Breaking:** the `zpbs_backup_last_success_timestamp_seconds` metric and its state file `/var/lib/zpbs-backup/state.json`. The file is no longer read or written; delete it by hand if you want it gone.
- The hidden `zpbs-backup notify` command group. Use `send-test-notification` or `show-config`.
- The `/usr/local/bin/pbs-send-notification` hook path. Rename the script to `/usr/local/bin/zpbs-send-notification`.
- The lock file no longer holds a process ID.
- `.gitlab-ci.yml` and `MANIFEST.in`.

### Breaking: stale-backup alerts
Alerts on `zpbs_backup_last_success_timestamp_seconds` will go silent, because the series no longer exists. Replace them with a per-dataset rule:

```yaml
- alert: ZpbsBackupStale
  expr: time() - zpbs_backup_dataset_last_backup_timestamp_seconds > 2 * 86400
  for: 15m
  annotations:
    summary: "No backup of {{ $labels.dataset }} on {{ $labels.instance }} for over 2 days"
```

Datasets on a `weekly` or `monthly` schedule need a longer threshold (for example 9 days and 35 days). Pair it with an alert for runs that stop happening:

```yaml
- alert: ZpbsBackupRunMissing
  expr: absent(zpbs_backup_last_run_timestamp_seconds)
  for: 15m
```

## [0.10.0] - 2026-09-23

### Added
- Continuous integration on every pull request and every branch push (`.github/workflows/ci.yml`): the test suite, `ruff` and `mypy` each run as their own required check. Before this, a workflow existed only for tag pushes, so a pull request reported no checks at all. `ruff` and `mypy` are configured in `pyproject.toml`; the repository passes all three.
- Configurable backup-id separator via `ZPBS_BACKUP_ID_SEPARATOR` (env or config file; default `-`). A multi-character separator such as `--` yields **reversible** backup-ids — you can recover the dataset path from the PBS group name by stripping the host and splitting on the separator — which the default `-` cannot guarantee, since `-` is also legal in ZFS dataset names (`pool/a-b` and `pool/a/b` both flatten to `pool-a-b`). The separator joins the hostname prefix too, so IDs split uniformly; it is validated against the PBS backup-id character set. Default behavior is unchanged.

### Removed
- **Breaking:** zpbs-backup no longer sends mail itself. `ZPBS_NOTIFY_EMAIL` and the `sendmail`/`mail` fallback are gone; a reader using it should install a script at `/usr/local/bin/zpbs-send-notification` instead (see the README's Notifications section). syslog, the script hook, `ZPBS_NOTIFY`/`--no-notify`, and `send-test-notification` are unaffected.

## [0.9.1] - 2026-09-19

### Fixed
- `zpbs_backup.prom` is now written mode `0644`. It was `0600` (the `mkstemp` default), so node_exporter, running as its own unprivileged user, could not read it and the metrics never reached Prometheus.

## [0.9.0] - 2026-09-18

### Added
- `ZPBS_TEXTFILE_DIR` environment variable: set to a node_exporter textfile collector directory to write run metrics to `<dir>/zpbs_backup.prom` instead of (or alongside) pushing to a Pushgateway. Same six metric names as the Pushgateway path. The write is atomic (temp file plus `os.replace`).
- `ZPBS_TEXTFILE_DIR` added to the diagnostic env-var display list next to `ZPBS_PUSHGATEWAY`.

### Notes
- `ZPBS_PUSHGATEWAY` and `ZPBS_TEXTFILE_DIR` are independent: set either, both, or neither. A missing or unwritable textfile directory logs an error to stderr and never fails the backup run.

## [0.8.0] - 2026-05-20

### Added
- Skip-unchanged optimization (default on): the `run` command skips datasets whose live filesystem is byte-identical to a snapshot already captured by the previous backup. Uses native ZFS `written` property and snapshot creation time — no state, no extra IO. For pools of hundreds of mostly-dormant datasets this elides nearly all PBS calls.
- Pre-flight clock-skew probe against the PBS HTTP `Date:` header. If skew exceeds the 60s safety margin (or the probe fails), the skip-unchanged optimization is disabled for that run and the tool falls back to schedule-only behavior. Never blocks the run.

### Notes
- `--force` continues to back up everything regardless of either check.
- The skew check is one HTTPS HEAD per run; not user-configurable.

## [0.7.0] - 2026-05-14

### Added
- `--change-detection-mode` option on `run`, passed through to `proxmox-backup-client`: `legacy` (full content read and hash comparison), `data` (content-based), or `metadata` (inode/mtime-based, fastest on large datasets). Unset leaves the client's own default in place.
- Prometheus Pushgateway integration: push 6 metrics after each backup run (last run timestamp, last success timestamp, duration, successful/failed/skipped dataset counts)
- `ZPBS_PUSHGATEWAY` environment variable: set to Pushgateway base URL to enable metrics; unset or empty disables (no-op)
- State persistence at `/var/lib/zpbs-backup/state.json`: `last_success_timestamp_seconds` survives failed runs so the staleness alert reflects the true last success
- `ZPBS_PUSHGATEWAY` added to diagnostic env-var display list
- GitHub Actions release workflow: a `v*` tag builds the wheel and the `.deb`/`.rpm` packages and publishes them as release assets.

### Notes
- The metrics and state-persistence work above was written against a `0.6.0` version bump that was never tagged or released. It first reached users here, in 0.7.0. No 0.6.0 release exists; a package reporting that version was built by hand from an untagged commit.

## [0.5.2] - 2026-02-12

### Fixed
- `status` showing "683 months ago" or "never" for recently backed-up datasets — PBS `list` returns `last-backup` field, not `backup-time`; now checks both
- Timestamp parsing handles missing, zero, or string (ISO 8601) values from PBS

### Added
- Automatically skip datasets with `canmount=off` or `mounted=no` (organizational/hierarchy-only datasets that hold no data)

## [0.5.0] - 2026-02-09

### Added
- `run --bg` / `run -b`: trigger backup in background via systemd, returns immediately
- POSIX short options for all commands: `-n` (dry-run), `-f` (force), `-d` (dataset), `-j` (json), `-v` (verbose), `-b` (background), `-c` (clear), `-s` (show-only), `-o` (orphans), `-r` (recursive)

## [0.4.2] - 2026-02-09

### Fixed
- Use actual ZFS mountpoint instead of assuming `/{dataset_name}` — fixes backup failures for datasets whose mountpoint differs from their name
- Skip datasets with `mountpoint=none` or `mountpoint=legacy` instead of failing

## [0.4.1] - 2026-02-08

### Changed
- `get` command: argument order changed to `get DATASET [PROPERTY]` — property defaults to showing all
- CLI hides `zpbs:` prefix from all user-facing output (use short names: backup, schedule, etc.)
- Permission denied errors now suggest using sudo
- Helpful hint on PBS privilege separation when connection check fails

## [0.4.0] - 2026-02-08

### Added
- `show-config` command: display active PBS configuration, source, and connectivity check
  - `--verbose` flag shows all config sources in priority order
  - `--json` flag for machine-parseable output (suitable for automation)
- `send-test-notification` command (replaces `notify test`)
- New config variables aligned with PBS API token terminology:
  - `PBS_API_TOKEN_SECRET` (preferred over `PBS_PASSWORD`)
  - `PBS_USER`, `PBS_API_TOKEN_NAME`, `PBS_SERVER`, `PBS_DATASTORE` (individual parts)
- Auto-composition of `PBS_REPOSITORY` from individual parts when not set explicitly
- Config source tracking: shows which file or env var provided each setting
- Per-user config file: `~/.config/zpbs-backup/pbs.conf`
- 40 new tests for config module

### Changed
- Config file search simplified to 2 locations: `~/.config/zpbs-backup/pbs.conf` and `/etc/zpbs-backup/pbs.conf`
- Removed legacy `/root` config file paths
- `notify test` and `notify config` are now hidden deprecated aliases

### Deprecated
- `notify test` — use `send-test-notification` instead
- `notify config` — use `show-config` instead

## [0.3.2] - 2026-02-06

### Added
- Upfront PBS connectivity check in status, audit, and run commands
- 30-second timeout on all PBS query commands (prevents indefinite hangs)

### Fixed
- Commands no longer hang when PBS server is unreachable

## [0.3.1] - 2026-02-06

### Fixed
- Graceful handling when non-root user lacks permission to read config files
- PBS namespace listing crash when API returns plain strings instead of objects

## [0.3.0] - 2026-02-06

### Added
- Native `.deb` and `.rpm` package builds via nfpm
- Makefile with `make packages`, `make deb`, `make rpm` targets
- GitLab CI/CD pipeline for building and publishing system packages
- GitLab Releases with downloadable `.deb` and `.rpm` assets
- GitHub Releases with `.deb` and `.rpm` assets (via GitLab CI mirror)
- Support for both amd64 and arm64 architectures

### Changed
- Version is now derived from `__init__.py` as single source of truth
  (`pyproject.toml` uses dynamic versioning via hatchling)

## [0.2.0] - 2026-02-02

### Added
- Syslog support for centralized logging
- Email notification system with `notify` command group
  - `zpbs-backup notify test` - Test notification configuration
  - `zpbs-backup notify config` - Show notification settings
- Shell variable interpolation in PBS configuration files
- Hierarchical namespace creation (auto-creates parent namespaces)
- `--no-notify` flag for `run` command

### Changed
- Configuration files now support variable interpolation (e.g., `${PBS_USER}`)
- Improved error handling and logging throughout

## [0.1.0] - Initial Release

### Added
- Initial implementation of zpbs-backup
- Auto-discovery of ZFS datasets via custom properties
- Property inheritance through dataset hierarchy
- Schedule-aware backups (daily/weekly/monthly)
- Priority-based backup ordering
- Retention policy management
- Dry-run mode for all operations
- Audit mode for orphaned backups and missed datasets
- CLI commands: status, run, audit, prune, get, set, inherit
- Systemd service and timer units
- Configuration via environment variables or config files
- PBS namespace support with auto-derivation
