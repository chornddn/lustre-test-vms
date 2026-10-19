"""Remote hosts: pairing and the ssh:// transport, with no real ssh.

`host add`, `host list`, `host remove` and `serve authorize` run through
`ltvm.run()`.  A fake replaces ssh, ssh-keygen and ssh-keyscan: it
records each command line and gives fixed output.  The fake ssh starts
the real `ltvm serve --stdio` child with the environment that sshd
gives, so the ssh:// tests run the full controller-to-serve path.

tests/test_remote_ssh_tier2.py has the tests with real sshd.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests.remote_helpers import write_serve_toml
from tests.test_ltvm_cli import _LTVM_PATH
from tests.test_remote_serve import TEST_KEY_FP, TEST_PUBKEY

# A fixed host key.  The fingerprint is the value that `ssh-keygen -lf`
# gave for this key.
HOST_PUBKEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIODQA5JaUa2Qnpp4THBI2T2EB2rOLKvScU7"
    "5/McEXcWb ltvm:test-host"
)
HOST_FP = "SHA256:zhmIc4w8p7p5k/fiaN1VlKFXSqIv/J7+DRBnJoOBkk0"
PREFIX = 'restrict,command="ltvm serve --stdio",from='


@pytest.fixture
def host_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The home of the dedicated user on the host."""
    home = tmp_path / "ltvm-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _json_error(err: str) -> dict[str, Any]:
    data = json.loads(err)
    assert isinstance(data, dict)
    return data


class TestServeAuthorize:
    def test_writes_one_restricted_line(
        self, ltvm: Any, host_home: Path, capsys: Any
    ) -> None:
        rc = ltvm.run(
            ["serve", "authorize", TEST_PUBKEY, "--from", "192.0.2.7"]
        )
        assert rc == 0
        keys = host_home / ".ssh" / "authorized_keys"
        assert keys.read_text() == (
            'restrict,command="ltvm serve --stdio",from="192.0.2.7" '
            + TEST_PUBKEY
            + "\n"
        )
        assert stat.S_IMODE(keys.stat().st_mode) == 0o600
        assert stat.S_IMODE(keys.parent.stat().st_mode) == 0o700

    def test_keeps_the_lines_that_are_there(
        self, ltvm: Any, host_home: Path, capsys: Any
    ) -> None:
        ssh = host_home / ".ssh"
        ssh.mkdir(mode=0o700)
        (ssh / "authorized_keys").write_text(HOST_PUBKEY)  # no newline
        rc = ltvm.run(["serve", "authorize", TEST_PUBKEY, "--from", "::1"])
        assert rc == 0
        lines = (ssh / "authorized_keys").read_text().splitlines()
        assert lines[0] == HOST_PUBKEY
        assert lines[1].startswith(PREFIX + '"::1" ssh-ed25519 ')

    def test_refuses_a_key_that_has_a_line(
        self, ltvm: Any, host_home: Path, capsys: Any
    ) -> None:
        ltvm.run(["serve", "authorize", TEST_PUBKEY, "--from", "192.0.2.7"])
        keys = host_home / ".ssh" / "authorized_keys"
        before = keys.read_text()
        # The same key with a different comment and address.
        blob = " ".join(TEST_PUBKEY.split()[:2])
        rc = ltvm.run(
            ["serve", "authorize", blob, "--from", "192.0.2.8", "--json"]
        )
        assert rc == 1
        assert "already" in _json_error(capsys.readouterr().err)["error"]
        assert keys.read_text() == before

    @pytest.mark.parametrize(
        "pubkey",
        [
            # Options in front of the key would replace the restrictions.
            'command="/bin/sh" ' + TEST_PUBKEY,
            "ssh-ed25519 not-base64!",
            # The blob says ed25519, the type says rsa.
            "ssh-rsa " + TEST_PUBKEY.split()[1],
            TEST_PUBKEY + "\nssh-ed25519 AAAA",
            TEST_PUBKEY.replace("ltvm:test-controller", 'x" y'),
        ],
    )
    def test_refuses_a_bad_key(
        self, ltvm: Any, host_home: Path, capsys: Any, pubkey: str
    ) -> None:
        rc = ltvm.run(["serve", "authorize", pubkey, "--from", "192.0.2.7"])
        assert rc == 1
        assert not (host_home / ".ssh" / "authorized_keys").exists()

    @pytest.mark.parametrize(
        "addr",
        [
            '1.2.3.4" ssh-ed25519',
            "1.2.3.4 5.6.7.8",
            "",
            "a,,b",
            "*",
            "192.0.2.*",
            "?.?.?.?",
            "!192.0.2.7",
            "192.0.2.7,198.51.100.4",
            "ctl.example.com",
            "192.0.2.0/33",
            "192.0.2.7/24",
        ],
    )
    def test_refuses_a_bad_address(
        self, ltvm: Any, host_home: Path, capsys: Any, addr: str
    ) -> None:
        rc = ltvm.run(["serve", "authorize", TEST_PUBKEY, "--from", addr])
        assert rc == 1
        assert not (host_home / ".ssh" / "authorized_keys").exists()

    @pytest.mark.parametrize("addr", ["192.0.2.7", "192.0.2.0/24", "::1"])
    def test_takes_one_address_or_cidr(
        self, ltvm: Any, host_home: Path, capsys: Any, addr: str
    ) -> None:
        rc = ltvm.run(["serve", "authorize", TEST_PUBKEY, "--from", addr])
        assert rc == 0
        line = (host_home / ".ssh" / "authorized_keys").read_text()
        assert f'from="{addr}"' in line

    def test_needs_an_address(
        self, ltvm: Any, host_home: Path, capsys: Any
    ) -> None:
        with pytest.raises(SystemExit):
            ltvm.run(["serve", "authorize", TEST_PUBKEY])
        assert not (host_home / ".ssh" / "authorized_keys").exists()


