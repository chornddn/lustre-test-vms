"""Run a Lustre test suite on a cluster and turn auster's own results
file into one structured report.

Everything here is deliberately free of argparse: `ltvm_pkg/cli/test.py`
owns the command surface, this module owns the behaviour.  The parsing
and preflight-evaluation halves take plain text and return plain data so
they are unit-testable without a VM.

Two facts about the Lustre test framework shape this module:

* ``run_suites()`` in ``lustre/tests/test-framework.sh`` starts each
  suite with ``unset ONLY EXCEPT START_AT STOP_AT``, so an
  EXCEPT environment variable is silently discarded.  Only the
  per-suite option form (``auster ... <suite> --except 50,109``) works.
  :func:`build_auster_argv` is the only place that spells this, and it
  cannot emit the environment form.
* ``auster -D <dir>`` sets the log directory verbatim and
  ``test-framework.sh`` writes ``$LOGDIR/results.yml``, one record per
  subtest.  That file is the source of truth; console output is not
  parsed anywhere in this module.
"""

from __future__ import annotations

import re
import shlex
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import yaml

# Statuses yaml.sh writes.  Anything else is treated as a failure rather
# than dropped -- an unrecognized status means the framework told us
# something we do not understand, which is never a pass.
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_SKIP = "SKIP"

# Known environment-caused failures, per suite.  Data, not code: an
# entry moves a FAIL into the report's `benign` bucket and carries the
# reason a reader needs so the diagnosis is not repeated every run.
# Keys are normalized subtest names ("test_50"), matching results.yml.
BENIGN_FAILURES: dict[str, dict[str, str]] = {
    "sanity-lnet": {
        "test_50": (
            "page-allocation shortage on 2 GB VMs with 64 KB pages; "
            "unrelated to any Lustre change"
        ),
        "test_109": ("calls ifconfig; the VM image ships no net-tools package"),
        "test_218": "needs a second LNet interface",
        "test_634": ("needs two interfaces to put two NUMA groups on one net"),
    },
}

DEFAULT_CFG = "local"
DEFAULT_TIMEOUT = 7200

# Section markers for the preflight probe.  Chosen so no shell or config
# content collides with them.
_PROBE_SECTIONS = ("uname", "modules", "lnet_conf", "cfg")
_MARKER = "@@ltvm-preflight:"


class TestRunnerError(RuntimeError):
    """A run could not produce a trustworthy result."""


def normalize_test_name(name: str) -> str:
    """Return the ``test_<n>`` spelling of a subtest name.

    Callers say ``50``; results.yml says ``test_50``.  Non-numeric names
    (``test_setup``, ``sanity``) pass through unchanged.
    """
    name = name.strip()
    if not name:
        return name
    if name.startswith("test_"):
        return name
    if name[0].isdigit():
        return f"test_{name}"
    return name


def parse_benign_overrides(specs: list[str]) -> dict[str, dict[str, str]]:
    """Parse ``--benign SUITE:TEST[,TEST...]`` values into the table shape.

    Raises TestRunnerError on a malformed spec rather than silently
    ignoring it -- a typo'd override that quietly does nothing would
    turn a real failure back into a surprise.
    """
    out: dict[str, dict[str, str]] = {}
    for spec in specs:
        for item in spec.split(","):
            item = item.strip()
            if not item:
                continue
            suite, sep, test = item.partition(":")
            if not sep or not suite.strip() or not test.strip():
                raise TestRunnerError(
                    f"--benign {item!r}: expected SUITE:TEST "
                    f"(e.g. sanity-lnet:50)"
                )
            out.setdefault(suite.strip(), {})[normalize_test_name(test)] = (
                "listed by --benign on the command line"
            )
    return out


def benign_for_suite(
    suite: str,
    *,
    overrides: dict[str, dict[str, str]] | None = None,
    enabled: bool = True,
) -> dict[str, str]:
    """Return the benign table for *suite* as ``{test_name: why}``.

    ``enabled=False`` (the ``--no-benign`` path) returns an empty table
    so every FAIL is reported as a failure, including the ones we
    normally excuse.
    """
    if not enabled:
        return {}
    table = dict(BENIGN_FAILURES.get(suite, {}))
    if overrides:
        table.update(overrides.get(suite, {}))
    return table


