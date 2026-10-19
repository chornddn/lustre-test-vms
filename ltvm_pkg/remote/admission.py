"""Admission control: the caps of serve.toml on remote VMs.

The host owner sets caps on vCPUs, RAM and disk in serve.toml
(serve_config.py).  The caps apply to all remote VMs together.  There
is no cap on the number of VMs.  A request that goes above a cap gets
over_cap before the verb runs, so serve makes nothing.  The error has
the requested, used and cap values of each resource, so the agent can
ask for less.

How serve counts each resource:

- vcpus and ram_gb: the sum over all remote VMs, running and stopped.
  A stopped VM can start at any time, so it keeps its share.  Thus
  `start` needs no check, and `create` of a remote VM that exists asks
  for nothing.  The VMs of the host owner do not count.
- disk_gb: the bytes on disk (allocated blocks, not the file size) of
  the remote VM disks, the job directories and the artifacts (images,
  kernels, containers) of the dedicated user.  A new VM asks for
  NEW_VM_DISK_GB, an estimate of what its overlay and scratch disks
  write first.  The disk files are sparse, so the full disk size would
  stop a host long before its disk is full.

The memory check at launch (qemu_run._memory_shortfall) still runs as
before.  The RAM cap is a second limit on top of it.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import scope, serve_config
from .protocol import RemoteError

if TYPE_CHECKING:
    from ltvm_pkg.vm_state import VMInfo

GIB = 1 << 30

# What a new VM asks for on disk.  A new overlay holds only the blocks
# that the guest writes, and the scratch disks are sparse.
NEW_VM_DISK_GB = 1.0

# The job directories of remote requests, under HOME of the dedicated
# user.  Ticket 06 makes them.
JOBS_DIR = "jobs"


@dataclass
class Resources:
    vcpus: int = 0
    ram_gb: float = 0.0
    disk_gb: float = 0.0

    def __add__(self, other: Resources) -> Resources:
        return Resources(
            self.vcpus + other.vcpus,
            self.ram_gb + other.ram_gb,
            self.disk_gb + other.disk_gb,
        )


def _show(value: float | None) -> float | int | None:
    if value is None or isinstance(value, int):
        return value
    return round(value, 2)


def _as_json(res: Resources | serve_config.ServeConfig) -> dict[str, object]:
    if isinstance(res, serve_config.ServeConfig):
        values = {
            "vcpus": res.max_vcpus,
            "ram_gb": res.max_ram_gb,
            "disk_gb": res.max_disk_gb,
        }
    else:
        values = asdict(res)
    return {k: _show(v) for k, v in values.items()}


# ── what a request asks for ──────────────────────────────


def _default_mem(target: str | None) -> int:
    """The memory in MiB of a VM with no --mem, as create resolves it."""
    from ltvm_pkg.target_config import TargetConfig
    from ltvm_pkg.vm_state import DEFAULT_TARGET

    try:
        return TargetConfig(target or DEFAULT_TARGET).default_mem
    except (ValueError, FileNotFoundError, KeyError):
        return 2048


def _new_vm(vcpus: int, mem: int | None, target: str | None) -> Resources:
    mem_mb = mem if mem is not None else _default_mem(target)
    return Resources(vcpus, mem_mb / 1024, NEW_VM_DISK_GB)


def _remote_exists(name: str) -> bool:
    exists, owner = scope._owner(name)
    return exists and scope.is_remote(owner)


def _create_request(args: argparse.Namespace) -> Resources:
    if _remote_exists(args.name):
        return Resources()
    target = getattr(args, "target", None) or getattr(
        args, "_target_flag", None
    )
    return _new_vm(args.vcpus, args.mem, target)


def _cluster_request(args: argparse.Namespace) -> Resources:
    specs = list(args.specs)
    target = getattr(args, "target", None)
    if specs and ":" not in specs[0]:
        target = specs.pop(0)
    node_targets = dict(
        item.partition("=")[::2] for item in args.node_target or []
    )
    # --failover PARTNER=PRIMARY: a new node with the target of PRIMARY.
    for item in getattr(args, "failover", None) or []:
        partner, _, primary = item.partition("=")
        if primary in node_targets:
            node_targets[partner] = node_targets[primary]
        specs.append(f"failover:{partner}")
    total = Resources()
    for spec in specs:
        node = scope.spec_node_name(spec)
        if node is None or _remote_exists(node):
            continue
        total += _new_vm(args.vcpus, args.mem, node_targets.get(node) or target)
    return total


def request(argv: list[str], args: argparse.Namespace) -> Resources | None:
    """What the request asks for, or None for a verb with no cost."""
    if argv[0] == "create":
        return _create_request(args)
    if argv[0] == "cluster" and getattr(args, "cluster_action", None) == (
        "create"
    ):
        return _cluster_request(args)
    return None


# ── what remote VMs use now ──────────────────────────────


def _allocated(paths: Iterable[Path]) -> int:
    """The bytes on disk of the files under paths.  Count a link once."""
    seen: set[tuple[int, int]] = set()
    total = 0

    def add(path: str) -> None:
        nonlocal total
        try:
            st = os.lstat(path)
        except OSError:
            return
        if (st.st_dev, st.st_ino) in seen:
            return
        seen.add((st.st_dev, st.st_ino))
        total += st.st_blocks * 512

    for top in paths:
        add(str(top))
        for root, dirs, files in os.walk(top):
            for name in dirs + files:
                add(os.path.join(root, name))
    return total


def _remote_vms() -> list[VMInfo]:
    from ltvm_pkg.vm_state import VMInfo

    vms: list[VMInfo] = []
    for name in VMInfo.all_names():
        try:
            vm = VMInfo.load(name)
        except (ValueError, OSError):
            continue
        if scope.is_remote(vm.owner_id):
            vms.append(vm)
    return vms


def _artifacts_dir() -> Path:
    from ltvm_pkg import target_config

    return Path(target_config.ARTIFACTS_DIR)


def usage(env: Mapping[str, str], *, disk: bool = True) -> Resources:
    """What the remote VMs, the jobs and the artifacts use now."""
    from ltvm_pkg.vm_state import OVERLAYS

    vms = _remote_vms()
    used = Resources(
        vcpus=sum(vm.vcpus for vm in vms),
        ram_gb=sum(vm.mem for vm in vms) / 1024,
    )
    if disk:
        files: list[Path] = []
        for vm in vms:
            files.append(Path(OVERLAYS) / f"{vm.name}.qcow2")
            files.extend(Path(OVERLAYS).glob(f"{vm.name}-disk*.img"))
        home = Path(env.get("HOME") or Path.home())
        files += [home / JOBS_DIR, _artifacts_dir()]
        used.disk_gb = _allocated(files) / GIB
    return used


# ── the check ────────────────────────────────────────────


def check(
    argv: list[str], args: argparse.Namespace, env: Mapping[str, str]
) -> None:
    """Raise over_cap if the request goes above a cap of serve.toml."""
    asked = request(argv, args)
    if asked is None:
        return
    try:
        cfg = serve_config.load(env)
    except (OSError, ValueError) as e:
        raise RemoteError(
            "bad_config",
            f"serve cannot read its caps: {e}",
            hint="The host owner corrects serve.toml.",
        ) from None
    limits = {
        "vcpus": cfg.max_vcpus,
        "ram_gb": cfg.max_ram_gb,
        "disk_gb": cfg.max_disk_gb,
    }
    if all(v is None for v in limits.values()):
        return
    used = usage(env, disk=cfg.max_disk_gb is not None)
    over = [
        key
        for key, cap in limits.items()
        if cap is not None
        and getattr(asked, key) > 0
        and getattr(used, key) + getattr(asked, key) > cap
    ]
    if not over:
        return
    raise RemoteError(
        "over_cap",
        "the request goes above the host cap for " + ", ".join(over),
        hint="Ask for less, or use local VMs.",
        details={
            "requested": _as_json(asked),
            # With no disk cap, serve does not count the disk.
            "used": {
                **_as_json(used),
                **({"disk_gb": None} if cfg.max_disk_gb is None else {}),
            },
            "cap": _as_json(cfg),
        },
    )
