"""`ltvm serve --stdio`: the only entry point for remote requests.

sshd runs this command as the forced command of each controller key.
The request is in SSH_ORIGINAL_COMMAND.  `serve` splits it into an
argument vector with shlex and gives it to no shell.  Then it does these
checks, in this order:

1. the protocol version from the handshake matches
2. the verb is on the allow-list

Later changes add these checks: the host is enabled (before 1), path
arguments stay inside the job or the VM state, and admission control.

Then `serve` runs the verb in this process, with the update check and
telemetry off.  It writes one audit line for each request, refused
requests too.
"""

from __future__ import annotations

import os
import shlex
import sys
import traceback
from collections.abc import Callable, Mapping
from typing import IO

from . import allowlist, audit, protocol
from .protocol import RemoteError

EXIT_OK = 0
EXIT_ERROR = 1

# ltvm.run(argv, *, update_check, telemetry, remote) -> exit status.
RunFn = Callable[..., int]


def _ltvm_version() -> str:
    from ltvm_pkg import __version__

    return str(__version__)


def _split(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError as e:
        raise RemoteError(
            "bad_request", f"cannot split the command: {e}"
        ) from None


def _check_hello(line: str) -> None:
    try:
        hello = protocol.parse_line(line)
    except ValueError:
        raise RemoteError(
            "bad_request", "the request has no version handshake"
        ) from None
    if hello["protocol"] != protocol.PROTOCOL_VERSION:
        raise RemoteError(
            "version_mismatch",
            f"protocol version {hello['protocol']} on the controller "
            f"(ltvm {hello.get('ltvm')}), {protocol.PROTOCOL_VERSION} on "
            f"this host (ltvm {_ltvm_version()})",
        )


def _run_verb(run: RunFn, argv: list[str]) -> tuple[int, str]:
    """Run the verb.  Return the exit status and the audit result."""
    try:
        rc = run(argv, update_check=False, telemetry=False, remote=False)
    except SystemExit as e:
        # argparse and die() exit through SystemExit.
        if e.code is None or isinstance(e.code, int):
            rc = e.code or 0
        else:
            print(e.code, file=sys.stderr)
            rc = EXIT_ERROR
    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return EXIT_ERROR, "exception"
    return rc, "ok" if rc == 0 else f"exit {rc}"


def serve_stdio(
    run: RunFn,
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    # Remote use sends no telemetry from the host.  Children of the verb
    # read the environment, not the arguments of run().
    os.environ["LTVM_TELEMETRY"] = "0"
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    env = os.environ if env is None else env
    key = audit.key_fingerprint(env)
    log = audit.log_path(env)
    command = env.get("SSH_ORIGINAL_COMMAND", "")

    argv: list[str] = []
    split_error: RemoteError | None = None
    try:
        argv = _split(command)
    except RemoteError as e:
        split_error = e

    try:
        _check_hello(stdin.readline())
        if split_error:
            raise split_error
        allowlist.check(argv)
    except RemoteError as e:
        stdout.write(protocol.reply_line(_ltvm_version(), e) + "\n")
        stdout.flush()
        audit.append(log, key=key, argv=argv or [command], result=e.code)
        return EXIT_ERROR

    stdout.write(protocol.reply_line(_ltvm_version(), None) + "\n")
    stdout.flush()
    rc, result = _run_verb(run, argv)
    audit.append(log, key=key, argv=argv, result=result)
    return rc
