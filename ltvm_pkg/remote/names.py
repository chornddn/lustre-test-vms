"""`<host>/<vm>` names on the controller.

On the controller a remote VM has the name `<host>/<vm>`.  A local VM
name cannot contain "/", so the two never collide.  On the host the VM
has its bare name.

- A VM name with the prefix of a registered host selects that host, as
  --host does.
- Before the request goes to the host, the prefix comes off.
- A remote `list` shows each VM with the prefix.
"""

from __future__ import annotations

import argparse
import json
import sys

from .protocol import RemoteError


def vm_names(args: argparse.Namespace) -> list[str]:
    """The VM names in a parsed command."""
    names: list[str] = []
    name = getattr(args, "name", None)
    if isinstance(name, str):
        names.append(name)
    more = getattr(args, "names", None)
    if isinstance(more, list):
        names.extend(n for n in more if isinstance(n, str))
    return names


def host_of(args: argparse.Namespace) -> str | None:
    """The registered host that the `<host>/` prefix of a VM name selects."""
    prefixes = {n.split("/", 1)[0] for n in vm_names(args) if "/" in n}
    if not prefixes:
        return None
    from . import hosts

    known = {h.name for h in hosts.list_hosts()}
    found = prefixes & known
    # Names on two hosts select none.  The verb then refuses the names
    # here, because a local VM name has no "/".
    return found.pop() if len(found) == 1 else None


def to_host(host: str, argv: list[str], args: argparse.Namespace) -> list[str]:
    """Remove the `<host>/` prefix from the VM names in argv.

    Raise RemoteError if a name has the prefix of a different host.
    """
    out = list(argv)
    for name in vm_names(args):
        if "/" not in name:
            continue
        prefix, bare = name.split("/", 1)
        if prefix != host:
            raise RemoteError(
                "bad_request",
                f"VM {name!r} is not on host {host!r}",
                hint=f"Use {host}/<vm> or the bare VM name.",
            )
        out[out.index(name)] = bare
    if out and out[0] == "list" and not getattr(args, "json", False):
        # The controller makes the text from the JSON, with the prefix.
        out.append("--json")
    return out


def show_list(host: str, stdout: str, use_json: bool) -> None:
    """Print the reply of a remote `list --json` with `<host>/` names."""
    from ltvm_pkg.vm_commands import print_list

    data = json.loads(stdout)
    for vm in data.get("vms", []):
        vm["name"] = f"{host}/{vm['name']}"
    if use_json:
        sys.stdout.write(json.dumps(data) + "\n")
    else:
        print_list(data)
