"""The one table of verbs that `serve` runs for a remote request.

A verb that is not in ALLOW_LIST is refused.  So a new ltvm verb stays
refused on remote hosts until a person adds it here.

An entry is a tuple of argv tokens:

- ``("list",)`` allows the verb and all of its arguments.
- ``("vm", "console-log")`` allows that one subcommand of `vm`.  The
  subcommand must be the token right after the verb.

The check reads the argv tokens and not the parsed namespace.  Some
subcommands (`vm exec`, `cluster exec`) write their own positionals to
args.command, so args.command is not always the verb.
"""

from __future__ import annotations

from .protocol import RemoteError

ALLOW_LIST: frozenset[tuple[str, ...]] = frozenset(
    {
        ("list",),
        ("create",),
        ("destroy",),
        ("start",),
        ("stop",),
        ("vm", "console-log"),
        ("vm", "crash-collect"),
        ("vm", "nmi"),
        ("vm", "snapshot"),
        ("vm", "restore"),
        ("cluster", "create"),
        ("cluster", "destroy"),
        ("cluster", "start"),
        ("cluster", "stop"),
        ("cluster", "status"),
        ("cluster", "list"),
        ("cluster", "exec"),
    }
)


def check(argv: list[str]) -> None:
    """Raise RemoteError(verb_refused) unless argv is on the allow-list."""
    if not argv:
        raise RemoteError("bad_request", "the request has no verb")
    if (argv[0],) in ALLOW_LIST:
        return
    if tuple(argv[:2]) in ALLOW_LIST:
        return
    shown = " ".join(argv[:2])
    raise RemoteError(
        "verb_refused",
        f"`{shown}` is not allowed on a remote host",
        hint="Run it on this machine without --host.",
    )
