"""Tests for repository parsing, the credentials file and the legacy reader."""

from __future__ import annotations

import os

import pytest

from zpbs_backup.config import (
    Credentials,
    CredentialsError,
    PBSConfig,
    _interpolate_variables,
    load_credentials,
    parse_repository,
    read_legacy_config,
    validate_backup_id_separator,
)

REPO = "backup@pbs!mytoken@pbs.example.com:backups"


class TestParseRepository:
    def test_standard_format(self):
        assert parse_repository("user@realm!tokenname@server:datastore") == (
            "user@realm",
            "tokenname",
            "server",
            "datastore",
        )

    def test_the_documented_example_parses(self):
        assert parse_repository("user@realm!tok@host:ds") == ("user@realm", "tok", "host", "ds")

    def test_ip_address_server(self):
        assert parse_repository("u@pbs!t@10.0.0.5:store")[2] == "10.0.0.5"

    def test_server_with_port(self):
        assert parse_repository("u@pbs!t@host:8007:store") == ("u@pbs", "t", "host:8007", "store")

    def test_datastore_with_path(self):
        assert parse_repository("u@pbs!t@host:store/sub")[3] == "store/sub"

    @pytest.mark.parametrize("bad", ["", "nonsense", "user@realm@host:ds", "u@pbs!t@host"])
    def test_unparseable_returns_nones(self, bad):
        assert parse_repository(bad) == (None, None, None, None)


class TestPBSConfig:
    def test_from_repository_fills_the_parsed_fields(self):
        config = PBSConfig.from_repository(REPO, "s3cret", "AA:BB")
        assert (config.user, config.token_name, config.server, config.datastore) == (
            "backup@pbs",
            "mytoken",
            "pbs.example.com",
            "backups",
        )

    def test_env_carries_repository_secret_and_fingerprint(self):
        config = PBSConfig.from_repository(REPO, "s3cret", "AA:BB")
        assert config.get_env() == {
            "PBS_REPOSITORY": REPO,
            "PBS_PASSWORD": "s3cret",
            "PBS_FINGERPRINT": "AA:BB",
        }

    def test_env_omits_what_is_not_set(self):
        assert PBSConfig(repository=REPO).get_env() == {"PBS_REPOSITORY": REPO}


class TestBackupIdSeparator:
    def test_validate_accepts_valid(self):
        assert validate_backup_id_separator("--") == "--"

    @pytest.mark.parametrize("bad", ["", "/", "a b", "a:b"])
    def test_validate_rejects_invalid(self, bad):
        with pytest.raises(ValueError):
            validate_backup_id_separator(bad)


def _write(path, text, mode=0o600):
    path.write_text(text)
    path.chmod(mode)
    return path


