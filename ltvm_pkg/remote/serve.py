"""`ltvm serve --stdio`: the only entry point for remote requests.

sshd runs this command as the forced command of each controller key.
The request is in SSH_ORIGINAL_COMMAND.  `serve` splits it into an
argument vector with shlex and gives it to no shell.  Then it does these
checks, in this order:

1. the protocol version from the handshake matches
2. the host has the shared layout, and serve does not run as root.
   A probe (no command) skips this check and the checks after it.
3. the verb is on the allow-list
4. the request stays in its scope: it names only VMs that remote
   requests made, it needs no root, and a Lustre tree is a job tree
   that `receive-tree` made (scope.py, jobs.py)
5. the request stays inside the caps of serve.toml (admission.py)
6. a request that builds has a rootless or VM-backed podman (podman.py)

A request with no command is a probe.  `serve` sends only the handshake
reply, so `host list` gets the versions with no verb.

`receive-tree` is not an ltvm verb.  After check 3, serve unpacks the
tar stream that follows the handshake line into a new job directory,
and prints the job (jobs.py).  serve also answers `pull` itself after
check 3: it sends the results as a tar stream (pull.py).

A later change adds this check: the host is enabled (before 1).

Then `serve` runs the verb in this process, with the update check and
telemetry off, and with sudo forbidden.  It writes one audit line for
each request, refused requests too.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shlex
import sys
import traceback
from collections.abc import Callable, Mapping
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import IO, Any

from ltvm_pkg import priv

from . import (
    admission,
    allowlist,
    audit,
    jobs,
    leases,
    podman,
    power,
    protocol,
    scope,
)
from .protocol import RemoteError
from .tree import RECEIVE_TREE

EXIT_OK = 0
EXIT_ERROR = 1

# ltvm.run(argv, *, update_check, telemetry, remote) -> exit status.
RunFn = Callable[..., int]
# Parse argv as ltvm does.  Raises SystemExit for a bad command.
ParseFn = Callable[[list[str]], argparse.Namespace]

# Tests only.  With the value "ready", serve does not check the layout of
# the host.  sshd does not let a client set the environment of a forced
# command, so a controller cannot set it.
TEST_LAYOUT_ENV = "LTVM_SERVE_TEST_LAYOUT"


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


def _read_hello(stdin: IO[Any]) -> str:
    """Read the handshake line.  The rest of stdin is the request data."""
    line = stdin.readline()
    if isinstance(line, bytes):
        return line.decode("utf-8", "replace")
    return str(line)


def _receive_tree(argv: list[str], stdin: IO[Any], home: Path, key: str) -> str:
    """Unpack the tree stream into a new job.  Return the verb stdout."""
    if len(argv) != 1:
        raise RemoteError("bad_request", f"{RECEIVE_TREE} takes no arguments")
    stream: object = stdin
    if isinstance(stream, io.TextIOBase):
        raise RemoteError("bad_request", "stdin cannot carry a tree stream")
    info = jobs.receive(stdin, home, key)
    return json.dumps({k: info[k] for k in ("job", "tree", "files", "bytes")})


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


def _check_layout(env: Mapping[str, str]) -> None:
    """Refuse to run on a host where a VM can need root."""
    if os.geteuid() == 0:
        raise RemoteError(
            "layout_unsupported",
            "serve does not run as root",
            hint="Run serve as the dedicated ltvm user.",
        )
    if env.get(TEST_LAYOUT_ENV) == "ready":
        return
    from ltvm_pkg import rootless

    ready = rootless.readiness()
    if not ready.ok:
        raise RemoteError(
            "layout_unsupported",
            "this host does not have the shared layout: "
            + "; ".join(ready.problems),
            hint="The host owner runs `ltvm install` for the shared layout.",
        )


def _check_scope(
    parse: ParseFn, argv: list[str], env: Mapping[str, str]
) -> list[str]:
    """Parse argv, and check its scope and the caps.  Return the argv to run."""
    sink = io.StringIO()
    try:
        # --help prints during the parse.  The verb prints it again
        # after the reply line.
        with redirect_stdout(sink), redirect_stderr(sink):
            args = parse(argv)
    except SystemExit:
        # The verb gets the same parse error and runs nothing.
        return argv
    to_run = scope.check(argv, args, home=Path(env["HOME"]))
    admission.check(argv, args, env)
    return to_run


def _run_verb(run: RunFn, argv: list[str]) -> tuple[int, str]:
    """Run the verb.  Return the exit status and the audit result.

    The suite verbs run in scope.suite_dir(), because `suite run --wait`
    collects the results under the working directory.
    """
    if argv[0] != "suite":
        return _run_in_cwd(run, argv)
    here = os.getcwd()
    work = scope.suite_dir()
    work.mkdir(parents=True, exist_ok=True)
    os.chdir(work)
    try:
        return _run_in_cwd(run, argv)
    finally:
        os.chdir(here)


def _run_in_cwd(run: RunFn, argv: list[str]) -> tuple[int, str]:
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


def _pull(
    argv: list[str],
    stdout: IO[str],
    env: Mapping[str, str],
    log: Path,
    key: str,
) -> int:
    """Send the results that a pull request names, as a tar stream."""
    from . import pull

    out = getattr(stdout, "buffer", None)
    if out is None:
        raise RemoteError("bad_request", "stdout cannot carry a pull stream")
    sources = pull.plan(argv, Path(env["HOME"]))
    stdout.write(protocol.reply_line(_ltvm_version(), None) + "\n")
    stdout.flush()
    try:
        pull.write(sources, out)
    except OSError:
        traceback.print_exc(file=sys.stderr)
        audit.append(log, key=key, argv=argv, result="exception")
        return EXIT_ERROR
    audit.append(log, key=key, argv=argv, result="ok")
    return EXIT_OK


def _guard_children(script: str | None) -> None:
    """Set the environment that the child ltvm processes of a verb get.

    `cluster create` and `cluster deploy` start child ltvm processes.
    The children read the environment, not the arguments of run():

    - no telemetry and no update check on the host
    - no LTVM_HOST, so a child never sends its command on to a host
    - the same ltvm script as serve, not the ltvm on the PATH of sshd
      (on macOS that PATH does not include /usr/local/bin)

    priv.forbid_sudo() adds LTVM_NO_SUDO after the checks.
    """
    from ltvm_pkg import paths
    from ltvm_pkg.update_check import NO_CHECK_ENV

    os.environ["LTVM_TELEMETRY"] = "0"
    os.environ[NO_CHECK_ENV] = "0"
    os.environ.pop("LTVM_HOST", None)
    if script:
        os.environ[paths.SELF_ENV] = script


def serve_stdio(
    run: RunFn,
    *,
    parse: ParseFn,
    stdin: IO[Any] | None = None,
    stdout: IO[str] | None = None,
    env: Mapping[str, str] | None = None,
    script: str | None = None,
) -> int:
    """Run one remote request.  Return its exit status.

    script is the absolute path of the ltvm script that runs serve.
    Child ltvm processes of the verb run that script.
    """
    _guard_children(script)
    # Binary: a receive-tree request has a tar stream after the line.
    stdin = stdin or sys.stdin.buffer
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

    hello = _read_hello(stdin)
    try:
        _check_hello(hello)
        if split_error:
            raise split_error
        if not argv:
            # A probe (`host list`): the handshake reply is the answer.
            # It runs no verb, so it does not need the shared layout.
            stdout.write(protocol.reply_line(_ltvm_version(), None) + "\n")
            stdout.flush()
            audit.append(log, key=key, argv=[], result="probe")
            return EXIT_OK
        _check_layout(env)
        allowlist.check(argv)
        if argv[0] == RECEIVE_TREE:
            out = _receive_tree(argv, stdin, Path(env["HOME"]), key)
            stdout.write(protocol.reply_line(_ltvm_version(), None) + "\n")
            stdout.write(out + "\n")
            stdout.flush()
            audit.append(log, key=key, argv=argv, result="ok")
            return EXIT_OK
        if argv[0] == "pull":
            return _pull(argv, stdout, env, log, key)
        to_run = _check_scope(parse, argv, env)
        podman.check(to_run)
        leases.on_request(to_run, parse, env, scope.owner_id(key))
    except RemoteError as e:
        stdout.write(protocol.reply_line(_ltvm_version(), e) + "\n")
        stdout.flush()
        audit.append(log, key=key, argv=argv or [command], result=e.code)
        return EXIT_ERROR

    priv.forbid_sudo()
    scope.apply(key)
    stdout.write(protocol.reply_line(_ltvm_version(), None) + "\n")
    stdout.flush()
    if power.makes_vm(to_run):
        power.sync(env, busy=True)
    rc, result = _run_verb(run, to_run)
    power.sync(env)
    audit.append(log, key=key, argv=argv, result=result)
    return rc
