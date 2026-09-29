"""`ltvm start|stop <cluster>` acts on every node of the cluster."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

from ltvm_pkg.cli import vm as cli_vm


def _node(name: str) -> MagicMock:
    n = MagicMock()
    n.name = name
    return n


def _expand(names: list[str], vms: list[str], clusters: dict[str, list[str]]):
    info = MagicMock()
    info.all_names.return_value = list(clusters)
    info.load.side_effect = lambda c: MagicMock(
        get_nodes=lambda: [_node(n) for n in clusters[c]]
    )
    vm = MagicMock()
    vm.all_names.return_value = vms
    with (
        patch("ltvm_pkg.vm_state.ClusterInfo", info),
        patch("ltvm_pkg.vm_state.VMInfo", vm),
    ):
        return cli_vm._expand_clusters(names)


class TestExpandClusters:
    def test_cluster_becomes_its_nodes(self) -> None:
        got = _expand(["co9"], [], {"co9": ["co9-mds", "co9-cli"]})
        assert got == ["co9-mds", "co9-cli"]

    def test_vm_of_the_same_name_wins(self) -> None:
        got = _expand(["co9"], ["co9"], {"co9": ["co9-mds"]})
        assert got == ["co9"]

    def test_unknown_name_passes_through(self) -> None:
        assert _expand(["nope"], [], {}) == ["nope"]

    def test_repeats_collapse(self) -> None:
        got = _expand(
            ["co9-cli", "co9"], [], {"co9": ["co9-mds", "co9-cli"]}
        )
        assert got == ["co9-cli", "co9-mds"]


class TestLifecycleUsesExpansion:
    def _run(self, fn, verb: str) -> list[str]:
        ns = argparse.Namespace(json=False, names=["co9"])
        seen: list[str] = []
        with (
            patch.object(
                cli_vm, "_expand_clusters", return_value=["co9-mds", "co9-cli"]
            ),
            patch.object(
                cli_vm,
                "_claim_error",
                side_effect=lambda n, v, j: seen.extend(n) or None,
            ),
            patch.object(cli_vm, "_vm_privileges", return_value=None),
            patch.object(cli_vm, "_vm_call", return_value=0),
        ):
            assert fn(ns) == 0
        assert ns.names == ["co9-mds", "co9-cli"]
        return seen

    def test_start_checks_claims_on_the_nodes(self) -> None:
        assert self._run(cli_vm.cmd_vm_start, "start") == [
            "co9-mds",
            "co9-cli",
        ]

    def test_stop_checks_claims_on_the_nodes(self) -> None:
        assert self._run(cli_vm.cmd_vm_stop, "stop") == ["co9-mds", "co9-cli"]
