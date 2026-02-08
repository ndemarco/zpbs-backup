"""Tests for zpbs_backup.config module."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from zpbs_backup.config import (
    PBSConfig,
    _config_from_variables,
    _interpolate_variables,
    _parse_config_variables,
    _parse_repository,
    get_all_config_sources,
    load_config,
    mask_secret,
)


class TestParseRepository:
    def test_standard_format(self):
        user, token, server, ds = _parse_repository(
            "backup@pbs!mytoken@pbs.example.com:backups"
        )
        assert user == "backup@pbs"
        assert token == "mytoken"
        assert server == "pbs.example.com"
        assert ds == "backups"

    def test_ip_address_server(self):
        user, token, server, ds = _parse_repository(
            "admin@pam!backup@192.168.1.100:datastore1"
        )
        assert user == "admin@pam"
        assert token == "backup"
        assert server == "192.168.1.100"
        assert ds == "datastore1"

    def test_datastore_with_path(self):
        user, token, server, ds = _parse_repository(
            "backup@pbs!tok@host:store/sub"
        )
        assert ds == "store/sub"

    def test_unparseable_returns_nones(self):
        assert _parse_repository("garbage") == (None, None, None, None)
        assert _parse_repository("") == (None, None, None, None)
        assert _parse_repository("user@server:store") == (None, None, None, None)


class TestMaskSecret:
    def test_none(self):
        assert mask_secret(None) == "(not set)"

    def test_empty_string(self):
        assert mask_secret("") == "(not set)"

    def test_short_secret(self):
        assert mask_secret("abc") == "****"

    def test_normal_secret(self):
        assert mask_secret("abcdefgh") == "abcd****"

    def test_exactly_four(self):
        assert mask_secret("abcd") == "****"


class TestInterpolateVariables:
    def test_braced_var(self):
        result = _interpolate_variables("${FOO}/bar", {"FOO": "hello"})
        assert result == "hello/bar"

    def test_unbraced_var(self):
        result = _interpolate_variables("$FOO/bar", {"FOO": "hello"})
        assert result == "hello/bar"

    def test_unknown_var_preserved(self):
        result = _interpolate_variables("${UNKNOWN}/bar", {})
        assert result == "${UNKNOWN}/bar"

    def test_multiple_vars(self):
        result = _interpolate_variables(
            "${USER}!${TOKEN}@${SERVER}:${DS}",
            {"USER": "a@b", "TOKEN": "t", "SERVER": "s", "DS": "d"},
        )
        assert result == "a@b!t@s:d"

    def test_no_vars(self):
        result = _interpolate_variables("plain text", {})
        assert result == "plain text"


class TestConfigFromVariables:
    def test_full_repository(self):
        config = _config_from_variables({
            "PBS_REPOSITORY": "backup@pbs!mytoken@pbs.example.com:backups",
            "PBS_PASSWORD": "secret123",
            "PBS_FINGERPRINT": "AA:BB:CC",
        })
        assert config.repository == "backup@pbs!mytoken@pbs.example.com:backups"
        assert config.password == "secret123"
        assert config.fingerprint == "AA:BB:CC"
        assert config.user == "backup@pbs"
        assert config.token_name == "mytoken"
        assert config.server == "pbs.example.com"
        assert config.datastore == "backups"

    def test_legacy_repository_alias(self):
        config = _config_from_variables({
            "REPOSITORY": "backup@pbs!mytoken@host:store",
            "PASSWORD": "secret",
        })
        assert config.repository == "backup@pbs!mytoken@host:store"
        assert config.password == "secret"

    def test_individual_parts_compose_repository(self):
        config = _config_from_variables({
            "PBS_USER": "backup@pbs",
            "PBS_API_TOKEN_NAME": "mytoken",
            "PBS_SERVER": "pbs.example.com",
            "PBS_DATASTORE": "backups",
            "PBS_API_TOKEN_SECRET": "secret",
        })
        assert config.repository == "backup@pbs!mytoken@pbs.example.com:backups"
        assert config.password == "secret"
        assert config.user == "backup@pbs"
        assert config.token_name == "mytoken"
        assert config.server == "pbs.example.com"
        assert config.datastore == "backups"

    def test_explicit_repository_wins_over_parts(self):
        config = _config_from_variables({
            "PBS_REPOSITORY": "override@pbs!tok@host:ds",
            "PBS_USER": "backup@pbs",
            "PBS_API_TOKEN_NAME": "mytoken",
            "PBS_SERVER": "pbs.example.com",
            "PBS_DATASTORE": "backups",
        })
        assert config.repository == "override@pbs!tok@host:ds"

    def test_api_token_secret_preferred_over_password(self):
        config = _config_from_variables({
            "PBS_REPOSITORY": "backup@pbs!tok@host:ds",
            "PBS_API_TOKEN_SECRET": "new_secret",
            "PBS_PASSWORD": "old_secret",
        })
        assert config.password == "new_secret"

    def test_pbs_password_as_fallback(self):
        config = _config_from_variables({
            "PBS_REPOSITORY": "backup@pbs!tok@host:ds",
            "PBS_PASSWORD": "fallback_secret",
        })
        assert config.password == "fallback_secret"

    def test_legacy_password_alias(self):
        config = _config_from_variables({
            "PBS_REPOSITORY": "backup@pbs!tok@host:ds",
            "PASSWORD": "legacy_secret",
        })
        assert config.password == "legacy_secret"

    def test_legacy_fingerprint_alias(self):
        config = _config_from_variables({
            "PBS_REPOSITORY": "backup@pbs!tok@host:ds",
            "FINGERPRINT": "AA:BB",
        })
        assert config.fingerprint == "AA:BB"

    def test_empty_vars_produces_empty_config(self):
        config = _config_from_variables({})
        assert config.repository == ""
        assert config.password is None
        assert config.fingerprint is None


class TestParseConfigVariables:
    def test_simple_key_value(self, tmp_path):
        conf = tmp_path / "pbs.conf"
        conf.write_text("PBS_REPOSITORY=backup@pbs!tok@host:ds\nPBS_PASSWORD=secret\n")
        result = _parse_config_variables(conf)
        assert result["PBS_REPOSITORY"] == "backup@pbs!tok@host:ds"
        assert result["PBS_PASSWORD"] == "secret"

    def test_export_prefix(self, tmp_path):
        conf = tmp_path / "pbs.conf"
        conf.write_text("export PBS_REPOSITORY=backup@pbs!tok@host:ds\n")
        result = _parse_config_variables(conf)
        assert result["PBS_REPOSITORY"] == "backup@pbs!tok@host:ds"

    def test_quoted_values(self, tmp_path):
        conf = tmp_path / "pbs.conf"
        conf.write_text('PBS_REPOSITORY="backup@pbs!tok@host:ds"\n')
        result = _parse_config_variables(conf)
        assert result["PBS_REPOSITORY"] == "backup@pbs!tok@host:ds"

    def test_single_quoted_values(self, tmp_path):
        conf = tmp_path / "pbs.conf"
        conf.write_text("PBS_REPOSITORY='backup@pbs!tok@host:ds'\n")
        result = _parse_config_variables(conf)
        assert result["PBS_REPOSITORY"] == "backup@pbs!tok@host:ds"

    def test_comments_and_blanks_ignored(self, tmp_path):
        conf = tmp_path / "pbs.conf"
        conf.write_text("# comment\n\nPBS_REPOSITORY=val\n")
        result = _parse_config_variables(conf)
        assert result == {"PBS_REPOSITORY": "val"}

    def test_variable_interpolation(self, tmp_path):
        conf = tmp_path / "pbs.conf"
        conf.write_text(
            "PBS_USER=backup@pbs\n"
            "PBS_API_TOKEN_NAME=mytoken\n"
            "PBS_SERVER=host\n"
            "PBS_DATASTORE=ds\n"
            "PBS_REPOSITORY=${PBS_USER}!${PBS_API_TOKEN_NAME}@${PBS_SERVER}:${PBS_DATASTORE}\n"
        )
        result = _parse_config_variables(conf)
        assert result["PBS_REPOSITORY"] == "backup@pbs!mytoken@host:ds"

    def test_existing_vars_merged(self, tmp_path):
        conf = tmp_path / "pbs.conf"
        conf.write_text("KEY2=value2\n")
        result = _parse_config_variables(conf, existing_vars={"KEY1": "value1"})
        assert result["KEY1"] == "value1"
        assert result["KEY2"] == "value2"


class TestLoadConfig:
    def test_env_vars_priority(self, monkeypatch):
        monkeypatch.setenv("PBS_REPOSITORY", "backup@pbs!tok@host:ds")
        monkeypatch.setenv("PBS_PASSWORD", "secret")
        config = load_config()
        assert config.repository == "backup@pbs!tok@host:ds"
        assert config.sources.get("PBS_REPOSITORY") == "environment"

    def test_individual_env_vars(self, monkeypatch):
        monkeypatch.setenv("PBS_USER", "backup@pbs")
        monkeypatch.setenv("PBS_API_TOKEN_NAME", "mytoken")
        monkeypatch.setenv("PBS_SERVER", "pbs.example.com")
        monkeypatch.setenv("PBS_DATASTORE", "backups")
        monkeypatch.setenv("PBS_API_TOKEN_SECRET", "secret")
        config = load_config()
        assert config.repository == "backup@pbs!mytoken@pbs.example.com:backups"
        assert config.password == "secret"

    def test_config_file_loading(self, tmp_path, monkeypatch):
        # Clear any env vars
        for var in [
            "PBS_REPOSITORY", "PBS_PASSWORD", "PBS_FINGERPRINT",
            "PBS_USER", "PBS_API_TOKEN_NAME", "PBS_SERVER", "PBS_DATASTORE",
            "PBS_API_TOKEN_SECRET", "REPOSITORY", "PASSWORD", "FINGERPRINT",
        ]:
            monkeypatch.delenv(var, raising=False)

        # Create a config file
        conf = tmp_path / "pbs.conf"
        conf.write_text(
            "PBS_REPOSITORY=backup@pbs!tok@host:ds\n"
            "PBS_PASSWORD=secret\n"
        )

        # Patch CONFIG_PATHS to use our temp file
        monkeypatch.setattr(
            "zpbs_backup.config.CONFIG_PATHS", [conf]
        )
        config = load_config()
        assert config.repository == "backup@pbs!tok@host:ds"
        assert config.sources.get("PBS_REPOSITORY") == str(conf)

    def test_no_config_raises(self, monkeypatch):
        for var in [
            "PBS_REPOSITORY", "PBS_PASSWORD", "PBS_FINGERPRINT",
            "PBS_USER", "PBS_API_TOKEN_NAME", "PBS_SERVER", "PBS_DATASTORE",
            "PBS_API_TOKEN_SECRET", "REPOSITORY", "PASSWORD", "FINGERPRINT",
        ]:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(
            "zpbs_backup.config.CONFIG_PATHS", []
        )
        with pytest.raises(ValueError, match="PBS_REPOSITORY not configured"):
            load_config()

    def test_source_tracking(self, monkeypatch):
        monkeypatch.setenv("PBS_REPOSITORY", "backup@pbs!tok@host:ds")
        monkeypatch.setenv("PBS_FINGERPRINT", "AA:BB")
        config = load_config()
        assert config.sources["PBS_REPOSITORY"] == "environment"
        assert config.sources["PBS_FINGERPRINT"] == "environment"


class TestPBSConfigGetEnv:
    def test_basic(self):
        config = PBSConfig(
            repository="backup@pbs!tok@host:ds",
            password="secret",
            fingerprint="AA:BB",
        )
        env = config.get_env()
        assert env == {
            "PBS_REPOSITORY": "backup@pbs!tok@host:ds",
            "PBS_PASSWORD": "secret",
            "PBS_FINGERPRINT": "AA:BB",
        }

    def test_without_optional(self):
        config = PBSConfig(repository="backup@pbs!tok@host:ds")
        env = config.get_env()
        assert env == {"PBS_REPOSITORY": "backup@pbs!tok@host:ds"}

    def test_active_source_from_repository(self):
        config = PBSConfig(
            repository="backup@pbs!tok@host:ds",
            sources={"PBS_REPOSITORY": "/etc/zpbs-backup/pbs.conf"},
        )
        assert config.active_source == "/etc/zpbs-backup/pbs.conf"

    def test_active_source_from_user(self):
        config = PBSConfig(
            repository="backup@pbs!tok@host:ds",
            sources={"PBS_USER": "environment"},
        )
        assert config.active_source == "environment"

    def test_active_source_unknown(self):
        config = PBSConfig(repository="backup@pbs!tok@host:ds")
        assert config.active_source == "unknown"
