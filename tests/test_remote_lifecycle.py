"""Remote hosts: single-VM lifecycle and the safety core of `serve`.

Two kinds of host are used here:

- `child`: the exec:// transport.  `serve` runs as a child process with
  the environment that sshd gives.  No patch reaches the child, so these
  tests use only verbs that need no QEMU, no root and no guest.
- `inproc`: the same `serve_stdio` in this process, behind the same
  Transport interface.  The conftest isolation and the QEMU stubs of
  test_vm_create_phases apply, so the verbs that start QEMU or change VM
  files can run.

Both go through `ltvm.run(["--host", "t", ...])`, the client, the
transport, the handshake and the checks in `serve`.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import stat
from collections.abc import Iterator
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from tests.test_ltvm_cli import _LTVM_PATH, _load_ltvm
from tests.test_remote_serve import TEST_KEY_FP, TEST_PUBKEY

REMOTE_OWNER = f"remote:{TEST_KEY_FP}"
REMOTE_VM = "rvm-sentinel-e5"
OWNER_VM = "owner-sentinel-f6"
LAYOUT_SWITCH = {"LTVM_SERVE_TEST_LAYOUT": "ready"}


@pytest.fixture(scope="module")
def ltvm() -> Any:
    return _load_ltvm()


def _save_vm(sockets: Path, name: str, owner_id: str | None) -> None:
    from ltvm_pkg.vm_state import VMInfo

    with patch("ltvm_pkg.vm_state.SOCKETS", sockets):
        VMInfo(name=name, ip="192.168.100.77", owner_id=owner_id).save()


def _audit(home: Path) -> list[dict[str, Any]]:
    log = home / ".local" / "state" / "ltvm" / "serve.log"
    return [json.loads(ln) for ln in log.read_text().splitlines()]


def _err(text: str) -> dict[str, Any]:
    data = json.loads(text)
    assert isinstance(data, dict)
    return data


# ── the child host (exec://) ─────────────────────────────


@pytest.fixture
def child(tmp_path: Path) -> SimpleNamespace:
    """Host `t` as an exec:// child, with one remote and one owner VM."""
    from ltvm_pkg.remote import hosts

    home = tmp_path / "host-home"
    home.mkdir()
    vm_dir = tmp_path / "host-vms"
    sockets = vm_dir / "sockets"
    sockets.mkdir(parents=True)
    _save_vm(sockets, REMOTE_VM, REMOTE_OWNER)
    _save_vm(sockets, OWNER_VM, "pid:4242")
    env = {
        "HOME": str(home),
        "LTVM_VM_DIR": str(vm_dir),
        "LTVM_ARTIFACTS_DIR": str(tmp_path / "host-artifacts"),
        "LTVM_SITE_CONFIG": str(tmp_path / "no-such-ltvm.conf"),
        **LAYOUT_SWITCH,
    }
    hosts.add(
        hosts.Host(
            name="t", url=f"exec://{_LTVM_PATH}", env=env, pubkey=TEST_PUBKEY
        )
    )
    return SimpleNamespace(
        home=home, vm_dir=vm_dir, sockets=sockets, env=env, tmp=tmp_path
    )


def _send(command: str) -> Any:
    from ltvm_pkg.remote import hosts

    return hosts.transport_for("t").send(command)  # type: ignore[attr-defined]


class TestRefusedVerbs:
    @pytest.mark.parametrize(
        "argv",
        [
            ["install"],
            ["update"],
            ["skills"],
            ["clean"],
            ["doctor", "--fix"],
            ["make-install", "co1"],
            ["make-uninstall", "co1"],
            ["make-reinstall", "co1"],
            ["target", "delete", "--remote", "rocky9", "--yes"],
            ["target", "publish", "rocky9"],
            ["telemetry", "off"],
            ["vm", "create", "co1"],
            ["vm", "set", REMOTE_VM, "--vcpus", "4"],
            ["claim", REMOTE_VM],
        ],
    )
    def test_verb_is_refused(
        self, child: SimpleNamespace, argv: list[str]
    ) -> None:
        resp = _send(shlex.join(argv))
        assert resp.error is not None
        assert resp.error.code == "verb_refused"
        assert resp.stdout == ""
        entry = _audit(child.home)[-1]
        assert entry["verb"] == argv[0]
        assert entry["result"] == "verb_refused"


