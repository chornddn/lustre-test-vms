"""Tests for ltvm_pkg/lnet_net.py: which net a cluster runs.

The invariant under test is that one resolution answers all three
questions -- net name, interfaces, NID address -- so cfg/local.sh and
/etc/modprobe.d/lnet.conf cannot disagree.
"""

from __future__ import annotations

import pytest

from ltvm_pkg.lnet_net import (
    NET_TYPES,
    NetUnavailable,
    has_passthrough,
    resolve_net,
)
from ltvm_pkg.vm_state import ClusterInfo, VMNotFound


class _FakeVM:
    def __init__(self, name: str, nics=None, nic_ips=None) -> None:
        self.name = name
        self.nics = list(nics or [])
        self.nic_ips = list(nic_ips or [])


def _cluster(*nodes) -> ClusterInfo:
    """Build a ClusterInfo from (name, roles, ip) tuples."""
    return ClusterInfo(
        name="co1",
        nodes=[
            {
                "name": n[0],
                "roles": list(n[1]),
                "mdt_disks": 0,
                "ost_disks": 0,
                "ip": n[2],
            }
            for n in nodes
        ],
    )


def _softroce_cluster() -> ClusterInfo:
    return _cluster(
        ("co1-mds", ["mgs", "mds"], "192.168.105.10"),
        ("co1-oss", ["oss"], "192.168.105.11"),
    )


def _softroce_vms(rails: int = 1):
    ips = {"co1-mds": "172.16.100.203", "co1-oss": "172.16.100.204"}

    def load(name: str) -> _FakeVM:
        # Every rail of one node shares the extra-NIC subnet; index 0 is
        # eth1, index 1 is eth2.
        base = ips[name]
        addrs = [base] + [
            base.rsplit(".", 1)[0] + f".{200 + i}" for i in range(1, rails)
        ]
        return _FakeVM(name, ["softroce"] * rails, addrs)

    return load


class TestResolveTcp:
    """tcp runs on the extra NICs, and on mgmt only without them."""

    def test_uses_the_extra_nic(self) -> None:
        net = resolve_net(
            _softroce_cluster(), "tcp", load_vm=_softroce_vms()
        )
        assert net.net_type == "tcp"
        assert net.net_name == "tcp0"
        assert net.nid("co1-mds") == "172.16.100.203@tcp"
        assert net.lnet_conf("co1-mds") == (
            'options lnet networks="tcp0(eth1)"\n'
        )

    def test_a_plain_tcp_nic_carries_it_too(self) -> None:
        net = resolve_net(
            _softroce_cluster(),
            "tcp",
            load_vm=lambda n: _FakeVM(n, ["tcp"], ["172.16.100.7"]),
        )
        assert net.nid("co1-oss") == "172.16.100.7@tcp"
        assert net.lnet_conf("co1-oss") == (
            'options lnet networks="tcp0(eth1)"\n'
        )

    def test_no_extra_nics_falls_back_to_mgmt(self) -> None:
        net = resolve_net(
            _softroce_cluster(), "tcp", load_vm=lambda n: _FakeVM(n)
        )
        assert net.nid("co1-oss") == "192.168.105.11@tcp"
        assert net.lnet_conf("co1-oss") == (
            'options lnet networks="tcp0(eth0)"\n'
        )

    def test_unreadable_node_falls_back_to_mgmt(self) -> None:
        def load(name: str) -> _FakeVM:
            raise VMNotFound(name)

        net = resolve_net(_softroce_cluster(), "tcp", load_vm=load)
        assert net.nid("co1-mds") == "192.168.105.10@tcp"

    def test_passthrough_node_falls_back_to_mgmt(self) -> None:
        """tcp over a real HCA's netdev is not deploy's to compose."""
        net = resolve_net(
            _softroce_cluster(),
            "tcp",
            load_vm=lambda n: _FakeVM(
                n, ["passthrough:0000:85:00.1"], ["172.16.100.9"]
            ),
        )
        assert net.nid("co1-mds") == "192.168.105.10@tcp"


