"""Remote hosts: leases on remote VMs and clusters, and `serve reap`.

The requests go through the in-process host of test_remote_lifecycle
(`ltvm.run(["--host", "t", ...])`, the transport, the checks of serve).
The clock of the lease code is fixed: each test sets `leases.clock`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from tests import test_remote_clusters as _clusters
from tests import test_remote_lifecycle as _lifecycle
from tests.test_ltvm_cli import _load_ltvm
from tests.test_remote_lifecycle import (
    REMOTE_OWNER,
    REMOTE_VM,
    _audit,
    _err,
)

inproc = _lifecycle.inproc
qemu = _lifecycle.qemu
icl = _clusters.icl

T0 = 1_800_000_000.0
HOUR = 3600.0


@pytest.fixture(scope="module")
def ltvm() -> Any:
    return _load_ltvm()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A fixed clock for the lease code.  Set `.now` to move it."""
    from ltvm_pkg.remote import leases

    c = SimpleNamespace(now=T0)
    monkeypatch.setattr(leases, "clock", lambda: c.now)
    return c


def _host_env(inproc: SimpleNamespace) -> dict[str, str]:
    """The environment that serve on host `t` sees (no XDG variables)."""
    return {"HOME": str(inproc.home)}


def _leases(inproc: SimpleNamespace) -> dict[str, Any]:
    from ltvm_pkg.remote import leases

    return {
        k: (v.owner, v.expires)
        for k, v in leases.load_all(_host_env(inproc)).items()
    }