# -- the controller side, with a fake ssh -----------------------------------

REMOTE_VM = "remote-sentinel-e5"


class FakeSsh:
    """Stand-in for ssh, ssh-keygen and ssh-keyscan.

    ssh-keygen writes TEST_PUBKEY as the new key.  ssh-keyscan gives
    `scan_key`, or fails when it is None.  ssh gives `ssh_result` when
    it is set.  Otherwise it starts `ltvm serve --stdio` with the
    environment that sshd gives to the forced command.
    """

    def __init__(self, tmp_path: Path, host_env: dict[str, str]) -> None:
        self.tmp_path = tmp_path
        self.host_env = host_env
        self.calls: list[list[str]] = []
        self.scan_key: str | None = HOST_PUBKEY
        self.ssh_result: subprocess.CompletedProcess[str] | None = None

    def tool_calls(self, tool: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == tool]

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        done = subprocess.CompletedProcess
        if argv[0] == "ssh-keygen":
            path = argv[argv.index("-f") + 1]
            comment = argv[argv.index("-C") + 1]
            Path(path).write_text("PRIVATE-SENTINEL\n")
            blob = " ".join(TEST_PUBKEY.split()[:2])
            Path(path + ".pub").write_text(f"{blob} {comment}\n")
            return done(argv, 0, "", "")
        if argv[0] == "ssh-keyscan":
            if self.scan_key is None:
                return done(argv, 1, "", "connect: Connection refused\n")
            key_type, blob = self.scan_key.split()[:2]
            return done(argv, 0, f"{argv[-1]} {key_type} {blob}\n", "")
        assert argv[0] == "ssh", argv
        if self.ssh_result is not None:
            return self.ssh_result
        return self._serve(argv, kwargs["input"])

    def _serve(self, argv: list[str], stdin: bytes) -> Any:
        from ltvm_pkg.remote.hosts import Host
        from ltvm_pkg.remote.transport import ExecTransport

        rest = argv[argv.index("--") + 2 :]
        command = rest[0] if rest else ""
        exec_host = Host(
            name="fake",
            url=f"exec://{_LTVM_PATH}",
            env=self.host_env,
            pubkey=TEST_PUBKEY,
        )
        env = ExecTransport(exec_host)._sshd_env(command, self.tmp_path)
        return subprocess.run(
            [sys.executable, _LTVM_PATH, "serve", "--stdio"],
            input=stdin,
            capture_output=True,
            env=env,
            cwd=env["HOME"],
            check=False,
        )


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSsh:
    """A fake ssh, and a host with one VM and its own state."""
    from ltvm_pkg.vm_state import VMInfo

    home = tmp_path / "remote-home"
    home.mkdir()
    write_serve_toml(home)
    vm_dir = tmp_path / "remote-vms"
    sockets = vm_dir / "sockets"
    sockets.mkdir(parents=True)
    # Not monkeypatch.undo(): it would also undo the conftest isolation.
    with patch("ltvm_pkg.vm_state.SOCKETS", sockets):
        # A remote request made this VM (serve gives it this owner ID).
        VMInfo(
            name=REMOTE_VM,
            ip="192.168.100.78",
            owner_id=f"remote:{TEST_KEY_FP}",
        ).save()
    fake = FakeSsh(
        tmp_path,
        {
            "HOME": str(home),
            "LTVM_VM_DIR": str(vm_dir),
            "LTVM_ARTIFACTS_DIR": str(tmp_path / "remote-artifacts"),
            "LTVM_SITE_CONFIG": str(tmp_path / "no-such-ltvm.conf"),
            # This machine need not have the shared layout.
            "LTVM_SERVE_TEST_LAYOUT": "ready",
        },
    )
    monkeypatch.setattr("ltvm_pkg.remote.ssh.run", fake)
    return fake


