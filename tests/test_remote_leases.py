"""Remote hosts: leases on remote VMs and clusters, and `serve reap`.

The requests go through the in-process host of test_remote_lifecycle
(`ltvm.run(["--host", "t", ...])`, the transport, the checks of serve).
The clock of the lease code is fixed: each test sets `leases.clock`.
"""

from __future__ import annotations

import json
from pathlib import Path
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
    _save_vm,
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
        assert _leases(inproc) == {
            "vm:new-l1": (REMOTE_OWNER, T0 + 8 * HOUR)
        }

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
        assert ltvm.run(["--host", "t", "create", "new-l1", "--lease", "30m"]) == 0
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
        self, icl: SimpleNamespace, clock: SimpleNamespace, ltvm: Any, capsys: Any
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
        assert _leases(icl) == {
            "cluster:new-cl": (REMOTE_OWNER, T0 + HOUR)
        }
