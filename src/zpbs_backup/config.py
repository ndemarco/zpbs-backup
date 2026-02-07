"""PBS connection configuration management."""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PBSConfig:
    """Proxmox Backup Server connection configuration."""

    repository: str
    password: str | None = None
    fingerprint: str | None = None

    def get_env(self) -> dict[str, str]:
        """Return environment variables for proxmox-backup-client."""
        env = {"PBS_REPOSITORY": self.repository}
        if self.password:
            env["PBS_PASSWORD"] = self.password
        if self.fingerprint:
            env["PBS_FINGERPRINT"] = self.fingerprint
        return env


def get_hostname() -> str:
    """Return the system hostname (short form)."""
    return socket.gethostname().split(".")[0]


def load_config() -> PBSConfig:
    """Load PBS configuration from environment variables or config files.

    Priority:
    1. Environment variables: PBS_REPOSITORY, PBS_PASSWORD, PBS_FINGERPRINT
    2. Config file: /etc/zpbs-backup/pbs.conf
    3. Config file: /root/.zpbs-backup.conf
    4. Config file: /root/.proxmox-backup-secrets (legacy format)

    Returns:
        PBSConfig instance

    Raises:
        ValueError: If PBS_REPOSITORY is not configured
    """
    # Try environment variables first
    repository = os.environ.get("PBS_REPOSITORY")
    password = os.environ.get("PBS_PASSWORD")
    fingerprint = os.environ.get("PBS_FINGERPRINT")

    if repository:
        return PBSConfig(
            repository=repository,
            password=password,
            fingerprint=fingerprint,
        )

    # Try config files - merge variables from multiple files
    config_paths = [
        Path("/etc/zpbs-backup/pbs.conf"),
        Path("/root/.zpbs-backup.conf"),
        Path("/root/proxmox-backup.conf"),
        Path("/root/.proxmox-backup-secrets"),
    ]

    merged_vars: dict[str, str] = {}
    for config_path in config_paths:
        try:
            if config_path.exists():
                file_vars = _parse_config_variables(config_path, merged_vars)
                merged_vars.update(file_vars)
        except PermissionError:
            continue

    config = _config_from_variables(merged_vars)
    if config.repository:
        return config

    raise ValueError(
        "PBS_REPOSITORY not configured. Set environment variable or create "
        "config file.\nHint: config files under /etc/zpbs-backup/ require "
        "root access — try running with sudo."
    )


def _parse_config_variables(
    path: Path, existing_vars: dict[str, str] | None = None
) -> dict[str, str]:
    """Parse variables from a config file.

    Supports both shell-style (VAR=value) and simple key=value formats.
    Handles shell variable interpolation like ${VAR} and $VAR.
    """
    variables: dict[str, str] = dict(existing_vars) if existing_vars else {}

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # Handle export VAR=value and VAR=value formats
            if line.startswith("export "):
                line = line[7:]

            if "=" not in line:
                continue

            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")

            # Resolve variable references in the value
            value = _interpolate_variables(value, variables)
            variables[key] = value

    return variables


def _config_from_variables(variables: dict[str, str]) -> PBSConfig:
    """Create PBSConfig from a dictionary of variables."""
    return PBSConfig(
        repository=variables.get("PBS_REPOSITORY", variables.get("REPOSITORY", "")),
        password=variables.get("PBS_PASSWORD", variables.get("PASSWORD")),
        fingerprint=variables.get("PBS_FINGERPRINT", variables.get("FINGERPRINT")),
    )


def _interpolate_variables(value: str, variables: dict[str, str]) -> str:
    """Resolve ${VAR} and $VAR references in a value."""

    def replace_var(match: re.Match[str]) -> str:
        var_name = match.group(1) or match.group(2)
        return variables.get(var_name, match.group(0))

    # Match ${VAR} or $VAR (but not $$)
    pattern = r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)"
    return re.sub(pattern, replace_var, value)