def build_auster_argv(
    suite: str,
    *,
    log_dir: str,
    cfg: str = DEFAULT_CFG,
    only: str | None = None,
    except_: str | None = None,
) -> list[str]:
    """Build the auster argv for one suite run.

    ``--only`` / ``--except`` are emitted **after** the suite name,
    where run_suites() parses them as suite options.  There is no code
    path here that can set ONLY or EXCEPT in the environment; see the
    module docstring for why that matters.
    """
    argv = ["./auster", "-r", "-v", "-D", log_dir, "-f", cfg, suite]
    if only:
        argv += ["--only", only]
    if except_:
        argv += ["--except", except_]
    return argv


def build_remote_command(tests_dir: str, argv: list[str]) -> str:
    """Quote *argv* into a command that runs in the node's tests dir.

    ssh transports one string, so the argv list is joined with
    shlex.join (each element quoted) rather than interpolated.
    """
    return (
        f"mkdir -p {shlex.quote(_log_dir_of(argv))} && "
        f"cd {shlex.quote(tests_dir)} && {shlex.join(argv)}"
    )


def _log_dir_of(argv: list[str]) -> str:
    """Return the -D value from an auster argv."""
    for i, tok in enumerate(argv):
        if tok == "-D" and i + 1 < len(argv):
            return argv[i + 1]
    raise TestRunnerError("auster argv has no -D log dir")


# ------------------------------------------------------------------
# results.yml parsing
# ------------------------------------------------------------------


def _suite_records(text: str) -> list[dict]:
    """Return the suite records in a results.yml, whatever the wrapper.

    ``init_logging`` writes a ``TestGroup`` / ``project`` header and puts
    the suite records under a top-level ``Tests:`` key, so the document
    is a mapping.  A file that begins directly with the suite list (the
    bare ``yml_log_test`` output, no header) is accepted too.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise TestRunnerError(f"results.yml is not valid YAML: {e}")
    if doc is None:
        return []
    if isinstance(doc, dict):
        doc = doc.get("Tests") or []
    if not isinstance(doc, list):
        raise TestRunnerError(
            "results.yml: expected a list of suite records under 'Tests', "
            f"got {type(doc).__name__}"
        )
    return [r for r in doc if isinstance(r, dict)]


def parse_results(text: str, suite: str | None = None) -> list[dict]:
    """Return the flat list of subtest records in a results.yml.

    Each suite record carries a ``SubTests`` list (yaml.sh
    ``yml_log_test`` / ``yml_log_sub_test_end``).  When *suite* is
    given, only that suite's records are returned; a run that executed
    one suite still gets a suite record per repeat.
    """
    subtests: list[dict] = []
    for record in _suite_records(text):
        if suite is not None and record.get("name") not in (suite, None):
            continue
        for sub in record.get("SubTests") or []:
            if isinstance(sub, dict) and sub.get("name"):
                subtests.append(sub)
    return subtests


def _suite_duration(text: str) -> int:
    """Best-effort total run duration, in seconds."""
    try:
        records = _suite_records(text)
    except TestRunnerError:
        return 0
    total = 0
    for record in records:
        try:
            total += int(record.get("duration") or 0)
        except (TypeError, ValueError):
            continue
    return total


def build_report(
    text: str,
    *,
    suite: str,
    cluster: str,
    cfg: str = DEFAULT_CFG,
    benign: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Turn results.yml text into the report dict agents consume.

    A FAIL on the benign table lands in ``benign`` and nowhere else.  An
    unrecognized status lands in ``fail`` with the raw status in its
    reason.  An empty result set is an error, not an empty pass list:
    it means the suite never ran.
    """
    benign = benign or {}
    subtests = parse_results(text, suite)
    if not subtests:
        raise TestRunnerError(
            f"results.yml records no subtests for {suite!r}: "
            f"the suite never ran"
        )

    passed: list[str] = []
    failed: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    benign_hits: list[dict[str, str]] = []

    for sub in subtests:
        name = str(sub.get("name"))
        status = str(sub.get("status") or "").strip()
        reason = str(sub.get("error") or "").strip()
        if status == STATUS_PASS:
            passed.append(name)
        elif status == STATUS_SKIP:
            skipped.append({"test": name, "reason": reason})
        elif status == STATUS_FAIL:
            why = benign.get(name)
            if why is not None:
                benign_hits.append({"test": name, "reason": reason, "why": why})
            else:
                failed.append({"test": name, "reason": reason})
        else:
            failed.append(
                {
                    "test": name,
                    "reason": f"unknown status {status!r}"
                    + (f": {reason}" if reason else ""),
                }
            )

    return {
        "suite": suite,
        "cluster": cluster,
        "cfg": cfg,
        "duration": _suite_duration(text),
        "pass": passed,
        "fail": failed,
        "skip": skipped,
        "benign": benign_hits,
        "counts": {
            "pass": len(passed),
            "fail": len(failed),
            "skip": len(skipped),
            "benign": len(benign_hits),
        },
    }