class TestLayout:
    def test_classic_layout_refuses_every_request(
        self, child: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        # conftest pins this process to the classic layout.  Run serve
        # here, with no test switch, so that it sees that layout.
        from ltvm_pkg.remote import protocol, serve, transport

        out = io.StringIO()
        env = {
            "HOME": str(child.home),
            "SSH_ORIGINAL_COMMAND": f"vm console-log {REMOTE_VM}",
        }
        rc = serve.serve_stdio(
            ltvm.run,
            parse=ltvm._parse_request,
            stdin=io.StringIO(protocol.hello_line("0") + "\n"),
            stdout=out,
            env=env,
        )
        resp = transport.parse_reply(rc, out.getvalue(), "")
        assert rc == 1
        assert resp.error is not None
        assert resp.error.code == "layout_unsupported"
        assert _audit(child.home)[-1]["result"] == "layout_unsupported"

    def test_root_is_refused(
        self, child: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        from ltvm_pkg.remote import protocol, serve, transport

        out = io.StringIO()
        env = {
            "HOME": str(child.home),
            "SSH_ORIGINAL_COMMAND": "list",
            **LAYOUT_SWITCH,
        }
        with patch("os.geteuid", return_value=0):
            rc = serve.serve_stdio(
                ltvm.run,
                parse=ltvm._parse_request,
                stdin=io.StringIO(protocol.hello_line("0") + "\n"),
                stdout=out,
                env=env,
            )
        resp = transport.parse_reply(rc, out.getvalue(), "")
        assert resp.error is not None
        assert resp.error.code == "layout_unsupported"


class TestRemoteList:
    def test_json_shows_only_remote_vms_with_the_host_prefix(
        self, child: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        rc = ltvm.run(["--host", "t", "list", "--json"])
        out = capsys.readouterr().out
        assert rc == 0
        data = json.loads(out)
        assert [vm["name"] for vm in data["vms"]] == [f"t/{REMOTE_VM}"]
        assert OWNER_VM not in out
        assert data["totals"]["stopped"] == 1

    def test_text_shows_only_remote_vms_with_the_host_prefix(
        self, child: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        rc = ltvm.run(["--host", "t", "list"])
        out = capsys.readouterr().out
        assert rc == 0
        assert out.startswith(f"t/{REMOTE_VM} ")
        assert OWNER_VM not in out
        assert "0 running, 1 stopped" in out

    def test_no_remote_vms(
        self, child: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        (child.sockets / f"{REMOTE_VM}.info").unlink()
        rc = ltvm.run(["--host", "t", "list"])
        out = capsys.readouterr().out
        assert rc == 0
        assert out == "(no VMs)\n"


class TestNames:
    """`<host>/<vm>` on the controller, the bare name on the host."""

    def _logs(self, child: SimpleNamespace, local_sockets: Path) -> None:
        # One name on both machines, each with its own console log.
        _save_vm(local_sockets, REMOTE_VM, None)
        (local_sockets / f"{REMOTE_VM}.log").write_text("local-log-a7\n")
        (child.sockets / f"{REMOTE_VM}.log").write_text("remote-log-b8\n")

    def test_prefixed_name_goes_to_its_host(
        self, child: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        from ltvm_pkg import vm_state

        self._logs(child, vm_state.SOCKETS)
        rc = ltvm.run(["vm", "console-log", f"t/{REMOTE_VM}"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "remote-log-b8" in out
        assert "local-log-a7" not in out
        assert _audit(child.home)[-1]["args"] == ["console-log", REMOTE_VM]

    def test_bare_name_stays_local(
        self, child: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        from ltvm_pkg import vm_state

        self._logs(child, vm_state.SOCKETS)
        rc = ltvm.run(["vm", "console-log", REMOTE_VM])
        out = capsys.readouterr().out
        assert rc == 0
        assert "local-log-a7" in out
        assert "remote-log-b8" not in out

    def test_host_option_takes_the_prefixed_name(
        self, child: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        (child.sockets / f"{REMOTE_VM}.log").write_text("remote-log-b8\n")
        rc = ltvm.run(["--host", "t", "vm", "console-log", f"t/{REMOTE_VM}"])
        assert rc == 0
        assert "remote-log-b8" in capsys.readouterr().out

    def test_prefix_of_another_host_is_refused(
        self, child: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        rc = ltvm.run(
            ["--host", "t", "vm", "console-log", f"u/{REMOTE_VM}", "--json"]
        )
        assert rc == 1
        err = _err(capsys.readouterr().err)
        assert err["code"] == "bad_request"
        assert not (child.home / ".local").exists()


class TestOwnerVmsAreOutOfReach:
    @pytest.mark.parametrize(
        "argv",
        [
            ["vm", "console-log", OWNER_VM],
            ["vm", "nmi", OWNER_VM],
            ["vm", "snapshot", OWNER_VM],
            ["vm", "restore", OWNER_VM],
            ["vm", "crash-collect", OWNER_VM],
            ["destroy", OWNER_VM],
            ["start", OWNER_VM],
            ["stop", OWNER_VM],
            ["destroy", REMOTE_VM, OWNER_VM],
            ["create", "new-g4", "--disks-from", OWNER_VM],
        ],
    )
    def test_owner_vm_looks_absent(
        self, child: SimpleNamespace, argv: list[str]
    ) -> None:
        before = sorted(p.name for p in child.sockets.iterdir())
        owner = _send(shlex.join(argv))
        absent = _send(shlex.join([*argv[:-1], "nosuch-vm-c9"]))
        assert owner.error is not None
        assert owner.error.code == "vm_not_found"
        assert absent.error is not None
        # The reply for an owner VM tells nothing that the reply for a
        # missing VM does not tell.
        assert owner.error.message.replace(OWNER_VM, "X") == (
            absent.error.message.replace("nosuch-vm-c9", "X")
        )
        assert sorted(p.name for p in child.sockets.iterdir()) == before

    def test_create_on_an_owner_name_is_refused(
        self, child: SimpleNamespace
    ) -> None:
        resp = _send(f"create {OWNER_VM}")
        assert resp.error is not None
        assert resp.error.code == "name_in_use"
        assert _audit(child.home)[-1]["result"] == "name_in_use"


class TestInspection:
    def test_console_log(self, child: SimpleNamespace) -> None:
        (child.sockets / f"{REMOTE_VM}.log").write_text("boot-line-d0\n")
        resp = _send(f"vm console-log {REMOTE_VM}")
        assert resp.error is None
        assert resp.status == 0
        assert "boot-line-d0" in resp.stdout

    def test_console_log_follow_is_refused(
        self, child: SimpleNamespace
    ) -> None:
        resp = _send(f"vm console-log --fol {REMOTE_VM}")
        assert resp.error is not None
        assert resp.error.code == "verb_refused"

    def test_nmi_reaches_the_vm(self, child: SimpleNamespace) -> None:
        resp = _send(f"vm nmi {REMOTE_VM} --json")
        assert resp.error is None
        assert json.loads(resp.stdout) == {
            "error": f"VM '{REMOTE_VM}' not running"
        }

    def test_crash_collect_writes_into_the_vm_state(
        self, child: SimpleNamespace
    ) -> None:
        resp = _send(f"vm crash-collect {REMOTE_VM} --trigger --json")
        assert resp.error is None
        assert "not running" in resp.stdout
        assert (child.vm_dir / "crashes" / REMOTE_VM).is_dir()
        assert not (child.home / "ltvm-crashes").exists()

    @pytest.mark.parametrize(
        "extra",
        [
            ["--outdir", "/tmp"],
            ["--outdir=/tmp"],
            ["--mod-dir", "/home/owner/lustre"],
        ],
    )
    def test_crash_collect_paths_are_refused(
        self, child: SimpleNamespace, extra: list[str]
    ) -> None:
        resp = _send(shlex.join(["vm", "crash-collect", REMOTE_VM, *extra]))
        assert resp.error is not None
        assert resp.error.code == "path_refused"


class TestNoShell:
    @pytest.mark.parametrize(
        "template",
        [
            "destroy '{vm}; touch {s}'",
            "destroy '$(touch {s})'",
            "vm console-log '`touch {s}`'",
            "create 'x|touch {s}'",
            "create co1 --kernel '$(touch {s})' --dry-run",
            "stop {vm} '&& touch {s}'",
        ],
    )
    def test_metacharacters_never_run(
        self, child: SimpleNamespace, template: str
    ) -> None:
        sentinel = child.tmp / "shell-ran-e1"
        resp = _send(template.format(vm=REMOTE_VM, s=sentinel))
        assert not sentinel.exists()
        # Either serve refused it, or the verb got the word and failed.
        assert resp.error is not None or resp.status != 0
        assert (child.sockets / f"{REMOTE_VM}.info").exists()


class TestNeverSudo:
    def test_a_verb_that_wants_sudo_does_not_get_it(
        self, child: SimpleNamespace
    ) -> None:
        # A fake sudo first on PATH records a call.  The layout check
        # passes by the test switch, so the verb sees the real layout of
        # this machine and may try to get root.
        fakebin = child.tmp / "fakebin"
        fakebin.mkdir()
        sentinel = child.tmp / "sudo-ran-f2"
        sudo = fakebin / "sudo"
        sudo.write_text(f"#!/bin/sh\ntouch {sentinel}\nexit 0\n")
        sudo.chmod(sudo.stat().st_mode | stat.S_IXUSR)
        from ltvm_pkg.remote import hosts

        hosts.remove("t")
        child.env["PATH"] = f"{fakebin}:/usr/bin:/bin"
        hosts.add(
            hosts.Host(
                name="t",
                url=f"exec://{_LTVM_PATH}",
                env=child.env,
                pubkey=TEST_PUBKEY,
            )
        )
        _send(f"destroy {REMOTE_VM}")
        _send(f"stop {REMOTE_VM}")
        assert not sentinel.exists()


# ── the in-process host ─────────────────────────────────


class InProcTransport:
    """The Transport interface, with serve_stdio in this process."""

    def __init__(self, ltvm: Any, env: dict[str, str]) -> None:
        self.ltvm = ltvm
        self.env = env

    def request(self, argv: list[str], data: bytes = b"") -> Any:
        return self.send(shlex.join(argv), data)

    def send(self, command: str, data: bytes = b"") -> Any:
        from ltvm_pkg.remote import serve, transport

        env = {**self.env, "SSH_ORIGINAL_COMMAND": command}
        out = io.TextIOWrapper(
            io.BytesIO(), encoding="utf-8", write_through=True
        )
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = serve.serve_stdio(
                self.ltvm.run,
                parse=self.ltvm._parse_request,
                stdin=io.BytesIO(transport.stdin_bytes(data)),
                stdout=out,
                env=env,
                script=str(_LTVM_PATH),
            )
        out.flush()
        raw = out.buffer.getvalue()  # type: ignore[attr-defined]
        return transport.parse_reply(
            rc, transport.text(raw), err.getvalue(), raw
        )


@pytest.fixture
def inproc(
    ltvm: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    """Host `t` in this process, on a layout that needs no root."""
    from ltvm_pkg import priv, rootless, vm_commands, vm_state
    from ltvm_pkg.remote import hosts

    home = tmp_path / "host-home"
    home.mkdir()
    # serve changes these for the process; give them back after.
    monkeypatch.setenv("LTVM_OWNER_ID", "controller-owner")
    monkeypatch.setenv("LTVM_TELEMETRY", "0")
    monkeypatch.setattr(priv, "_sudo_forbidden", False)
    monkeypatch.setattr(vm_commands, "list_filter", None)
    monkeypatch.setattr(vm_commands, "list_lease", None)
    monkeypatch.setattr(vm_state, "VM_DIR", tmp_path)
    monkeypatch.setattr(rootless, "readiness", lambda: rootless.Readiness())
    auth = tmp_path / "auth"
    key_type, blob = TEST_PUBKEY.split()[:2]
    auth.write_text(f"publickey {key_type} {blob}\n")
    env = {"HOME": str(home), "SSH_USER_AUTH": str(auth), **LAYOUT_SWITCH}
    hosts.add(hosts.Host(name="t", url=f"exec://{_LTVM_PATH}"))
    monkeypatch.setattr(
        hosts, "transport_for", lambda name: InProcTransport(ltvm, env)
    )
    sockets = vm_state.SOCKETS
    # vm_commands binds SOCKETS by name when it is first imported.
    monkeypatch.setattr(vm_commands, "SOCKETS", sockets)
    _save_vm(sockets, REMOTE_VM, REMOTE_OWNER)
    _save_vm(sockets, OWNER_VM, "pid:4242")
    yield SimpleNamespace(home=home, sockets=sockets, vm_dir=tmp_path)


@pytest.fixture
def qemu(tmp_path: Path) -> Iterator[dict[str, Any]]:
    """Stub QEMU, disks, IPs and ssh for create and start."""
    from tests.test_vm_create_phases import _create_env

    with (
        patch("ltvm_pkg.vm_commands.OVERLAYS", tmp_path / "overlays"),
        patch("ltvm_pkg.vm_state.OVERLAYS", tmp_path / "overlays"),
        _create_env(tmp_path) as env,
    ):
        (tmp_path / "overlays").mkdir(exist_ok=True)
        yield env


class TestCreate:
    def test_dry_run_plans_and_writes_nothing(
        self,
        inproc: SimpleNamespace,
        qemu: dict[str, Any],
        ltvm: Any,
        capsys: Any,
    ) -> None:
        rc = ltvm.run(["--host", "t", "create", "t/new-g3", "--dry-run"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Would create VM: new-g3" in out
        assert not (inproc.sockets / "new-g3.info").exists()
        assert not qemu["launch"].called

    def test_create_marks_the_vm_as_remote(
        self,
        inproc: SimpleNamespace,
        qemu: dict[str, Any],
        ltvm: Any,
        capsys: Any,
    ) -> None:
        from ltvm_pkg.vm_state import VMInfo

        rc = ltvm.run(["--host", "t", "create", "new-g3"])
        assert rc == 0
        assert qemu["launch"].called
        assert VMInfo.load("new-g3").owner_id == REMOTE_OWNER
        capsys.readouterr()
        ltvm.run(["--host", "t", "list", "--json"])
        names = [v["name"] for v in json.loads(capsys.readouterr().out)["vms"]]
        assert sorted(names) == ["t/new-g3", f"t/{REMOTE_VM}"]

    def test_create_on_an_owner_vm_does_not_start_it(
        self,
        inproc: SimpleNamespace,
        qemu: dict[str, Any],
        ltvm: Any,
        capsys: Any,
    ) -> None:
        rc = ltvm.run(["--host", "t", "create", OWNER_VM, "--json"])
        assert rc == 1
        assert _err(capsys.readouterr().err)["code"] == "name_in_use"
        assert not qemu["launch"].called

    def test_create_on_a_remote_vm_converges_it(
        self,
        inproc: SimpleNamespace,
        qemu: dict[str, Any],
        ltvm: Any,
    ) -> None:
        rc = ltvm.run(["--host", "t", "create", REMOTE_VM])
        assert rc == 0
        assert qemu["launch"].called

    @pytest.mark.parametrize(
        ("extra", "code"),
        [
            (["--owner-id", "someone"], "bad_request"),
            (["--owner", "someone"], "bad_request"),
            (["--nic", "passthrough:0000:01:00.0"], "needs_root"),
            (["--image", "/home/owner/disk.qcow2"], "path_refused"),
        ],
    )
    def test_refused_create_writes_nothing(
        self,
        inproc: SimpleNamespace,
        qemu: dict[str, Any],
        ltvm: Any,
        capsys: Any,
        extra: list[str],
        code: str,
    ) -> None:
        rc = ltvm.run(["--host", "t", "create", "new-g3", *extra, "--json"])
        assert rc == 1
        assert _err(capsys.readouterr().err)["code"] == code
        assert not (inproc.sockets / "new-g3.info").exists()
        assert not qemu["launch"].called
        assert _audit(inproc.home)[-1]["result"] == code


class TestLifecycle:
    def test_start_launches_a_remote_vm(
        self,
        inproc: SimpleNamespace,
        qemu: dict[str, Any],
        ltvm: Any,
        capsys: Any,
    ) -> None:
        rc = ltvm.run(["--host", "t", "start", f"t/{REMOTE_VM}"])
        assert rc == 0
        assert qemu["launch"].call_args.args[0].name == REMOTE_VM
        assert f"started {REMOTE_VM}" in capsys.readouterr().out

    def test_start_of_a_passthrough_vm_is_refused(
        self,
        inproc: SimpleNamespace,
        qemu: dict[str, Any],
        ltvm: Any,
        capsys: Any,
    ) -> None:
        from ltvm_pkg.vm_state import VMInfo

        vm = VMInfo.load(REMOTE_VM)
        vm.nics = ["passthrough:0000:01:00.0"]
        vm.save()
        rc = ltvm.run(["--host", "t", "start", REMOTE_VM, "--json"])
        assert rc == 1
        assert _err(capsys.readouterr().err)["code"] == "needs_root"
        assert not qemu["launch"].called

    def test_stop_a_stopped_remote_vm(
        self, inproc: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        rc = ltvm.run(["--host", "t", "stop", REMOTE_VM])
        assert rc == 0

    def test_destroy_removes_only_the_remote_vm(
        self, inproc: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        with patch("ltvm_pkg.vm_commands.unregister_vm_dns", create=True):
            rc = ltvm.run(["--host", "t", "destroy", f"t/{REMOTE_VM}"])
        assert rc == 0
        assert not (inproc.sockets / f"{REMOTE_VM}.info").exists()
        assert (inproc.sockets / f"{OWNER_VM}.info").exists()

    def test_snapshot_and_restore_reach_qemu_img(
        self, inproc: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        from unittest.mock import MagicMock

        ran = MagicMock(returncode=0, stdout="", stderr="")
        with patch("ltvm_pkg.vm_commands.run", return_value=ran) as m_run:
            rc_snap = ltvm.run(
                ["--host", "t", "vm", "snapshot", REMOTE_VM, "s1"]
            )
            rc_list = ltvm.run(["--host", "t", "vm", "restore", REMOTE_VM])
        assert (rc_snap, rc_list) == (0, 0)
        cmds = [c.args[0] for c in m_run.call_args_list]
        assert any("-c" in c and "s1" in c for c in cmds)
        assert any("-l" in c for c in cmds)
        assert all(REMOTE_VM in " ".join(map(str, c)) for c in cmds)


class TestNeverSudoInProcess:
    def test_classic_layout_verb_fails_without_sudo(
        self,
        inproc: SimpleNamespace,
        ltvm: Any,
        capsys: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ltvm_pkg import priv, rootless

        # serve passes by the test switch, but the verb sees a layout
        # that needs root.
        monkeypatch.setattr(
            rootless,
            "readiness",
            lambda: rootless.Readiness(problems=["classic"]),
        )
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kw: Any) -> Any:
            calls.append(cmd)
            raise AssertionError("no command may run")

        monkeypatch.setattr(priv, "_run", fake_run)
        rc = ltvm.run(["--host", "t", "destroy", REMOTE_VM])
        assert rc != 0
        assert not [c for c in calls if c and c[0] == "sudo"]
        assert os.environ.get("LTVM_OWNER_ID") == REMOTE_OWNER