class TestLoadCredentials:
    """The credentials file is trusted only if root-owned and private."""

    def test_reads_a_secret_per_repository_section(self, tmp_path):
        path = _write(
            tmp_path / "credentials",
            f"[{REPO}]\nsecret = aaa\n\n[x@pbs!t@other:ds]\nsecret = bbb\n",
        )

        creds = load_credentials(path, owner_uid=os.getuid())

        assert creds.secret_for(REPO) == "aaa"
        assert creds.secret_for("x@pbs!t@other:ds") == "bbb"

    def test_a_percent_sign_in_a_secret_is_literal(self, tmp_path):
        path = _write(tmp_path / "credentials", f"[{REPO}]\nsecret = a%b\n")
        assert load_credentials(path, owner_uid=os.getuid()).secret_for(REPO) == "a%b"

    def test_a_missing_file_is_an_empty_set(self, tmp_path):
        creds = load_credentials(tmp_path / "absent", owner_uid=os.getuid())
        assert creds.secrets == {}

    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o604, 0o666, 0o607])
    def test_a_file_open_to_group_or_others_is_refused(self, tmp_path, mode):
        path = _write(tmp_path / "credentials", f"[{REPO}]\nsecret = aaa\n", mode)

        with pytest.raises(CredentialsError, match="chmod 600"):
            load_credentials(path, owner_uid=os.getuid())

    def test_a_file_not_owned_by_the_required_user_is_refused(self, tmp_path):
        path = _write(tmp_path / "credentials", f"[{REPO}]\nsecret = aaa\n")

        with pytest.raises(CredentialsError, match="owned by uid"):
            load_credentials(path, owner_uid=os.getuid() + 1)

    def test_the_secret_is_not_in_the_refusal_message(self, tmp_path):
        path = _write(tmp_path / "credentials", f"[{REPO}]\nsecret = topsecret\n", 0o644)

        with pytest.raises(CredentialsError) as raised:
            load_credentials(path, owner_uid=os.getuid())

        assert "topsecret" not in str(raised.value)

    def test_a_section_without_a_secret_is_no_entry(self, tmp_path):
        path = _write(tmp_path / "credentials", f"[{REPO}]\nother = 1\n")
        assert not load_credentials(path, owner_uid=os.getuid()).has_entry(REPO)

    def test_malformed_ini_is_reported(self, tmp_path):
        path = _write(tmp_path / "credentials", "secret = orphan\n")

        with pytest.raises(CredentialsError, match="Cannot parse"):
            load_credentials(path, owner_uid=os.getuid())


class TestCredentialsBinding:
    """A secret serves only the exact repository string it is listed under."""

    creds = Credentials({REPO: "aaa"})

    @pytest.mark.parametrize(
        "other",
        [
            REPO.upper(),
            REPO + " ",
            REPO.replace("pbs.example.com", "evil.example.com"),
            REPO.replace("backups", "other"),
            REPO.replace("mytoken", "othertoken"),
            "",
        ],
    )
    def test_any_different_string_gets_no_secret(self, other):
        assert self.creds.secret_for(other) is None
        assert not self.creds.has_entry(other)

    def test_the_exact_string_gets_the_secret(self):
        assert self.creds.secret_for(REPO) == "aaa"


class TestInterpolateVariables:
    def test_braced_var(self):
        assert _interpolate_variables("${A}-x", {"A": "1"}) == "1-x"

    def test_unknown_var_preserved(self):
        assert _interpolate_variables("${NOPE}", {}) == "${NOPE}"


class TestReadLegacyConfig:
    def test_composes_the_repository_from_parts(self, tmp_path):
        path = tmp_path / "pbs.conf"
        path.write_text(
            'PBS_USER="backup@pbs"\nPBS_API_TOKEN_NAME="mytoken"\n'
            'PBS_SERVER="pbs.example.com"\nPBS_DATASTORE="backups"\n'
            'PBS_API_TOKEN_SECRET="xyz"\nPBS_FINGERPRINT="AA:BB"\n'
            'PBS_ENCRYPTION_KEYFILE="/k.enc"\nZPBS_BACKUP_ID_SEPARATOR="--"\n'
            'ZPBS_PUSHGATEWAY="http://gw:9091"\n'
        )

        legacy = read_legacy_config(path)

        assert legacy.repository == REPO
        assert legacy.secret == "xyz"
        assert legacy.fingerprint == "AA:BB"
        assert legacy.keyfile == "/k.enc"
        assert legacy.separator == "--"
        assert legacy.reporting == {"ZPBS_PUSHGATEWAY": "http://gw:9091"}

    def test_interpolation_and_legacy_names(self, tmp_path):
        path = tmp_path / "pbs.conf"
        path.write_text(
            "export PBS_USER=u@pbs\nexport PBS_REPOSITORY=${PBS_USER}!t@h:d\nPASSWORD=old\n"
        )

        legacy = read_legacy_config(path)

        assert legacy.repository == "u@pbs!t@h:d"
        assert legacy.secret == "old"

    def test_empty_file(self, tmp_path):
        path = tmp_path / "pbs.conf"
        path.write_text("# nothing\n")
        assert read_legacy_config(path).repository is None
