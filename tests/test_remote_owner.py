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