def _audit(fake: FakeSsh) -> list[dict[str, Any]]:
    log = Path(fake.host_env["HOME"]) / ".local/state/ltvm/serve.log"
    return [json.loads(line) for line in log.read_text().splitlines()]


def _add(ltvm: Any, capsys: Any, *extra: str, name: str = "lab") -> Any:
    rc = ltvm.run(
        [
            "host",
            "add",
            name,
            "ssh://ltvm@127.0.0.1",
            "--hostkey",
            HOST_FP,
            "--json",
            *extra,
        ]
    )
    out = capsys.readouterr().out
    return rc, (json.loads(out) if rc == 0 else None)


def _keys_dir() -> Path:
    return Path(os.environ["XDG_CONFIG_HOME"]) / "ltvm" / "keys"


class TestHostAdd:
    def test_makes_a_key_with_no_passphrase(
        self, ltvm: Any, fake: FakeSsh, capsys: Any
    ) -> None:
        rc, data = _add(ltvm, capsys)
        assert rc == 0
        (keygen,) = fake.tool_calls("ssh-keygen")
        assert keygen[keygen.index("-t") + 1] == "ed25519"
        assert keygen[keygen.index("-N") + 1] == ""
        assert data["key"] == str(_keys_dir() / "lab")
        assert (_keys_dir() / "lab").exists()

    def test_pins_only_the_host_key_with_the_fingerprint(
        self, ltvm: Any, fake: FakeSsh, capsys: Any
    ) -> None:
        rc, data = _add(ltvm, capsys)
        assert rc == 0
        assert data["hostkey_pinned"] is True
        known = (_keys_dir() / "lab.known_hosts").read_text()
        assert known == f"ltvm-lab {' '.join(HOST_PUBKEY.split()[:2])}\n"

    def test_prints_the_authorize_command(
        self, ltvm: Any, fake: FakeSsh, capsys: Any
    ) -> None:
        rc = ltvm.run(
            ["host", "add", "lab", "ssh://ltvm@127.0.0.1", "--hostkey", HOST_FP]
        )
        out = capsys.readouterr().out
        assert rc == 0
        blob = TEST_PUBKEY.split()[1]
        lines = [ln.strip() for ln in out.splitlines()]
        (command,) = [ln for ln in lines if "serve authorize" in ln]
        argv = shlex.split(command)
        assert argv[:7] == [
            "sudo",
            "-u",
            "ltvm",
            "-H",
            "ltvm",
            "serve",
            "authorize",
        ]
        assert argv[7].split()[1] == blob
        assert argv[8:] == ["--from", "127.0.0.1"]

    def test_the_printed_command_authorizes_the_key(
        self, ltvm: Any, fake: FakeSsh, host_home: Path, capsys: Any
    ) -> None:
        rc, data = _add(ltvm, capsys, "--from", "198.51.100.4")
        assert rc == 0
        argv = shlex.split(data["authorize_command"])
        assert ltvm.run(argv[5:]) == 0
        line = (host_home / ".ssh" / "authorized_keys").read_text()
        assert line.startswith(
            PREFIX + '"198.51.100.4" ssh-ed25519 ' + TEST_PUBKEY.split()[1]
        )

    def test_wrong_host_key_adds_nothing(
        self, ltvm: Any, fake: FakeSsh, capsys: Any
    ) -> None:
        rc = ltvm.run(
            [
                "host",
                "add",
                "lab",
                "ssh://ltvm@127.0.0.1",
                "--hostkey",
                TEST_KEY_FP,
                "--json",
            ]
        )
        assert rc == 1
        assert _json_error(capsys.readouterr().err)["code"] == (
            "host_key_mismatch"
        )
        from ltvm_pkg.remote import hosts

        assert hosts.list_hosts() == []
        assert not list(_keys_dir().iterdir())

    @pytest.mark.parametrize(
        "url, fp",
        [
            ("ssh://ltvm@127.0.0.1", "SHA256:short"),
            ("ssh://ltvm@127.0.0.1", "MD5:" + "a" * 43),
            ("http://127.0.0.1", HOST_FP),
            ("ssh://-oProxyCommand=x@127.0.0.1", HOST_FP),
            ("ssh://ltvm@127.0.0.1/path", HOST_FP),
        ],
    )
    def test_bad_url_or_fingerprint_makes_no_key(
        self, ltvm: Any, fake: FakeSsh, capsys: Any, url: str, fp: str
    ) -> None:
        rc = ltvm.run(["host", "add", "lab", url, "--hostkey", fp])
        assert rc == 1
        assert fake.calls == []

    def test_a_name_in_use_is_refused(
        self, ltvm: Any, fake: FakeSsh, capsys: Any
    ) -> None:
        assert _add(ltvm, capsys)[0] == 0
        keygens = len(fake.tool_calls("ssh-keygen"))
        assert _add(ltvm, capsys)[0] == 1
        assert len(fake.tool_calls("ssh-keygen")) == keygens

    def test_unreachable_host_is_pinned_on_first_use(
        self, ltvm: Any, fake: FakeSsh, capsys: Any
    ) -> None:
        fake.scan_key = None
        rc, data = _add(ltvm, capsys)
        assert rc == 0
        assert data["hostkey_pinned"] is False
        assert not (_keys_dir() / "lab.known_hosts").exists()
        fake.scan_key = HOST_PUBKEY
        assert ltvm.run(["--host", "lab", "list", "--json"]) == 0
        assert (_keys_dir() / "lab.known_hosts").exists()

    def test_first_use_refuses_a_different_host_key(
        self, ltvm: Any, fake: FakeSsh, capsys: Any
    ) -> None:
        fake.scan_key = None
        assert _add(ltvm, capsys)[0] == 0
        fake.scan_key = TEST_PUBKEY
        rc = ltvm.run(["--host", "lab", "list", "--json"])
        assert rc == 1
        err = _json_error(capsys.readouterr().err)
        assert err["code"] == "host_key_mismatch"
        assert fake.tool_calls("ssh") == []


