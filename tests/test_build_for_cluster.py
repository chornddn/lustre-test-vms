"""`build lustre --for-cluster` takes target/kernel/arch from the cluster.

A target's default kernel is often not the kernel a given cluster was
created with, so `ltvm build lustre <target>` silently produces modules
that cluster cannot load.  The mismatch only surfaces at deploy or
insmod time, after a full build has been paid for.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest

from ltvm_pkg.cli.build import _apply_for_cluster
from ltvm_pkg.vm_cluster import ClusterBuildParams

PARAMS = ClusterBuildParams(
    target="rocky9",
    os_family="rhel",
    kernel="5.14-rhel9.5",
    arch="aarch64",
)


def _args(**kw) -> argparse.Namespace:  # type: ignore[no-untyped-def]
    base = {
        "for_cluster": "co2",
        "target": None,
        "kernel": None,
        "arch": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _patched(params: ClusterBuildParams = PARAMS):  # type: ignore[no-untyped-def]
    return (
        patch("ltvm_pkg.vm_cluster.ClusterInfo.load", return_value=object()),
        patch(
            "ltvm_pkg.vm_cluster.cluster_build_params",
            return_value=params,
        ),
    )


class TestForClusterResolution:
    def test_noop_without_the_flag(self) -> None:
        args = _args(for_cluster=None, target="rocky9")
        assert _apply_for_cluster(args) is None
        assert args.kernel is None and args.arch is None

    def test_fills_target_kernel_and_arch(self) -> None:
        args = _args()
        load, params = _patched()
        with load, params:
            assert _apply_for_cluster(args) is None
        assert args.target == "rocky9"
        assert args.kernel == "5.14-rhel9.5"
        assert args.arch == "aarch64"

    def test_explicit_kernel_wins(self) -> None:
        """An explicit override stays possible."""
        args = _args(kernel="5.14-rhel9.7")
        load, params = _patched()
        with load, params:
            assert _apply_for_cluster(args) is None
        assert args.kernel == "5.14-rhel9.7"
        assert args.arch == "aarch64"

    def test_explicit_arch_wins(self) -> None:
        args = _args(arch="x86_64")
        load, params = _patched()
        with load, params:
            assert _apply_for_cluster(args) is None
        assert args.arch == "x86_64"

    def test_matching_target_is_accepted(self) -> None:
        args = _args(target="rocky9")
        load, params = _patched()
        with load, params:
            assert _apply_for_cluster(args) is None
        assert args.kernel == "5.14-rhel9.5"

    def test_conflicting_target_is_an_error(self) -> None:
        """Silently preferring one of two disagreeing values is how the
        wrong-kernel build happened in the first place."""
        args = _args(target="ubuntu2004")
        load, params = _patched()
        with load, params:
            err = _apply_for_cluster(args)
        assert err is not None
        assert "conflicts with cluster" in err

    def test_unknown_cluster_is_an_error(self) -> None:
        args = _args(for_cluster="nope")
        with patch(
            "ltvm_pkg.vm_cluster.ClusterInfo.load",
            side_effect=RuntimeError("no such cluster"),
        ):
            err = _apply_for_cluster(args)
        assert err is not None
        assert "cannot load cluster" in err

    def test_cluster_without_recorded_kernel_leaves_kernel_unset(
        self,
    ) -> None:
        """kernel=None means 'the target default', which is the
        pre-existing behaviour -- not an error."""
        args = _args()
        load, params = _patched(
            ClusterBuildParams(
                target="rocky9", os_family="rhel", kernel=None, arch="x86_64"
            )
        )
        with load, params:
            assert _apply_for_cluster(args) is None
        assert args.kernel is None
        assert args.arch == "x86_64"


class TestClusterBuildParamsShape:
    def test_is_frozen(self) -> None:
        """Deploy and build must see the same values; a mutable result
        invites one caller to adjust it."""
        with pytest.raises(Exception):
            PARAMS.kernel = "other"  # type: ignore[misc]
