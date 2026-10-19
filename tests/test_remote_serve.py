"""Remote hosts: the controller-to-serve path through the exec:// transport.

These tests run the real `ltvm --host t <verb>` path.  The exec://
transport starts `ltvm serve --stdio` as a child process with the
environment that sshd gives.  Each test gives the host a temporary home,
VM directory and audit log, so the child never sees the real host state.

The child is the code under test.  It can run only the verbs on the
serve allow-list, and every test points it at temporary state.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from tests.remote_helpers import write_serve_toml
from tests.test_ltvm_cli import _LTVM_PATH, _load_ltvm

# A fixed controller key.  The fingerprint is the value that
# `ssh-keygen -lf` gave for this key.
TEST_PUBKEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDfTEuzjmvfU/AyerplQgSfOmDmfY4HFN"
    "sDouH3P0dM+ ltvm:test-controller"
)
TEST_KEY_FP = "SHA256:EkIy9dZjzIZwWP1yIP/T771kMSUM9hzVq+TI2IZNItw"

REMOTE_VM = "remote-sentinel-b2"
LOCAL_VM = "local-sentinel-a1"


@pytest.fixture(scope="module")
def ltvm() -> Any:
    return _load_ltvm()


def _save_vm(sockets: Path, name: str, owner_id: str | None = None) -> None:
    from ltvm_pkg.vm_state import VMInfo

    with patch("ltvm_pkg.vm_state.SOCKETS", sockets):
        VMInfo(name=name, ip="192.168.100.77", owner_id=owner_id).save()


@pytest.fixture
def host_t(tmp_path: Path) -> SimpleNamespace:
    """Register host `t` on the controller, with its own state.

    The host has one VM.  The controller has a different VM in the
    sockets directory that conftest gives each test.
    """
    from ltvm_pkg import vm_state
    from ltvm_pkg.remote import hosts

    home = tmp_path / "host-home"
    home.mkdir()
    write_serve_toml(home)
    vm_dir = tmp_path / "host-vms"
    sockets = vm_dir / "sockets"
    sockets.mkdir(parents=True)
    # A remote request made this VM (serve gives it this owner ID).
    _save_vm(sockets, REMOTE_VM, f"remote:{TEST_KEY_FP}")
    _save_vm(vm_state.SOCKETS, LOCAL_VM)

    hosts.add(
        hosts.Host(
            name="t",
            url=f"exec://{_LTVM_PATH}",
            env={
                "HOME": str(home),
                "LTVM_VM_DIR": str(vm_dir),
                "LTVM_ARTIFACTS_DIR": str(tmp_path / "host-artifacts"),
                "LTVM_SITE_CONFIG": str(tmp_path / "no-such-ltvm.conf"),
                # This machine need not have the shared layout.
                "LTVM_SERVE_TEST_LAYOUT": "ready",
            },
            pubkey=TEST_PUBKEY,
        )
    )
    return SimpleNamespace(
        home=home,
        sockets=sockets,
        audit=home / ".local" / "state" / "ltvm" / "serve.log",
    )


class TestRemoteList:
    def test_host_flag_lists_the_host_vms(
        self, ltvm: Any, host_t: SimpleNamespace, capsys: Any
    ) -> None:
        rc = ltvm.run(["--host", "t", "list", "--json"])
        out = capsys.readouterr().out
        assert rc == 0
        names = [vm["name"] for vm in json.loads(out)["vms"]]
        assert names == [f"t/{REMOTE_VM}"]

    def test_ltvm_host_env_lists_the_host_vms(
        self,
        ltvm: Any,
        host_t: SimpleNamespace,
        capsys: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LTVM_HOST", "t")
        rc = ltvm.run(["list", "--json"])
        out = capsys.readouterr().out
        assert rc == 0
        assert [vm["name"] for vm in json.loads(out)["vms"]] == [
            f"t/{REMOTE_VM}"
        ]

    def test_text_output_comes_back(
        self, ltvm: Any, host_t: SimpleNamespace, capsys: Any
    ) -> None:
        rc = ltvm.run(["--host=t", "list"])
        out = capsys.readouterr().out
        assert rc == 0
        assert REMOTE_VM in out
        assert LOCAL_VM not in out

    def test_abbreviated_host_option(
        self, ltvm: Any, host_t: SimpleNamespace, capsys: Any
    ) -> None:
        rc = ltvm.run(["--ho", "t", "list", "--json"])
        out = capsys.readouterr().out
        assert rc == 0
        assert [vm["name"] for vm in json.loads(out)["vms"]] == [
            f"t/{REMOTE_VM}"
        ]

    def test_ltvm_host_env_does_not_take_serve(
        self,
        ltvm: Any,
        host_t: SimpleNamespace,
        capsys: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # serve without --stdio fails here, before it reads stdin.
        monkeypatch.setenv("LTVM_HOST", "t")
        rc = ltvm.run(["serve", "--json"])
        assert rc == 1
        assert "serve needs --stdio" in capsys.readouterr().err
        assert not host_t.audit.exists()

    def test_request_writes_one_audit_line(
        self, ltvm: Any, host_t: SimpleNamespace, capsys: Any
    ) -> None:
        ltvm.run(["--host", "t", "list", "--json"])
        lines = host_t.audit.read_text().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["key"] == TEST_KEY_FP
        assert entry["verb"] == "list"
        assert entry["args"] == ["--json"]
        assert entry["result"] == "ok"
        assert entry["time"].endswith("Z")


def _json_error(err: str) -> dict[str, Any]:
    data = json.loads(err)
    assert isinstance(data, dict)
    return data


class TestRefusals:
    @pytest.mark.parametrize(
        "argv",
        [
            ["vm", "set", REMOTE_VM, "--vcpus", "4"],
            ["clean"],
            ["install"],
            ["telemetry", "off"],
            ["serve", "--stdio"],
        ],
    )
    def test_verb_not_on_allow_list_is_refused(
        self,
        ltvm: Any,
        host_t: SimpleNamespace,
        capsys: Any,
        argv: list[str],
    ) -> None:
        rc = ltvm.run(["--host", "t", *argv, "--json"])
        captured = capsys.readouterr()
        assert rc == 1
        err = _json_error(captured.err)
        assert err["code"] == "verb_refused"
        assert argv[0] in err["error"]
        entry = json.loads(host_t.audit.read_text().splitlines()[-1])
        assert entry["verb"] == argv[0]
        assert entry["result"] == "verb_refused"
        assert entry["key"] == TEST_KEY_FP

    def test_refused_vm_set_leaves_the_vm(
        self, ltvm: Any, host_t: SimpleNamespace, capsys: Any
    ) -> None:
        info = host_t.sockets / f"{REMOTE_VM}.info"
        before = info.read_text()
        ltvm.run(["--host", "t", "vm", "set", REMOTE_VM, "--vcpus", "4"])
        assert info.read_text() == before

    def test_refusal_without_json_is_text(
        self, ltvm: Any, host_t: SimpleNamespace, capsys: Any
    ) -> None:
        rc = ltvm.run(["--host", "t", "clean"])
        err = capsys.readouterr().err
        assert rc == 1
        assert err.startswith("error: host t: ")
        assert "not allowed on a remote host" in err

    def test_version_mismatch_stops_the_request(
        self,
        ltvm: Any,
        host_t: SimpleNamespace,
        capsys: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The controller speaks a newer protocol than the host.
        monkeypatch.setattr("ltvm_pkg.remote.protocol.PROTOCOL_VERSION", 999)
        rc = ltvm.run(["--host", "t", "list", "--json"])
        captured = capsys.readouterr()
        assert rc == 1
        assert REMOTE_VM not in captured.out
        err = _json_error(captured.err)
        assert err["code"] == "version_mismatch"
        assert "999" in err["error"]
        assert "owner of host 't'" in err["hint"]
        assert "ltvm update" in err["hint"]
        entry = json.loads(host_t.audit.read_text().splitlines()[-1])
        assert entry["result"] == "version_mismatch"

    def test_unknown_host(self, ltvm: Any, capsys: Any) -> None:
        rc = ltvm.run(["--host", "nosuch", "list", "--json"])
        assert rc == 1
        assert _json_error(capsys.readouterr().err)["code"] == "unknown_host"


class TestNoShell:
    """serve splits the command with no shell, so no argument runs."""

    def _transport(self) -> Any:
        from ltvm_pkg.remote import hosts

        return hosts.transport_for("t")

    def test_metacharacters_in_the_verb_are_refused(
        self, host_t: SimpleNamespace, tmp_path: Path
    ) -> None:
        sentinel = tmp_path / "shell-ran-c3"
        resp = self._transport().send(f"list; touch {sentinel}")
        assert resp.error is not None
        assert resp.error.code == "verb_refused"
        assert not sentinel.exists()

    def test_metacharacters_in_an_argument_reach_the_verb_unchanged(
        self, host_t: SimpleNamespace, tmp_path: Path
    ) -> None:
        sentinel = tmp_path / "shell-ran-d4"
        resp = self._transport().send(f"list --json '$(touch {sentinel})'")
        # The verb gets the argument as one word and rejects it.
        assert resp.error is None
        assert resp.status == 2
        assert f"$(touch {sentinel})" in resp.stderr
        assert not sentinel.exists()
        entry = json.loads(host_t.audit.read_text().splitlines()[-1])
        assert entry["args"] == ["--json", f"$(touch {sentinel})"]
        assert entry["result"] == "exit 2"

    def test_unbalanced_quote_is_a_bad_request(
        self, host_t: SimpleNamespace
    ) -> None:
        resp = self._transport().send("list 'oops")
        assert resp.error is not None
        assert resp.error.code == "bad_request"
        entry = json.loads(host_t.audit.read_text().splitlines()[-1])
        assert entry["result"] == "bad_request"


class TestAllowListTable:
    def test_a_subcommand_entry_allows_only_that_subcommand(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg.remote import allowlist
        from ltvm_pkg.remote.protocol import RemoteError

        monkeypatch.setattr(
            allowlist, "ALLOW_LIST", frozenset({("vm", "console-log")})
        )
        allowlist.check(["vm", "console-log", "co1"])
        for argv in (["vm", "nmi", "co1"], ["vm"], ["vm", "--json"]):
            with pytest.raises(RemoteError) as e:
                allowlist.check(argv)
            assert e.value.code == "verb_refused"
