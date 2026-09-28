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

from collections.abc import Callable
from dataclasses import dataclass

from .vm_state import ClusterInfo, VMInfo, VMNotFound

# The nets a cluster can be asked to run.
NET_TYPES = ("tcp", "o2ib")

# The address families a cluster can be asked to run its net on.  The
# nodes hold both, so this only picks which one the NIDs use.
IP_FAMILIES = ("ipv4", "ipv6")

# NIC types that can carry o2ib.  `passthrough` is a real HCA and is
# configured by the boot-time emitter, which resolves its ib device at
# runtime; deploy cannot compose that mapping, so it is refused here
# rather than emitted wrong.
O2IB_NIC_TYPES = ("softroce",)

# NIC types that can carry tcp.  socklnd binds an ordinary netdev, and
# a softroce NIC is an ordinary virtio-net device with an rxe link on
# top, so both extras qualify.
TCP_NIC_TYPES = ("tcp", "softroce")


class NetUnavailable(Exception):
    """The cluster's NICs cannot carry the requested net."""


@dataclass(frozen=True)
class NodeNet:
    """One node's share of the cluster net."""

    name: str
    interfaces: tuple[str, ...]
    ip: str
    # The same interface's IPv6 address; "" for a VM created before the
    # extras carried one.
    ip6: str = ""


@dataclass(frozen=True)
class ClusterNet:
    """A cluster's LNet net: its name, its interfaces, its addresses."""

    net_type: str
    net_name: str
    nodes: tuple[NodeNet, ...]
    ip_family: str = "ipv4"

    def node(self, name: str) -> NodeNet:
        for n in self.nodes:
            if n.name == name:
                return n
        raise KeyError(f"node {name!r} is not in this cluster net")

    @property
    def force_large_nid(self) -> bool:
        """Whether the test framework must configure large NIDs.

        LNet takes an interface's IPv6 address only when it is loaded
        with ``lnetctl lnet configure --large``, which is what
        ``FORCE_LARGE_NID=true`` makes the framework do.
        """
        return self.ip_family == "ipv6"

    def nid(self, name: str) -> str:
        """The node's LNet NID on this net.

        Emitted without the net index: ``o2ib`` and ``o2ib0`` name the
        same net to Lustre, and the unindexed form is what every
        existing generated config uses.  An IPv6 NID is unbracketed
        too, so the two families differ only in the address.
        """
        node = self.node(name)
        if self.ip_family != "ipv6":
            return f"{node.ip}@{self.net_type}"
        if not node.ip6:
            raise NetUnavailable(
                f"{name} records no IPv6 address on "
                f"{node.interfaces[0]}; recreate the VM so its .info "
                f"carries NIC_IP6S, or deploy --ip-family ipv4"
            )
        return f"{node.ip6}@{self.net_type}"

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


def _pick_extras(vm: VMInfo, nic_types: tuple[str, ...]) -> list[int]:
    """Indices of the extra NICs of one node that carry a given net.

    NICs of the same type are rails of one net, matching the boot
    emitter; a node with several softroce NICs yields one net with
    several interfaces.
    """
    return [i for i, spec in enumerate(vm.nics) if _nic_type(spec) in nic_types]


def _resolve_extra_node(
    node_name: str, vm: VMInfo, picked: list[int]
) -> NodeNet:
    """One node's share of a net that runs on its extra NICs."""
    # nics[i] is eth{i+1}: eth0 is the mgmt NIC and is never an extra.
    interfaces = tuple(f"eth{i + 1}" for i in picked)
    try:
        ip = vm.nic_ips[picked[0]]
    except IndexError:
        raise NetUnavailable(
            f"{node_name} records no address for {interfaces[0]}; "
            f"recreate the VM so its .info carries NIC_IPS"
        )
    # nic_ip6s is index-parallel to nic_ips, and empty on a VM created
    # before the extras carried IPv6.  An absent address is reported by
    # nid(), which is the only caller that needs one.
    ip6 = ""
    if picked[0] < len(vm.nic_ip6s):
        ip6 = vm.nic_ip6s[picked[0]]
    return NodeNet(name=node_name, interfaces=interfaces, ip=ip, ip6=ip6)


def resolve_net(
    cluster: ClusterInfo,
    net_type: str,
    load_vm: Callable[[str], VMInfo] | None = None,
    ip_family: str = "ipv4",
) -> ClusterNet:
    """Resolve *net_type* against what *cluster*'s nodes actually have.

    Raises ``NetUnavailable`` -- naming what the cluster has and what
    the net needs -- before any node is touched.

    Both nets run on the extra NICs, whose addresses live only in each
    node's ``VMInfo``.  Only a node with no extra NIC at all falls back
    to the mgmt NIC.

    *ip_family* picks which of each interface's two addresses the NIDs
    use.  Both are assigned either way, so it is a per-deploy choice
    rather than a property of the cluster's hardware.
    """
    if net_type not in NET_TYPES:
        raise NetUnavailable(
            f"unknown net {net_type!r}: valid nets are {', '.join(NET_TYPES)}"
        )
    if ip_family not in IP_FAMILIES:
        raise NetUnavailable(
            f"unknown address family {ip_family!r}: valid families "
            f"are {', '.join(IP_FAMILIES)}"
        )
    if ip_family == "ipv6" and net_type != "tcp":
        raise NetUnavailable(
            f"{net_type} cannot run --ip-family ipv6: test-framework.sh "
            f'errors with "FORCE_LARGE_NID only supported by tcp". The '
            f"nodes still hold IPv6 addresses for manual lnetctl work."
        )

    nodes = cluster.get_nodes()
    if not nodes:
        raise NetUnavailable(f"cluster {cluster.name!r} has no nodes")

    load = load_vm or VMInfo.load
    resolved: list[NodeNet] = []
    for node in nodes:
        if net_type == "tcp":
            # A node with no readable .info keeps the mgmt NIC: tcp has
            # always worked without per-VM state, and the deploy that
            # follows fails on that node anyway.
            maybe_vm: VMInfo | None
            try:
                maybe_vm = load(node.name)
            except VMNotFound:
                maybe_vm = None
            picked = _pick_extras(maybe_vm, TCP_NIC_TYPES) if maybe_vm else []
            if maybe_vm is not None and picked:
                resolved.append(
                    _resolve_extra_node(node.name, maybe_vm, picked)
                )
                continue
            # The extras are the Lustre network and mgmt is for SSH,
            # which is already what the boot emitter assumes:
            # setup-lnet-config.sh drops eth0 out of LNet as soon as the
            # node has an extra NIC.  eth0 is therefore only for a
            # cluster created with no --nic at all.
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
        picked = _pick_extras(vm, O2IB_NIC_TYPES)
        if not picked:
            have = ", ".join(_nic_type(s) for s in vm.nics) or "none"
            raise NetUnavailable(
                f"{node.name} has no o2ib-capable NIC (has: {have}); "
                f"o2ib needs a node created with --nic softroce"
            )
        resolved.append(_resolve_extra_node(node.name, vm, picked))

    # One net index per cluster: the resolver picks the first
    # o2ib-capable NIC type it finds, so the net is always index 0 --
    # the same index the boot emitter assigns it.
    net = ClusterNet(
        net_type=net_type,
        net_name=f"{net_type}0",
        nodes=tuple(resolved),
        ip_family=ip_family,
    )
    # Ask for every NID here, so a node that cannot supply one for this
    # family is reported before any node is touched rather than halfway
    # through writing the cluster's configs.
    for n in resolved:
        net.nid(n.name)
    return net
