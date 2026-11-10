"""Tests for per-dataset repository connections (0.12.0).

Connection settings are inherited ZFS properties; the token secret comes from
the credentials file and is bound to the exact repository string.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from zpbs_backup import backup as backup_mod
from zpbs_backup import cli as cli_mod
from zpbs_backup import connections as connections_mod
from zpbs_backup import lock as lock_mod
from zpbs_backup import pbs as pbs_mod
from zpbs_backup.backup import (
    BackupOrchestrator,
    PruneOrchestrator,
    recorded_separator,
)
from zpbs_backup.cli import main
from zpbs_backup.config import Credentials, load_credentials
from zpbs_backup.connections import ConnectionProblem, Connections, Target
from zpbs_backup.pbs import BackupSnapshot, PBSClient
from zpbs_backup.zfs import (
    PROP_BACKUP,
    PROP_FINGERPRINT,
    PROP_ID_SEPARATOR,
    PROP_KEYFILE,
    PROP_REPOSITORY,
    Dataset,
    InvalidPropertyError,
    PropertyValue,
    validate_property_value,
)

REPO_A = "backup@pbs!tok@pbs-a.example.com:alpha"
REPO_B = "backup@pbs!tok@pbs-b.example.com:beta"
OK = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _ds(name, repository=REPO_A, extra=None, source="local"):
    properties = {PROP_BACKUP: PropertyValue("true", "local")}
    if repository is not None:
        properties[PROP_REPOSITORY] = PropertyValue(repository, source)
    for prop, value in (extra or {}).items():
        properties[prop] = PropertyValue(value, "local")
    return Dataset(name=name, properties=properties, mountpoint=f"/{name}", mounted=True)


@pytest.fixture
def server():
    """Replace every server-touching PBSClient method; yields the mocks."""
    mocks: dict[str, MagicMock] = {}
    with patch.object(PBSClient, "check_connection") as check, \
         patch.object(PBSClient, "get_clock_skew_seconds", return_value=0.0), \
         patch.object(PBSClient, "get_last_backup_time", return_value=None) as last, \
         patch.object(PBSClient, "create_namespace", return_value=True), \
         patch.object(PBSClient, "backup", return_value=OK) as backup, \
         patch.object(PBSClient, "prune", return_value=OK) as prune, \
         patch.object(PBSClient, "list_snapshot_details", return_value=[]) as snaps, \
         patch.object(PBSClient, "set_snapshot_notes", return_value=True) as notes:
        mocks.update(
            check=check, last=last, backup=backup, prune=prune, snaps=snaps, notes=notes
        )
        yield mocks


def _orch(credentials, datasets, **kwargs):
    sink = io.StringIO()
    orch = BackupOrchestrator(
        Connections(credentials, output=sink), output=sink, **kwargs
    )
    patcher = patch.object(backup_mod, "get_all_datasets", return_value=datasets)
    return orch, sink, patcher


class TestConnectionsResolve:
    def test_no_repository_is_a_problem_naming_the_dataset(self):
        with pytest.raises(ConnectionProblem, match="tank/data: zpbs:repository is not set"):
            Connections(Credentials({})).resolve(_ds("tank/data", repository=None))

    def test_no_credentials_entry_is_a_problem_naming_the_repository(self):
        with pytest.raises(ConnectionProblem, match="no credentials entry") as raised:
            Connections(Credentials({REPO_B: "x"})).resolve(_ds("tank/data"))
        assert REPO_A in str(raised.value)

    def test_a_malformed_repository_is_a_problem(self):
        with pytest.raises(ConnectionProblem, match="not valid"):
            Connections(Credentials({})).resolve(_ds("tank/data", repository="nonsense"))

    def test_group_splits_by_repository_and_reports_the_rest(self):
        conns = Connections(Credentials({REPO_A: "a", REPO_B: "b"}))
        a1, a2, b1 = _ds("tank/a1"), _ds("tank/a2"), _ds("pool/b1", REPO_B)
        none = _ds("pool/none", repository=None)
        unknown = _ds("pool/unknown", "u@p!t@elsewhere:ds")

        groups, problems = conns.group([a1, b1, a2, none, unknown])

        assert groups == {Target(REPO_A): [a1, a2], Target(REPO_B): [b1]}
        assert {d.name for d, _ in problems} == {"pool/none", "pool/unknown"}

    def test_one_client_serves_every_dataset_of_a_repository(self):
        conns = Connections(Credentials({REPO_A: "a"}))
        assert conns.client(Target(REPO_A)) is conns.client(Target(REPO_A))

    def test_the_client_carries_the_bound_secret_and_fingerprint(self):
        conns = Connections(Credentials({REPO_A: "a-secret"}))
        client = conns.client(Target(REPO_A, "AA:BB"))
        assert client.config.get_env() == {
            "PBS_REPOSITORY": REPO_A,
            "PBS_PASSWORD": "a-secret",
            "PBS_FINGERPRINT": "AA:BB",
        }

    def test_a_target_is_logged_in_to_once(self, server):
        conns = Connections(Credentials({REPO_A: "a"}))
        conns.reachable(Target(REPO_A))
        conns.reachable(Target(REPO_A))
        assert server["check"].call_count == 1

    def test_a_rejected_login_is_a_problem_and_remembered(self, server):
        server["check"].side_effect = ConnectionError("bad token")
        conns = Connections(Credentials({REPO_A: "a"}))
        for _ in range(2):
            with pytest.raises(ConnectionProblem, match="bad token"):
                conns.reachable(Target(REPO_A))
        assert server["check"].call_count == 1


class TestRunWithoutPbsConf:
    """No pbs.conf and no PBS_* environment: properties plus credentials suffice."""

    def test_backs_up_using_properties_and_the_credentials_file(
        self, tmp_path, monkeypatch
    ):
        for name in list(os.environ):
            if name.startswith("PBS_"):
                monkeypatch.delenv(name)

        creds_file = tmp_path / "credentials"
        creds_file.write_text(f"[{REPO_A}]\nsecret = file-secret\n")
        creds_file.chmod(0o600)

        dataset = _ds(
            "tank/data",
            extra={PROP_FINGERPRINT: "AA:BB", PROP_KEYFILE: "/etc/zpbs-backup/key.enc"},
        )
        captured = {}

        def fake_stream(cmd, env=None, output=None, timeout=None):
            captured["cmd"], captured["env"] = cmd, env
            return OK

        with patch.object(lock_mod, "LOCK_PATH", tmp_path / "run.lock"), \
             patch.object(
                 cli_mod, "load_credentials",
                 lambda: load_credentials(creds_file, owner_uid=os.getuid()),
             ), \
             patch.object(cli_mod, "report_metrics"), \
             patch.object(cli_mod, "send_notification"), \
             patch.object(backup_mod, "get_all_datasets", return_value=[dataset]), \
             patch.object(PBSClient, "check_connection"), \
             patch.object(PBSClient, "get_clock_skew_seconds", return_value=0.0), \
             patch.object(PBSClient, "get_last_backup_time", return_value=None), \
             patch.object(PBSClient, "create_namespace", return_value=True), \
             patch.object(PBSClient, "list_snapshot_details", return_value=[]), \
             patch.object(PBSClient, "set_snapshot_notes", return_value=True), \
             patch.object(pbs_mod, "run_streaming", fake_stream):
            result = CliRunner().invoke(main, ["run", "--force"])

        assert result.exit_code == 0, result.output
        assert captured["env"]["PBS_REPOSITORY"] == REPO_A
        assert captured["env"]["PBS_PASSWORD"] == "file-secret"
        assert captured["env"]["PBS_FINGERPRINT"] == "AA:BB"
        cmd = captured["cmd"]
        assert cmd[cmd.index("--keyfile") + 1] == "/etc/zpbs-backup/key.enc"


class TestFailClosed:
    def test_no_credentials_entry_fails_the_dataset_and_never_builds_a_client(self, server):
        orch, _sink, patcher = _orch(Credentials({REPO_B: "b"}), [_ds("tank/data")])

        with patcher, patch.object(connections_mod, "PBSClient") as constructed:
            summary = orch.run()

        constructed.assert_not_called()
        assert summary.failed == 1
        assert "no credentials entry" in (summary.results[0].error or "")
        for mock in server.values():
            mock.assert_not_called()

    def test_a_secret_listed_for_another_repository_is_not_used(self, server):
        """The token is bound to its repository: a redirected property gets nothing."""
        redirected = _ds("tank/data", repository="backup@pbs!tok@attacker.example.com:alpha")
        orch, _, patcher = _orch(Credentials({REPO_A: "host-token"}), [redirected])

        with patcher, patch.object(connections_mod, "PBSClient") as constructed:
            summary = orch.run()

        constructed.assert_not_called()
        assert summary.failed == 1
        for mock in server.values():
            mock.assert_not_called()

    def test_the_unconnectable_dataset_does_not_stop_the_others(self, server):
        good, bad = _ds("tank/good"), _ds("tank/bad", "u@p!t@elsewhere:ds")
        orch, _, patcher = _orch(Credentials({REPO_A: "a"}), [good, bad])

        with patcher:
            summary = orch.run()

        assert (summary.successful, summary.failed) == (1, 1)
        assert server["backup"].call_count == 1

    def test_prune_fails_closed_without_a_client(self, server):
        sink = io.StringIO()
        pruner = PruneOrchestrator(Connections(Credentials({})), output=sink)

        with patch.object(backup_mod, "discover_datasets", return_value=[_ds("tank/data")]), \
             patch.object(connections_mod, "PBSClient") as constructed:
            counts = pruner.run()

        constructed.assert_not_called()
        assert counts == (0, 1)
        server["prune"].assert_not_called()


class TestNoRepository:
    def test_counts_as_failed_in_results_and_contacts_no_server(self, server):
        orch, _, patcher = _orch(Credentials({REPO_A: "a"}), [_ds("tank/data", repository=None)])

        with patcher:
            summary = orch.run()

        assert summary.failed == 1
        assert summary.results[0].success is False
        assert "zpbs:repository is not set" in (summary.results[0].error or "")
        for mock in server.values():
            mock.assert_not_called()

    def test_run_exits_non_zero_and_reports_it_to_metrics(self, tmp_path):
        reported = []

        with patch.object(lock_mod, "LOCK_PATH", tmp_path / "run.lock"), \
             patch.object(cli_mod, "load_credentials", return_value=Credentials({})), \
             patch.object(cli_mod, "report_metrics", lambda s, h: reported.append(s)), \
             patch.object(cli_mod, "send_notification"), \
             patch.object(
                 backup_mod, "get_all_datasets",
                 return_value=[_ds("tank/data", repository=None)],
             ):
            result = CliRunner().invoke(main, ["run"])

        assert result.exit_code == 1
        assert reported[0].failed == 1


class TestGroupingByRepository:
    def test_run_uses_one_client_per_repository(self, server):
        datasets = [_ds("a/1"), _ds("b/1", REPO_B), _ds("a/2")]
        orch, _, patcher = _orch(Credentials({REPO_A: "a", REPO_B: "b"}), datasets)
        built = []
        real = PBSClient

        def factory(config, output):
            built.append(config.repository)
            return real(config, output)

        orch.connections._factory = factory

        with patcher:
            summary = orch.run(None)

        assert sorted(built) == [REPO_A, REPO_B]
        assert summary.successful == 3
        assert server["check"].call_count == 2

    def test_each_dataset_is_backed_up_on_its_own_repositorys_client(self, server):
        datasets = [_ds("a/1"), _ds("b/1", REPO_B)]
        orch, _, patcher = _orch(Credentials({REPO_A: "a", REPO_B: "b"}), datasets)
        used: list[str] = []

        def record_backup(*args, **kwargs):
            used.append(kwargs["backup_id"])
            return OK

        server["backup"].side_effect = record_backup

        with patcher:
            orch.run()

        assert len(used) == 2
        client_a = orch.connections.client(Target(REPO_A))
        client_b = orch.connections.client(Target(REPO_B))
        assert client_a is not client_b

    def test_an_unreachable_repository_fails_only_its_datasets(self, server):
        datasets = [_ds("a/1"), _ds("b/1", REPO_B)]
        orch, _, patcher = _orch(Credentials({REPO_A: "a", REPO_B: "b"}), datasets)
        server["check"].side_effect = [ConnectionError("down"), None]

        with patcher:
            summary = orch.run()

        by_name = {r.dataset.name: r for r in summary.results}
        assert by_name["a/1"].success is False and "down" in (by_name["a/1"].error or "")
        assert by_name["b/1"].success is True

    def test_prune_groups_by_repository(self, server):
        datasets = [_ds("a/1"), _ds("b/1", REPO_B), _ds("a/2")]
        sink = io.StringIO()
        conns = Connections(Credentials({REPO_A: "a", REPO_B: "b"}), output=sink)
        pruner = PruneOrchestrator(conns, output=sink)
        order: list[str] = []

        def record_prune(**kwargs):
            order.append(kwargs["backup_id"])
            return OK

        server["prune"].side_effect = record_prune

        with patch.object(backup_mod, "discover_datasets", return_value=datasets):
            counts = pruner.run()

        assert counts == (3, 0)
        # Both repository-A datasets are pruned together, before repository B's.
        assert [i.removeprefix(f"{pruner.hostname}-") for i in order] == ["a-1", "a-2", "b-1"]
        assert len(conns._clients) == 2


def _snapshot(backup_id, comment, when=datetime(2026, 11, 1, 2, 0)):
    return BackupSnapshot("host", backup_id, when, comment=comment)


class TestSeparatorGuard:
    def test_the_separator_in_use_is_recorded_after_a_backup(self, server):
        ds = _ds("tank/data", extra={PROP_ID_SEPARATOR: "--"})
        orch, _, patcher = _orch(Credentials({REPO_A: "a"}), [ds])
        snap = _snapshot(ds.get_backup_id(orch.hostname), None)
        server["snaps"].return_value = [snap]

        with patcher:
            summary = orch.run(None)

        assert summary.successful == 1
        args = server["notes"].call_args.args
        assert args[0] is snap
        assert args[1] == "zpbs:id-separator=--"

    def test_a_changed_separator_fails_the_dataset_naming_both_values(self, server):
        ds = _ds("tank/data", extra={PROP_ID_SEPARATOR: "--"})
        orch, _, patcher = _orch(Credentials({REPO_A: "a"}), [ds])
        old_id = ds.get_backup_id(orch.hostname, "-")
        server["snaps"].return_value = [_snapshot(old_id, "zpbs:id-separator=-")]

        with patcher:
            summary = orch.run(None)

        assert summary.failed == 1
        error = summary.results[0].error or ""
        assert "'--'" in error and "'-'" in error
        server["backup"].assert_not_called()

    def test_reverting_the_property_clears_the_failure(self, server):
        ds = _ds("tank/data")  # default separator again
        orch, _, patcher = _orch(Credentials({REPO_A: "a"}), [ds])
        server["snaps"].return_value = [
            _snapshot(ds.get_backup_id(orch.hostname, "-"), "zpbs:id-separator=-")
        ]

        with patcher:
            summary = orch.run(None)

        assert summary.successful == 1

    def test_only_the_newest_snapshots_record_counts(self, server):
        ds = _ds("tank/data", extra={PROP_ID_SEPARATOR: "--"})
        orch, _, patcher = _orch(Credentials({REPO_A: "a"}), [ds])
        server["snaps"].return_value = [
            _snapshot(ds.get_backup_id(orch.hostname, "-"), "zpbs:id-separator=-",
                      datetime(2026, 10, 1)),
            _snapshot(ds.get_backup_id(orch.hostname, "--"), "zpbs:id-separator=--",
                      datetime(2026, 11, 1)),
        ]

        with patcher:
            assert orch.run(None).successful == 1

    def test_snapshots_without_a_record_are_accepted(self, server):
        ds = _ds("tank/data", extra={PROP_ID_SEPARATOR: "--"})
        orch, _, patcher = _orch(Credentials({REPO_A: "a"}), [ds])
        new = _snapshot(ds.get_backup_id(orch.hostname, "--"), None)
        server["snaps"].return_value = [
            _snapshot(ds.get_backup_id(orch.hostname, "-"), None, datetime(2026, 10, 1)),
            new,
        ]

        with patcher:
            summary = orch.run(None)

        assert summary.successful == 1
        assert server["notes"].call_args.args[0] is new
        assert server["notes"].call_args.args[1] == "zpbs:id-separator=--"

    def test_the_guard_applies_per_dataset(self, server):
        changed = _ds("tank/changed", extra={PROP_ID_SEPARATOR: "--"})
        same = _ds("tank/same")
        orch, _, patcher = _orch(Credentials({REPO_A: "a"}), [changed, same])
        server["snaps"].return_value = [
            _snapshot(changed.get_backup_id(orch.hostname, "-"), "zpbs:id-separator=-"),
            _snapshot(same.get_backup_id(orch.hostname, "-"), "zpbs:id-separator=-"),
        ]

        with patcher:
            summary = orch.run(None)

        by_name = {r.dataset.name: r.success for r in summary.results}
        assert by_name == {"tank/changed": False, "tank/same": True}

    def test_recorded_separator_parsing(self):
        assert recorded_separator("zpbs:id-separator=--") == "--"
        assert recorded_separator("note\nzpbs:id-separator=_x") == "_x"
        assert recorded_separator("nothing here") is None
        assert recorded_separator(None) is None


class TestPropertyValidation:
    def test_set_repository_accepts_the_documented_form(self):
        assert validate_property_value(PROP_REPOSITORY, "user@realm!tok@host:ds") == (True, "")

    @pytest.mark.parametrize("bad", ["", "host:ds", "u@r@host:ds", "u@r!tok@host"])
    def test_set_repository_rejects_the_rest(self, bad):
        assert validate_property_value(PROP_REPOSITORY, bad)[0] is False

    def test_fingerprint_keyfile_and_separator(self):
        assert validate_property_value(PROP_FINGERPRINT, "AA:bb:09")[0]
        assert not validate_property_value(PROP_FINGERPRINT, "xyz")[0]
        assert validate_property_value(PROP_KEYFILE, "/etc/k.enc")[0]
        assert not validate_property_value(PROP_KEYFILE, "k.enc")[0]
        assert validate_property_value(PROP_ID_SEPARATOR, "--")[0]
        assert not validate_property_value(PROP_ID_SEPARATOR, "/")[0]

    def test_cli_set_repository_runs_zfs_set(self):
        with patch.object(cli_mod, "set_property") as zfs_set:
            result = CliRunner().invoke(
                main, ["set", "repository=user@realm!tok@host:ds", "tank"]
            )

        assert result.exit_code == 0, result.output
        zfs_set.assert_called_once_with("tank", PROP_REPOSITORY, "user@realm!tok@host:ds")

    def test_a_malformed_repository_property_is_a_property_error(self):
        errors = _ds("tank/data", repository="nonsense").property_errors()
        assert [e.prop for e in errors] == [PROP_REPOSITORY]
        with pytest.raises(InvalidPropertyError):
            _ds("tank/data", repository="nonsense").repository  # noqa: B018

    def test_the_separator_is_inherited_into_the_backup_id(self):
        ds = _ds("tank/a/b", extra={PROP_ID_SEPARATOR: "--"})
        assert ds.get_backup_id("h") == "h--tank--a--b"


class TestStatusConnections:
    def _invoke(self, datasets, credentials=None, args=("status", "--connections")):
        creds = credentials if credentials is not None else Credentials({REPO_A: "SECRETVALUE"})
        with patch.object(cli_mod, "load_credentials", return_value=creds), \
             patch.object(cli_mod, "get_all_datasets", return_value=datasets), \
             patch.object(connections_mod, "PBSClient") as constructed:
            result = CliRunner().invoke(main, list(args))
        constructed.assert_not_called()
        return result

    def test_shows_each_setting_with_its_source_and_credential_presence(self):
        ds = _ds("tank/data", source="inherited from tank")
        ds.properties[PROP_FINGERPRINT] = PropertyValue("AA:BB", "inherited from tank")
        ds.properties[PROP_KEYFILE] = PropertyValue("/k.enc", "local")

        result = self._invoke([ds])

        assert result.exit_code == 0, result.output
        assert f"{REPO_A}  (inherited from tank)" in result.output
        assert "AA:BB  (inherited from tank)" in result.output
        assert "/k.enc  (local)" in result.output
        assert "-  (default)" in result.output
        assert "credentials  yes" in result.output

    def test_never_prints_the_secret(self):
        result = self._invoke([_ds("tank/data")])
        assert "SECRETVALUE" not in result.output
        as_json = self._invoke(
            [_ds("tank/data")], args=("status", "--connections", "--json")
        )
        assert "SECRETVALUE" not in as_json.output

    def test_reports_a_repository_with_no_credentials_entry(self):
        result = self._invoke([_ds("tank/data")], credentials=Credentials({}))
        assert "credentials  no" in result.output

    def test_reports_a_dataset_with_no_repository(self):
        result = self._invoke([_ds("tank/data", repository=None)])
        assert "repository   -  (not set)" in result.output
        assert "credentials  no" in result.output

    def test_lists_only_backup_enabled_datasets(self):
        off = Dataset("tank/off", {PROP_REPOSITORY: PropertyValue(REPO_A, "local")})
        result = self._invoke([_ds("tank/on"), off])
        assert "tank/on" in result.output and "tank/off" not in result.output

    def test_json_form(self):
        result = self._invoke([_ds("tank/data")], args=("status", "--connections", "--json"))
        row = json.loads(result.output)["datasets"][0]
        assert row["repository"] == {"value": REPO_A, "source": "local"}
        assert row["id_separator"] == {"value": "-", "source": "default"}
        assert row["credentials"] == "yes"

    def test_unreadable_credentials_are_reported_not_fatal(self):
        from zpbs_backup.config import CredentialsError

        with patch.object(cli_mod, "load_credentials", side_effect=CredentialsError("mode 0644")), \
             patch.object(cli_mod, "get_all_datasets", return_value=[_ds("tank/data")]):
            result = CliRunner().invoke(main, ["status", "--connections"])

        assert result.exit_code == 0
        assert "credentials  unknown" in result.output


class TestCheckCommand:
    def _invoke(self, datasets, credentials):
        with patch.object(cli_mod, "load_credentials", return_value=credentials), \
             patch.object(cli_mod, "discover_datasets", return_value=datasets):
            return CliRunner().invoke(main, ["check"])

    def test_one_login_per_repository(self, server):
        datasets = [_ds("a/1"), _ds("a/2"), _ds("b/1", REPO_B)]

        result = self._invoke(datasets, Credentials({REPO_A: "a", REPO_B: "b"}))

        assert result.exit_code == 0, result.output
        assert server["check"].call_count == 2
        assert f"OK             {REPO_A} (2 dataset(s))" in result.output

    def test_a_repository_without_a_credential_is_failed_and_not_contacted(self, server):
        result = self._invoke([_ds("a/1"), _ds("b/1", REPO_B)], Credentials({REPO_A: "a"}))

        assert result.exit_code == 1
        assert f"NO CREDENTIAL  {REPO_B}" in result.output
        assert server["check"].call_count == 1

    def test_a_rejected_token_fails(self, server):
        server["check"].side_effect = ConnectionError("permission check failed")

        result = self._invoke([_ds("a/1")], Credentials({REPO_A: "a"}))

        assert result.exit_code == 1
        assert "REJECTED" in result.output and "permission check failed" in result.output

    def test_a_dataset_with_no_repository_fails(self, server):
        result = self._invoke([_ds("a/1", repository=None)], Credentials({}))

        assert result.exit_code == 1
        assert "NO REPOSITORY  a/1" in result.output
        server["check"].assert_not_called()


class TestMigrateConfig:
    CONF = (
        'PBS_REPOSITORY="backup@pbs!mytoken@pbs.example.com:backups"\n'
        'PBS_API_TOKEN_SECRET="the-secret"\nPBS_FINGERPRINT="AA:BB"\n'
        'PBS_ENCRYPTION_KEYFILE="/k.enc"\nZPBS_BACKUP_ID_SEPARATOR="--"\n'
        'ZPBS_PUSHGATEWAY="http://gw:9091"\nZPBS_SYSLOG="false"\n'
    )
    REPO = "backup@pbs!mytoken@pbs.example.com:backups"

    def _invoke(self, tmp_path, *extra, datasets=None, conf=None):
        pbs_conf = tmp_path / "pbs.conf"
        pbs_conf.write_text(conf if conf is not None else self.CONF)
        creds = tmp_path / "etc" / "credentials"
        enabled = datasets
        if enabled is None:
            enabled = [_ds("tank/data", None), _ds("pool/x", None)]
        with patch.object(cli_mod, "get_all_datasets", return_value=enabled), \
             patch.object(cli_mod, "set_property") as zfs_set:
            result = CliRunner().invoke(
                main,
                ["migrate-config", "--pbs-conf", str(pbs_conf), "--credentials", str(creds),
                 *extra],
            )
        return result, creds, pbs_conf, zfs_set

    def test_writes_the_credentials_file_private_and_leaves_pbs_conf(self, tmp_path):
        result, creds, pbs_conf, _ = self._invoke(tmp_path)

        assert result.exit_code == 0, result.output
        assert creds.stat().st_mode & 0o777 == 0o600
        loaded = load_credentials(creds, owner_uid=os.getuid())
        assert loaded.secret_for(self.REPO) == "the-secret"
        assert pbs_conf.read_text() == self.CONF

    def test_never_prints_the_secret(self, tmp_path):
        result, *_ = self._invoke(tmp_path)
        assert "the-secret" not in result.output

    def test_never_overwrites_an_existing_credentials_file(self, tmp_path):
        creds = tmp_path / "etc" / "credentials"
        creds.parent.mkdir()
        creds.write_text("[other]\nsecret = keep\n")

        result, _, _, _ = self._invoke(tmp_path)

        assert result.exit_code == 0
        assert creds.read_text() == "[other]\nsecret = keep\n"
        assert "was not changed" in result.output

    def test_prints_zfs_set_commands_for_the_pool_roots_without_applying(self, tmp_path):
        result, _, _, zfs_set = self._invoke(tmp_path)

        out = result.output
        assert f"zfs set 'zpbs:repository={self.REPO}' tank" in out
        assert f"zfs set 'zpbs:repository={self.REPO}' pool" in out
        assert "zfs set zpbs:fingerprint=AA:BB tank" in out
        assert "zfs set zpbs:keyfile=/k.enc tank" in out
        assert "zfs set zpbs:id-separator=-- tank" in out
        zfs_set.assert_not_called()

    def test_apply_sets_the_properties(self, tmp_path):
        _, _, _, zfs_set = self._invoke(tmp_path, "--apply")

        applied = {(c.args[0], c.args[1], c.args[2]) for c in zfs_set.call_args_list}
        assert ("tank", PROP_REPOSITORY, self.REPO) in applied
        assert ("pool", PROP_KEYFILE, "/k.enc") in applied
        assert len(applied) == 8

    def test_prints_reporting_settings_as_environment_lines(self, tmp_path):
        result, *_ = self._invoke(tmp_path)

        assert "Environment=ZPBS_PUSHGATEWAY=http://gw:9091" in result.output
        assert "Environment=ZPBS_SYSLOG=false" in result.output

    def test_a_pbs_conf_without_a_repository_is_an_error(self, tmp_path):
        result, creds, *_ = self._invoke(tmp_path, conf="# empty\n")

        assert result.exit_code == 1
        assert not creds.exists()

    def test_apply_without_datasets_is_refused(self, tmp_path):
        result, _, _, zfs_set = self._invoke(tmp_path, "--apply", datasets=[])

        assert result.exit_code == 1
        assert "<pool>" in result.output
        zfs_set.assert_not_called()


def test_the_removed_commands_are_gone():
    result = CliRunner().invoke(main, ["show-config"])
    assert result.exit_code != 0
