"""Which LNet network a cluster runs, resolved in one place.

A cluster runs exactly one LNet net, chosen by ``ltvm deploy --net``.
Two files have to agree about it: ``cfg/local.sh`` (``NETTYPE`` /
``MGSNID``, read by the test framework) and ``/etc/modprobe.d/lnet.conf``
(read by ``modprobe lnet``).  They are answered here, together, from one
resolution -- answering them separately is how they drift, and the drift
shows up as ``no connections available: rc = -22`` at mount, which reads
as a Lustre fault rather than a config fault.

The net name to interface mapping matches
``targets/common/setup-lnet-config.sh``, the boot-time emitter that
configures a node nobody has deployed to yet.

Addresses are read from each node's ``VMInfo`` on demand.  They are
deliberately not cached in the cluster JSON: two copies of an address
is the same class of bug as two copies of the net.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .vm_state import ClusterInfo, VMInfo, VMNotFound

# The nets a cluster can be asked to run.
NET_TYPES = ("tcp", "o2ib")

# NIC types that can carry o2ib.  `passthrough` is a real HCA and is
# configured by the boot-time emitter, which resolves its ib device at
# runtime; deploy cannot compose that mapping, so it is refused here
# rather than emitted wrong.
O2IB_NIC_TYPES = ("softroce",)


class NetUnavailable(Exception):
    """The cluster's NICs cannot carry the requested net."""


@dataclass(frozen=True)
class NodeNet:
    """One node's share of the cluster net."""

    name: str
    interfaces: tuple[str, ...]
    ip: str


@dataclass(frozen=True)
class ClusterNet:
    """A cluster's LNet net: its name, its interfaces, its addresses."""

    net_type: str
    net_name: str
    nodes: tuple[NodeNet, ...]

    def node(self, name: str) -> NodeNet:
        for n in self.nodes:
            if n.name == name:
                return n
        raise KeyError(f"node {name!r} is not in this cluster net")

    def nid(self, name: str) -> str:
        """The node's LNet NID on this net.

        Emitted without the net index: ``o2ib`` and ``o2ib0`` name the
        same net to Lustre, and the unindexed form is what every
        existing generated config uses.
        """
        return f"{self.node(name).ip}@{self.net_type}"

    def lnet_conf(self, name: str) -> str:
        """The node's ``/etc/modprobe.d/lnet.conf`` body."""
        ifaces = ",".join(self.node(name).interfaces)
        return f'options lnet networks="{self.net_name}({ifaces})"\n'


def _nic_type(spec: str) -> str:
    """The type part of a ``VMInfo.nics`` entry (``passthrough:BDF``)."""
    return spec.split(":", 1)[0].strip().lower()


def has_passthrough(
    cluster: ClusterInfo,
    load_vm: Callable[[str], VMInfo] | None = None,
) -> bool:
    """True when any node owns a passthrough (real HCA) NIC.

    A node whose VM state cannot be read reports False: this guard
    protects an HCA config it can see, and the deploy that follows
    fails on that node anyway.
    """
    load = load_vm or VMInfo.load
    for node in cluster.get_nodes():
        try:
            vm = load(node.name)
        except VMNotFound:
            continue
        if any(_nic_type(s) == "passthrough" for s in vm.nics):
            return True
    return False


def _resolve_o2ib_node(node_name: str, vm: VMInfo) -> NodeNet:
    """Pick the o2ib-capable NICs of one node.

    NICs of the same type are rails of one net, matching the boot
    emitter; a node with several softroce NICs yields one net with
    several interfaces.
    """
    picked = [
        i for i, spec in enumerate(vm.nics)
        if _nic_type(spec) in O2IB_NIC_TYPES
    ]
    if not picked:
        have = ", ".join(_nic_type(s) for s in vm.nics) or "none"
        raise NetUnavailable(
            f"{node_name} has no o2ib-capable NIC (has: {have}); "
            f"o2ib needs a node created with --nic softroce"
        )
    # nics[i] is eth{i+1}: eth0 is the mgmt NIC and is never an extra.
    interfaces = tuple(f"eth{i + 1}" for i in picked)
    try:
        ip = vm.nic_ips[picked[0]]
    except IndexError:
        raise NetUnavailable(
            f"{node_name} records no address for {interfaces[0]}; "
            f"recreate the VM so its .info carries NIC_IPS"
        )
    return NodeNet(name=node_name, interfaces=interfaces, ip=ip)


def resolve_net(
    cluster: ClusterInfo,
    net_type: str,
    load_vm: Callable[[str], VMInfo] | None = None,
) -> ClusterNet:
    """Resolve *net_type* against what *cluster*'s nodes actually have.

    Raises ``NetUnavailable`` -- naming what the cluster has and what
    the net needs -- before any node is touched.

    ``tcp`` runs on the mgmt NIC (eth0), which every node has; that is
    the address every generated config has always used.  ``o2ib`` runs
    on the extra NICs, whose addresses live only in each node's
    ``VMInfo``.
    """
    if net_type not in NET_TYPES:
        raise NetUnavailable(
            f"unknown net {net_type!r}: valid nets are "
            f"{', '.join(NET_TYPES)}"
        )

    nodes = cluster.get_nodes()
    if not nodes:
        raise NetUnavailable(f"cluster {cluster.name!r} has no nodes")

    load = load_vm or VMInfo.load
    resolved: list[NodeNet] = []
    for node in nodes:
        if net_type == "tcp":
            resolved.append(
                NodeNet(name=node.name, interfaces=("eth0",), ip=node.ip)
            )
            continue
        vm = load(node.name)
        if any(_nic_type(s) == "passthrough" for s in vm.nics):
            raise NetUnavailable(
                f"{node.name} has a passthrough NIC; deploy does not "
                f"configure o2ib over a real HCA -- its ib device is "
                f"resolved at boot by setup-lnet-config.sh. Configure "
                f"that cluster by hand."
            )
        resolved.append(_resolve_o2ib_node(node.name, vm))

    # One net index per cluster: the resolver picks the first
    # o2ib-capable NIC type it finds, so the net is always index 0 --
    # the same index the boot emitter assigns it.
    return ClusterNet(
        net_type=net_type,
        net_name=f"{net_type}0",
        nodes=tuple(resolved),
    )
