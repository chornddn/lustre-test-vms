"""Remote hosts: `pull` copies results from the host to the controller.

The tests run the real `ltvm --host t pull ...` with the exec:// child
host, so the request goes through the client, the transport, the
handshake and the checks in `serve`.  The result files are fixtures in
the VM directory of the host.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests import test_remote_lifecycle as _lifecycle
from tests.test_remote_clusters import REMOTE_CL, REMOTE_NODE, _save_cluster
from tests.test_remote_lifecycle import (
    OWNER_VM,
    REMOTE_OWNER,
    REMOTE_VM,
    _audit,
)

child = _lifecycle.child

# Bytes that are not UTF-8, so a text transport would change them.
BINARY = bytes(range(256)) * 4


@pytest.fixture
def results(child: SimpleNamespace) -> SimpleNamespace:
    """Result files on the host for the remote VM and the owner VM."""
    vm_dir = child.vm_dir
    for vm in (REMOTE_VM, OWNER_VM):
        (child.sockets / f"{vm}.log").write_text(f"console of {vm}\n")
        crash = vm_dir / "crashes" / vm / f"crash-{vm}-20261019-101010"
        crash.mkdir(parents=True)
        (crash / "vmcore").write_bytes(BINARY)
        (crash / "vmcore-dmesg.txt").write_text(f"dmesg of {vm}\n")
        dk = vm_dir / "dk" / vm
        dk.mkdir(parents=True)
        (dk / f"dk-{vm}-20261019-101010.log").write_text(f"dk of {vm}\n")
        run = vm_dir / "suites" / "ltvm-suite" / vm / "r-1"
        run.mkdir(parents=True)
        (run / "results.yml").write_text(f"suite of {vm}\n")
    return child


@pytest.fixture
def local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The working directory of the controller."""
    cwd = tmp_path / "controller"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return cwd


def _files(root: Path) -> dict[str, bytes]:
    out = {}
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            p = Path(dirpath) / n
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


