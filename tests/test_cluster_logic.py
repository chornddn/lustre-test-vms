"""Tests for ltvm_pkg/vm_cluster.py: spec parsing, local.sh generation."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import vm_cluster
from ltvm_pkg.lnet_net import resolve_net
from ltvm_pkg.vm_state import ClusterInfo

# ── parse_node_spec ──────────────────────────────────────


class TestParseNodeSpec:
    """parse_node_spec accepts roles:name[:disks] and rejects garbage."""

    def test_mgs_mds_combined_defaults_to_one_mdt(self) -> None:
        """mgs+mds with no disk count gets the minimum 1 MDT disk."""
        n = vm_cluster.parse_node_spec("mgs+mds:co1-mds")
        assert n.roles == ["mgs", "mds"]
        assert n.is_mgs and n.is_mds
        assert n.mdt_disks == 1
        assert n.ost_disks == 0

    def test_mds_with_explicit_disk_count(self) -> None:
        """Explicit disk count overrides the minimum."""
        n = vm_cluster.parse_node_spec("mds:co1-mds:3")
        assert n.mdt_disks == 3
        assert n.ost_disks == 0

    def test_oss_disk_count_goes_to_ost(self) -> None:
        n = vm_cluster.parse_node_spec("oss:co1-oss:4")
        assert n.mdt_disks == 0
        assert n.ost_disks == 4
        assert n.is_oss

    def test_client_no_disks(self) -> None:
        """Client role gets no MDT or OST disks."""
        n = vm_cluster.parse_node_spec("client:co1-client")
        assert n.is_client
        assert n.mdt_disks == 0
        assert n.ost_disks == 0

    def test_mgs_alone_no_disks(self) -> None:
        """mgs without mds gets no MDT disks from parse_node_spec itself;
        the extra MGS disk is added later at create time."""
        n = vm_cluster.parse_node_spec("mgs:co1-mgs")
        assert n.is_mgs and not n.is_mds
        assert n.mdt_disks == 0

    def test_oss_default_to_one(self) -> None:
        """oss with no count still gets a minimum of 1 OST."""
        n = vm_cluster.parse_node_spec("oss:co1-oss")
        assert n.ost_disks == 1

    def test_unknown_role_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_cluster.parse_node_spec("junk:co1-x")

    def test_missing_name_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_cluster.parse_node_spec("mds")

    def test_invalid_vm_name_dies(self) -> None:
        """Names with spaces or leading hyphen are rejected by validator."""
        with pytest.raises(SystemExit):
            vm_cluster.parse_node_spec("mds:-bad-name:1")
        with pytest.raises(SystemExit):
            vm_cluster.parse_node_spec("mds:bad name:1")

    def test_non_integer_disk_count_dies_cleanly(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A typo like 'mds:foo:abc' must produce a clean error
        message via die(), not a raw ValueError traceback."""
        with pytest.raises(SystemExit):
            vm_cluster.parse_node_spec("mds:co1-mds:abc")
        err = capsys.readouterr().err
        assert "abc" in err
        assert "integer" in err.lower() or "disk" in err.lower()

    def test_role_case_insensitive(self) -> None:
        """Roles are lowercased before comparison."""
        n = vm_cluster.parse_node_spec("MDS:co1-mds:2")
        assert n.roles == ["mds"]
        assert n.mdt_disks == 2


# ── generate_local_sh ────────────────────────────────────


def _cluster(*nodes) -> ClusterInfo:
    """Build a ClusterInfo from (name, roles, mdt, ost, ip) tuples."""
    return ClusterInfo(
        name="testc",
        nodes=[
            {
                "name": n[0],
                "roles": list(n[1]),
                "mdt_disks": n[2],
                "ost_disks": n[3],
                "ip": n[4],
            }
            for n in nodes
        ],
    )