class TestSshTransport:
    def test_list_goes_through_ssh_to_serve(
        self, ltvm: Any, fake: FakeSsh, capsys: Any
    ) -> None:
        _add(ltvm, capsys)
        rc = ltvm.run(["--host", "lab", "list", "--json"])
        out = capsys.readouterr().out
        assert rc == 0
        names = [vm["name"] for vm in json.loads(out)["vms"]]
        assert names == [f"lab/{REMOTE_VM}"]
        entry = _audit(fake)[-1]
        assert (entry["verb"], entry["key"]) == ("list", TEST_KEY_FP)

    def test_ssh_uses_the_key_and_the_pin_and_no_forwards(
        self, ltvm: Any, fake: FakeSsh, capsys: Any
    ) -> None:
        _add(ltvm, capsys)
        ltvm.run(["--host", "lab", "list", "--json"])
        (argv,) = fake.tool_calls("ssh")
        key = str(_keys_dir() / "lab")
        assert argv[argv.index("-i") + 1] == key
        assert argv[argv.index("-F") + 1] == "/dev/null"
        assert "-T" in argv
        opts = {
            argv[i + 1].split("=", 1)[0]: argv[i + 1].split("=", 1)[1]
            for i, a in enumerate(argv)
            if a == "-o"
        }
        assert opts["StrictHostKeyChecking"] == "yes"
        assert opts["UserKnownHostsFile"] == key + ".known_hosts"
        assert opts["GlobalKnownHostsFile"] == "/dev/null"
        assert opts["HostKeyAlias"] == "ltvm-lab"
        assert opts["IdentitiesOnly"] == "yes"
        assert opts["IdentityAgent"] == "none"
        assert opts["BatchMode"] == "yes"
        assert opts["ForwardAgent"] == "no"
        assert opts["ForwardX11"] == "no"
        assert opts["ClearAllForwardings"] == "yes"
        assert opts["PermitLocalCommand"] == "no"
        assert opts["ControlPath"] == "none"
        assert argv[argv.index("--") + 1 :] == ["127.0.0.1", "list --json"]

    def test_changed_host_key_fails(
        self, ltvm: Any, fake: FakeSsh, capsys: Any
    ) -> None:
        _add(ltvm, capsys)
        fake.ssh_result = subprocess.CompletedProcess(
            [], 255, "", "Host key verification failed.\n"
        )
        rc = ltvm.run(["--host", "lab", "list", "--json"])
        assert rc == 1
        err = _json_error(capsys.readouterr().err)
        assert err["code"] == "host_key_mismatch"

    def test_refused_key_names_serve_authorize(
        self, ltvm: Any, fake: FakeSsh, capsys: Any
    ) -> None:
        _add(ltvm, capsys)
        fake.ssh_result = subprocess.CompletedProcess(
            [], 255, "", "ltvm@127.0.0.1: Permission denied (publickey).\n"
        )
        rc = ltvm.run(["--host", "lab", "list", "--json"])
        assert rc == 1
        err = _json_error(capsys.readouterr().err)
        assert err["code"] == "unreachable"
        assert "serve authorize" in err["hint"]