class TestPullTypes:
    def test_console_goes_to_the_default_destination(
        self, results: SimpleNamespace, local: Path, ltvm: Any, capsys: Any
    ) -> None:
        rc = ltvm.run(["--host", "t", "pull", REMOTE_VM, "console"])
        assert rc == 0, capsys.readouterr()
        dest = local / "ltvm-pull" / "t" / "console"
        assert _files(dest) == {
            f"{REMOTE_VM}/console.log": f"console of {REMOTE_VM}\n".encode()
        }
        assert str(dest) in capsys.readouterr().out

    def test_crash_bytes_arrive_unchanged(
        self, results: SimpleNamespace, local: Path, ltvm: Any, capsys: Any
    ) -> None:
        rc = ltvm.run(["pull", f"t/{REMOTE_VM}", "crash"])
        assert rc == 0, capsys.readouterr()
        crash = f"{REMOTE_VM}/crash-{REMOTE_VM}-20261019-101010"
        assert _files(local / "ltvm-pull" / "t" / "crash") == {
            f"{crash}/vmcore": BINARY,
            f"{crash}/vmcore-dmesg.txt": f"dmesg of {REMOTE_VM}\n".encode(),
        }

    def test_suite_results(
        self, results: SimpleNamespace, local: Path, ltvm: Any, capsys: Any
    ) -> None:
        rc = ltvm.run(["--host", "t", "pull", REMOTE_VM, "suite"])
        assert rc == 0, capsys.readouterr()
        assert _files(local / "ltvm-pull" / "t" / "suite") == {
            f"{REMOTE_VM}/r-1/results.yml": f"suite of {REMOTE_VM}\n".encode()
        }

    def test_dk_to_dest_with_json(
        self, results: SimpleNamespace, local: Path, ltvm: Any, capsys: Any
    ) -> None:
        dest = local / "mine"
        rc = ltvm.run(
            [
                "--host",
                "t",
                "pull",
                REMOTE_VM,
                "dk",
                "--dest",
                str(dest),
                "--json",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0, out
        name = f"{REMOTE_VM}/dk-{REMOTE_VM}-20261019-101010.log"
        assert _files(dest) == {name: f"dk of {REMOTE_VM}\n".encode()}
        data = json.loads(out)
        assert data["dest"] == str(dest)
        assert data["files"] == [name]
        assert not (local / "ltvm-pull").exists()

    def test_pull_again_replaces_the_files(
        self, results: SimpleNamespace, local: Path, ltvm: Any, capsys: Any
    ) -> None:
        assert ltvm.run(["--host", "t", "pull", REMOTE_VM, "console"]) == 0
        log = results.sockets / f"{REMOTE_VM}.log"
        log.write_text("newer\n")
        assert ltvm.run(["--host", "t", "pull", REMOTE_VM, "console"]) == 0
        got = local / "ltvm-pull" / "t" / "console" / REMOTE_VM / "console.log"
        assert got.read_text() == "newer\n"

    def test_no_results_is_not_an_error(
        self, child: SimpleNamespace, local: Path, ltvm: Any, capsys: Any
    ) -> None:
        rc = ltvm.run(["--host", "t", "pull", REMOTE_VM, "crash"])
        assert rc == 0
        assert "no crash results" in capsys.readouterr().out

    def test_audit_line(
        self, results: SimpleNamespace, local: Path, ltvm: Any
    ) -> None:
        assert ltvm.run(["--host", "t", "pull", REMOTE_VM, "console"]) == 0
        entry = _audit(results.home)[-1]
        assert entry["verb"] == "pull"
        assert entry["result"] == "ok"


class TestPullClusterAndJob:
    def test_cluster_console_selects_its_nodes(
        self, results: SimpleNamespace, local: Path, ltvm: Any, capsys: Any
    ) -> None:
        _save_cluster(results.sockets, REMOTE_CL, REMOTE_NODE, REMOTE_OWNER)
        (results.sockets / f"{REMOTE_NODE}.log").write_text("node log\n")
        rc = ltvm.run(["--host", "t", "pull", REMOTE_CL, "console"])
        assert rc == 0, capsys.readouterr()
        assert _files(local / "ltvm-pull" / "t" / "console") == {
            f"{REMOTE_NODE}/console.log": b"node log\n"
        }

    def test_job_selects_what_its_deploys_named(
        self, results: SimpleNamespace, local: Path, ltvm: Any, capsys: Any
    ) -> None:
        from ltvm_pkg.remote import hosts
        from tests.test_remote_deploy import _tar

        resp = hosts.transport_for("t").request(
            ["receive-tree"], _tar(("file", "configure.ac", "AC_INIT\n"))
        )
        assert resp.error is None
        info = json.loads(resp.stdout)
        # The deploy fails on the host (no artifacts), after the
        # checks recorded its VM in the job.
        hosts.transport_for("t").request(
            ["deploy-lustre", REMOTE_VM, "--lustre-tree", info["tree"]]
        )
        rc = ltvm.run(["--host", "t", "pull", info["job"], "suite"])
        assert rc == 0, capsys.readouterr()
        assert _files(local / "ltvm-pull" / "t" / "suite") == {
            f"{REMOTE_VM}/r-1/results.yml": f"suite of {REMOTE_VM}\n".encode()
        }

    def test_unknown_job_is_refused(
        self, results: SimpleNamespace, local: Path, ltvm: Any, capsys: Any
    ) -> None:
        rc = ltvm.run(
            [
                "--host",
                "t",
                "pull",
                "j-20261019-101010-0123abcd",
                "suite",
                "--json",
            ]
        )
        assert rc == 1
        assert json.loads(capsys.readouterr().err)["code"] == "vm_not_found"


def _send(command: str) -> Any:
    from ltvm_pkg.remote import hosts

    return hosts.transport_for("t").send(command)


class TestPullRefusals:
    @pytest.mark.parametrize(
        "command",
        [
            f"pull {REMOTE_VM} vmcore",
            f"pull {REMOTE_VM} ../../etc",
            f"pull {REMOTE_VM}",
            f"pull {REMOTE_VM} crash /etc",
            f"pull {REMOTE_VM} crash --dest /tmp",
        ],
    )
    def test_other_requests_are_refused(
        self, results: SimpleNamespace, command: str
    ) -> None:
        resp = _send(command)
        assert resp.error is not None
        assert resp.error.code == "bad_request"
        assert resp.stdout == ""
        assert _audit(results.home)[-1]["result"] == "bad_request"

    def test_controller_refuses_other_types(
        self, results: SimpleNamespace, local: Path, ltvm: Any
    ) -> None:
        with pytest.raises(SystemExit):
            ltvm.run(["--host", "t", "pull", REMOTE_VM, "vmcore"])

    @pytest.mark.parametrize(
        "name",
        [OWNER_VM, "absent-vm", "..", f"../{REMOTE_VM}", f"x/../{REMOTE_VM}"],
    )
    def test_names_outside_the_remote_vms_are_refused(
        self, results: SimpleNamespace, name: str
    ) -> None:
        resp = _send(f"pull {name} console")
        assert resp.error is not None
        assert resp.error.code in ("vm_not_found", "cluster_not_found")
        assert resp.stdout == ""

    def test_pull_with_no_host_is_an_error(
        self, local: Path, ltvm: Any, capsys: Any
    ) -> None:
        assert ltvm.run(["pull", REMOTE_VM, "console"]) == 1
        assert "remote host" in capsys.readouterr().err


class TestPullSymlinks:
    def _secret(self, results: SimpleNamespace) -> Path:
        secret = results.home / "secret-sentinel-c3"
        secret.mkdir(exist_ok=True)
        (secret / "key").write_text("sentinel-c3\n")
        return secret

    def _refused(self, command: str) -> None:
        resp = _send(command)
        assert resp.error is not None
        assert resp.error.code == "path_refused"
        assert "sentinel-c3" not in resp.stdout

    def test_console_log_that_is_a_link(self, results: SimpleNamespace) -> None:
        secret = self._secret(results)
        log = results.sockets / f"{REMOTE_VM}.log"
        log.unlink()
        log.symlink_to(secret / "key")
        self._refused(f"pull {REMOTE_VM} console")

    def test_vm_result_dir_that_is_a_link(
        self, results: SimpleNamespace
    ) -> None:
        secret = self._secret(results)
        crash = results.vm_dir / "crashes" / REMOTE_VM
        os.rename(crash, results.tmp / "moved")
        crash.symlink_to(secret)
        self._refused(f"pull {REMOTE_VM} crash")

    def test_top_result_dir_that_is_a_link(
        self, results: SimpleNamespace
    ) -> None:
        secret = self._secret(results)
        dk = results.vm_dir / "dk"
        os.rename(dk, results.tmp / "moved")
        dk.symlink_to(secret)
        self._refused(f"pull {REMOTE_VM} dk")

    def test_links_in_the_suite_results_are_not_followed(
        self, results: SimpleNamespace, local: Path, ltvm: Any, capsys: Any
    ) -> None:
        # rsync from the guest keeps links, so the guest can plant them.
        secret = self._secret(results)
        run = results.vm_dir / "suites" / "ltvm-suite" / REMOTE_VM / "r-1"
        (run / "file-link").symlink_to(secret / "key")
        (run / "dir-link").symlink_to(secret)
        (run / "dotdot-link").symlink_to("../../../../../host-home")
        rc = ltvm.run(["--host", "t", "pull", REMOTE_VM, "suite"])
        assert rc == 0
        got = _files(local / "ltvm-pull" / "t" / "suite")
        assert list(got) == [f"{REMOTE_VM}/r-1/results.yml"]
        assert "sentinel-c3" not in json.dumps(
            {k: v.decode() for k, v in got.items()}
        )
        assert "skipped the symbolic link" in capsys.readouterr().err


class TestUnpack:
    """The controller side refuses a stream that writes outside dest."""

    @staticmethod
    def _tar(*members: tuple[str, str]) -> bytes:
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name, kind in members:
                info = tarfile.TarInfo(name)
                if kind == "file":
                    info.size = 2
                    tar.addfile(info, io.BytesIO(b"x\n"))
                elif kind == "link":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/etc"
                    tar.addfile(info)
        return buf.getvalue()

    @pytest.mark.parametrize(
        "members",
        [
            [("../escape", "file")],
            [("/abs", "file")],
            [("a/../../escape", "file")],
            [("link", "link")],
        ],
        ids=["dotdot", "absolute", "dotdot-inside", "symlink"],
    )
    def test_unsafe_members(self, tmp_path: Path, members: Any) -> None:
        from ltvm_pkg.remote import pull
        from ltvm_pkg.remote.protocol import RemoteError

        dest = tmp_path / "d"
        with pytest.raises(RemoteError):
            pull.unpack(io.BytesIO(self._tar(*members)), dest)
        assert not (tmp_path / "escape").exists()
        assert not (tmp_path / "abs").exists()

    def test_link_in_dest_is_not_written_through(self, tmp_path: Path) -> None:
        from ltvm_pkg.remote import pull
        from ltvm_pkg.remote.protocol import RemoteError

        dest = tmp_path / "d"
        outside = tmp_path / "outside"
        outside.mkdir()
        dest.mkdir()
        (dest / "vm").symlink_to(outside)
        with pytest.raises(RemoteError):
            pull.unpack(io.BytesIO(self._tar(("vm/f", "file"))), dest)
        assert list(outside.iterdir()) == []
        (dest / "f").symlink_to(outside / "g")
        with pytest.raises(RemoteError):
            pull.unpack(io.BytesIO(self._tar(("f", "file"))), dest)
        assert list(outside.iterdir()) == []


inproc = _lifecycle.inproc


def _fake_dk(text: str) -> Any:
    """A run_ssh that answers `lctl dk` with text."""
    import subprocess

    calls: list[str] = []

    def run_ssh(
        ip: str, command: str, timeout: int = 120, *, stream: Any = None
    ) -> Any:
        calls.append(command)
        if stream is not None:
            stream.write(text)
            stream.flush()
            return subprocess.CompletedProcess([], 0)
        return subprocess.CompletedProcess([], 0, text, "")

    run_ssh.calls = calls  # type: ignore[attr-defined]
    return run_ssh


class TestDkCollect:
    def test_local_writes_a_file_in_outdir(
        self,
        tmp_path: Path,
        ltvm: Any,
        capsys: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ltvm_pkg import vm_commands
        from ltvm_pkg.vm_state import VMInfo

        VMInfo(name="co1-dk", ip="192.168.100.79").save()
        fake = _fake_dk("dk-sentinel-d4\n")
        monkeypatch.setattr(vm_commands, "is_running", lambda vm: True)
        monkeypatch.setattr(vm_commands, "run_ssh", fake)
        out = tmp_path / "out"
        rc = ltvm.run(
            ["vm", "dk-collect", "co1-dk", "--outdir", str(out), "--json"]
        )
        assert rc == 0, capsys.readouterr()
        data = json.loads(capsys.readouterr().out)
        path = Path(data["path"])
        assert path.parent == out
        assert path.name.startswith("dk-co1-dk-")
        assert path.read_text() == "dk-sentinel-d4\n"
        assert fake.calls == ["lctl dk"]

    def test_stopped_vm_is_an_error(
        self,
        tmp_path: Path,
        ltvm: Any,
        capsys: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ltvm_pkg import vm_commands
        from ltvm_pkg.vm_state import VMInfo

        VMInfo(name="co1-dk", ip="192.168.100.79").save()
        monkeypatch.setattr(vm_commands, "is_running", lambda vm: False)
        out = tmp_path / "out"
        rc = ltvm.run(["vm", "dk-collect", "co1-dk", "--outdir", str(out)])
        assert rc != 0
        assert not out.exists() or list(out.iterdir()) == []

    def test_remote_capture_then_pull(
        self,
        inproc: SimpleNamespace,
        local: Path,
        ltvm: Any,
        capsys: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ltvm_pkg import vm_commands

        monkeypatch.setattr(vm_commands, "is_running", lambda vm: True)
        monkeypatch.setattr(
            vm_commands, "run_ssh", _fake_dk("dk-sentinel-d4\n")
        )
        rc = ltvm.run(["--host", "t", "vm", "dk-collect", REMOTE_VM])
        assert rc == 0, capsys.readouterr()
        dk = inproc.vm_dir / "dk" / REMOTE_VM
        assert [p.read_text() for p in dk.iterdir()] == ["dk-sentinel-d4\n"]
        assert not (inproc.home / "ltvm-dk").exists()
        rc = ltvm.run(["--host", "t", "pull", REMOTE_VM, "dk"])
        assert rc == 0, capsys.readouterr()
        got = _files(local / "ltvm-pull" / "t" / "dk")
        assert list(got.values()) == [b"dk-sentinel-d4\n"]

    @pytest.mark.parametrize("extra", [["--outdir", "/tmp"], ["--outdir=/tmp"]])
    def test_remote_outdir_is_refused(
        self, child: SimpleNamespace, extra: list[str]
    ) -> None:
        resp = _send(" ".join(["vm", "dk-collect", REMOTE_VM, *extra]))
        assert resp.error is not None
        assert resp.error.code == "path_refused"

    def test_remote_owner_vm_is_refused(self, child: SimpleNamespace) -> None:
        resp = _send(f"vm dk-collect {OWNER_VM}")
        assert resp.error is not None
        assert resp.error.code == "vm_not_found"


class TestSuiteCollect:
    def test_collect_reaches_the_verb(self, child: SimpleNamespace) -> None:
        resp = _send(f"suite collect {REMOTE_VM} r-1 --json")
        assert resp.error is None

    @pytest.mark.parametrize("option", ["--dest", "--results-dir"])
    def test_collect_dest_is_refused(
        self, child: SimpleNamespace, option: str
    ) -> None:
        resp = _send(f"suite collect {REMOTE_VM} r-1 {option} /tmp/x")
        assert resp.error is not None
        assert resp.error.code == "path_refused"

    def test_collect_owner_vm_is_refused(self, child: SimpleNamespace) -> None:
        resp = _send(f"suite collect {OWNER_VM} r-1")
        assert resp.error is not None
        assert resp.error.code == "vm_not_found"


BIG = 64 << 20  # bytes in the large vmcore
BOUND = 16 << 20  # peak Python memory allowed for one pull


class _CountingSink(io.RawIOBase):
    """A binary stdout that counts the bytes and keeps none of them."""

    def __init__(self) -> None:
        self.count = 0

    def writable(self) -> bool:
        return True

    def write(self, b: Any) -> int:
        self.count += len(b)
        return len(b)


def _big_crash(vm_dir: Path) -> None:
    crash = vm_dir / "crashes" / REMOTE_VM / f"crash-{REMOTE_VM}-big"
    crash.mkdir(parents=True)
    with open(crash / "vmcore", "wb") as f:
        f.truncate(BIG)  # sparse: no disk use


class TestLargeBody:
    """A vmcore is several GB, so no side holds the whole body."""

    def test_controller_peak_memory_stays_under_the_bound(
        self, child: SimpleNamespace, local: Path, ltvm: Any, capsys: Any
    ) -> None:
        import tracemalloc

        _big_crash(child.vm_dir)
        tracemalloc.start()
        try:
            rc = ltvm.run(["--host", "t", "pull", REMOTE_VM, "crash"])
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert rc == 0, capsys.readouterr()
        got = local / "ltvm-pull" / "t" / "crash" / REMOTE_VM
        assert (got / f"crash-{REMOTE_VM}-big" / "vmcore").stat().st_size == BIG
        assert peak < BOUND, f"peak {peak} bytes"

    def test_serve_peak_memory_stays_under_the_bound(
        self, child: SimpleNamespace, ltvm: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tracemalloc

        from ltvm_pkg import vm_state
        from ltvm_pkg.remote import protocol, serve

        _big_crash(child.vm_dir)
        monkeypatch.setattr(vm_state, "VM_DIR", child.vm_dir)
        monkeypatch.setattr(vm_state, "SOCKETS", child.sockets)
        sink = _CountingSink()
        out = io.TextIOWrapper(io.BufferedWriter(sink), encoding="utf-8")
        env = {
            **child.env,
            "SSH_ORIGINAL_COMMAND": f"pull {REMOTE_VM} crash",
        }
        tracemalloc.start()
        try:
            rc = serve.serve_stdio(
                ltvm.run,
                parse=ltvm._parse_request,
                stdin=io.BytesIO((protocol.hello_line("0") + "\n").encode()),
                stdout=out,
                env=env,
            )
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        out.flush()
        assert rc == 0
        assert sink.count > BIG
        assert peak < BOUND, f"peak {peak} bytes"


class TestSshStream:
    """ssh:// streams a pull reply.  A local shell stands in for ssh."""

    @pytest.fixture
    def ssh_host(self, tmp_path: Path) -> Any:
        from ltvm_pkg.remote.hosts import Host
        from ltvm_pkg.remote.ssh import SshTransport

        key = tmp_path / "k"
        Path(str(key) + ".known_hosts").write_text("pinned\n")
        return SshTransport(
            Host(
                name="s",
                url="ssh://ltvm@192.0.2.9",
                key=str(key),
                hostkey="SHA256:" + "A" * 43,
            )
        )

    def _run(self, ssh_host: Any, script: str) -> tuple[Any, list[bytes]]:
        got: list[bytes] = []
        ssh_host.ssh_argv = lambda command: ["/bin/sh", "-c", script]
        resp = ssh_host.stream(
            ["pull", REMOTE_VM, "crash"], lambda f: got.append(f.read())
        )
        return resp, got

    def test_body_goes_to_consume(self, ssh_host: Any) -> None:
        from ltvm_pkg.remote import protocol

        line = protocol.reply_line("0", None)
        resp, got = self._run(
            ssh_host, f"cat >/dev/null; printf '%s\\nBODY' '{line}'"
        )
        assert resp.error is None
        assert resp.status == 0
        assert got == [b"BODY"]

    def test_refusal_does_not_reach_consume(self, ssh_host: Any) -> None:
        from ltvm_pkg.remote import protocol
        from ltvm_pkg.remote.protocol import RemoteError

        line = protocol.reply_line("0", RemoteError("vm_not_found", "no"))
        resp, got = self._run(ssh_host, f"printf '%s\\n' '{line}'; exit 1")
        assert resp.error is not None
        assert resp.error.code == "vm_not_found"
        assert got == []

    def test_ssh_failure(self, ssh_host: Any) -> None:
        resp, got = self._run(
            ssh_host, "echo 'Connection refused' >&2; exit 255"
        )
        assert resp.error is not None
        assert resp.error.code == "unreachable"
        assert got == []

    def test_hung_request_times_out(
        self, ssh_host: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_REMOTE_TIMEOUT", "0.5")
        resp, got = self._run(ssh_host, "exec sleep 30")
        assert resp.error is not None
        assert resp.error.code == "timed_out"
