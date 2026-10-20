"""Remote hosts: clusters, artifacts, and the guards of child ltvm processes.

The child host (exec://) runs `serve` as a separate process, so these
tests use cluster dry runs and the read-only artifact verbs there.  The
in-process host (`inproc`, from test_remote_lifecycle) runs the verbs
that change VM state, with the conftest isolation in place.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

# The real function: conftest replaces the module attribute in each test.
from ltvm_pkg.update_check import maybe_check_for_updates as _real_update_check
from tests import test_remote_lifecycle as _lifecycle
from tests.test_ltvm_cli import _LTVM_PATH
from tests.test_remote_lifecycle import REMOTE_OWNER, _audit, _err, _save_vm

# The host fixtures of test_remote_lifecycle.
child = _lifecycle.child
inproc = _lifecycle.inproc


# ── guards that child ltvm processes inherit ─────────────


class TestChildGuards:
    def test_no_sudo_env_forbids_sudo_in_run(
        self, ltvm: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        from ltvm_pkg import priv

        monkeypatch.setattr(priv, "_sudo_forbidden", False)
        monkeypatch.setenv("LTVM_NO_SUDO", "1")
        ltvm.run(["list", "--json"])
        assert priv._sudo_forbidden is True
        with pytest.raises(priv.SudoForbidden):
            priv.sudo_run(["true"])

    def test_without_the_env_run_leaves_sudo_allowed(
        self, ltvm: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        from ltvm_pkg import priv

        monkeypatch.setattr(priv, "_sudo_forbidden", False)
        ltvm.run(["list", "--json"])
        assert priv._sudo_forbidden is False

    def test_forbid_sudo_sets_the_env_for_children(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg import priv

        monkeypatch.setattr(priv, "_sudo_forbidden", False)
        priv.forbid_sudo()
        assert os.environ.get("LTVM_NO_SUDO") == "1"

    def test_update_check_env_stops_the_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg import update_check

        def sentinel() -> None:
            raise AssertionError("the update check read its config")

        monkeypatch.setattr(update_check, "_load_config", sentinel)
        monkeypatch.setenv("LTVM_UPDATE_CHECK", "0")
        _real_update_check(force=True)

    def test_child_ltvm_is_this_script_when_serve_set_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg import paths

        monkeypatch.setenv("LTVM_SELF", "/opt/ltvm/ltvm")
        assert paths.ltvm_command() == [sys.executable, "/opt/ltvm/ltvm"]


# ── clusters ─────────────────────────────────────────────

REMOTE_CL = "rcl-sentinel-a1"
REMOTE_NODE = "rcl-node-sentinel-a1"
OWNER_CL = "ocl-sentinel-b2"
OWNER_NODE = "ocl-node-sentinel-b2"


def _save_cluster(
    sockets: Path, name: str, node: str, owner_id: str | None
) -> None:
    from ltvm_pkg.vm_state import ClusterInfo

    _save_vm(sockets, node, owner_id)
    with patch("ltvm_pkg.vm_state.SOCKETS", sockets):
        ClusterInfo(
            name=name,
            owner_id=owner_id,
            nodes=[
                {
                    "name": node,
                    "roles": ["mgs", "mds"],
                    "mdt_disks": 1,
                    "ost_disks": 0,
                    "ip": "192.168.100.78",
                }
            ],
        ).save()


@pytest.fixture
def ccl(child: SimpleNamespace) -> SimpleNamespace:
    """The child host, with one remote and one owner cluster."""
    _save_cluster(child.sockets, REMOTE_CL, REMOTE_NODE, REMOTE_OWNER)
    _save_cluster(child.sockets, OWNER_CL, OWNER_NODE, "pid:4242")
    return child


class TestClusterDryRun:
    def test_create_dry_run_plans_a_remote_cluster(
        self, ccl: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        rc = ltvm.run(
            [
                "--host",
                "t",
                "cluster",
                "create",
                "t/new-cl",
                "mgs+mds:t/new-cl-a:1",
                "oss:new-cl-b:2",
                "--mem",
                "1024",
                "--dry-run",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "Would create cluster 'new-cl' with 2 nodes" in out
        assert "new-cl-a " in out
        assert f"owner:   {REMOTE_OWNER}" in out
        assert not (ccl.sockets / "new-cl.cluster").exists()
        assert _audit(ccl.home)[-1]["result"] == "ok"

    @pytest.mark.parametrize(
        ("argv", "code"),
        [
            # The name of an owner cluster, or of an owner VM as a node.
            (["cluster", "create", OWNER_CL, "mgs+mds:x-new:1"], "name_in_use"),
            (
                ["cluster", "create", "x-new", f"mgs+mds:{OWNER_NODE}:1"],
                "name_in_use",
            ),
            (
                [
                    "cluster",
                    "create",
                    "x-new",
                    "mgs+mds:x-a:1",
                    "--failover",
                    f"{OWNER_NODE}=x-a",
                ],
                "name_in_use",
            ),
            (
                [
                    "cluster",
                    "create",
                    "x-new",
                    "mgs+mds:x-a:1",
                    "--owner",
                    "me",
                ],
                "bad_request",
            ),
            (
                [
                    "cluster",
                    "create",
                    "x-new",
                    "mgs+mds:x-a:1",
                    "--nic",
                    "passthrough:0000:01:00.0",
                ],
                "needs_root",
            ),
            (
                [
                    "cluster",
                    "create",
                    "x-new",
                    "mgs+mds:x-a:1",
                    "--nic",
                    "PassThrough:0000:01:00.0",
                ],
                "needs_root",
            ),
        ],
    )
    def test_create_refusals(
        self, ccl: SimpleNamespace, argv: list[str], code: str
    ) -> None:
        resp = _send(shlex.join([*argv, "--dry-run"]))
        assert resp.error is not None
        assert resp.error.code == code
        assert resp.stdout == ""


def _send(command: str) -> Any:
    from ltvm_pkg.remote import hosts

    return hosts.transport_for("t").send(command)


class TestClusterScope:
    @pytest.mark.parametrize(
        "argv",
        [
            ["cluster", "status", OWNER_CL],
            ["cluster", "status", "no-such-cl"],
            ["cluster", "status", "../../x"],
            ["cluster", "destroy", OWNER_CL],
            ["cluster", "start", OWNER_CL],
            ["cluster", "stop", OWNER_CL],
            ["cluster", "exec", OWNER_CL, "mds", "true"],
        ],
    )
    def test_owner_or_absent_cluster_is_not_found(
        self, ccl: SimpleNamespace, argv: list[str]
    ) -> None:
        resp = _send(shlex.join(argv))
        assert resp.error is not None
        assert resp.error.code == "cluster_not_found"
        assert (ccl.sockets / f"{OWNER_CL}.cluster").exists()

    @pytest.mark.parametrize(
        "argv",
        [
            # Upstream verbs stay refused until the allow-list adds them.
            ["cluster", "failover", REMOTE_CL, REMOTE_NODE],
            ["cluster", "failback", REMOTE_CL, REMOTE_NODE],
            ["cluster", "ssh", REMOTE_CL, "mds"],
            ["cluster", "krb5", REMOTE_CL],
        ],
    )
    def test_other_cluster_actions_are_refused(
        self, ccl: SimpleNamespace, argv: list[str]
    ) -> None:
        resp = _send(shlex.join(argv))
        assert resp.error is not None
        assert resp.error.code == "verb_refused"

    def test_status_of_a_remote_cluster_has_the_prefix(
        self, ccl: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        rc = ltvm.run(["--host", "t", "cluster", "status", REMOTE_CL, "--json"])
        data = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert data["cluster"] == f"t/{REMOTE_CL}"
        assert [n["name"] for n in data["nodes"]] == [f"t/{REMOTE_NODE}"]
        assert [r["node"] for r in data["lustre"]] == [f"t/{REMOTE_NODE}"]

    def test_status_text_has_the_prefix(
        self, ccl: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        rc = ltvm.run(["--host", "t", "cluster", "status", f"t/{REMOTE_CL}"])
        out = capsys.readouterr().out
        assert rc == 0
        assert f"cluster: t/{REMOTE_CL}" in out
        assert f"t/{REMOTE_NODE}" in out

    def test_cluster_list_shows_only_remote_clusters(
        self, ccl: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        rc = ltvm.run(["--host", "t", "cluster", "list", "--json"])
        out = capsys.readouterr().out
        assert rc == 0
        data = json.loads(out)
        assert [c["cluster"] for c in data["clusters"]] == [f"t/{REMOTE_CL}"]
        assert data["clusters"][0]["nodes"][0]["name"] == f"t/{REMOTE_NODE}"
        assert OWNER_CL not in out

    def test_cluster_list_text_has_the_prefix(
        self, ccl: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        rc = ltvm.run(["--host", "t", "cluster", "list"])
        out = capsys.readouterr().out
        assert rc == 0
        assert out.startswith(f"t/{REMOTE_CL}: t/{REMOTE_NODE}(mgs+mds,")
        assert OWNER_CL not in out

    def test_list_prefixes_clusters_and_members(
        self, ccl: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        rc = ltvm.run(["--host", "t", "list", "--json"])
        out = capsys.readouterr().out
        assert rc == 0
        data = json.loads(out)
        assert data["clusters"] == [
            {
                "name": f"t/{REMOTE_CL}",
                "owner_id": REMOTE_OWNER,
                "members": [f"t/{REMOTE_NODE}"],
                "lease_expires": None,
            }
        ]
        node = next(v for v in data["vms"] if v["name"] == f"t/{REMOTE_NODE}")
        assert node["cluster"] == f"t/{REMOTE_CL}"
        assert OWNER_CL not in out

    def test_cluster_exec_with_ltvm_host_env(
        self,
        ccl: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # cluster exec writes its command to args.command, a list.  The
        # LTVM_HOST path must still read the verb.
        monkeypatch.setenv("LTVM_HOST", "t")
        rc = ltvm.run(["cluster", "exec", "--json", OWNER_CL, "mds", "true"])
        assert rc == 1
        assert _err(capsys.readouterr().err)["code"] == "cluster_not_found"


@pytest.fixture
def icl(
    inproc: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """The in-process host, with sockets bound in vm_cluster too."""
    from ltvm_pkg import vm_cluster

    monkeypatch.setattr(vm_cluster, "SOCKETS", inproc.sockets)
    monkeypatch.setattr(vm_cluster, "_distribute_cluster_hosts", lambda *a: [])
    return inproc


class TestTextErrors:
    """Text mode sends --json.  A failed verb must still print text."""

    def test_a_failed_status_prints_an_error_line(
        self, icl: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        _save_cluster(icl.sockets, REMOTE_CL, REMOTE_NODE, REMOTE_OWNER)
        with patch(
            "ltvm_pkg.cluster_lustre_state.lustre_state",
            side_effect=RuntimeError("state-sentinel-d4"),
        ):
            rc = ltvm.run(["--host", "t", "cluster", "status", REMOTE_CL])
        captured = capsys.readouterr()
        assert rc == 1
        assert "error: state-sentinel-d4" in captured.err
        assert "{" not in captured.err + captured.out

    def test_a_failed_status_keeps_the_json_envelope_with_json(
        self, icl: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        _save_cluster(icl.sockets, REMOTE_CL, REMOTE_NODE, REMOTE_OWNER)
        with patch(
            "ltvm_pkg.cluster_lustre_state.lustre_state",
            side_effect=RuntimeError("state-sentinel-d4"),
        ):
            rc = ltvm.run(
                ["--host", "t", "cluster", "status", REMOTE_CL, "--json"]
            )
        assert rc == 1
        assert json.loads(capsys.readouterr().err)["error"] == (
            "state-sentinel-d4"
        )

    def test_a_recovery_timeout_prints_the_status_as_text(
        self, icl: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        _save_cluster(icl.sockets, REMOTE_CL, REMOTE_NODE, REMOTE_OWNER)
        with patch(
            "ltvm_pkg.cluster_lustre_state.wait_recovery",
            return_value=([], False),
        ):
            rc = ltvm.run(
                [
                    "--host",
                    "t",
                    "cluster",
                    "status",
                    REMOTE_CL,
                    "--wait-recovery",
                    "5",
                ]
            )
        captured = capsys.readouterr()
        assert rc == 1
        assert f"cluster: t/{REMOTE_CL}" in captured.out
        assert "recovery: not done" in captured.out
        assert "{" not in captured.out


class TestClusterLifecycle:
    def test_create_runs_guarded_children_of_this_ltvm(
        self, icl: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        from ltvm_pkg import vm_cluster
        from ltvm_pkg.vm_state import ClusterInfo, VMInfo

        children: list[tuple[list[str], dict[str, str]]] = []

        def fake_child(cmd: list[str], name: str, *, timeout: float) -> Any:
            import subprocess

            children.append((cmd, dict(os.environ)))
            owner = cmd[cmd.index("--owner-id") + 1]
            VMInfo(name=name, ip="192.168.100.90", owner_id=owner).save()
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(vm_cluster, "_run_prefixed", side_effect=fake_child):
            rc = ltvm.run(
                [
                    "--host",
                    "t",
                    "cluster",
                    "create",
                    "t/new-cl",
                    "mgs+mds:t/new-cl-a:1",
                    "--mem",
                    "1024",
                ]
            )
        capsys.readouterr()
        assert rc == 0
        assert ClusterInfo.load("new-cl").owner_id == REMOTE_OWNER
        [(cmd, env)] = children
        # The same Python and ltvm script as serve, never sudo or PATH.
        assert cmd[:3] == [sys.executable, str(_LTVM_PATH), "create"]
        assert cmd[3] == "new-cl-a"
        assert env["LTVM_NO_SUDO"] == "1"
        assert env["LTVM_UPDATE_CHECK"] == "0"
        assert env["LTVM_TELEMETRY"] == "0"
        assert env["LTVM_OWNER_ID"] == REMOTE_OWNER
        assert "LTVM_HOST" not in env

    def test_exec_runs_on_the_nodes_of_a_remote_cluster(
        self, icl: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        import subprocess

        _save_cluster(icl.sockets, REMOTE_CL, REMOTE_NODE, REMOTE_OWNER)
        ran = subprocess.CompletedProcess([], 0, "exec-sentinel-d4\n", "")
        with patch("ltvm_pkg.vm_cluster.run_ssh", return_value=ran) as ssh:
            rc = ltvm.run(
                ["--host", "t", "cluster", "exec", f"t/{REMOTE_CL}"]
                + [f"t/{REMOTE_NODE}", "lctl", "dl"]
            )
        assert rc == 0
        assert "exec-sentinel-d4" in capsys.readouterr().out
        assert ssh.call_args.args[1] == "lctl dl"

    def test_destroy_removes_a_remote_cluster(
        self, icl: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        _save_cluster(icl.sockets, REMOTE_CL, REMOTE_NODE, REMOTE_OWNER)
        _save_cluster(icl.sockets, OWNER_CL, OWNER_NODE, "pid:4242")
        rc = ltvm.run(["--host", "t", "cluster", "destroy", f"t/{REMOTE_CL}"])
        capsys.readouterr()
        assert rc == 0
        assert not (icl.sockets / f"{REMOTE_CL}.cluster").exists()
        assert not (icl.sockets / f"{REMOTE_NODE}.info").exists()
        assert (icl.sockets / f"{OWNER_CL}.cluster").exists()
        assert (icl.sockets / f"{OWNER_NODE}.info").exists()

    def test_destroy_refuses_a_remote_cluster_with_an_owner_node(
        self, icl: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        # A remote cluster record that names an owner VM as a node.
        from ltvm_pkg.vm_state import ClusterInfo

        _save_vm(icl.sockets, OWNER_NODE, "pid:4242")
        with patch("ltvm_pkg.vm_state.SOCKETS", icl.sockets):
            ClusterInfo(
                name=REMOTE_CL,
                owner_id=REMOTE_OWNER,
                nodes=[{"name": OWNER_NODE, "roles": ["mgs", "mds"]}],
            ).save()
        rc = ltvm.run(
            ["--host", "t", "cluster", "destroy", "--json", REMOTE_CL]
        )
        assert rc == 1
        assert _err(capsys.readouterr().err)["code"] == "cluster_not_found"
        assert (icl.sockets / f"{OWNER_NODE}.info").exists()


# ── artifacts ────────────────────────────────────────────


class TestArtifacts:
    def test_serve_uses_the_artifacts_dir_in_its_home(
        self, child: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        # No LTVM_ARTIFACTS_DIR from sshd: serve must use HOME/artifacts,
        # not the artifacts directory of the checkout.
        from ltvm_pkg import target_config
        from ltvm_pkg.remote import hosts

        host = hosts.get("t")
        env = {k: v for k, v in host.env.items() if k != "LTVM_ARTIFACTS_DIR"}
        hosts.remove("t")
        hosts.add(dataclasses.replace(host, env=env))
        with patch.object(
            target_config, "ARTIFACTS_DIR", child.home / "artifacts"
        ):
            tc = target_config.TargetConfig("rocky9", arch="x86_64")
            meta = tc.meta_path("container")
        meta.parent.mkdir(parents=True)
        meta.write_text(json.dumps({"sentinel": "home-artifacts-c7"}))
        rc = ltvm.run(
            ["--host", "t", "build", "status", "--arch", "x86_64", "--json"]
        )
        data = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert data["rocky9"]["container"]["sentinel"] == "home-artifacts-c7"

    @pytest.mark.parametrize("action", ["show", "fetch"])
    def test_target_verbs_reach_the_verb(
        self, child: SimpleNamespace, action: str
    ) -> None:
        # An unknown target fails in the verb, with no network use.  So
        # the request went through every check in serve.
        resp = _send(shlex.join(["target", action, "nosuch-x9", "--json"]))
        assert resp.error is None
        assert resp.status == 2
        assert "Unknown target: 'nosuch-x9'" in resp.stderr
        assert _audit(child.home)[-1]["result"] == "exit 2"

    @pytest.mark.parametrize(
        "argv",
        [
            ["build", "kernel", "rocky9", "--lustre-tree", "/home/owner/lr"],
            ["build", "all", "rocky9", "--lustre-tree=/home/owner/lr"],
            ["build", "image", "rocky9", "--lustre-tree", "../x"],
            ["build", "zfs", "rocky9", "--zfs-source", "/home/owner/zfs"],
            ["target", "fetch", "rocky9", "--url", "file:///etc/passwd"],
        ],
    )
    def test_host_paths_and_urls_are_refused(
        self, child: SimpleNamespace, argv: list[str]
    ) -> None:
        resp = _send(shlex.join(argv))
        assert resp.error is not None
        assert resp.error.code == "path_refused"
        assert _audit(child.home)[-1]["result"] == "path_refused"

    @pytest.mark.parametrize(
        "argv",
        [
            ["build", "status", "--arch", "../../x"],
            ["build", "kernel", "rocky9", "--arch=a/b"],
            ["target", "fetch", "rocky9", "--kernel", "../../../etc"],
            ["build", "image", "rocky9", "--kernel", ".."],
            ["build", "mofed-kmods", "rocky9", "--mofed-version", "a/b"],
            ["build", "zfs", "rocky9", "--zfs-version", "2.2/../x"],
            ["target", "fetch", "rocky9", "--variant", "../base"],
            ["build", "all", "rocky9", "--target", "a/b"],
            ["list", "--arch", "x86_64\n"],
            [
                "cluster",
                "create",
                "x-new",
                "mgs+mds:x-a:1",
                "--node-kernel",
                "x-a=../../k",
                "--dry-run",
            ],
            [
                "cluster",
                "create",
                "x-new",
                "mgs+mds:x-a:1",
                "--node-target",
                "x-a=a/b",
                "--dry-run",
            ],
        ],
    )
    def test_names_that_make_a_path_are_refused(
        self, child: SimpleNamespace, argv: list[str]
    ) -> None:
        # These values become parts of host paths, for example the
        # artifacts directory and the Lustre staging directory.
        resp = _send(shlex.join(argv))
        assert resp.error is not None
        assert resp.error.code == "bad_request"
        assert _audit(child.home)[-1]["result"] == "bad_request"

    def test_a_good_name_reaches_the_verb(self, child: SimpleNamespace) -> None:
        """Negative control: a normal arch and kernel are not refused."""
        resp = _send(
            shlex.join(
                [
                    "target",
                    "show",
                    "nosuch-x9",
                    "--arch",
                    "x86_64",
                    "--json",
                ]
            )
        )
        assert resp.error is None

    @pytest.mark.parametrize(
        "argv",
        [
            ["build", "shell", "rocky9"],
            ["build", "lustre", "rocky9"],
            ["target", "build", "rocky9"],
            ["target", "clean", "rocky9"],
            ["target", "delete", "rocky9"],
            ["target", "validate", "rocky9"],
            ["target", "export", "rocky9"],
        ],
    )
    def test_other_artifact_verbs_are_refused(
        self, child: SimpleNamespace, argv: list[str]
    ) -> None:
        resp = _send(shlex.join(argv))
        assert resp.error is not None
        assert resp.error.code == "verb_refused"


# ── the exec:// transport ────────────────────────────────


def test_exec_transport_stops_a_hung_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ltvm_pkg.remote import hosts, transport

    hung = tmp_path / "hung-serve"
    hung.write_text("import time\ntime.sleep(60)\n")
    hosts.add(hosts.Host(name="h", url=f"exec://{hung}"))
    monkeypatch.setattr(transport.ExecTransport, "timeout", 0.5)
    resp = hosts.transport_for("h").request(["list"])
    assert resp.error is not None
    assert resp.error.code == "timed_out"