# ------------------------------------------------------------------
# Preflight
# ------------------------------------------------------------------


@dataclass
class NodeProbe:
    """What one node reported about its test environment."""

    node: str
    reachable: bool = True
    error: str = ""
    uname: str = ""
    module_kvers: list[str] = field(default_factory=list)
    lnet_conf: str = ""
    cfg_present: bool = False
    cfg_text: str = ""


def probe_script(cfg_path: str) -> str:
    """Shell that reports one node's test-environment state.

    One round trip per node: the cluster config text (checksummed and
    NETTYPE-parsed by the caller), the running kernel, the kernel
    versions Lustre modules are installed for, and the LNet modprobe
    config.
    """
    quoted = shlex.quote(cfg_path)
    return "\n".join(
        [
            f"echo '{_MARKER}uname'",
            "uname -r",
            f"echo '{_MARKER}modules'",
            "ls -d /lib/modules/*/*/kernel/fs/lustre 2>/dev/null "
            "| awk -F/ '{print $4}'",
            f"echo '{_MARKER}lnet_conf'",
            "cat /etc/modprobe.d/lnet.conf 2>/dev/null",
            f"echo '{_MARKER}cfg'",
            f"cat {quoted} 2>/dev/null",
            # Every command above is allowed to fail (a missing config is
            # a finding, not an unreachable node), so the script's own
            # exit status must not carry their failures: only a genuine
            # ssh/transport failure may make this non-zero.
            "exit 0",
        ]
    )


def parse_probe_output(node: str, text: str) -> NodeProbe:
    """Parse :func:`probe_script` output into a :class:`NodeProbe`."""
    sections: dict[str, list[str]] = {s: [] for s in _PROBE_SECTIONS}
    current = None
    for line in text.splitlines():
        if line.startswith(_MARKER):
            current = line[len(_MARKER) :].strip()
            continue
        if current in sections:
            sections[current].append(line)
    uname = "\n".join(sections["uname"]).strip()
    kvers = [k.strip() for k in sections["modules"] if k.strip()]
    cfg_text = "\n".join(sections["cfg"]).strip()
    return NodeProbe(
        node=node,
        uname=uname,
        module_kvers=kvers,
        lnet_conf="\n".join(sections["lnet_conf"]).strip(),
        cfg_present=bool(cfg_text),
        cfg_text=cfg_text,
    )


def _cfg_value(cfg_text: str, key: str) -> str | None:
    """Return the last ``KEY=value`` assignment in a cfg/*.sh file."""
    found = None
    for line in cfg_text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            found = value.strip().strip("\"'")
    return found


def _lnet_net_types(lnet_conf: str) -> list[str]:
    """Return the LNet network types named in a modprobe lnet.conf.

    ``options lnet networks="o2ib0(eth0)"`` yields ``["o2ib"]``.

    A multi-rail net lists its interfaces comma-separated inside one
    set of parens -- ``o2ib0(eth1,eth2)`` -- so the net names are the
    identifiers in front of a '(', not whatever a split on ',' leaves.
    """
    types: list[str] = []
    for line in lnet_conf.splitlines():
        line = line.strip()
        if line.startswith("#") or "networks" not in line:
            continue
        _, _, rest = line.partition("networks")
        rest = rest.lstrip("=").strip().strip("\"'")
        if "(" in rest:
            nets = re.findall(r"([A-Za-z][A-Za-z0-9_]*)\s*\(", rest)
        else:
            # No interface lists at all, e.g. networks="tcp".
            nets = rest.replace(",", " ").split()
        for net in nets:
            base = net.rstrip("0123456789")
            if base and base not in types:
                types.append(base)
    return types


