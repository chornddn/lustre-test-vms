"""Remote hosts, tier 2: the key restrictions with real sshd on localhost.

These tests connect to the real sshd on 127.0.0.1 as a second local
user, the dedicated host user.  They show what the authorized_keys line
from `serve authorize` permits, and they do not assume it.

The tests change nothing on the machine.  A person sets up the machine
one time, as an admin.  The test runner does not need sudo:

1. Make a second local user, for example `ltvmtest`, with ltvm on the
   PATH that sshd gives it.  Add `Match User ltvmtest` and
   `ExposeAuthInfo yes` to sshd_config.
2. As the test runner, make two keys with no passphrase:
   `ssh-keygen -t ed25519 -N '' -f DIR/ok` and the same for
   `DIR/elsewhere`.
3. As the second user, authorize them:
   `ltvm serve authorize "$(cat DIR/ok.pub)" --from 127.0.0.1` and
   `ltvm serve authorize "$(cat DIR/elsewhere.pub)" --from 192.0.2.1`.
4. Run the tests with `LTVM_TIER2_USER=ltvmtest LTVM_TIER2_KEYS=DIR`.
   `LTVM_TIER2_PORT` sets the sshd port (default 22).

Each test skips when an item is missing, and the reason names it.
"""

from __future__ import annotations

import getpass
import json
import os
import pwd
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ltvm_pkg.remote import hosts, ssh

USER = os.environ.get("LTVM_TIER2_USER", "")
KEYS = Path(os.environ.get("LTVM_TIER2_KEYS", "/nonexistent"))
PORT = int(os.environ.get("LTVM_TIER2_PORT", "22"))
HOST_KEY_PUB = Path("/etc/ssh/ssh_host_ed25519_key.pub")
# A valid fingerprint that no real key has.
WRONG_FP = "SHA256:" + "A" * 43


def _sshd_listens() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=2):
            return True
    except OSError:
        return False


def _host_fingerprint() -> str | None:
    try:
        return ssh.fingerprint(HOST_KEY_PUB.read_text().split()[1])
    except (OSError, IndexError, ValueError):
        return None


def _missing() -> list[str]:
    missing = [
        f"the {tool} client"
        for tool in ("ssh", "ssh-keyscan")
        if shutil.which(tool) is None
    ]
    if not _sshd_listens():
        missing.append(f"sshd on 127.0.0.1:{PORT}")
    if not USER:
        missing.append("a second local user (set LTVM_TIER2_USER)")
    else:
        try:
            pwd.getpwnam(USER)
        except KeyError:
            missing.append(f"the second local user {USER!r}")
        if USER == getpass.getuser():
            missing.append("a second local user (not the test runner)")
    for name in ("ok", "elsewhere"):
        if not (KEYS / name).is_file():
            missing.append(
                f"the authorized key {KEYS / name} (set LTVM_TIER2_KEYS)"
            )
    if _host_fingerprint() is None:
        missing.append(f"a readable sshd host key {HOST_KEY_PUB}")
    return missing


_MISSING = _missing()

pytestmark = pytest.mark.skipif(
    bool(_MISSING),
    reason="tier-2 sshd tests need "
    + "; ".join(_MISSING)
    + " (see the docstring of tests/test_remote_ssh_tier2.py)",
)


def _add(name: str, key: str, hostkey: str | None = None) -> hosts.Host:
    """Register a host with a copy of a provisioned key."""
    path = ssh.key_path(name)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(KEYS / key, path)
    path.chmod(0o600)
    host = hosts.Host(
        name=name,
        url=f"ssh://{USER}@127.0.0.1:{PORT}",
        key=str(path),
        hostkey=hostkey or _host_fingerprint() or "",
        from_addr="127.0.0.1",
    )
    hosts.add(host)
    return host


def _plain_ssh(
    host: hosts.Host, *args: str, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    """An ssh command with the key, as a person would type it."""
    ssh.pin_host_key(host)
    return subprocess.run(
        [
            "ssh",
            "-F",
            "/dev/null",
            "-i",
            host.key,
            "-p",
            str(PORT),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            f"UserKnownHostsFile={ssh.known_hosts_path(host.key)}",
            "-o",
            f"HostKeyAlias={ssh.host_alias(host.name)}",
            *args,
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_probe_reaches_serve() -> None:
    _add("t2", "ok")
    response = hosts.transport_for("t2").request([])
    assert response.error is None
    assert isinstance(response.peer.get("protocol"), int)


def test_a_plain_ssh_command_gets_serve_and_not_a_shell() -> None:
    host = _add("t2", "ok")
    proc = _plain_ssh(host, f"{USER}@127.0.0.1", "id; echo shell-ran-h8")
    first = proc.stdout.splitlines()[0]
    reply: dict[str, Any] = json.loads(first)
    assert isinstance(reply["protocol"], int)
    # serve read no handshake, so it refused the request.
    assert reply["error"]["code"] == "bad_request"
    assert "shell-ran-h8" not in proc.stdout
    assert "uid=" not in proc.stdout


def test_a_port_forward_fails() -> None:
    host = _add("t2", "ok")
    proc = _plain_ssh(host, "-W", f"127.0.0.1:{PORT}", f"{USER}@127.0.0.1")
    assert proc.returncode != 0
    assert "open failed" in proc.stderr
    proc = _plain_ssh(
        host,
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-R",
        f"0:127.0.0.1:{PORT}",
        f"{USER}@127.0.0.1",
    )
    assert proc.returncode != 0
    assert "forwarding failed" in proc.stderr


def test_an_agent_forward_fails() -> None:
    # The client does not wait for an answer to its agent request.  So
    # read the key options that sshd sends to the client at -v: sshd
    # names "agent-forwarding" there only when the key permits it.
    host = _add("t2", "ok")
    proc = _plain_ssh(host, "-v", "-A", f"{USER}@127.0.0.1", "list")
    options = [ln for ln in proc.stderr.splitlines() if "key options:" in ln]
    assert options, proc.stderr
    for line in options:
        assert "agent-forwarding" not in line
        assert "port-forwarding" not in line
        assert "pty" not in line.split("key options:")[1]


def test_an_address_not_in_from_fails() -> None:
    _add("t2", "elsewhere")
    response = hosts.transport_for("t2").request([])
    assert response.error is not None
    assert response.error.code == "unreachable"
    assert "Permission denied" in (response.error.hint or "")


def test_a_wrong_host_key_fails_on_the_controller() -> None:
    _add("t2", "ok", hostkey=WRONG_FP)
    with pytest.raises(ssh.RemoteError) as e:
        hosts.transport_for("t2").request([])
    assert e.value.code == "host_key_mismatch"


def test_a_changed_host_key_fails_on_the_controller() -> None:
    host = _add("t2", "ok")
    # A pin from an earlier pairing with a different key.
    other = (
        "AAAAC3NzaC1lZDI1NTE5AAAAIODQA5JaUa2Qnpp4THBI2T2EB2rOLKvScU75/McEXcWb"
    )
    ssh.known_hosts_path(host.key).write_text(
        f"{ssh.host_alias('t2')} ssh-ed25519 {other}\n"
    )
    response = hosts.transport_for("t2").request([])
    assert response.error is not None
    assert response.error.code == "host_key_mismatch"
