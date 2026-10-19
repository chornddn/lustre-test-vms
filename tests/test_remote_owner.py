"""Remote hosts: the owner controls `serve disable`, `enable`, `status`.

The owner commands run in this process as the user of host `t` (the
`reaper` fixture of test_remote_leases).  The remote requests go
through the in-process host of test_remote_lifecycle.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from tests import test_remote_clusters as _clusters
from tests import test_remote_leases as _leases_t
from tests import test_remote_lifecycle as _lifecycle
from tests.test_ltvm_cli import _load_ltvm
from tests.test_remote_lifecycle import (
    OWNER_VM,
    REMOTE_VM,
    _audit,
    _err,
)

inproc = _lifecycle.inproc
qemu = _lifecycle.qemu
icl = _clusters.icl
reaper = _leases_t.reaper
clock = _leases_t.clock


@pytest.fixture(scope="module")
def ltvm() -> Any:
    return _load_ltvm()


def _serve(ltvm: Any, capsys: Any, *argv: str) -> tuple[int, Any]:
    """Run `ltvm serve <argv> --json`.  Return (rc, the JSON on stdout)."""
    capsys.readouterr()
    with patch("ltvm_pkg.vm_commands.unregister_vm_dns", create=True):
        rc = ltvm.run(["serve", *argv, "--json"])
    out = capsys.readouterr().out
    return rc, json.loads(out) if out.strip() else None


class TestDisable:
    @pytest.mark.parametrize(
        "argv", [["list", "--json"], ["create", "new-d1", "--json"]]
    )
    def test_every_later_request_fails_with_disabled(
        self,
        reaper: SimpleNamespace,
        qemu: dict[str, Any],
        ltvm: Any,
        capsys: Any,
        argv: list[str],
    ) -> None:
        rc, out = _serve(ltvm, capsys, "disable")
        assert (rc, out["enabled"]) == (0, False)
        rc = ltvm.run(["--host", "t", *argv])
        assert rc == 1
        assert _err(capsys.readouterr().err)["code"] == "disabled"
        assert not (reaper.sockets / "new-d1.info").exists()
        assert not qemu["launch"].called
        assert _audit(reaper.home)[-1]["result"] == "disabled"

    def test_disabled_comes_before_the_protocol_check(
        self, reaper: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        import io

        from ltvm_pkg.remote import protocol, serve

        _serve(ltvm, capsys, "disable")
        out = io.StringIO()
        rc = serve.serve_stdio(
            ltvm.run,
            parse=ltvm._parse_request,
            stdin=io.StringIO('{"protocol": 999, "ltvm": "9"}\n'),
            stdout=out,
            env={"HOME": str(reaper.home), "SSH_ORIGINAL_COMMAND": "list"},
        )
        assert rc == 1
        err = protocol.parse_line(out.getvalue().splitlines()[0])["error"]
        assert err["code"] == "disabled"

    def test_disable_keeps_the_caps(
        self, reaper: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        from ltvm_pkg.remote import serve_config

        env = _leases_t._host_env(reaper)
        serve_config.save(env, serve_config.ServeConfig(max_vcpus=6))
        _serve(ltvm, capsys, "disable")
        cfg = serve_config.load(env)
        assert (cfg.enabled, cfg.max_vcpus) == (False, 6)

    def test_disable_alone_destroys_nothing(
        self, reaper: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        _serve(ltvm, capsys, "disable")
        assert (reaper.sockets / f"{REMOTE_VM}.info").exists()

    def test_a_bad_serve_toml_refuses_every_request(
        self, reaper: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        _leases_t._serve_toml(reaper, "enabled = flase\n")
        rc = ltvm.run(["--host", "t", "list", "--json"])
        assert rc == 1
        assert _err(capsys.readouterr().err)["code"] == "bad_config"


class TestEnable:
    def test_enable_accepts_requests_again(
        self, reaper: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        _serve(ltvm, capsys, "disable")
        rc, out = _serve(ltvm, capsys, "enable")
        assert (rc, out["enabled"]) == (0, True)
        rc = ltvm.run(["--host", "t", "list", "--json"])
        assert rc == 0
        names = [
            vm["name"] for vm in json.loads(capsys.readouterr().out)["vms"]
        ]
        assert names == [f"t/{REMOTE_VM}"]


class TestDisableDestroy:
    def test_destroys_leased_and_unleased_remote_vms_and_no_owner_vm(
        self,
        reaper: SimpleNamespace,
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        from tests.test_remote_lifecycle import REMOTE_OWNER, _save_vm

        _save_vm(reaper.sockets, "rvm-leased-a1", REMOTE_OWNER)
        _leases_t._grant(reaper, "vm", "rvm-leased-a1", 8)
        # REMOTE_VM has no lease: a VM from before leases existed.
        rc, out = _serve(ltvm, capsys, "disable", "--destroy")
        assert rc == 0
        assert out["enabled"] is False
        done = sorted(
            (r["kind"], r["name"], r["result"]) for r in out["destroyed"]
        )
        assert done == [
            ("vm", "rvm-leased-a1", "reaped"),
            ("vm", REMOTE_VM, "reaped"),
        ]
        assert not (reaper.sockets / f"{REMOTE_VM}.info").exists()
        assert not (reaper.sockets / "rvm-leased-a1.info").exists()
        assert (reaper.sockets / f"{OWNER_VM}.info").exists()
        assert _leases_t._leases(reaper) == {}
        logged = sorted(
            (e["verb"], *e["args"], e["result"]) for e in _audit(reaper.home)
        )
        assert logged == [
            ("destroy", "rvm-leased-a1", "reaped"),
            ("destroy", REMOTE_VM, "reaped"),
        ]

    def test_destroys_a_remote_cluster_with_no_lease(
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
        from tests.test_remote_lifecycle import REMOTE_OWNER

        _save_cluster(icl.sockets, REMOTE_CL, REMOTE_NODE, REMOTE_OWNER)
        _save_cluster(icl.sockets, "owner-cl-b2", "owner-node-b2", "pid:4242")
        rc, out = _serve(ltvm, capsys, "disable", "--destroy")
        assert rc == 0
        assert ("cluster", REMOTE_CL, "reaped") in [
            (r["kind"], r["name"], r["result"]) for r in out["destroyed"]
        ]
        assert not (icl.sockets / f"{REMOTE_CL}.cluster").exists()
        assert not (icl.sockets / f"{REMOTE_NODE}.info").exists()
        assert (icl.sockets / "owner-cl-b2.cluster").exists()
        assert (icl.sockets / "owner-node-b2.info").exists()
        assert (icl.sockets / f"{OWNER_VM}.info").exists()

    def test_a_failed_destroy_exits_1(
        self,
        reaper: SimpleNamespace,
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        with patch("ltvm_pkg.cli.setup._vm_call", return_value=1):
            rc, out = _serve(ltvm, capsys, "disable", "--destroy")
        assert rc == 1
        assert [r["result"] for r in out["destroyed"]] == ["exit 1"]
        assert out["enabled"] is False


class TestStatus:
    @pytest.fixture
    def busy(
        self, reaper: SimpleNamespace, clock: SimpleNamespace, ltvm: Any
    ) -> SimpleNamespace:
        """One remote VM (3 vCPUs, 2 GiB) with a lease, and two requests."""
        from ltvm_pkg.vm_state import VMInfo
        from tests.test_remote_lifecycle import REMOTE_OWNER

        (reaper.sockets / f"{REMOTE_VM}.info").unlink()
        with patch("ltvm_pkg.vm_state.SOCKETS", reaper.sockets):
            VMInfo(
                name="rvm-big-c3",
                ip="192.168.100.79",
                owner_id=REMOTE_OWNER,
                vcpus=3,
                mem=2048,
            ).save()
        _leases_t._grant(reaper, "vm", "rvm-big-c3", 2)
        _leases_t._grant(reaper, "vm", "gone-sentinel-c7", 1)
        _leases_t._serve_toml(reaper, "max_vcpus = 8\nmax_ram_gb = 16\n")
        ltvm.run(["--host", "t", "list", "--json"])
        ltvm.run(["--host", "t", "vm", "set", "rvm-big-c3", "--vcpus", "1"])
        clock.now = _leases_t.T0 + 1.5 * _leases_t.HOUR
        return reaper

    def test_json_shows_leases_usage_caps_and_recent_requests(
        self, busy: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        rc, out = _serve(ltvm, capsys, "status")
        assert rc == 0
        assert out["enabled"] is True
        leases = {(x["kind"], x["name"]): x for x in out["leases"]}
        big = leases[("vm", "rvm-big-c3")]
        assert big["expires"] == _leases_t.T0 + 2 * _leases_t.HOUR
        assert big["state"] == "active"
        assert leases[("vm", "gone-sentinel-c7")]["state"] == "expired"
        assert out["used"]["vcpus"] == 3
        assert out["used"]["ram_gb"] == 2
        assert out["cap"] == {"vcpus": 8, "ram_gb": 16, "disk_gb": None}
        recent = [(e["verb"], e["result"]) for e in out["recent"]]
        assert recent == [("list", "ok"), ("vm", "verb_refused")]

    def test_recent_limits_the_requests(
        self, busy: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        rc, out = _serve(ltvm, capsys, "status", "--recent", "1")
        assert [e["verb"] for e in out["recent"]] == ["vm"]

    def test_text_names_each_part(
        self, busy: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        capsys.readouterr()
        assert ltvm.run(["serve", "status"]) == 0
        out = capsys.readouterr().out
        assert "enabled" in out
        assert "rvm-big-c3" in out
        assert "vcpus: 3 / 8" in out
        assert "disk_gb" in out
        assert "verb_refused" in out

    def test_a_disabled_host_with_no_log(
        self, reaper: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        _serve(ltvm, capsys, "disable")
        rc, out = _serve(ltvm, capsys, "status")
        assert rc == 0
        assert (out["enabled"], out["leases"], out["recent"]) == (False, [], [])


class TestListShowsLeases:
    def test_list_shows_the_lease_expiry_of_each_remote_vm(
        self,
        inproc: SimpleNamespace,
        qemu: dict[str, Any],
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        assert (
            ltvm.run(["--host", "t", "create", "new-l5", "--lease", "2h"]) == 0
        )
        capsys.readouterr()
        assert ltvm.run(["--host", "t", "list", "--json"]) == 0
        vms = {
            vm["name"]: vm for vm in json.loads(capsys.readouterr().out)["vms"]
        }
        assert vms["t/new-l5"]["lease_expires"] == _leases_t.T0 + 2 * 3600
        # A VM from before leases existed has no lease.
        assert vms[f"t/{REMOTE_VM}"]["lease_expires"] is None
        assert ltvm.run(["--host", "t", "list"]) == 0
        line = next(
            ln
            for ln in capsys.readouterr().out.splitlines()
            if ln.startswith("t/new-l5 ")
        )
        assert "lease=" in line

    def test_local_list_has_no_lease_field(
        self, inproc: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        assert ltvm.run(["list", "--json"]) == 0
        vms = json.loads(capsys.readouterr().out)["vms"]
        assert all("lease_expires" not in vm for vm in vms)

    def test_cluster_list_and_list_show_the_cluster_lease(
        self,
        icl: SimpleNamespace,
        clock: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        from tests.test_remote_clusters import (
            REMOTE_CL,
            REMOTE_NODE,
            _save_cluster,
        )
        from tests.test_remote_lifecycle import REMOTE_OWNER

        _save_cluster(icl.sockets, REMOTE_CL, REMOTE_NODE, REMOTE_OWNER)
        _leases_t._grant(icl, "cluster", REMOTE_CL, 3)
        expires = _leases_t.T0 + 3 * 3600
        capsys.readouterr()
        assert ltvm.run(["--host", "t", "cluster", "list", "--json"]) == 0
        rows = json.loads(capsys.readouterr().out)["clusters"]
        assert [(c["cluster"], c["lease_expires"]) for c in rows] == [
            (f"t/{REMOTE_CL}", expires)
        ]
        assert ltvm.run(["--host", "t", "cluster", "list"]) == 0
        assert "lease=" in capsys.readouterr().out
        assert ltvm.run(["--host", "t", "list", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        node = next(v for v in data["vms"] if v["name"] == f"t/{REMOTE_NODE}")
        assert node["lease_expires"] == expires
        assert data["clusters"][0]["lease_expires"] == expires
