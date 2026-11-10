"""PBS connection settings: repository parsing and the credentials file.

Dataset-level settings (repository, fingerprint, keyfile, id separator) are
ZFS properties; see zfs.py. The one thing a property cannot hold is the token
secret, because any local user can read properties with ``zfs get``. Secrets
live in a root-only credentials file, each bound to the exact repository
string it is listed under.
"""

from __future__ import annotations

import configparser
import os
import re
import socket
import stat
from dataclasses import dataclass
from pathlib import Path

CREDENTIALS_PATH = Path("/etc/zpbs-backup/credentials")
LEGACY_CONFIG_PATH = Path("/etc/zpbs-backup/pbs.conf")

# Default separator used when building a PBS backup-id from a dataset path.
# "-" preserves historical ids ({hostname}-{path-with-slashes-as-dashes}).
DEFAULT_BACKUP_ID_SEPARATOR = "-"

# user@realm!tokenname@server[:port]:datastore
_REPOSITORY_RE = re.compile(r"^([^!@\s]+@[^!@\s]+)!([^@\s]+)@([^:@\s]+(?::\d+)?):(\S+)$")


class CredentialsError(Exception):
    """The credentials file exists but cannot be trusted or parsed."""


@dataclass
class PBSConfig:
    """Connection settings for one Proxmox Backup Server repository."""

    repository: str
    password: str | None = None
    fingerprint: str | None = None

    # Parsed display fields
    user: str | None = None
    token_name: str | None = None
    server: str | None = None
    datastore: str | None = None

    @classmethod
    def from_repository(
        cls, repository: str, password: str | None, fingerprint: str | None = None
    ) -> PBSConfig:
        """Build a config from a repository string, filling the parsed fields."""
        user, token_name, server, datastore = parse_repository(repository)
        return cls(
            repository=repository,
            password=password,
            fingerprint=fingerprint,
            user=user,
            token_name=token_name,
            server=server,
            datastore=datastore,
        )

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


def parse_repository(repository: str) -> tuple[str | None, str | None, str | None, str | None]:
    """Parse a repository into (user, token_name, server, datastore).

    Format: user@realm!tokenname@server[:port]:datastore
    Returns (None, None, None, None) for unparseable strings.
    """
    match = _REPOSITORY_RE.match(repository)
    if not match:
        return None, None, None, None
    return match.group(1), match.group(2), match.group(3), match.group(4)


def validate_backup_id_separator(separator: str) -> str:
    """Validate a backup-id separator.

    The separator is inserted into PBS backup-ids, so it must be non-empty and
    use only characters allowed in a PBS backup-id ([A-Za-z0-9_.-]).

    Returns:
        The separator unchanged if valid.

    Raises:
        ValueError: If the separator is empty or contains invalid characters.
    """
    if not separator or not re.fullmatch(r"[A-Za-z0-9_.\-]+", separator):
        raise ValueError(
            f"Invalid backup-id separator {separator!r}: must be non-empty and "
            "contain only [A-Za-z0-9_.-] (the PBS backup-id character set)."
        )
    return separator


@dataclass
class Credentials:
    """Token secrets keyed by the exact repository string they belong to."""

    secrets: dict[str, str]
    path: Path = CREDENTIALS_PATH

    def secret_for(self, repository: str) -> str | None:
        """Return the secret listed under exactly this repository, else None.

        Exact string comparison, no normalisation: a user who can write
        ``zpbs:repository`` must not be able to point the host's token at a
        server of their choosing.
        """
        return self.secrets.get(repository) or None

    def has_entry(self, repository: str) -> bool:
        return self.secret_for(repository) is not None


def load_credentials(path: Path | None = None, owner_uid: int = 0) -> Credentials:
    """Read the credentials file.

    A missing file is an empty set of credentials (every dataset then fails
    closed). A file that exists is refused unless it is owned by ``owner_uid``
    (root) and grants nothing to group or others.

    Args:
        path: Credentials file; defaults to /etc/zpbs-backup/credentials
        owner_uid: Required owner of the file (injectable for tests)

    Raises:
        CredentialsError: If the file is untrusted or malformed
    """
    path = path if path is not None else CREDENTIALS_PATH

    try:
        handle = path.open()
    except FileNotFoundError:
        return Credentials({}, path)
    except PermissionError as e:
        raise CredentialsError(f"Cannot read {path}: {e.strerror}. Run as root.") from None

    with handle:
        info = os.fstat(handle.fileno())
        if info.st_uid != owner_uid:
            raise CredentialsError(
                f"Refusing to read {path}: owned by uid {info.st_uid}, expected {owner_uid}."
            )
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise CredentialsError(
                f"Refusing to read {path}: mode {stat.S_IMODE(info.st_mode):04o} lets other "
                f"users read it. Run: chmod 600 {path}"
            )

        parser = configparser.ConfigParser(interpolation=None, delimiters=("=",))
        try:
            parser.read_file(handle)
        except configparser.Error as e:
            raise CredentialsError(f"Cannot parse {path}: {e}") from None

    secrets = {
        section: parser[section].get("secret", "").strip() for section in parser.sections()
    }
    return Credentials({repo: secret for repo, secret in secrets.items() if secret}, path)


@dataclass
class LegacyConfig:
    """What an old pbs.conf held, for ``migrate-config``."""

    repository: str | None
    secret: str | None
    fingerprint: str | None
    keyfile: str | None
    separator: str | None
    reporting: dict[str, str]


_REPORTING_VARS = ("ZPBS_PUSHGATEWAY", "ZPBS_TEXTFILE_DIR", "ZPBS_SYSLOG", "ZPBS_NOTIFY")


def read_legacy_config(path: Path) -> LegacyConfig:
    """Parse a 0.11.0-style shell-variable pbs.conf (read-only)."""
    variables = _parse_config_variables(path)

    repository = variables.get("PBS_REPOSITORY") or variables.get("REPOSITORY")
    parts = [
        variables.get(k) for k in ("PBS_USER", "PBS_API_TOKEN_NAME", "PBS_SERVER", "PBS_DATASTORE")
    ]
    if not repository and all(parts):
        user, token, server, datastore = parts
        repository = f"{user}!{token}@{server}:{datastore}"

    return LegacyConfig(
        repository=repository or None,
        secret=(
            variables.get("PBS_API_TOKEN_SECRET")
            or variables.get("PBS_PASSWORD")
            or variables.get("PASSWORD")
            or None
        ),
        fingerprint=variables.get("PBS_FINGERPRINT") or variables.get("FINGERPRINT") or None,
        keyfile=variables.get("PBS_ENCRYPTION_KEYFILE") or None,
        separator=variables.get("ZPBS_BACKUP_ID_SEPARATOR") or None,
        reporting={k: variables[k] for k in _REPORTING_VARS if variables.get(k)},
    )


def _parse_config_variables(path: Path) -> dict[str, str]:
    """Parse shell-style (VAR=value) lines, resolving ${VAR} and $VAR."""
    variables: dict[str, str] = {}

    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            line = line.removeprefix("export ")
            if "=" not in line:
                continue

            key, _, value = line.partition("=")
            variables[key.strip()] = _interpolate_variables(
                value.strip().strip("'\""), variables
            )

    return variables


def _interpolate_variables(value: str, variables: dict[str, str]) -> str:
    """Resolve ${VAR} and $VAR references in a value."""

    def replace_var(match: re.Match[str]) -> str:
        var_name = match.group(1) or match.group(2)
        return variables.get(var_name, match.group(0))

    return re.sub(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)", replace_var, value)
