"""Send one request to `serve` and read the reply.

A transport sends the argument vector of one ltvm command to `ltvm serve
--stdio` on the host.  It returns the exit status, the stdout and the
stderr of the verb, and a RemoteError when `serve` refused the request
or the reply was bad.

The request goes the way SSH sends it.  The command is one string.
sshd gives it to the forced command as SSH_ORIGINAL_COMMAND.  stdin carries
the handshake line, then the request data (the tar stream of
`receive-tree`).

The exec:// transport is for tests: it starts `serve` as a local child
process, with the environment that sshd gives to a forced command.  The
ssh:// transport is in ssh.py.
"""

from __future__ import annotations

import getpass
import shlex
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Protocol

from . import protocol
from .hosts import Host
from .protocol import RemoteError


@dataclass
class Response:
    status: int
    stdout: str
    stderr: str
    error: RemoteError | None = None
    # The handshake reply from serve: its protocol and ltvm versions.
    peer: dict[str, Any] = field(default_factory=dict)


# Reads the verb stdout of a reply, while serve writes it.
Consume = Callable[[IO[bytes]], None]


class Transport(Protocol):
    def request(self, argv: list[str], data: bytes = b"") -> Response: ...

    def send(self, command: str, data: bytes = b"") -> Response:
        """Send one command string, as an SSH client sends it.

        request() quotes argv into this string.  data goes to the stdin
        of serve after the handshake line.
        """
        ...

    def stream(self, argv: list[str], consume: Consume) -> Response:
        """Send argv.  Give the verb stdout to consume, as a stream.

        consume runs only when serve accepted the request.  The
        Response then has an empty stdout.  An exception in consume
        stops the request and goes to the caller.
        """
        ...


@dataclass
class Streamed:
    """What run_streamed saw: the status, the reply line and stderr."""

    status: int
    first: str
    stderr: str
    timed_out: bool = False


_CHUNK = 1 << 20


def run_streamed(
    cmd: list[str],
    consume: Consume,
    *,
    timeout: float | None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> Streamed:
    """Run a child that answers one request, and stream its stdout.

    The child gets the handshake line on stdin.  This function reads the
    reply line.  If the reply has no error, consume reads the rest of
    stdout while the child writes it.  So no full copy is in memory.
    stderr goes to a temporary file, so a full pipe cannot stop the
    child.
    """
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=err,
            env=env,
            cwd=cwd,
        )
        assert proc.stdin is not None and proc.stdout is not None
        expired = threading.Event()

        def stop() -> None:
            expired.set()
            proc.kill()

        timer = threading.Timer(timeout, stop) if timeout else None
        if timer:
            timer.start()
        try:
            try:
                proc.stdin.write(stdin_bytes(b""))
                proc.stdin.close()
            except BrokenPipeError:
                pass
            first = text(proc.stdout.readline())
            if parse_reply(0, first, "").error is None:
                consume(proc.stdout)
            # Read what consume left, so the child can finish.
            while proc.stdout.read(_CHUNK):
                pass
            status = proc.wait()
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            if timer:
                timer.cancel()
            proc.stdout.close()
        err.seek(0)
        return Streamed(status, first, text(err.read()), expired.is_set())


def stdin_bytes(data: bytes) -> bytes:
    """The stdin of serve: the handshake line, then the request data."""
    return (protocol.hello_line(_ltvm_version()) + "\n").encode() + data


def text(out: bytes | str | None) -> str:
    """Decode the output of a child process."""
    if isinstance(out, bytes):
        return out.decode("utf-8", "replace")
    return out or ""


def _ltvm_version() -> str:
    from ltvm_pkg import __version__

    return str(__version__)


