"""PBS connection configuration management."""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path


# Config file search order (first match with PBS_REPOSITORY wins).
# Per-user config overrides system-wide.
CONFIG_PATHS = [
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "zpbs-backup"
    / "pbs.conf",
    Path("/etc/zpbs-backup/pbs.conf"),
]

# All recognized environment variable names for PBS config
_ENV_VAR_NAMES = [
    "PBS_REPOSITORY",
    "PBS_API_TOKEN_SECRET",
    "PBS_PASSWORD",
    "PBS_FINGERPRINT",
    "PBS_USER",
    "PBS_API_TOKEN_NAME",
    "PBS_SERVER",
    "PBS_DATASTORE",
    # Metrics / observability
    "ZPBS_PUSHGATEWAY",
    # Legacy aliases
    "REPOSITORY",
    "PASSWORD",
    "FINGERPRINT",
]


@dataclass
class ConfigSourceInfo:
    """Information about a single config source."""

    name: str  # e.g., "environment", "/etc/zpbs-backup/pbs.conf"
    status: str  # "active", "found", "not found", "permission denied"
    variables: dict[str, str] | None = None  # variable values if found


@dataclass
class PBSConfig:
    """Proxmox Backup Server connection configuration."""

    repository: str
    password: str | None = None
    fingerprint: str | None = None

    # Parsed display fields
    user: str | None = None
    token_name: str | None = None
    server: str | None = None
    datastore: str | None = None

    # Source tracking: variable name → source description
    sources: dict[str, str] = field(default_factory=dict)

    def get_env(self) -> dict[str, str]:
        """Return environment variables for proxmox-backup-client."""
        env = {"PBS_REPOSITORY": self.repository}
        if self.password:
            env["PBS_PASSWORD"] = self.password
        if self.fingerprint:
            env["PBS_FINGERPRINT"] = self.fingerprint
        return env

    @property
    def active_source(self) -> str:
        """Return the primary config source description."""
        # Find the source that provided PBS_REPOSITORY (or the composing vars)
        repo_source = self.sources.get("PBS_REPOSITORY")
        if repo_source:
            return repo_source
        user_source = self.sources.get("PBS_USER")
        if user_source:
            return user_source
        return "unknown"


def get_hostname() -> str:
    """Return the system hostname (short form)."""
    return socket.gethostname().split(".")[0]


def mask_secret(secret: str | None) -> str:
    """Mask a secret value for display."""
    if not secret:
        return "(not set)"
    if len(secret) <= 4:
        return "****"
    return secret[:4] + "****"


def _parse_repository(repository: str) -> tuple[str | None, str | None, str | None, str | None]:
    """Parse PBS_REPOSITORY into (user, token_name, server, datastore).

    Format: user@realm!tokenname@server:datastore
    Returns (None, None, None, None) for unparseable strings.
    """
    match = re.match(r"^([^!@]+@[^!@]+)!([^@]+)@([^:]+):(.+)$", repository)
    if not match:
        return None, None, None, None
    return match.group(1), match.group(2), match.group(3), match.group(4)


def load_config() -> PBSConfig:
    """Load PBS configuration from environment variables or config files.

    Priority:
    1. Environment variables
    2. Per-user config: ~/.config/zpbs-backup/pbs.conf
    3. System config: /etc/zpbs-backup/pbs.conf

    Returns:
        PBSConfig instance

    Raises:
        ValueError: If PBS_REPOSITORY is not configured
    """
    sources: dict[str, str] = {}

    # Try environment variables first
    env_vars: dict[str, str] = {}
    for name in _ENV_VAR_NAMES:
        val = os.environ.get(name)
        if val:
            env_vars[name] = val
            sources[name] = "environment"

    has_repo = "PBS_REPOSITORY" in env_vars or "REPOSITORY" in env_vars
    has_parts = all(
        k in env_vars for k in ("PBS_USER", "PBS_API_TOKEN_NAME", "PBS_SERVER", "PBS_DATASTORE")
    )

    if has_repo or has_parts:
        config = _config_from_variables(env_vars)
        config.sources = sources
        return config

    # Try config files — merge variables from files in order
    merged_vars: dict[str, str] = {}
    for config_path in CONFIG_PATHS:
        try:
            if config_path.exists():
                old_vars = dict(merged_vars)
                file_vars = _parse_config_variables(config_path, merged_vars)
                for key, value in file_vars.items():
                    if key not in old_vars or old_vars[key] != value:
                        sources[key] = str(config_path)
                merged_vars = file_vars
        except PermissionError:
            continue

    config = _config_from_variables(merged_vars)
    config.sources = sources
    if config.repository:
        return config

    raise ValueError(
        "PBS_REPOSITORY not configured. Set environment variable or create "
        "config file.\nHint: config files under /etc/zpbs-backup/ require "
        "root access — try running with sudo."
    )