class TestResolveO2ib:
    """o2ib runs on the extra NICs, addressed from each node's .info."""

    def test_softroce_node_uses_the_extra_nic(self) -> None:
        net = resolve_net(
            _softroce_cluster(), "o2ib", load_vm=_softroce_vms()
        )
        assert net.net_name == "o2ib0"
        assert net.nid("co1-mds") == "172.16.100.203@o2ib"
        assert net.nid("co1-oss") == "172.16.100.204@o2ib"
        assert net.lnet_conf("co1-oss") == (
            'options lnet networks="o2ib0(eth1)"\n'
        )

    def test_multi_rail_is_one_net_with_two_interfaces(self) -> None:
        """Two softroce NICs are rails of one net, as the boot emitter
        composes them -- not two one-rail nets."""
        net = resolve_net(
            _softroce_cluster(), "o2ib", load_vm=_softroce_vms(rails=2)
        )
        assert net.lnet_conf("co1-mds") == (
            'options lnet networks="o2ib0(eth1,eth2)"\n'
        )
        # The NID is the first rail's address, not the second's.
        assert net.nid("co1-mds") == "172.16.100.203@o2ib"

    def test_tcp_only_cluster_is_refused_by_node_name(self) -> None:
        with pytest.raises(NetUnavailable) as e:
            resolve_net(
                _softroce_cluster(),
                "o2ib",
                load_vm=lambda n: _FakeVM(n, ["tcp"], ["172.16.100.5"]),
            )
        assert "co1-mds" in str(e.value)
        assert "softroce" in str(e.value)

    def test_no_extra_nics_at_all_is_refused(self) -> None:
        with pytest.raises(NetUnavailable) as e:
            resolve_net(
                _softroce_cluster(), "o2ib", load_vm=lambda n: _FakeVM(n)
            )
        assert "none" in str(e.value)

    def test_missing_nic_ip_is_refused_not_guessed(self) -> None:
        with pytest.raises(NetUnavailable) as e:
            resolve_net(
                _softroce_cluster(),
                "o2ib",
                load_vm=lambda n: _FakeVM(n, ["softroce"], []),
            )
        assert "NIC_IPS" in str(e.value)

    def test_passthrough_is_refused_with_a_reason(self) -> None:
        """Deploy cannot compose an HCA's lnet.conf: the ib device is
        resolved at boot."""
        with pytest.raises(NetUnavailable) as e:
            resolve_net(
                _softroce_cluster(),
                "o2ib",
                load_vm=lambda n: _FakeVM(
                    n, ["passthrough:0000:85:00.1"], ["172.16.100.9"]
                ),
            )
        assert "passthrough" in str(e.value)


class TestResolveArguments:
    def test_unknown_net_names_the_valid_ones(self) -> None:
        with pytest.raises(NetUnavailable) as e:
            resolve_net(_softroce_cluster(), "ib")
        for name in NET_TYPES:
            assert name in str(e.value)

    def test_empty_cluster_is_refused(self) -> None:
        with pytest.raises(NetUnavailable):
            resolve_net(_cluster(), "tcp")

    def test_unknown_node_is_not_silently_answered(self) -> None:
        net = resolve_net(
            _softroce_cluster(), "tcp", load_vm=_softroce_vms()
        )
        with pytest.raises(KeyError):
            net.nid("co1-cli")


class TestHasPassthrough:
    def test_detects_one_node(self) -> None:
        def load(name: str) -> _FakeVM:
            if name == "co1-oss":
                return _FakeVM(name, ["passthrough:0000:85:00.1"], ["1.2.3.4"])
            return _FakeVM(name, ["softroce"], ["172.16.100.203"])

        assert has_passthrough(_softroce_cluster(), load_vm=load)

    def test_softroce_cluster_has_none(self) -> None:
        assert not has_passthrough(
            _softroce_cluster(), load_vm=_softroce_vms()
        )

    def test_unloadable_node_reports_none(self) -> None:
        def load(name: str) -> _FakeVM:
            raise VMNotFound(name)

        assert not has_passthrough(_softroce_cluster(), load_vm=load)