def parse_reply(status: int, stdout: str, stderr: str) -> Response:
    """Split the handshake reply from the verb output.

    Every transport uses this function, so they all read the reply the
    same way.
    """
    first, _, rest = stdout.partition("\n")
    try:
        peer = protocol.parse_line(first)
    except ValueError:
        tail = stderr.strip().splitlines()[-1:] or ["no output"]
        return Response(
            status,
            "",
            stderr,
            RemoteError(
                "bad_reply",
                "the host did not answer the version handshake",
                hint=f"The host said: {tail[0]}",
            ),
        )
    ours = protocol.PROTOCOL_VERSION
    if peer["protocol"] != ours:
        err = RemoteError(
            "version_mismatch",
            f"protocol version {ours} on this controller "
            f"(ltvm {_ltvm_version()}), "
            f"{peer['protocol']} on the host (ltvm {peer.get('ltvm')})",
            details={
                "host_protocol": peer["protocol"],
                "host_ltvm": peer.get("ltvm"),
            },
        )
        return Response(status, "", stderr, err, peer)
    if peer.get("error"):
        try:
            err = RemoteError.from_json(peer["error"])
        except ValueError as e:
            err = RemoteError("bad_reply", f"bad error in the reply: {e}")
        return Response(status, "", stderr, err, peer)
    return Response(status, rest, stderr, None, peer)


class ExecTransport:
    """Run `ltvm serve --stdio` as a local child process (tests only)."""

    # Seconds.  A request that takes longer fails with timed_out, so a
    # broken test fails and does not hang.
    timeout: float = 120.0

    def __init__(self, host: Host) -> None:
        self.host = host
        self.script = Path(host.url[len("exec://") :])
        if not self.script.is_absolute():
            raise ValueError(f"exec:// needs an absolute path: {host.url}")

    def request(self, argv: list[str], data: bytes = b"") -> Response:
        return self.send(shlex.join(argv), data)

    def send(self, command: str, data: bytes = b"") -> Response:
        """Send one command string, as an SSH client sends it.

        data goes to the stdin of serve after the handshake line.
        """
        with tempfile.TemporaryDirectory(prefix="ltvm-exec-") as tmp:
            env = self._sshd_env(command, Path(tmp))
            try:
                proc = subprocess.run(
                    [sys.executable, str(self.script), "serve", "--stdio"],
                    input=stdin_bytes(data),
                    capture_output=True,
                    env=env,
                    cwd=env["HOME"],
                    check=False,
                    timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                return Response(
                    1,
                    "",
                    "",
                    RemoteError(
                        "timed_out",
                        f"serve did not finish in {self.timeout:g} s",
                    ),
                )
        return parse_reply(
            proc.returncode, text(proc.stdout), text(proc.stderr)
        )

    def stream(self, argv: list[str], consume: Consume) -> Response:
        command = shlex.join(argv)
        with tempfile.TemporaryDirectory(prefix="ltvm-exec-") as tmp:
            env = self._sshd_env(command, Path(tmp))
            got = run_streamed(
                [sys.executable, str(self.script), "serve", "--stdio"],
                consume,
                timeout=self.timeout,
                env=env,
                cwd=env["HOME"],
            )
        if got.timed_out:
            return Response(
                1,
                "",
                "",
                RemoteError(
                    "timed_out", f"serve did not finish in {self.timeout:g} s"
                ),
            )
        return parse_reply(got.status, got.first, got.stderr)

    def _sshd_env(self, command: str, tmp: Path) -> dict[str, str]:
        """The environment that sshd gives to a forced command."""
        user = getpass.getuser()
        env = {
            "HOME": str(Path.home()),
            "USER": user,
            "LOGNAME": user,
            "SHELL": "/bin/sh",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            # No SSH_CONNECTION: serve refuses its test switch under sshd.
            "SSH_ORIGINAL_COMMAND": command,
        }
        if self.host.pubkey:
            # sshd with ExposeAuthInfo writes this file.  Each line is
            # "publickey <type> <base64 key>".
            key_type, blob = self.host.pubkey.split()[:2]
            auth = tmp / "auth"
            auth.write_text(f"publickey {key_type} {blob}\n")
            env["SSH_USER_AUTH"] = str(auth)
        env.update(self.host.env)
        return env


def for_host(host: Host) -> Transport:
    if host.url.startswith("exec://"):
        return ExecTransport(host)
    if host.url.startswith("ssh://"):
        from .ssh import SshTransport

        return SshTransport(host)
    raise RemoteError(
        "unknown_host",
        f"host {host.name!r} has an unsupported URL: {host.url}",
    )