class TestHostList:
    def test_shows_reachability_and_versions(
        self, ltvm: Any, fake: FakeSsh, capsys: Any
    ) -> None:
        _add(ltvm, capsys, name="up")
        _add(ltvm, capsys, name="down")
        from ltvm_pkg.remote import protocol

        real = fake.__call__

        def run(argv: list[str], **kwargs: Any) -> Any:
            if argv[0] == "ssh" and "HostKeyAlias=ltvm-down" in argv:
                return subprocess.CompletedProcess(
                    argv, 255, "", "connect: Connection refused\n"
                )
            return real(argv, **kwargs)

        import ltvm_pkg.remote.ssh as ssh_mod

        with pytest.MonkeyPatch.context() as m:
            m.setattr(ssh_mod, "run", run)
            rc = ltvm.run(["host", "list", "--json"])
        assert rc == 0
        rows = {
            r["name"]: r for r in json.loads(capsys.readouterr().out)["hosts"]
        }
        assert rows["up"]["status"] == "ok"
        assert rows["up"]["protocol"] == protocol.PROTOCOL_VERSION
        assert rows["up"]["ltvm"]
        assert rows["down"]["status"] == "unreachable"
        assert rows["down"]["protocol"] is None
        # The probe runs no verb, and the host logs it.
        assert [e["result"] for e in _audit(fake)] == ["probe"]

    def test_text_output(self, ltvm: Any, fake: FakeSsh, capsys: Any) -> None:
        _add(ltvm, capsys)
        assert ltvm.run(["host", "list"]) == 0
        out = capsys.readouterr().out
        assert out.splitlines()[0].split() == [
            "NAME",
            "URL",
            "STATUS",
            "PROTOCOL",
            "LTVM",
        ]
        assert out.splitlines()[1].split()[:3] == [
            "lab",
            "ssh://ltvm@127.0.0.1",
            "ok",
        ]

    def test_ltvm_host_does_not_send_host_list(
        self,
        ltvm: Any,
        capsys: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LTVM_HOST", "nosuch-host-f6")
        assert ltvm.run(["host", "list", "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == {"hosts": []}


class TestHostRemove:
    def test_deletes_the_entry_and_the_key_and_prints_the_line(
        self, ltvm: Any, fake: FakeSsh, capsys: Any
    ) -> None:
        _add(ltvm, capsys, "--from", "198.51.100.4")
        rc = ltvm.run(["host", "remove", "lab"])
        out = capsys.readouterr().out
        assert rc == 0
        from ltvm_pkg.remote import hosts

        assert hosts.list_hosts() == []
        assert not list(_keys_dir().iterdir())
        blob = TEST_PUBKEY.split()[1]
        (line,) = [ln.strip() for ln in out.splitlines() if blob in ln]
        assert line.startswith(PREFIX + '"198.51.100.4" ssh-ed25519 ' + blob)
        assert "~ltvm/.ssh/authorized_keys" in out

    def test_unknown_host(self, ltvm: Any, capsys: Any) -> None:
        rc = ltvm.run(["host", "remove", "nosuch", "--json"])
        assert rc == 1
        assert _json_error(capsys.readouterr().err)["code"] == "unknown_host"

    def test_keeps_a_key_outside_the_keys_directory(
        self, ltvm: Any, tmp_path: Path, capsys: Any
    ) -> None:
        from ltvm_pkg.remote import hosts

        outside = tmp_path / "not-mine-g7"
        outside.write_text("KEEP\n")
        hosts.add(
            hosts.Host(
                name="odd",
                url="ssh://ltvm@127.0.0.1",
                key=str(outside),
                hostkey=HOST_FP,
                from_addr="127.0.0.1",
            )
        )
        assert ltvm.run(["host", "remove", "odd"]) == 0
        assert outside.read_text() == "KEEP\n"
