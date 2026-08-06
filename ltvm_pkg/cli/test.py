"""cmd_test: run a Lustre test suite on a cluster and report parsed results.

The command surface lives here; the behaviour (argv building, preflight
evaluation, results parsing) lives in ``ltvm_pkg.test_runner`` so it can
be tested without a VM.  vm_cluster/vm_state imports are done inside the
function, matching ``ltvm_pkg/cli/cluster.py``, to keep the cli package
free of import cycles.
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from ltvm_pkg.cli.util import (
    EXIT_ERROR,
    EXIT_OK,
    _error,
    _output,
)


def _resolve_node(nodes: list[Any], wanted: str | None) -> Any | None:
    """Pick the node to drive auster from.

    Explicit ``--node`` wins and may name a VM or a role.  Otherwise the
    client runs the suite, as it would in a real cluster; a cluster with
    no client node falls back to the MGS.
    """
    if wanted:
        return next(
            (n for n in nodes if n.name == wanted or wanted in n.roles),
            None,
        )
    for role in ("client", "mgs", "mds"):
        match = next((n for n in nodes if role in n.roles), None)
        if match is not None:
            return match
    return nodes[0] if nodes else None


def _print_report(report: dict[str, Any]) -> None:
    counts = report["counts"]
    print(
        f"{report['suite']} on {report['cluster']} "
        f"(cfg={report['cfg']}, {report['duration']}s): "
        f"{counts['pass']} PASS, {counts['fail']} FAIL, "
        f"{counts['skip']} SKIP, {counts['benign']} BENIGN"
    )
    for item in report["fail"]:
        print(f"  FAIL   {item['test']}: {item['reason']}")
    for item in report["benign"]:
        print(f"  BENIGN {item['test']}: {item['why']}")
    for item in report["skip"]:
        print(f"  SKIP   {item['test']}: {item['reason']}")
    print(f"  logs: {report['log_dir']} on {report['node']}")


def cmd_test(args: argparse.Namespace) -> int:
    use_json = args.json

    from ltvm_pkg import test_runner as tr
    from ltvm_pkg.vm_cluster import cluster_build_params
    from ltvm_pkg.vm_net import run_ssh, sshpass_scp_argv
    from ltvm_pkg.vm_state import (
        ClusterInfo,
        ClusterNotFound,
        VMInfo,
        VMNotFound,
        lustre_libdir,
    )

    try:
        cluster = ClusterInfo.load(args.cluster)
    except ClusterNotFound:
        return _error(
            f"no such cluster: {args.cluster}",
            use_json,
            hint="ltvm cluster list",
        )
    except RuntimeError as e:
        return _error(str(e), use_json)

    nodes = cluster.get_nodes()
    node = _resolve_node(nodes, args.node)
    if node is None:
        return _error(
            f"no node matching {args.node!r} in cluster {args.cluster}",
            use_json,
        )
    try:
        vm = VMInfo.load(node.name)
    except VMNotFound as e:
        return _error(str(e), use_json)

    try:
        params = cluster_build_params(cluster)
    except SystemExit as e:
        return int(e.code) if e.code is not None else EXIT_ERROR

    tests_dir = f"{lustre_libdir(params.os_family)}/tests"
    cfg_path = f"{tests_dir}/cfg/{args.cfg}.sh"

    try:
        overrides = tr.parse_benign_overrides(args.benign or [])
    except tr.TestRunnerError as e:
        return _error(str(e), use_json)
    benign = tr.benign_for_suite(
        args.suite, overrides=overrides, enabled=not args.no_benign
    )

    if args.skip_preflight:
        if not use_json:
            print(
                "preflight skipped: cluster config, kernel/module match "
                "and lnet.conf are unchecked"
            )
    else:
        node_ips = []
        for n in nodes:
            try:
                node_ips.append((n.name, VMInfo.load(n.name).ip))
            except VMNotFound as e:
                return _error(str(e), use_json)
        probes = tr.gather_probes(node_ips, cfg_path, run_ssh)
        problems = tr.evaluate_preflight(
            probes, cluster=cluster.name, cfg=args.cfg
        )
        if problems:
            return _error(
                "preflight failed; auster was not started:\n  - "
                + "\n  - ".join(problems),
                use_json,
            )

    log_dir = args.log_dir or (
        f"/tmp/ltvm-test/{cluster.name}-{args.suite}-{int(time.time())}"
    )
    argv = tr.build_auster_argv(
        args.suite,
        log_dir=log_dir,
        cfg=args.cfg,
        only=args.only,
        except_=getattr(args, "except"),
    )
    command = tr.build_remote_command(tests_dir, argv)
    if not use_json:
        print(f"running on {node.name}: {command}")

    try:
        run_ssh(vm.ip, command, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        return _error(
            f"auster timed out after {args.timeout}s on {node.name}; "
            f"logs (if any) are in {log_dir}",
            use_json,
        )
    except OSError as e:
        return _error(f"ssh to {node.name} failed: {e}", use_json)

    # auster's own exit code is not consulted: it is non-zero for a
    # benign-listed failure too.  results.yml decides.
    with tempfile.TemporaryDirectory() as td:
        local = Path(td) / "results.yml"
        scp = subprocess.run(
            sshpass_scp_argv(f"root@{vm.ip}:{log_dir}/results.yml", str(local)),
            capture_output=True,
            text=True,
        )
        if scp.returncode != 0 or not local.is_file():
            return _error(
                f"no results.yml at {log_dir} on {node.name}: the suite "
                f"did not run to the point of writing results "
                f"({(scp.stderr or '').strip()})",
                use_json,
            )
        text = local.read_text()

    try:
        report = tr.build_report(
            text,
            suite=args.suite,
            cluster=cluster.name,
            cfg=args.cfg,
            benign=benign,
        )
    except tr.TestRunnerError as e:
        return _error(str(e), use_json)

    report["node"] = node.name
    report["log_dir"] = log_dir

    if use_json:
        _output(report, True)
    else:
        _print_report(report)

    return EXIT_ERROR if report["counts"]["fail"] else EXIT_OK
