"""`<host>/<vm>` names on the controller.

On the controller a remote VM has the name `<host>/<vm>`.  A local VM
name cannot contain "/", so the two never collide.  On the host the VM
has its bare name.

- A VM name with the prefix of a registered host selects that host, as
  --host does.
- Before the request goes to the host, the prefix comes off.
- A remote `list`, `cluster list` and `cluster status` show each VM
  and cluster with the prefix.
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


def _strip(host: str, name: str) -> str:
    """The bare name of `name` on `host`.  Raise for another host."""
    if "/" not in name:
        return name
    prefix, bare = name.split("/", 1)
    if prefix != host:
        raise RemoteError(
            "bad_request",
            f"{name!r} is not on host {host!r}",
            hint=f"Use {host}/<name> or the bare name.",
        )
    return bare


def _replace(out: list[str], old: str, new: str) -> None:
    if old != new and old in out:
        out[out.index(old)] = new


# The commands whose output the controller makes from the --json reply,
# so that it can add the `<host>/` prefix to the names.
_JSON_VIEWS = (["list"], ["cluster", "list"], ["cluster", "status"])


def _json_view(argv: list[str]) -> bool:
    return any(argv[: len(v)] == v for v in _JSON_VIEWS)


def to_host(host: str, argv: list[str], args: argparse.Namespace) -> list[str]:
    """Remove the `<host>/` prefix from the VM and cluster names in argv.

    The names are the VM or cluster names, the node names in the node
    specs of `cluster create`, and the node of `cluster exec`.  Raise
    RemoteError if a name has the prefix of a different host.
    """
    out = list(argv)
    for name in vm_names(args):
        _replace(out, name, _strip(host, name))
    if argv[:2] == ["cluster", "create"]:
        from .scope import spec_node_name

        for spec in getattr(args, "specs", None) or []:
            node = spec_node_name(spec)
            if node:
                parts = spec.split(":")
                parts[1] = _strip(host, node)
                _replace(out, spec, ":".join(parts))
    if argv[:2] == ["cluster", "exec"]:
        target = getattr(args, "target", None)
        if isinstance(target, str):
            _replace(out, target, _strip(host, target))
    if _json_view(out) and not getattr(args, "json", False):
        # The controller makes the text from the JSON, with the prefix.
        out.append("--json")
    return out


def show(host: str, argv: list[str], stdout: str, use_json: bool) -> bool:
    """Print the reply of a JSON view with `<host>/` names.

    Return False if argv is not a JSON view.  Then the caller prints
    the reply as it is.
    """
    if not _json_view(argv):
        return False
    from ltvm_pkg import vm_cluster
    from ltvm_pkg.vm_commands import print_list

    def p(name: str | None) -> str | None:
        return f"{host}/{name}" if name else name

    data = json.loads(stdout)
    if argv[0] == "list":
        for vm in data.get("vms", []):
            vm["name"] = p(vm["name"])
            vm["cluster"] = p(vm.get("cluster"))
        for c in data.get("clusters", []):
            c["name"] = p(c["name"])
            c["members"] = [p(m) for m in c.get("members", [])]
    elif argv[1] == "list":
        for c in data.get("clusters", []):
            c["cluster"] = p(c["cluster"])
            for n in c.get("nodes", []):
                n["name"] = p(n["name"])
    else:
        data["cluster"] = p(data["cluster"])
        for n in data.get("nodes", []):
            n["name"] = p(n["name"])
            n["failover_for"] = p(n.get("failover_for"))
        for row in data.get("lustre", []):
            row["node"] = p(row["node"])
    if use_json:
        sys.stdout.write(json.dumps(data) + "\n")
    elif argv[0] == "list":
        print_list(data)
    elif argv[1] == "list":
        vm_cluster.print_cluster_list(data["clusters"])
    else:
        vm_cluster.print_cluster_status(data)
    return True