def evaluate_preflight(
    probes: list[NodeProbe],
    *,
    cluster: str,
    cfg: str = DEFAULT_CFG,
) -> list[str]:
    """Return the list of reasons this cluster must not run a suite.

    Pure: takes what the nodes reported, returns messages.  An empty
    list means every check passed.
    """
    errors: list[str] = []
    unreachable = [p.node for p in probes if not p.reachable]
    if unreachable:
        details = "; ".join(
            f"{p.node}: {p.error}" for p in probes if not p.reachable
        )
        errors.append(f"nodes unreachable: {details}")

    live = [p for p in probes if p.reachable]

    # 1. cluster config present and identical everywhere
    missing = [p.node for p in live if not p.cfg_present]
    if missing:
        errors.append(
            f"cfg/{cfg}.sh missing on: {', '.join(sorted(missing))} "
            f"(run: ltvm cluster deploy {cluster})"
        )
    have_cfg = [p for p in live if p.cfg_present]
    digests = {p.node: _digest(p.cfg_text) for p in have_cfg}
    if len(set(digests.values())) > 1:
        groups: dict[str, list[str]] = {}
        for node, digest in digests.items():
            groups.setdefault(digest, []).append(node)
        summary = " vs ".join(
            f"[{', '.join(sorted(nodes))}]"
            for _, nodes in sorted(groups.items())
        )
        errors.append(
            f"cfg/{cfg}.sh differs between nodes: {summary} "
            f"(run: ltvm cluster deploy {cluster})"
        )

    # 2. deployed modules match the running kernel
    for p in live:
        if not p.uname:
            errors.append(f"{p.node}: could not read the running kernel")
            continue
        if not p.module_kvers:
            errors.append(
                f"{p.node}: no Lustre modules installed under "
                f"/lib/modules (running {p.uname}); run: "
                f"ltvm build lustre --for-cluster {cluster} && "
                f"ltvm cluster deploy {cluster}"
            )
        elif p.uname not in p.module_kvers:
            errors.append(
                f"{p.node}: Lustre modules are built for "
                f"{', '.join(p.module_kvers)} but the node is running "
                f"{p.uname}; run: ltvm build lustre --for-cluster "
                f"{cluster} && ltvm cluster deploy {cluster}"
            )

    # 3. every statement of the cluster's net must name the same one
    for p in live:
        if not p.cfg_present:
            continue
        nettype = _cfg_value(p.cfg_text, "NETTYPE")
        if not nettype:
            continue
        expected = nettype.strip().rstrip("0123456789")

        if p.lnet_conf:
            stale = [t for t in _lnet_net_types(p.lnet_conf) if t != expected]
            if stale:
                errors.append(
                    f"{p.node}: /etc/modprobe.d/lnet.conf configures "
                    f"{', '.join(stale)} but the cluster config uses "
                    f"NETTYPE={nettype}; remove or fix lnet.conf"
                )

        # MGSNID's net has to be NETTYPE's too.  A correct lnet.conf and
        # a correct NETTYPE with an MGSNID still on the old net is the
        # one mismatch a net switch can leave behind, and it surfaces as
        # `no connections available: rc = -22` at mount -- a Lustre
        # fault to read, a config fault in fact.
        mgsnid = _cfg_value(p.cfg_text, "MGSNID")
        if mgsnid and "@" in mgsnid:
            nid_net = mgsnid.rsplit("@", 1)[1].strip().rstrip("0123456789")
            if nid_net and nid_net != expected:
                errors.append(
                    f"{p.node}: cfg/{cfg}.sh has MGSNID={mgsnid} on "
                    f"{nid_net} but NETTYPE={nettype}; redeploy with "
                    f"ltvm deploy {cluster} --net {expected}"
                )
    return errors


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def gather_probes(
    nodes: list[tuple[str, str]],
    cfg_path: str,
    runner: Any,
    timeout: int = 60,
) -> list[NodeProbe]:
    """Run the preflight probe on every node in parallel.

    *nodes* is a list of ``(name, ip)``; *runner* is
    ``vm_net.run_ssh``-shaped (``(ip, command, timeout=...)`` returning
    an object with ``returncode`` / ``stdout`` / ``stderr``).

    This does its own fan-out rather than using
    ``vm_cluster._parallel_cluster_op``: that helper prints per-node
    progress (which would corrupt ``--json`` output) and discards the
    output of successful nodes, which is exactly the data the
    cross-node config comparison needs.
    """
    script = probe_script(cfg_path)

    def one(item: tuple[str, str]) -> NodeProbe:
        name, ip = item
        try:
            r = runner(ip, script, timeout=timeout)
        except Exception as e:  # noqa: BLE001 - reported, not raised
            return NodeProbe(node=name, reachable=False, error=str(e))
        if r.returncode != 0:
            detail = (getattr(r, "stderr", "") or "").strip()
            return NodeProbe(
                node=name,
                reachable=False,
                error=f"probe failed (rc={r.returncode}): {detail}",
            )
        return parse_probe_output(name, r.stdout or "")

    if not nodes:
        return []
    with ThreadPoolExecutor(max_workers=len(nodes)) as pool:
        return list(pool.map(one, nodes))
