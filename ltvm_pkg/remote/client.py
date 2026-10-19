"""The `--host` dispatch on the controller."""

from __future__ import annotations

import argparse
import sys

from . import hosts, names, protocol, tree
from .protocol import RemoteError

# Verbs that ignore LTVM_HOST and run on this machine.  An explicit
# --host still sends them to the host.
LOCAL_ONLY_VERBS = frozenset({"serve", "host"})


def strip_host_option(argv: list[str]) -> list[str]:
    """Remove the top-level host option from argv.

    The host option is the only top-level option that does not exit the
    parser (--version and --help exit).  So every option token before
    the verb is the host option, also an abbreviation such as `--ho`.
    """
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        i += 1 if "=" in argv[i] else 2
    return argv[i:]


def _hint(name: str, err: RemoteError) -> str | None:
    if err.code != "version_mismatch":
        return err.hint
    theirs = err.details.get("host_protocol")
    if isinstance(theirs, int) and theirs > protocol.PROTOCOL_VERSION:
        return "Update ltvm on this controller: ltvm update"
    return (
        f"Ask the owner of host {name!r} to update ltvm on the host: "
        "ltvm update"
    )


def run_remote(
    name: str,
    argv: list[str],
    *,
    use_json: bool,
    args: argparse.Namespace | None = None,
) -> int:
    """Send argv to host `name`.  Return the exit status of the verb.

    args is the parsed argv.  With it, `<host>/<vm>` names become bare
    names, and the JSON views (names.show) show the `<host>/` prefix.
    """
    from ltvm_pkg.cli.util import EXIT_ERROR, _emit_error

    try:
        if args is not None:
            argv = names.to_host(name, argv, args)
            argv = tree.attach(name, argv, args)
        response = hosts.transport_for(name).request(argv)
    except RemoteError as e:
        return _emit_error(
            e.message, use_json, hint=_hint(name, e), error_code=e.code
        )
    if response.error:
        sys.stderr.write(response.stderr)
        err = response.error
        _emit_error(
            f"host {name}: {err.message}",
            use_json,
            hint=_hint(name, err),
            error_code=err.code,
            details=err.details,
        )
        return EXIT_ERROR
    if not names.show(name, argv, response.status, response.stdout, use_json):
        sys.stdout.write(response.stdout)
    sys.stderr.write(response.stderr)
    return response.status
