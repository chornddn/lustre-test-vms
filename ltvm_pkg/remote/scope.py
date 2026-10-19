"""Keep each remote request to the VMs that remote requests made.

The VMs of the host owner and the remote VMs are in one VM directory.
`serve` sets the owner ID of each VM that a remote request makes to
``remote:<key fingerprint>``.  A remote request can see and change only
the VMs with that prefix.  To a remote request, a VM of the owner looks
the same as a VM that does not exist.  A cluster is in scope when its
record and each of its nodes have the remote owner ID.

`check` reads the parsed command, because argparse accepts option
abbreviations (`--fol` for `--follow`) that a token check can miss.
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

from . import jobs
from .protocol import RemoteError

REMOTE_OWNER_PREFIX = "remote:"

# The VM name rule of vm_commands.  serve reads no state file for a name
# that does not match it, so "../x" cannot reach a file outside the VM
# directory.
_VM_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")

# The verbs whose `names` are existing VMs.
_NAMES_VERBS = frozenset({"destroy", "start", "stop"})


def owner_id(key: str) -> str:
    """The owner ID that serve gives to a VM that a remote request makes."""
    return REMOTE_OWNER_PREFIX + key


def is_remote(owner: str | None) -> bool:
    return bool(owner) and str(owner).startswith(REMOTE_OWNER_PREFIX)


def apply(key: str) -> None:
    """Set the scope of remote requests for this process.

    New VMs get the remote owner ID through LTVM_OWNER_ID, which child
    ltvm processes also read.  `list` shows only remote VMs.
    """
    from ltvm_pkg import vm_commands

    os.environ["LTVM_OWNER_ID"] = owner_id(key)
    vm_commands.list_filter = is_remote


def crash_dir(name: str) -> Path:
    """Where `vm crash-collect` writes for a remote request."""
    from ltvm_pkg import vm_state

    return Path(vm_state.VM_DIR) / "crashes" / name


def _owner(name: str) -> tuple[bool, str | None]:
    """(the VM exists, its owner ID)."""
    from ltvm_pkg.vm_state import VMInfo, VMNotFound

    if not _VM_NAME_RE.fullmatch(name):
        return False, None
    try:
        return True, VMInfo.load(name).owner_id
    except VMNotFound:
        return False, None
    except (ValueError, OSError):
        # Unreadable state: it exists, but it is not a remote VM.
        return True, None


def _require_remote_vm(name: str) -> None:
    exists, owner = _owner(name)
    if not (exists and is_remote(owner)):
        raise RemoteError(
            "vm_not_found",
            f"this host has no remote VM named {name!r}",
            hint="`list` with --host shows the remote VMs.",
        )


def _has_passthrough(nics: list[str] | None) -> bool:
    return any(str(n).split(":", 1)[0] == "passthrough" for n in nics or [])


def _refuse_root(what: str) -> RemoteError:
    return RemoteError(
        "needs_root",
        f"{what} needs root on the host, and serve never uses root",
        hint="Use a VM with no passthrough NIC, or run it on the host.",
    )


def _refuse_path(option: str) -> RemoteError:
    return RemoteError(
        "path_refused",
        f"{option} is not allowed on a remote host",
        hint="serve chooses the paths on the host.",
    )


def _check_create(args: argparse.Namespace) -> None:
    if getattr(args, "owner_id", None) is not None:
        raise RemoteError(
            "bad_request",
            "a remote request cannot set the owner of a VM",
            hint="Remove --owner.  serve sets the owner of remote VMs.",
        )
    if _has_passthrough(getattr(args, "nic", None)):
        raise _refuse_root("a passthrough NIC")
    if getattr(args, "image", None):
        raise _refuse_path("--image")
    if getattr(args, "disks_from", None):
        # A failover partner attaches the data disks of that VM.
        _require_remote_vm(args.disks_from)
    exists, owner = _owner(args.name)
    if exists and not is_remote(owner):
        raise RemoteError(
            "name_in_use",
            f"the name {args.name!r} is in use on this host",
            hint="Choose a different VM name.",
        )


def _check_existing(names: list[str]) -> None:
    from ltvm_pkg.vm_state import VMInfo

    for name in names:
        _require_remote_vm(name)
    for name in names:
        if _has_passthrough(VMInfo.load(name).nics):
            raise _refuse_root(f"VM {name!r} has a passthrough NIC, so it")


def _cluster_owner(name: str) -> tuple[bool, str | None, list[str]]:
    """(the cluster exists, its owner ID, its node names)."""
    from ltvm_pkg.vm_state import ClusterInfo, ClusterNotFound

    if not _VM_NAME_RE.fullmatch(name):
        return False, None, []
    try:
        cluster = ClusterInfo.load(name)
        return True, cluster.owner_id, [n.name for n in cluster.get_nodes()]
    except ClusterNotFound:
        return False, None, []
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):
        # Unreadable state: it exists, but it is not a remote cluster.
        return True, None, []


def _check_clusters(names: list[str], *, lifecycle: bool) -> None:
    """Every cluster and each of its nodes must be remote.

    With lifecycle, a node with a passthrough NIC is refused too,
    because start, stop and destroy of that node need root.
    """
    from ltvm_pkg.vm_state import VMInfo

    for name in names:
        exists, owner, nodes = _cluster_owner(name)
        owners = [_owner(n) for n in nodes]
        remote_nodes = all(is_remote(o) for found, o in owners if found)
        if not (exists and is_remote(owner) and remote_nodes):
            raise RemoteError(
                "cluster_not_found",
                f"this host has no remote cluster named {name!r}",
                hint="`cluster list` with --host shows the remote clusters.",
            )
        if not lifecycle:
            continue
        for node in nodes:
            if _owner(node)[0] and _has_passthrough(VMInfo.load(node).nics):
                raise _refuse_root(
                    f"node {node!r} has a passthrough NIC, so it"
                )


def spec_node_name(spec: str) -> str | None:
    """The node name in a `cluster create` node spec (roles:name[:disks])."""
    parts = spec.split(":")
    return parts[1] if len(parts) >= 2 else None


def _check_cluster_create(args: argparse.Namespace) -> None:
    if getattr(args, "owner_id", None) is not None:
        raise RemoteError(
            "bad_request",
            "a remote request cannot set the owner of a cluster",
            hint="Remove --owner.  serve sets the owner of remote clusters.",
        )
    if _has_passthrough(getattr(args, "nic", None)):
        raise _refuse_root("a passthrough NIC")
    exists, owner, _ = _cluster_owner(args.name)
    taken = exists and not is_remote(owner)
    nodes = [spec_node_name(spec) for spec in args.specs]
    # --failover PARTNER=PRIMARY makes the node PARTNER too.
    nodes += [
        f.partition("=")[0] for f in getattr(args, "failover", None) or []
    ]
    for node in nodes:
        if not node:
            continue
        node_exists, node_owner = _owner(node)
        taken = taken or (node_exists and not is_remote(node_owner))
    if taken:
        raise RemoteError(
            "name_in_use",
            "the cluster name or a node name is in use on this host",
            hint="Choose different names.",
        )


def _check_cluster(args: argparse.Namespace, home: Path) -> None:
    action = getattr(args, "cluster_action", None)
    if action == "create":
        _check_cluster_create(args)
    elif action in ("destroy", "start", "stop"):
        _check_clusters(list(args.names), lifecycle=True)
    elif action in ("status", "exec", "llmount", "llumount"):
        _check_clusters([args.name], lifecycle=False)
    elif action == "deploy":
        _check_clusters([args.name], lifecycle=False)
        _check_build_paths(args, home, tree=args.lustre_source)


def _check_build_paths(
    args: argparse.Namespace, home: Path, *, tree: str | None
) -> None:
    """A Lustre tree must be a job tree.  Refuse other host paths."""
    for dest, option in (("zfs_source", "--zfs-source"), ("url", "--url")):
        if getattr(args, dest, None):
            raise _refuse_path(option)
    if tree is not None:
        jobs.job_tree(home, tree, "--lustre-tree")


def _check_artifacts(args: argparse.Namespace, home: Path) -> None:
    """Refuse host paths and free URLs in `build` and `target fetch`.

    --lustre-tree is optional for the build verbs, but when it is given
    it must be a job tree.  The verbs write only into the artifacts
    directory of serve.
    """
    tree = getattr(args, "lustre_tree", None)
    _check_build_paths(args, home, tree=tree if isinstance(tree, str) else None)


def suite_dir() -> Path:
    """The working directory of the suite verbs for a remote request.

    `suite run --wait` collects into ./ltvm-suite/<target>/<run-id>, so
    the results of remote runs are under SUITES_DIR in the VM directory.
    """
    from ltvm_pkg import vm_state

    return Path(vm_state.VM_DIR) / "suites"


def _check_suite(args: argparse.Namespace) -> None:
    from ltvm_pkg.vm_state import ClusterInfo, ClusterNotFound

    name = args.target
    is_cluster = False
    if _VM_NAME_RE.fullmatch(name):
        try:
            ClusterInfo.load(name)
            is_cluster = True
        except ClusterNotFound:
            pass
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
            is_cluster = True
    if is_cluster:
        _check_clusters([name], lifecycle=False)
    else:
        _require_remote_vm(name)
    if getattr(args, "results_dir", None):
        raise _refuse_path("--results-dir")


def _check_lease(args: argparse.Namespace) -> None:
    """`lease renew` names a remote cluster, or a remote VM."""
    from . import leases

    if _cluster_owner(args.name)[0]:
        _check_clusters([args.name], lifecycle=False)
    else:
        _require_remote_vm(args.name)
    if args.for_time:
        leases.parse_length(args.for_time)


def check(
    argv: list[str], args: argparse.Namespace, *, home: Path | None = None
) -> list[str]:
    """Refuse a request outside the remote VMs.  Return the argv to run.

    Call it after the allow-list check.  The argv that comes back can
    have options that serve adds, such as the crash directory.  home is
    the home of serve, which holds the job directories.
    """
    home = Path.home() if home is None else home
    verb = argv[0]
    if verb == "create":
        _check_create(args)
    elif verb in _NAMES_VERBS:
        _check_existing(list(args.names))
    elif verb == "cluster":
        _check_cluster(args, home)
    elif verb in ("build", "target"):
        _check_artifacts(args, home)
    elif verb == "deploy-lustre":
        _check_existing([args.vm])
        _check_build_paths(args, home, tree=args.lustre_tree or "")
    elif verb in ("llmount", "llumount"):
        _check_existing([args.vm])
    elif verb == "suite":
        _check_suite(args)
    elif verb == "lease":
        _check_lease(args)
    elif verb == "vm":
        action = getattr(args, "vm_action", None)
        _check_existing([args.name])
        if action == "console-log" and getattr(args, "follow", False):
            raise RemoteError(
                "verb_refused",
                "`vm console-log --follow` is not allowed on a remote host",
                hint="Run `vm console-log` again to see new lines.",
            )
        if action == "crash-collect":
            if args.outdir is not None:
                raise _refuse_path("--outdir")
            if args.mod_dir is not None:
                raise _refuse_path("--mod-dir")
            return [*argv[:2], "--outdir", str(crash_dir(args.name)), *argv[2:]]
    return argv