class TestGenerateLocalSh:
    """generate_local_sh produces a valid cfg/local.sh for Lustre tests."""

    def test_combined_mgs_mds_plus_oss(self) -> None:
        """Classic MGS+MDS on one node, OSS on another."""
        c = _cluster(
            ("co2-mds", ["mgs", "mds"], 1, 0, "10.0.0.10"),
            ("co2-oss", ["oss"], 0, 3, "10.0.0.11"),
        )
        text = vm_cluster.generate_local_sh(c, resolve_net(c, "tcp"))
        assert "mgs_HOST=co2-mds" in text
        assert "MGSNID=10.0.0.10@tcp" in text
        # combined=True -> no separate MGSDEV
        assert "MGSDEV" not in text
        assert "mds_HOST=co2-mds" in text
        assert "MDSCOUNT=1" in text
        assert "MDSDEV1=/dev/vdb" in text
        assert "ost_HOST=co2-oss" in text
        assert "OSTCOUNT=3" in text
        # OSS is not MDS/MGS, so ost disks start at vdb
        assert "OSTDEV1=/dev/vdb" in text
        assert "OSTDEV2=/dev/vdc" in text
        assert "OSTDEV3=/dev/vdd" in text

    def test_split_mgs_mds_oss(self) -> None:
        """Three dedicated nodes: MGS with its own disk, separate MDS."""
        c = _cluster(
            ("co3-mgs", ["mgs"], 0, 0, "10.0.0.1"),
            ("co3-mds", ["mds"], 1, 0, "10.0.0.2"),
            ("co3-oss", ["oss"], 0, 2, "10.0.0.3"),
        )
        text = vm_cluster.generate_local_sh(c, resolve_net(c, "tcp"))
        assert "mgs_HOST=co3-mgs" in text
        # standalone MGS -> MGSDEV is set
        assert "MGSDEV=/dev/vdb" in text
        assert "mds_HOST=co3-mds" in text
        assert "MDSDEV1=/dev/vdb" in text
        # OSS doesn't host MGS, starts at vdb
        assert "OSTDEV1=/dev/vdb" in text
        assert "OSTDEV2=/dev/vdc" in text

    def test_multi_mds_numbers_hosts(self) -> None:
        """Two MDS nodes get per-index MDSDEV + mdsN_HOST entries."""
        c = _cluster(
            ("co-mgs", ["mgs"], 0, 0, "10.0.0.1"),
            ("co-mds1", ["mds"], 1, 0, "10.0.0.2"),
            ("co-mds2", ["mds"], 1, 0, "10.0.0.3"),
            ("co-oss", ["oss"], 0, 1, "10.0.0.4"),
        )
        text = vm_cluster.generate_local_sh(c, resolve_net(c, "tcp"))
        assert "MDSCOUNT=2" in text
        assert "MDSDEV1=/dev/vdb" in text
        assert "MDSDEV2=/dev/vdb" in text  # each on its own node
        assert "mds1_HOST=co-mds1" in text
        assert "mds2_HOST=co-mds2" in text

    def test_multi_oss_numbers_hosts(self) -> None:
        """Multiple OSS nodes get per-index OSTDEV + ostN_HOST entries."""
        c = _cluster(
            ("co-mds", ["mgs", "mds"], 1, 0, "10.0.0.1"),
            ("co-oss1", ["oss"], 0, 2, "10.0.0.2"),
            ("co-oss2", ["oss"], 0, 1, "10.0.0.3"),
        )
        text = vm_cluster.generate_local_sh(c, resolve_net(c, "tcp"))
        assert "OSTCOUNT=3" in text
        # oss1: OST 1+2, vdb+vdc on co-oss1; oss2: OST 3, vdb on co-oss2
        assert "OSTDEV1=/dev/vdb" in text
        assert "OSTDEV2=/dev/vdc" in text
        assert "OSTDEV3=/dev/vdb" in text  # new node, reset to vdb
        assert "ost1_HOST=co-oss1" in text
        assert "ost2_HOST=co-oss1" in text
        assert "ost3_HOST=co-oss2" in text

    def test_combined_mds_oss_disk_offset(self) -> None:
        """A single node hosting MDS+OSS: OST disks start after MDT disks."""
        c = _cluster(
            ("co-all", ["mgs", "mds", "oss"], 2, 2, "10.0.0.1"),
        )
        text = vm_cluster.generate_local_sh(c, resolve_net(c, "tcp"))
        # MDT: vdb, vdc; OST: vdd, vde
        assert "MDSDEV1=/dev/vdb" in text
        assert "MDSDEV2=/dev/vdc" in text
        assert "OSTDEV1=/dev/vdd" in text
        assert "OSTDEV2=/dev/vde" in text

    def test_clients_listed(self) -> None:
        """Client nodes are available to mounting and test-framework setup."""
        c = _cluster(
            ("co-mds", ["mgs", "mds"], 1, 0, "10.0.0.1"),
            ("co-oss", ["oss"], 0, 1, "10.0.0.2"),
            ("co-c1", ["client"], 0, 0, "10.0.0.3"),
            ("co-c2", ["client"], 0, 0, "10.0.0.4"),
        )
        text = vm_cluster.generate_local_sh(c, resolve_net(c, "tcp"))
        assert "CLIENTS=co-c1,co-c2" in text
        assert 'RCLIENTS="co-c2"' in text

    def test_rclients_excludes_test_runner(self) -> None:
        """RCLIENTS= lists the clients other than the first.

        init_clients_lists() rebuilds CLIENTS from RCLIENTS, so without
        this the remote clients drop out of every multi-client suite.
        """
        c = _cluster(
            ("co-mds", ["mgs", "mds"], 1, 0, "10.0.0.1"),
            ("co-c1", ["client"], 0, 0, "10.0.0.3"),
            ("co-c2", ["client"], 0, 0, "10.0.0.4"),
            ("co-c3", ["client"], 0, 0, "10.0.0.5"),
        )
        text = vm_cluster.generate_local_sh(c, resolve_net(c, "tcp"))
        assert 'RCLIENTS="co-c2 co-c3"' in text

    def test_rclients_omitted_for_single_client(self) -> None:
        """One client means no remote clients -- don't emit RCLIENTS."""
        c = _cluster(
            ("co-mds", ["mgs", "mds"], 1, 0, "10.0.0.1"),
            ("co-c1", ["client"], 0, 0, "10.0.0.3"),
        )
        text = vm_cluster.generate_local_sh(c, resolve_net(c, "tcp"))
        assert "CLIENTS=co-c1" in text
        assert "RCLIENTS" not in text

    def test_rhel_libdir_default(self) -> None:
        c = _cluster(("n", ["mgs", "mds"], 1, 0, "10.0.0.1"))
        text = vm_cluster.generate_local_sh(
            c, resolve_net(c, "tcp"), os_family="rhel")
        assert "LUSTRE=/usr/lib64/lustre" in text
        assert "RLUSTRE=/usr/lib64/lustre" in text
        assert "RPWD=/usr/lib64/lustre/tests" in text

    def test_debian_libdir(self) -> None:
        c = _cluster(("n", ["mgs", "mds"], 1, 0, "10.0.0.1"))
        text = vm_cluster.generate_local_sh(
            c, resolve_net(c, "tcp"), os_family="debian")
        assert "LUSTRE=/usr/lib/lustre" in text
        assert "RPWD=/usr/lib/lustre/tests" in text

    def test_common_invariants(self) -> None:
        """Every cluster config gets the standard fsname/net/ldiskfs block."""
        c = _cluster(("n", ["mgs", "mds"], 1, 0, "10.0.0.1"))
        text = vm_cluster.generate_local_sh(c, resolve_net(c, "tcp"))
        assert "FSNAME=lustre" in text
        assert "NETTYPE=tcp" in text
        assert "FSTYPE=ldiskfs" in text
        assert "MOUNT=/mnt/lustre" in text
        assert "MOUNT2=/mnt/lustre2" in text
        assert "DIR=${DIR:-$MOUNT}" in text
        assert "DIR1=${DIR1:-$MOUNT1}" in text
        assert "DIR2=${DIR2:-$MOUNT2}" in text
        assert "LOAD_MODULES_REMOTE=true" in text