def get_all_config_sources() -> list[ConfigSourceInfo]:
    """Return information about all config sources for diagnostic display.

    Returns a list of ConfigSourceInfo in priority order (highest first).
    """
    result: list[ConfigSourceInfo] = []

    # Check environment variables
    env_vars: dict[str, str] = {}
    for name in _ENV_VAR_NAMES:
        val = os.environ.get(name)
        if val:
            env_vars[name] = val

    if env_vars:
        result.append(ConfigSourceInfo(
            name="environment",
            status="active" if _has_config(env_vars) else "found",
            variables=env_vars,
        ))
    else:
        result.append(ConfigSourceInfo(name="environment", status="not set"))

    # Determine which source is actually active
    env_is_active = result[0].status == "active"

    # Check config files
    for config_path in CONFIG_PATHS:
        try:
            if config_path.exists():
                file_vars = _parse_config_variables(config_path)
                has_config = _has_config(file_vars)
                if has_config and not env_is_active:
                    status = "active"
                    env_is_active = True  # prevent later files from also being "active"
                else:
                    status = "found"
                result.append(ConfigSourceInfo(
                    name=str(config_path),
                    status=status,
                    variables=file_vars,
                ))
            else:
                result.append(ConfigSourceInfo(
                    name=str(config_path),
                    status="not found",
                ))
        except PermissionError:
            result.append(ConfigSourceInfo(
                name=str(config_path),
                status="permission denied",
            ))

    return result


def _has_config(variables: dict[str, str]) -> bool:
    """Check if variables contain enough info to build a PBS config."""
    has_repo = bool(variables.get("PBS_REPOSITORY") or variables.get("REPOSITORY"))
    has_parts = all(
        variables.get(k) for k in ("PBS_USER", "PBS_API_TOKEN_NAME", "PBS_SERVER", "PBS_DATASTORE")
    )
    return has_repo or has_parts


def _config_from_variables(variables: dict[str, str]) -> PBSConfig:
    """Create PBSConfig from a dictionary of variables."""
    # Get repository — direct or composed from parts
    repository = variables.get("PBS_REPOSITORY", variables.get("REPOSITORY", ""))

    user = variables.get("PBS_USER")
    token_name = variables.get("PBS_API_TOKEN_NAME")
    server = variables.get("PBS_SERVER")
    datastore = variables.get("PBS_DATASTORE")

    if not repository and all([user, token_name, server, datastore]):
        repository = f"{user}!{token_name}@{server}:{datastore}"

    # Get token secret — new name preferred, legacy aliases as fallback
    password = variables.get(
        "PBS_API_TOKEN_SECRET",
        variables.get("PBS_PASSWORD", variables.get("PASSWORD")),
    )

    fingerprint = variables.get("PBS_FINGERPRINT", variables.get("FINGERPRINT"))

    # Parse repository for display fields if not set from individual vars
    if repository and not all([user, token_name, server, datastore]):
        user, token_name, server, datastore = _parse_repository(repository)

    return PBSConfig(
        repository=repository,
        password=password,
        fingerprint=fingerprint,
        user=user,
        token_name=token_name,
        server=server,
        datastore=datastore,
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


def _interpolate_variables(value: str, variables: dict[str, str]) -> str:
    """Resolve ${VAR} and $VAR references in a value."""

    def replace_var(match: re.Match[str]) -> str:
        var_name = match.group(1) or match.group(2)
        return variables.get(var_name, match.group(0))

    # Match ${VAR} or $VAR (but not $$)
    pattern = r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)"
    return re.sub(pattern, replace_var, value)