def _serve_toml(inproc: SimpleNamespace, text: str) -> None:
    path = inproc.home / ".config" / "ltvm" / "serve.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TestCreateGivesALease:
    def test_default_length_is_eight_hours(
        self,
        inproc: SimpleNamespace,
        qemu: dict[str, Any],
        clock: SimpleNamespace,
        ltvm: Any,
    ) -> None:
        rc = ltvm.run(["--host", "t", "create", "new-l1"])
        assert rc == 0
        assert _leases(inproc) == {"vm:new-l1": (REMOTE_OWNER, T0 + 8 * HOUR)}

    def test_serve_toml_sets_the_default_length(
        self,
        inproc: SimpleNamespace,
        qemu: dict[str, Any],
        clock: SimpleNamespace,
        ltvm: Any,
    ) -> None:
        _serve_toml(inproc, "lease_hours = 2.5\n")
        assert ltvm.run(["--host", "t", "create", "new-l1"]) == 0
        assert _leases(inproc)["vm:new-l1"][1] == T0 + 2.5 * HOUR

    def test_lease_option_sets_the_length(
        self,
        inproc: SimpleNamespace,
        qemu: dict[str, Any],
        clock: SimpleNamespace,
        ltvm: Any,
    ) -> None:
        _serve_toml(inproc, "lease_hours = 2.5\n")
        assert (
            ltvm.run(["--host", "t", "create", "new-l1", "--lease", "30m"]) == 0
        )
        assert _leases(inproc)["vm:new-l1"][1] == T0 + 1800

    def test_bad_lease_is_refused_and_makes_nothing(
        self,
        inproc: SimpleNamespace,
        qemu: dict[str, Any],
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        rc = ltvm.run(
            ["--host", "t", "create", "new-l1", "--lease", "soon", "--json"]
        )
        assert rc == 1
        assert _err(capsys.readouterr().err)["code"] == "bad_request"
        assert not qemu["launch"].called
        assert _leases(inproc) == {}
        assert _audit(inproc.home)[-1]["result"] == "bad_request"

    def test_dry_run_gives_no_lease(
        self,
        inproc: SimpleNamespace,
        qemu: dict[str, Any],
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        assert ltvm.run(["--host", "t", "create", "new-l1", "--dry-run"]) == 0
        assert _leases(inproc) == {}

    def test_local_create_refuses_lease(
        self, qemu: dict[str, Any], ltvm: Any, capsys: Any
    ) -> None:
        rc = ltvm.run(["create", "new-l1", "--lease", "4h", "--json"])
        assert rc == 1
        assert "--host" in _err(capsys.readouterr().err)["error"]
        assert not qemu["launch"].called

    def test_cluster_create_gives_the_cluster_a_lease(
        self,
        icl: SimpleNamespace,
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        import subprocess

        from ltvm_pkg import vm_cluster
        from ltvm_pkg.vm_state import VMInfo

        def fake_child(cmd: list[str], name: str, *, timeout: float) -> Any:
            owner = cmd[cmd.index("--owner-id") + 1]
            VMInfo(name=name, ip="192.168.100.90", owner_id=owner).save()
            assert "--lease" not in cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(vm_cluster, "_run_prefixed", side_effect=fake_child):
            rc = ltvm.run(
                ["--host", "t", "cluster", "create", "t/new-cl"]
                + ["mgs+mds:t/new-cl-a:1", "--mem", "1024", "--lease", "1h"]
            )
        capsys.readouterr()
        assert rc == 0
        assert _leases(icl) == {"cluster:new-cl": (REMOTE_OWNER, T0 + HOUR)}


# ── serve reap ───────────────────────────────────────────


@pytest.fixture
def reaper(
    inproc: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """`ltvm serve reap` in this process, as the user of host `t`."""
    monkeypatch.setenv("XDG_STATE_HOME", str(inproc.home / ".local" / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(inproc.home / ".config"))
    return inproc


def _reap(ltvm: Any, capsys: Any) -> tuple[int, list[dict[str, Any]]]:
    capsys.readouterr()
    with patch("ltvm_pkg.vm_commands.unregister_vm_dns", create=True):
        rc = ltvm.run(["serve", "reap", "--json"])
    return rc, json.loads(capsys.readouterr().out)["reaped"]


def _grant(inproc: SimpleNamespace, kind: str, name: str, hours: float) -> None:
    from ltvm_pkg.remote import leases

    leases.grant(_host_env(inproc), kind, name, REMOTE_OWNER, hours * HOUR)


class TestReap:
    def test_a_live_lease_keeps_the_vm(
        self,
        reaper: SimpleNamespace,
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        _grant(reaper, "vm", REMOTE_VM, 8)
        clock.now = T0 + 8 * HOUR - 1
        rc, reaped = _reap(ltvm, capsys)
        assert (rc, reaped) == (0, [])
        assert (reaper.sockets / f"{REMOTE_VM}.info").exists()
        assert "vm:" + REMOTE_VM in _leases(reaper)

    def test_an_expired_lease_destroys_the_vm_and_logs_it(
        self,
        reaper: SimpleNamespace,
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        from tests.test_remote_serve import TEST_KEY_FP

        _grant(reaper, "vm", REMOTE_VM, 8)
        clock.now = T0 + 8 * HOUR
        rc, reaped = _reap(ltvm, capsys)
        assert rc == 0
        assert [(r["kind"], r["name"], r["result"]) for r in reaped] == [
            ("vm", REMOTE_VM, "reaped")
        ]
        assert not (reaper.sockets / f"{REMOTE_VM}.info").exists()
        assert _leases(reaper) == {}
        entry = _audit(reaper.home)[-1]
        assert entry["key"] == TEST_KEY_FP
        assert (entry["verb"], entry["args"]) == ("destroy", [REMOTE_VM])
        assert entry["result"] == "reaped"

    def test_an_expired_lease_destroys_the_cluster(
        self,
        icl: SimpleNamespace,
        reaper: SimpleNamespace,
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        from tests.test_remote_clusters import (
            REMOTE_CL,
            REMOTE_NODE,
            _save_cluster,
        )

        _save_cluster(icl.sockets, REMOTE_CL, REMOTE_NODE, REMOTE_OWNER)
        _grant(icl, "cluster", REMOTE_CL, 1)
        clock.now = T0 + 2 * HOUR
        rc, reaped = _reap(ltvm, capsys)
        assert rc == 0
        assert [r["result"] for r in reaped] == ["reaped"]
        assert not (icl.sockets / f"{REMOTE_CL}.cluster").exists()
        assert not (icl.sockets / f"{REMOTE_NODE}.info").exists()
        entry = _audit(icl.home)[-1]
        assert entry["verb"] == "cluster"
        assert entry["args"] == ["destroy", REMOTE_CL]

    def test_never_destroys_an_owner_vm(
        self,
        reaper: SimpleNamespace,
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        from tests.test_remote_lifecycle import OWNER_VM

        _grant(reaper, "vm", OWNER_VM, 1)
        clock.now = T0 + 2 * HOUR
        rc, reaped = _reap(ltvm, capsys)
        assert [r["result"] for r in reaped] == ["vm_not_found"]
        assert (reaper.sockets / f"{OWNER_VM}.info").exists()
        assert _leases(reaper) == {}
        assert _audit(reaper.home)[-1]["result"] == "vm_not_found"

    def test_a_vm_that_is_gone_drops_its_lease(
        self,
        reaper: SimpleNamespace,
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        _grant(reaper, "vm", "gone-sentinel-c7", 1)
        clock.now = T0 + 2 * HOUR
        rc, reaped = _reap(ltvm, capsys)
        assert [r["result"] for r in reaped] == ["vm_not_found"]
        assert _leases(reaper) == {}

    def test_a_failed_destroy_keeps_the_lease_for_the_next_reap(
        self,
        reaper: SimpleNamespace,
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        _grant(reaper, "vm", REMOTE_VM, 1)
        clock.now = T0 + 2 * HOUR
        with patch("ltvm_pkg.cli.setup._vm_call", return_value=1):
            rc, reaped = _reap(ltvm, capsys)
        assert rc == 1
        assert [r["result"] for r in reaped] == ["exit 1"]
        assert "vm:" + REMOTE_VM in _leases(reaper)
        assert _audit(reaper.home)[-1]["result"] == "exit 1"

    def test_reap_never_uses_sudo(
        self,
        reaper: SimpleNamespace,
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        from ltvm_pkg import priv

        _reap(ltvm, capsys)
        assert priv._sudo_forbidden


# ── lease renew ──────────────────────────────────────────


class TestRenew:
    def test_renew_extends_by_the_default_length(
        self,
        reaper: SimpleNamespace,
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        _grant(reaper, "vm", REMOTE_VM, 1)
        clock.now = T0 + 0.5 * HOUR
        rc = ltvm.run(["--host", "t", "lease", "renew", f"t/{REMOTE_VM}"])
        assert rc == 0
        assert REMOTE_VM in capsys.readouterr().out
        assert _leases(reaper)["vm:" + REMOTE_VM] == (
            REMOTE_OWNER,
            T0 + 8.5 * HOUR,
        )

    def test_for_sets_the_length(
        self,
        reaper: SimpleNamespace,
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        _grant(reaper, "vm", REMOTE_VM, 1)
        rc = ltvm.run(
            [
                "--host",
                "t",
                "lease",
                "renew",
                REMOTE_VM,
                "--for",
                "2d",
                "--json",
            ]
        )
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert (out["kind"], out["name"]) == ("vm", REMOTE_VM)
        assert out["expires"] == T0 + 48 * HOUR

    def test_a_renewed_lease_is_not_reaped(
        self,
        reaper: SimpleNamespace,
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        _grant(reaper, "vm", REMOTE_VM, 1)
        clock.now = T0 + 0.5 * HOUR
        ltvm.run(["--host", "t", "lease", "renew", REMOTE_VM, "--for", "2h"])
        clock.now = T0 + 2 * HOUR
        assert _reap(ltvm, capsys) == (0, [])
        assert (reaper.sockets / f"{REMOTE_VM}.info").exists()

    def test_a_remote_vm_with_no_lease_gets_one(
        self,
        reaper: SimpleNamespace,
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        assert ltvm.run(["--host", "t", "lease", "renew", REMOTE_VM]) == 0
        assert _leases(reaper) == {
            "vm:" + REMOTE_VM: (REMOTE_OWNER, T0 + 8 * HOUR)
        }

    def test_a_cluster_or_its_node_renews_the_cluster_lease(
        self,
        icl: SimpleNamespace,
        reaper: SimpleNamespace,
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        from tests.test_remote_clusters import (
            REMOTE_CL,
            REMOTE_NODE,
            _save_cluster,
        )

        _save_cluster(icl.sockets, REMOTE_CL, REMOTE_NODE, REMOTE_OWNER)
        _grant(icl, "cluster", REMOTE_CL, 1)
        assert ltvm.run(["--host", "t", "lease", "renew", REMOTE_CL]) == 0
        assert _leases(icl) == {
            "cluster:" + REMOTE_CL: (REMOTE_OWNER, T0 + 8 * HOUR)
        }
        rc = ltvm.run(
            ["--host", "t", "lease", "renew", REMOTE_NODE, "--for", "9h"]
        )
        assert rc == 0
        assert _leases(icl) == {
            "cluster:" + REMOTE_CL: (REMOTE_OWNER, T0 + 9 * HOUR)
        }

    @pytest.mark.parametrize("name", ["owner-sentinel-f6", "gone-sentinel-c7"])
    def test_an_owner_or_absent_vm_is_not_found(
        self,
        reaper: SimpleNamespace,
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
        name: str,
    ) -> None:
        rc = ltvm.run(["--host", "t", "lease", "renew", name, "--json"])
        assert rc == 1
        assert _err(capsys.readouterr().err)["code"] == "vm_not_found"
        assert _leases(reaper) == {}

    def test_bad_length_is_refused(
        self,
        reaper: SimpleNamespace,
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        _grant(reaper, "vm", REMOTE_VM, 1)
        rc = ltvm.run(
            ["--host", "t", "lease", "renew", REMOTE_VM, "--for", "x", "--json"]
        )
        assert rc == 1
        assert _leases(reaper)["vm:" + REMOTE_VM][1] == T0 + HOUR