# ── _validate_lustre_source ──────────────────────────────


class TestValidateLustreSource:
    """_validate_lustre_source catches obvious non-Lustre-tree inputs."""

    def test_rejects_non_directory(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            vm_cluster._validate_lustre_source(tmp_path / "nope")

    def test_rejects_missing_files(self, tmp_path: Path) -> None:
        """Empty dir is missing configure.ac, lustre/, lnet/."""
        with pytest.raises(SystemExit):
            vm_cluster._validate_lustre_source(tmp_path)

    def test_accepts_minimal_tree(self, tmp_path: Path) -> None:
        """A tree with the three sentinel entries passes."""
        (tmp_path / "configure.ac").write_text("")
        (tmp_path / "lustre").mkdir()
        (tmp_path / "lnet").mkdir()
        # Should not raise
        vm_cluster._validate_lustre_source(tmp_path)


# ── ClusterInfo helpers used by generate_local_sh ────────


class TestClusterInfoRoleQueries:
    """Role-query helpers on ClusterInfo feed generate_local_sh correctly."""

    def test_mgs_node_raises_when_missing(self) -> None:
        c = ClusterInfo(
            name="no-mgs",
            nodes=[
                {
                    "name": "lonely",
                    "roles": ["client"],
                    "mdt_disks": 0,
                    "ost_disks": 0,
                    "ip": "1.2.3.4",
                }
            ],
        )
        with pytest.raises(RuntimeError, match="no MGS"):
            c.mgs_node()

    def test_role_filters_split_correctly(self) -> None:
        """mds_nodes / oss_nodes / client_nodes isolate their roles."""
        c = _cluster(
            ("a", ["mgs", "mds"], 1, 0, "1.1.1.1"),
            ("b", ["oss"], 0, 1, "1.1.1.2"),
            ("c", ["client"], 0, 0, "1.1.1.3"),
            ("d", ["client"], 0, 0, "1.1.1.4"),
        )
        assert [n.name for n in c.mds_nodes()] == ["a"]
        assert [n.name for n in c.oss_nodes()] == ["b"]
        assert [n.name for n in c.client_nodes()] == ["c", "d"]
        assert c.mgs_node().name == "a"


class TestClusterBlockKeepsTheStockLocalSh:
    """The cluster settings go into the tree's own cfg/local.sh, after each
    node's disk block, instead of replacing the file.  The replacement
    carried only what someone had noticed missing: without TSTUSR,
    sanity-quota died at load in reset_quota_settings() ("clear quota for
    [type:-u name:] failed"), and before that RUNAS was missing too."""

    STOCK = (
        "FSNAME=${FSNAME:-lustre}\n"
        "mds_HOST=${mds_HOST:-$(hostname)}\n"
        "MDSCOUNT=${MDSCOUNT:-1}\n"
        'TSTUSR=${TSTUSR:-"quota_usr"}\n'
        'TSTUSR2=${TSTUSR2:-"quota_2usr"}\n'
        "if [ $UID -ne 0 ]; then\n"
        '\tRUNAS_ID="$UID"\n'
        "else\n"
        "\tRUNAS_ID=${RUNAS_ID:-500}\n"
        "fi\n"
    )
    DISK_BLOCK = (
        "\n# --- VM disk configuration (generated by ltvm deploy) ---\n"
        "OSTCOUNT=3\n"
        "OSTDEV1=/dev/vdb\n"
        "CLEANUP_DM_DEV=true\n"
        "# --- END VM disk configuration ---\n"
    )

    def _cluster(self) -> ClusterInfo:
        return _cluster(
            ("co2-mds", ["mgs", "mds"], 1, 0, "10.0.0.10"),
            ("co2-oss", ["oss"], 0, 3, "10.0.0.11"),
            ("co2-client", ["client"], 0, 0, "10.0.0.12"),
        )

    def _write(self, cfg: Path, local_sh: str) -> None:
        """Run the script _write_cluster_local_sh sends, against ``cfg``."""
        sent: dict[str, str] = {}

        def argv(ip: str, script: str) -> list[str]:
            sent["script"] = script
            return ["true"]

        with (
            patch.object(vm_cluster, "sshpass_ssh_argv", side_effect=argv),
            patch.object(vm_cluster.subprocess, "run") as run,
        ):
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            vm_cluster._write_cluster_local_sh(
                "co2-oss", "10.0.0.11", local_sh, []
            )
        script = (
            sent["script"]
            .replace("/usr/lib64/lustre/tests/cfg/local.sh", str(cfg))
            .replace("/usr/lib64/lustre/tests/cfg", str(cfg.parent))
        )
        subprocess.run(
            ["bash", "-c", script], input=local_sh, text=True, check=True
        )

    def _source(self, cfg: Path, *names: str) -> list[str]:
        echo = "; ".join(f'echo "${name}"' for name in names)
        out = subprocess.run(
            ["bash", "-c", f". {cfg}; {echo}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.splitlines()

    def test_the_stock_settings_stay_and_the_cluster_wins(
        self, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "cfg" / "local.sh"
        cfg.parent.mkdir()
        cfg.write_text(self.STOCK + self.DISK_BLOCK)

        self._write(cfg, vm_cluster.generate_local_sh(
            self._cluster(), resolve_net(self._cluster(), "tcp")
        ))

        tstusr, tstusr2, mds_host, ostcount, cleanup = self._source(
            cfg, "TSTUSR", "TSTUSR2", "mds_HOST", "OSTCOUNT", "CLEANUP_DM_DEV"
        )
        assert (tstusr, tstusr2) == ("quota_usr", "quota_2usr")
        assert mds_host == "co2-mds"
        assert ostcount == "3"
        # The client drives the tests and has no disk block of its own.
        assert cleanup == "true"
        body = cfg.read_text()
        assert body.index("VM disk configuration") < body.index(
            "Cluster configuration"
        )

    def test_a_second_deploy_rewrites_the_block_in_place(
        self, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "cfg" / "local.sh"
        cfg.parent.mkdir()
        cfg.write_text(self.STOCK + self.DISK_BLOCK)
        local_sh = vm_cluster.generate_local_sh(
            self._cluster(), resolve_net(self._cluster(), "tcp")
        )

        self._write(cfg, local_sh)
        self._write(cfg, local_sh)

        body = cfg.read_text()
        assert body.count("# --- Cluster configuration") == 1
        assert body.count("# --- END cluster configuration") == 1
        assert body.startswith(self.STOCK)

    def test_the_block_is_valid_shell(self) -> None:
        r = subprocess.run(
            ["bash", "-n"],
            input=vm_cluster.generate_local_sh(
            self._cluster(), resolve_net(self._cluster(), "tcp")
        ),
            text=True,
            capture_output=True,
        )
        assert r.returncode == 0, r.stderr

    def test_a_node_with_no_local_sh_gets_the_block(
        self, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "cfg" / "local.sh"
        cfg.parent.mkdir()
        self._write(cfg, vm_cluster.generate_local_sh(
            self._cluster(), resolve_net(self._cluster(), "tcp")
        ))
        assert "mds_HOST=co2-mds" in cfg.read_text()


class TestClusterJsonOutput:
    """`--json` was accepted on every `cluster` subcommand and read by
    none of them, so a machine consumer got a human table and exit 0.

    `cluster status` and `cluster list` are what an agent polls, and
    `cluster exec` is what it collects results from; those three emit
    documents now.  `create`/`destroy`/`deploy` still stream human
    progress under --json, and `ssh` execs an interactive session where
    the flag has no meaning.
    """

    def _args(self, **over):
        import argparse

        base = dict(name="testc", json=True)
        base.update(over)
        return argparse.Namespace(**base)

    def test_status_emits_a_document(
        self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        c = _cluster(
            ("testc-mds", ["mgs", "mds"], 1, 0, "192.168.100.11"),
            ("testc-oss1", ["oss"], 0, 2, "192.168.100.12"),
        )
        monkeypatch.setattr(ClusterInfo, "load", staticmethod(lambda n: c))
        monkeypatch.setattr(vm_cluster, "_node_state", lambda n: "up")

        vm_cluster.cmd_cluster_status(self._args())
        doc = json.loads(capsys.readouterr().out)

        assert doc["cluster"] == "testc"
        assert [n["name"] for n in doc["nodes"]] == [
            "testc-mds",
            "testc-oss1",
        ]
        mds = doc["nodes"][0]
        assert mds["roles"] == ["mgs", "mds"]
        assert mds["state"] == "up"
        assert mds["ip"] == "192.168.100.11"
        assert doc["nodes"][1]["ost_disks"] == 2

    def test_status_human_output_is_unchanged(
        self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = _cluster(("testc-mds", ["mgs", "mds"], 1, 0, "192.168.100.11"))
        monkeypatch.setattr(ClusterInfo, "load", staticmethod(lambda n: c))
        monkeypatch.setattr(vm_cluster, "_node_state", lambda n: "down")

        vm_cluster.cmd_cluster_status(self._args(json=False))
        out = capsys.readouterr().out

        assert "cluster: testc" in out
        assert "testc-mds" in out
        assert "stopped" in out
        assert "mgs+mds" in out
        assert "mdt=1" in out

    def test_list_emits_a_document(
        self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        c = _cluster(("testc-mds", ["mgs", "mds"], 1, 0, "192.168.100.11"))
        monkeypatch.setattr(
            ClusterInfo, "all_names", staticmethod(lambda: ["testc"])
        )
        monkeypatch.setattr(ClusterInfo, "load", staticmethod(lambda n: c))
        monkeypatch.setattr(vm_cluster, "_node_state", lambda n: "up")

        vm_cluster.cmd_cluster_list(self._args())
        doc = json.loads(capsys.readouterr().out)

        assert doc["clusters"][0]["cluster"] == "testc"
        assert doc["clusters"][0]["nodes"][0]["state"] == "up"

    def test_list_with_no_clusters_is_still_a_document(
        self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The empty case printed "(no clusters)" regardless of --json."""
        import json

        monkeypatch.setattr(ClusterInfo, "all_names", staticmethod(lambda: []))
        vm_cluster.cmd_cluster_list(self._args())
        assert json.loads(capsys.readouterr().out) == {"clusters": []}

    def test_list_reports_a_broken_cluster_file(
        self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        def boom(n):
            raise ValueError("bad node list")

        monkeypatch.setattr(
            ClusterInfo, "all_names", staticmethod(lambda: ["broken"])
        )
        monkeypatch.setattr(ClusterInfo, "load", staticmethod(boom))

        vm_cluster.cmd_cluster_list(self._args())
        doc = json.loads(capsys.readouterr().out)
        assert doc["clusters"][0]["error"] == "bad node list"

    def test_node_state_survives_a_corrupt_info(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """VMInfo.load raises ValueError by design on a truncated .info;
        one casualty must not take out a whole listing."""
        from ltvm_pkg.vm_state import VMInfo

        def boom(name):
            raise ValueError("invalid literal for int()")

        monkeypatch.setattr(VMInfo, "load", staticmethod(boom))
        assert vm_cluster._node_state("whatever") == "corrupt"


# ── cfg distribution ─────────────────────────────────────


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess from a cfg write."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = "" if returncode == 0 else "no space left on device"


class TestWriteClusterCfg:
    """_write_cluster_cfg targets <lustre libdir>/tests/cfg/<name>.sh."""

    def _capture(self, monkeypatch, rc: int = 0) -> list:
        calls: list = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return _FakeCompleted(rc)

        monkeypatch.setattr(vm_cluster.subprocess, "run", fake_run)
        return calls

    def test_profile_lands_at_cfg_path(self, monkeypatch) -> None:
        calls = self._capture(monkeypatch)
        name, rc, _ = vm_cluster._write_cluster_cfg(
            "co2-oss", "10.0.0.11", "co2sn", "OSTCOUNT=2\n", [], "rhel"
        )
        assert (name, rc) == ("co2-oss", 0)
        argv, kwargs = calls[0]
        assert "/usr/lib64/lustre/tests/cfg/co2sn.sh" in " ".join(argv)
        assert kwargs["input"] == "OSTCOUNT=2\n"

    def test_local_sh_path_is_unchanged_by_the_refactor(
        self, monkeypatch
    ) -> None:
        """Regression guard: 'local' still writes cfg/local.sh."""
        calls = self._capture(monkeypatch)
        vm_cluster._write_cluster_cfg(
            "co2-mds", "10.0.0.10", "local", "FSNAME=lustre\n", [], "rhel"
        )
        argv, _ = calls[0]
        assert "/usr/lib64/lustre/tests/cfg/local.sh" in " ".join(argv)

    def test_write_failure_is_reported_not_raised(self, monkeypatch) -> None:
        self._capture(monkeypatch, rc=1)
        name, rc, out = vm_cluster._write_cluster_cfg(
            "co2-cli", "10.0.0.12", "co2sn", "x\n", [], "rhel"
        )
        assert (name, rc) == ("co2-cli", 1)
        assert "no space" in out

    def test_timeout_returns_failure_tuple(self, monkeypatch) -> None:
        """A stalled node must not traceback out of the thread pool."""

        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=10)

        monkeypatch.setattr(vm_cluster.subprocess, "run", fake_run)
        name, rc, out = vm_cluster._write_cluster_cfg(
            "co2-oss", "10.0.0.11", "co2sn", "x\n", [], "rhel"
        )
        assert (name, rc) == ("co2-oss", 1)
        assert "timed out" in out


class TestLoadCfgProfiles:
    """_load_cfg_profiles validates --cfg-dir before anything is deployed."""

    def test_reads_every_sh_file(self, tmp_path: Path) -> None:
        (tmp_path / "co2sn.sh").write_text("OSTCOUNT=2\n")
        (tmp_path / "aaa.sh").write_text("A=1\n")
        (tmp_path / "notes.txt").write_text("ignored\n")
        got = vm_cluster._load_cfg_profiles(tmp_path)
        assert got == [("aaa", "A=1\n"), ("co2sn", "OSTCOUNT=2\n")]

    def test_rejects_local_sh(self, tmp_path: Path) -> None:
        """local.sh is generated by ltvm; a profile may not replace it."""
        (tmp_path / "local.sh").write_text("FSNAME=lustre\n")
        with pytest.raises(SystemExit):
            vm_cluster._load_cfg_profiles(tmp_path)

    def test_rejects_missing_directory(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            vm_cluster._load_cfg_profiles(tmp_path / "nope")

    def test_rejects_empty_directory(self, tmp_path: Path) -> None:
        """An empty --cfg-dir is an operator error, not a silent no-op."""
        with pytest.raises(SystemExit):
            vm_cluster._load_cfg_profiles(tmp_path)


class _FakeVM:
    def __init__(self, name: str, ip: str, nics=None, nic_ips=None) -> None:
        self.name = name
        self.ip = ip
        self.kver = "5.14"
        self.variant = "base"
        self.nics = list(nics or [])
        self.nic_ips = list(nic_ips or [])

    def update_deploy(self, *a, **kw) -> None:
        pass


def _deploy_harness(
    monkeypatch, tmp_path: Path, write_rc, staging=True, nics=(),
):
    """Patch cmd_cluster_deploy's I/O; return the list of cfg writes.

    *nics* is the ``--nic`` list every node was created with; the extra
    NIC addresses follow the real allocator's shape (one subnet, one
    address per NIC per node).
    """
    cluster = _cluster(
        ("co2-mds", ["mgs", "mds"], 1, 0, "10.0.0.10"),
        ("co2-oss", ["oss"], 0, 1, "10.0.0.11"),
        ("co2-cli", ["client"], 0, 0, "10.0.0.12"),
    )
    ips = {"co2-mds": "10.0.0.10", "co2-oss": "10.0.0.11",
           "co2-cli": "10.0.0.12"}
    nic_ips = {
        "co2-mds": ["172.16.100.10", "172.16.100.13"],
        "co2-oss": ["172.16.100.11", "172.16.100.14"],
        "co2-cli": ["172.16.100.12", "172.16.100.15"],
    }
    monkeypatch.setattr(vm_cluster.ClusterInfo, "save", lambda self: None)

    src = tmp_path / "lustre-release"
    (src / "lustre").mkdir(parents=True)
    (src / "lnet").mkdir()
    (src / "configure.ac").write_text("")
    if staging:
        from ltvm_pkg.lustre_build import staging_path as _sp

        stage = _sp(src, "rocky9", arch="x86_64", kernel="5.14-rhel9.7")
        stage.mkdir(parents=True)
        (stage / "lustre.ko").write_text("")
        (stage / ".ltvm-staging-stamp").write_text("5.14.0\n")

    writes: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        vm_cluster.ClusterInfo, "load", classmethod(lambda cls, n: cluster)
    )
    monkeypatch.setattr(
        vm_cluster.VMInfo,
        "load",
        classmethod(
            lambda cls, n: _FakeVM(
                n, ips[n], list(nics), nic_ips[n][: len(nics)]
            )
        ),
    )
    monkeypatch.setattr(
        vm_cluster,
        "_deploy_one_node",
        lambda name, build, os_family, *a: (name, 0, "ok"),
    )
    monkeypatch.setattr(
        vm_cluster,
        "cluster_build_params",
        lambda c: vm_cluster.ClusterBuildParams(
            target="rocky9", os_family="rhel",
            kernel="5.14-rhel9.7", arch="x86_64",
        ),
    )

    def fake_run(argv, **kwargs):
        # cfg and lnet.conf writes carry input=; anything else would be
        # a build, which deploy must never spawn.
        if "input" not in kwargs:
            return _FakeCompleted(0)
        cmd = [
            a for a in argv
            if "/tests/cfg/" in a or vm_cluster.LNET_CONF_PATH in a
        ][0]
        written = re.search(
            r"\S*(?:/tests/cfg/\w+\.sh|" + re.escape(vm_cluster.LNET_CONF_PATH) + ")",
            cmd,
        ).group(0)
        node = [a for a in argv if a.startswith("root@")][0]
        writes.append((node, written, kwargs["input"]))
        return _FakeCompleted(write_rc(written))

    monkeypatch.setattr(vm_cluster.subprocess, "run", fake_run)
    monkeypatch.setattr(
        vm_cluster, "run_ssh", lambda ip, cmd, timeout=0: _FakeCompleted(0)
    )
    return src, writes


class TestDeployCfgDir:
    """cluster deploy --cfg-dir distributes profiles after local.sh."""

    def test_distributes_every_profile_to_every_node(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        (cfg_dir / "co2sn.sh").write_text("OSTCOUNT=2\n")
        (cfg_dir / "co2big.sh").write_text("OSTSIZE=900000\n")
        src, writes = _deploy_harness(monkeypatch, tmp_path, lambda c: 0)

        vm_cluster.cmd_cluster_deploy(
            argparse.Namespace(
                name="co2", lustre_tree=str(src), cfg_dir=str(cfg_dir),
            )
        )

        for cfg in ("local.sh", "co2sn.sh", "co2big.sh"):
            nodes = {n for n, c, _ in writes if c.endswith("/" + cfg)}
            assert len(nodes) == 3, f"{cfg} reached {nodes}"
        # local.sh first, so a profile sourcing it finds it in place.
        order = [c.rsplit("/", 1)[-1] for _, c, _ in writes]
        assert order.index("local.sh") < order.index("co2sn.sh")

    def test_profile_write_failure_is_fatal(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A failed profile write must not be a silent partial deploy."""
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        (cfg_dir / "co2sn.sh").write_text("OSTCOUNT=2\n")
        src, _ = _deploy_harness(
            monkeypatch, tmp_path,
            lambda c: 1 if c.endswith("co2sn.sh") else 0,
        )

        with pytest.raises(SystemExit):
            vm_cluster.cmd_cluster_deploy(
                argparse.Namespace(
                    name="co2", lustre_tree=str(src),
                    cfg_dir=str(cfg_dir),
                )
            )

    def test_no_cfg_dir_writes_only_the_net_config(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Without --cfg-dir, deploy writes the two files it owns.

        local.sh and lnet.conf are one pair: a node holding one without
        the other names two different networks and cannot mount.
        """
        src, writes = _deploy_harness(monkeypatch, tmp_path, lambda c: 0)
        vm_cluster.cmd_cluster_deploy(
            argparse.Namespace(
                name="co2", lustre_tree=str(src), cfg_dir=None,
            )
        )
        assert {c.rsplit("/", 1)[-1] for _, c, _ in writes} == {
            "local.sh", "lnet.conf",
        }

    def test_missing_staging_refuses_and_builds_nothing(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Deploy names the build command rather than running one."""
        src, writes = _deploy_harness(
            monkeypatch, tmp_path, lambda c: 0, staging=False
        )
        with pytest.raises(SystemExit):
            vm_cluster.cmd_cluster_deploy(
                argparse.Namespace(
                    name="co2", lustre_tree=str(src), cfg_dir=None
                )
            )
        out = capsys.readouterr()
        assert "ltvm build lustre --for-cluster testc" in out.out + out.err
        assert "--configure" in out.out + out.err
        assert writes == []

    def test_cfg_dir_with_local_sh_is_rejected_before_the_build(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        (cfg_dir / "local.sh").write_text("FSNAME=nope\n")
        src, writes = _deploy_harness(monkeypatch, tmp_path, lambda c: 0)

        with pytest.raises(SystemExit):
            vm_cluster.cmd_cluster_deploy(
                argparse.Namespace(
                    name="co2", lustre_tree=str(src),
                    cfg_dir=str(cfg_dir),
                )
            )
        assert writes == []


class TestDeployNet:
    """deploy --net writes local.sh and lnet.conf from one resolved net.

    The pair is the correctness requirement: a node holding a local.sh
    for one net and an lnet.conf for another mounts nothing, and the
    error it produces reads as a Lustre fault.
    """

    def _written(self, writes, suffix):
        return {
            node: text
            for node, path, text in writes
            if path.endswith(suffix)
        }

    def test_o2ib_writes_a_matching_pair_on_every_node(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        src, writes = _deploy_harness(
            monkeypatch, tmp_path, lambda c: 0, nics=("softroce",)
        )
        vm_cluster.cmd_cluster_deploy(
            argparse.Namespace(
                name="co2", lustre_tree=str(src), cfg_dir=None, net="o2ib",
            )
        )
        local = self._written(writes, "local.sh")
        lnet = self._written(writes, "lnet.conf")
        assert len(local) == 3 and len(lnet) == 3
        for node, text in local.items():
            assert "NETTYPE=o2ib" in text
            # The MGS NID is the MGS's extra-NIC address, not its mgmt
            # address -- the mgmt address is not on the o2ib net at all.
            assert "MGSNID=172.16.100.10@o2ib" in text
            assert 'networks="o2ib0(eth1)"' in lnet[node]

    def test_multi_rail_o2ib_lists_both_interfaces(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        src, writes = _deploy_harness(
            monkeypatch, tmp_path, lambda c: 0,
            nics=("softroce", "softroce"),
        )
        vm_cluster.cmd_cluster_deploy(
            argparse.Namespace(
                name="co2", lustre_tree=str(src), cfg_dir=None, net="o2ib",
            )
        )
        lnet = self._written(writes, "lnet.conf")
        assert all('o2ib0(eth1,eth2)' in t for t in lnet.values())

    def test_tcp_writes_the_mgmt_pair(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        src, writes = _deploy_harness(
            monkeypatch, tmp_path, lambda c: 0, nics=("softroce",)
        )
        vm_cluster.cmd_cluster_deploy(
            argparse.Namespace(
                name="co2", lustre_tree=str(src), cfg_dir=None, net="tcp",
            )
        )
        local = self._written(writes, "local.sh")
        lnet = self._written(writes, "lnet.conf")
        for node, text in local.items():
            assert "NETTYPE=tcp" in text
            assert "MGSNID=10.0.0.10@tcp" in text
            assert 'networks="tcp0(eth0)"' in lnet[node]

    def test_net_the_nics_cannot_carry_touches_no_node(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Refuse before the rsync, not after half the cluster is
        reconfigured."""
        src, writes = _deploy_harness(
            monkeypatch, tmp_path, lambda c: 0, nics=()
        )
        with pytest.raises(SystemExit):
            vm_cluster.cmd_cluster_deploy(
                argparse.Namespace(
                    name="co2", lustre_tree=str(src), cfg_dir=None,
                    net="o2ib",
                )
            )
        assert writes == []

    def test_the_net_is_recorded_on_the_cluster(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        src, _ = _deploy_harness(
            monkeypatch, tmp_path, lambda c: 0, nics=("softroce",)
        )
        cluster = vm_cluster.ClusterInfo.load("co2")
        vm_cluster.cmd_cluster_deploy(
            argparse.Namespace(
                name="co2", lustre_tree=str(src), cfg_dir=None, net="o2ib",
            )
        )
        assert cluster.net == "o2ib"

    def test_bare_deploy_keeps_the_recorded_net(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A redeploy must not quietly move the cluster back to tcp."""
        src, writes = _deploy_harness(
            monkeypatch, tmp_path, lambda c: 0, nics=("softroce",)
        )
        vm_cluster.ClusterInfo.load("co2").net = "o2ib"
        vm_cluster.cmd_cluster_deploy(
            argparse.Namespace(
                name="co2", lustre_tree=str(src), cfg_dir=None, net=None,
            )
        )
        assert all(
            "NETTYPE=o2ib" in t
            for t in self._written(writes, "local.sh").values()
        )

    def test_bare_deploy_on_a_fresh_cluster_is_tcp(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Behaviour before --net existed, unchanged."""
        src, writes = _deploy_harness(
            monkeypatch, tmp_path, lambda c: 0, nics=("softroce",)
        )
        vm_cluster.cmd_cluster_deploy(
            argparse.Namespace(
                name="co2", lustre_tree=str(src), cfg_dir=None, net=None,
            )
        )
        assert all(
            "NETTYPE=tcp" in t
            for t in self._written(writes, "local.sh").values()
        )

    def test_a_failed_lnet_conf_write_is_fatal(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A node left with one file of the pair is worse than a node
        left untouched, so the deploy must not report success."""
        src, _ = _deploy_harness(
            monkeypatch, tmp_path,
            lambda c: 1 if c.endswith("lnet.conf") else 0,
            nics=("softroce",),
        )
        with pytest.raises(SystemExit):
            vm_cluster.cmd_cluster_deploy(
                argparse.Namespace(
                    name="co2", lustre_tree=str(src), cfg_dir=None,
                    net="o2ib",
                )
            )

    def test_passthrough_cluster_needs_an_explicit_net(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A bare deploy must not overwrite the lnet.conf an HCA's
        boot-time emitter composed."""
        src, writes = _deploy_harness(
            monkeypatch, tmp_path, lambda c: 0,
            nics=("passthrough:0000:85:00.1",),
        )
        with pytest.raises(SystemExit):
            vm_cluster.cmd_cluster_deploy(
                argparse.Namespace(
                    name="co2", lustre_tree=str(src), cfg_dir=None,
                    net=None,
                )
            )
        assert writes == []

    def test_unknown_net_is_refused(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        src, _ = _deploy_harness(
            monkeypatch, tmp_path, lambda c: 0, nics=("softroce",)
        )
        with pytest.raises(SystemExit):
            vm_cluster.cmd_cluster_deploy(
                argparse.Namespace(
                    name="co2", lustre_tree=str(src), cfg_dir=None,
                    net="ib",
                )
            )


class TestStaleLnetCheck:
    """A net change is inert until the running LNet is dropped.

    lnet.conf is read by modprobe, so a node with LNet already loaded
    keeps serving the old net while both config files name the new one
    -- the same drift, one layer down.
    """

    def test_leaves_a_node_already_on_the_net_alone(self) -> None:
        """A same-net redeploy must not disturb a mounted filesystem."""
        script = vm_cluster.stale_lnet_check("tcp")
        assert "exit 0" in script
        assert "lustre_rmmod" in script

    def test_ignores_the_loopback_nid(self) -> None:
        """0@lo is on every node and belongs to no net; counting it
        would make every node look mismatched."""
        assert "@lo$" in vm_cluster.stale_lnet_check("o2ib")

    def test_matches_indexed_nids(self) -> None:
        """`@o2ib0` and `@o2ib` name the same net."""
        assert "@o2ib[0-9]*$" in vm_cluster.stale_lnet_check("o2ib")

    def test_a_node_that_cannot_unload_names_the_cleanup(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            vm_cluster, "run_ssh",
            lambda ip, cmd, timeout=0: _FakeCompleted(1),
        )
        name, rc, out = vm_cluster._drop_stale_lnet(
            "co2-oss", "10.0.0.11", "o2ib"
        )
        assert rc != 0
        assert "llmount co2-oss --cleanup" in out

    def test_deploy_fails_when_a_node_keeps_the_old_net(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        src, _ = _deploy_harness(
            monkeypatch, tmp_path, lambda c: 0, nics=("softroce",)
        )
        monkeypatch.setattr(
            vm_cluster, "run_ssh",
            lambda ip, cmd, timeout=0: _FakeCompleted(1),
        )
        with pytest.raises(SystemExit):
            vm_cluster.cmd_cluster_deploy(
                argparse.Namespace(
                    name="co2", lustre_tree=str(src), cfg_dir=None,
                    net="o2ib",
                )
            )
