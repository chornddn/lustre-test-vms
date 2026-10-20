"""The two guest networks: one for the VMs of the owner, one for remote VMs.

A remote VM (owner ID ``remote:...``, which serve sets) is on its own
network.  It has its own subnets, its own bridge on Linux and its own
socket_vmnet on macOS.  The host does not forward packets between the
two networks (see guest_net_install).  So a remote guest cannot reach a
guest of the owner, and the root password of the image does not help it.

Every other VM is on the owner network, with the values of vm_state, as
before this module.  The owner ID of a VM does not change after create,
so the owner ID alone selects the network.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REMOTE_BRIDGE = "fcbr1"
# Linux default.  macOS uses 192.168.106, the network that Lima names
# "host" next to its "shared" 192.168.105.
REMOTE_SUBNET_LINUX = "192.168.101"
REMOTE_SUBNET_MACOS = "192.168.106"
REMOTE_EXTRA_SUBNET = "172.16.101"
REMOTE_EXTRA_SUBNET6 = "fd17:2016:1000:f101"

# Linux units, and the files that they read.
REMOTE_NET_UNIT = "ltvm-remote-net.service"
REMOTE_DNSMASQ_UNIT = "ltvm-dnsmasq-remote.service"
# macOS LaunchDaemons.
SOCKET_VMNET_REMOTE_LABEL = "io.github.ltvm.socket_vmnet-remote"
DNSMASQ_REMOTE_LABEL = "io.github.ltvm.dnsmasq-remote"
DNSMASQ_REMOTE_RELOAD_LABEL = "io.github.ltvm.dnsmasq-remote-reload"
PF_REMOTE_LABEL = "io.github.ltvm.pf-remote"
LAUNCH_DAEMONS = Path("/Library/LaunchDaemons")
SYS_CLASS_NET = Path("/sys/class/net")


def _darwin() -> bool:
    return sys.platform == "darwin"


@dataclass(frozen=True)
class GuestNet:
    """The addresses and the host attachment of one guest network."""

    kind: str  # "owner" or "remote"
    subnet: str  # mgmt /24 prefix, "a.b.c"
    extra_subnet: str  # /24 prefix of every extra NIC
    extra_subnet6: str  # /64 prefix of every extra NIC
    bridge: str  # Linux bridge of every NIC

    @property
    def remote(self) -> bool:
        return self.kind == "remote"

    @property
    def gateway(self) -> str:
        return f"{self.subnet}.1"

    def subnet_for_nic(self, idx: int) -> str:
        """The /24 prefix of NIC idx (0 is mgmt, 1..N are the extras)."""
        if idx == 0:
            return self.subnet
        if self.extra_subnet == self.subnet:
            raise ValueError(
                f"extra-NIC subnet {self.extra_subnet}.0/24 is the mgmt "
                f"subnet of the {self.kind} network"
            )
        return self.extra_subnet

    def nic_ip6(self, ipv4: str) -> str:
        """The IPv6 address of an extra NIC (see vm_state.nic_ip6)."""
        octets = ipv4.split(".")
        if len(octets) != 4:
            raise ValueError(f"not an IPv4 address: {ipv4!r}")
        return ":".join(
            [self.extra_subnet6] + [f"f{int(o):03d}" for o in octets]
        )

    def contains(self, ip: str) -> bool:
        """True when ip is in the mgmt or the extra /24 of this network."""
        prefix = ip.rsplit(".", 1)[0]
        return prefix in (self.subnet, self.extra_subnet)


def owner() -> GuestNet:
    """The owner network.  The values of vm_state, read at call time."""
    from . import vm_state

    return GuestNet(
        kind="owner",
        subnet=vm_state.SUBNET,
        extra_subnet=vm_state.EXTRA_SUBNET,
        extra_subnet6=vm_state.EXTRA_SUBNET6,
        bridge=vm_state.BRIDGE,
    )


def _setting(env: str, file: str, default: str) -> str:
    from . import vm_state

    value = os.environ.get(env)
    if value:
        return value
    path = Path(vm_state.VM_DIR) / file
    try:
        value = path.read_text().strip()
    except OSError:
        value = ""
    return value or default


def remote() -> GuestNet:
    """The remote network.

    The env, then the file that `install --serve` writes in VM_DIR, then
    the default.
    """
    default = REMOTE_SUBNET_MACOS if _darwin() else REMOTE_SUBNET_LINUX
    return GuestNet(
        kind="remote",
        subnet=_setting("LTVM_REMOTE_SUBNET", "remote-subnet", default),
        extra_subnet=_setting(
            "LTVM_REMOTE_EXTRA_SUBNET",
            "remote-extra-subnet",
            REMOTE_EXTRA_SUBNET,
        ),
        extra_subnet6=os.environ.get("LTVM_REMOTE_EXTRA_SUBNET6")
        or REMOTE_EXTRA_SUBNET6,
        bridge=REMOTE_BRIDGE,
    )


def for_owner(owner_id: str | None) -> GuestNet:
    """The network of a VM with this owner ID."""
    from .remote import scope

    return remote() if scope.is_remote(owner_id) else owner()


def for_vm(vm: object) -> GuestNet:
    return for_owner(getattr(vm, "owner_id", None))


def remote_vmnet_socket() -> Path:
    """The socket of the remote socket_vmnet, next to the owner one."""
    env = os.environ.get("LTVM_REMOTE_VMNET_SOCKET")
    if env:
        return Path(env)
    from .host_setup import socket_vmnet_socket_path

    owner_sock = socket_vmnet_socket_path()
    return owner_sock.with_name(owner_sock.name + ".ltvm-remote")


def launchd_plist(label: str) -> Path:
    return LAUNCH_DAEMONS / f"{label}.plist"


def _active(unit: str) -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return r.returncode == 0


def remote_problems() -> list[str]:
    """Why the remote network is not usable, or [] when it is.

    No root is needed: serve runs this as the dedicated user.
    """
    from . import rootless

    problems: list[str] = []
    if _darwin():
        sock = remote_vmnet_socket()
        if not rootless._accessible(sock, os.R_OK | os.W_OK):
            problems.append(f"{sock} is not usable by this user")
        for label in (
            SOCKET_VMNET_REMOTE_LABEL,
            PF_REMOTE_LABEL,
            DNSMASQ_REMOTE_LABEL,
        ):
            if not launchd_plist(label).exists():
                problems.append(f"{launchd_plist(label)} is missing")
        return problems
    if not (SYS_CLASS_NET / REMOTE_BRIDGE).exists():
        problems.append(f"the remote bridge {REMOTE_BRIDGE} does not exist")
    if not rootless.bridge_allowed(REMOTE_BRIDGE, rootless.BRIDGE_ACL):
        problems.append(f"{rootless.BRIDGE_ACL} does not allow {REMOTE_BRIDGE}")
    for unit in (REMOTE_NET_UNIT, REMOTE_DNSMASQ_UNIT):
        if not _active(unit):
            problems.append(f"{unit} is not active")
    return problems
