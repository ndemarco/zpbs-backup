# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
