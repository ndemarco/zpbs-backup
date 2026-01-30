"""PBS connection configuration management."""

from __future__ import annotations

import os
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

    # Try config files
    config_paths = [
        Path("/etc/zpbs-backup/pbs.conf"),
        Path("/root/.zpbs-backup.conf"),
        Path("/root/.proxmox-backup-secrets"),
    ]

    for config_path in config_paths:
        if config_path.exists():
            config = _parse_config_file(config_path)
            if config.repository:
                return config

    raise ValueError(
        "PBS_REPOSITORY not configured. Set environment variable or create config file."
    )


def _parse_config_file(path: Path) -> PBSConfig:
    """Parse a PBS config file.

    Supports both shell-style (VAR=value) and simple key=value formats.
    """
    repository = None
    password = None
    fingerprint = None

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

            if key in ("PBS_REPOSITORY", "REPOSITORY"):
                repository = value
            elif key in ("PBS_PASSWORD", "PASSWORD"):
                password = value
            elif key in ("PBS_FINGERPRINT", "FINGERPRINT"):
                fingerprint = value

    return PBSConfig(
        repository=repository or "",
        password=password,
        fingerprint=fingerprint,
    )
