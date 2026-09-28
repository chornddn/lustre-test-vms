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

import json
import logging
import re
import shlex
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

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
    no_setup: bool = False,
) -> list[str]:
    """Build the auster argv for one suite run.

    ``--only`` / ``--except`` are emitted **after** the suite name,
    where run_suites() parses them as suite options.  There is no code
    path here that can set ONLY or EXCEPT in the environment; see the
    module docstring for why that matters.

    ``no_setup`` emits ``-N``, which clears do_setup so
    setup_if_needed() and do_check_and_setup_lustre() format and mount
    nothing.  It is a global auster option, so it goes before the suite
    name.  An LNet suite configures its own LNet and needs no
    filesystem.
    """
    argv = ["./auster", "-r", "-v", "-D", log_dir, "-f", cfg]
    if no_setup:
        argv.append("-N")
    argv.append(suite)
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


# Escapes YAML accepts after a backslash inside a double-quoted scalar
# (YAML 1.2 sec. 5.7).  Anything else makes the document invalid.
_YAML_ESCAPES = set('0abtnvfre "/\\N_LPxuU\t')


def _unshell_quote(text: str) -> str:
    """Drop backslashes that shell quoting left inside YAML strings.

    ``yaml.sh`` writes a failure message with
    ``printf 'error: "%q"' "$*"``.  ``%q`` is *shell* quoting, and it
    lands inside a *YAML* double-quoted scalar, so any message holding a
    character bash wants to escape produces a file no YAML parser will
    read::

        error: "Health\\ hasn\\'t\\ recovered"

    ``\\'`` is not a YAML escape, so the whole run becomes unreportable
    -- and precisely when it failed, which is when the report matters.
    This strips the backslash from any escape YAML does not define,
    which is what the shell meant by it: quote the next character.

    Only double-quoted scalars are touched.  A backslash in a plain or
    single-quoted scalar is already literal, and YAML escapes such as
    ``\\n`` and ``\\"`` are left alone.
    """
    out: list[str] = []
    in_quotes = False
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and in_quotes and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in _YAML_ESCAPES:
                # A real escape: copy both, so \\ cannot be split and
                # \" cannot be mistaken for the end of the scalar.
                out.append(c)
                out.append(nxt)
            else:
                out.append(nxt)
            i += 2
            continue
        if c == '"':
            in_quotes = not in_quotes
        elif c == "\n":
            # An unterminated quote is a malformed line, not a licence
            # to treat the rest of the file as one string.
            in_quotes = False
        out.append(c)
        i += 1
    return "".join(out)


def _suite_records(text: str) -> list[dict]:
    """Return the suite records in a results.yml, whatever the wrapper.

    ``init_logging`` writes a ``TestGroup`` / ``project`` header and puts
    the suite records under a top-level ``Tests:`` key, so the document
    is a mapping.  A file that begins directly with the suite list (the
    bare ``yml_log_test`` output, no header) is accepted too.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as first:
        # Repair only after a real failure, so a well-formed file is
        # never rewritten on the way in.
        try:
            doc = yaml.safe_load(_unshell_quote(text))
        except yaml.YAMLError:
            raise TestRunnerError(
                f"results.yml is not valid YAML: {first}"
            ) from first
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
    buckets = classify(subtests, benign)
    return {
        "suite": suite,
        "cluster": cluster,
        "cfg": cfg,
        "duration": _suite_duration(text),
        # The subtest the run reached last, so a caller can say where a
        # halted suite stopped without re-reading results.yml.
        "last": str(subtests[-1].get("name") or ""),
        **buckets,
        "counts": {
            k: len(buckets[k]) for k in ("pass", "fail", "skip", "benign")
        },
    }


def classify(
    subtests: list[dict], benign: dict[str, str] | None = None
) -> dict[str, Any]:
    """Sort subtest records into pass / fail / skip / benign buckets.

    Shared by the final report and the progress reader so a run in
    flight is bucketed by exactly the rules that will judge it at the
    end -- including the benign table.
    """
    benign = benign or {}
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
        "pass": passed,
        "fail": failed,
        "skip": skipped,
        "benign": benign_hits,
    }


# ------------------------------------------------------------------
# Run records and progress
# ------------------------------------------------------------------

# A suite runs for tens of minutes inside one blocking ssh call, so the
# session that started it cannot report on it, and a second session
# knows nothing about it at all.  Every agent then invents its own way
# to look: `pgrep -f auster` (matches the shell running the pgrep),
# tailing the console (rotated and buffered), or timing guesses.  The
# record below is written beside the cluster state, like a claim, so
# "how far in is it?" has one answer readable from any session --
# including after the session that started the run has died.

_PROGRESS_SECTIONS = ("alive", "total", "current", "results")


def _sockets() -> Path:
    """Resolve the state dir at call time (see cluster_claim._sockets)."""
    from ltvm_pkg import vm_state

    return vm_state.SOCKETS


def run_record_path(cluster: str) -> Path:
    return _sockets() / f"{cluster}.testrun"


@dataclass
class RunRecord:
    """Where one suite run is happening, and whether it has ended."""

    cluster: str
    suite: str
    node: str
    ip: str
    log_dir: str
    tests_dir: str
    cfg: str = DEFAULT_CFG
    only: str = ""
    excepted: str = ""
    started: int = 0
    #: 0 while the run is in flight; set when `ltvm test` returns.  This
    #: separates "still going" from "over" without asking the node.
    finished: int = 0

    def save(self) -> None:
        """Persist this record; a failure costs visibility, not the run."""
        from ltvm_pkg import vm_state
        from ltvm_pkg.priv import chown_to_real_user

        path = run_record_path(self.cluster)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            vm_state._atomic_write(
                path, json.dumps(asdict(self), indent=2) + "\n"
            )
            chown_to_real_user(path)
        except OSError as e:
            log.warning("cannot record the run at %s: %s", path, e)

    @staticmethod
    def load(cluster: str) -> RunRecord | None:
        """Return the last recorded run for *cluster*, or None."""
        try:
            data = json.loads(run_record_path(cluster).read_text())
        except (OSError, json.JSONDecodeError):
            return None
        known = set(RunRecord.__dataclass_fields__)
        try:
            return RunRecord(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            return None


def suite_total_script(tests_dir: str, suite: str) -> str:
    """Shell that counts the subtests a suite defines.

    The suite script's own ``run_test`` lines are the only honest total:
    it is what the run would have covered had nothing stopped it.
    """
    return f"grep -c '^run_test ' {shlex.quote(f'{tests_dir}/{suite}.sh')}"


def parse_total(text: str) -> int:
    """Parse :func:`suite_total_script` output; 0 when it is unusable."""
    try:
        return int(text.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0


def progress_script(record: RunRecord) -> str:
    """Shell that reports one run's progress in a single round trip."""
    log_dir = shlex.quote(record.log_dir)
    # Bracketed so pgrep cannot match the shell that runs it -- the
    # mistake every hand-rolled check makes -- and carrying the log dir
    # so it identifies this run, not another suite on the same node.
    pattern = shlex.quote(f"[a]uster.*{re.escape(record.log_dir)}")
    return "\n".join(
        [
            f"echo '{_MARKER}alive'",
            f"pgrep -f {pattern} >/dev/null && echo yes || echo no",
            f"echo '{_MARKER}total'",
            suite_total_script(record.tests_dir, record.suite) + " 2>/dev/null",
            f"echo '{_MARKER}current'",
            f"ls -t {log_dir}/*.test_log.*.log 2>/dev/null | head -1",
            # Last: results.yml is the only multi-line section, so a
            # stray newline in it cannot be read as another section.
            f"echo '{_MARKER}results'",
            f"cat {log_dir}/results.yml 2>/dev/null",
            # Nothing above is required to succeed; only a transport
            # failure may make this script non-zero.
            "exit 0",
        ]
    )


def parse_progress_output(text: str) -> dict[str, str]:
    """Split :func:`progress_script` output into its sections."""
    sections: dict[str, list[str]] = {s: [] for s in _PROGRESS_SECTIONS}
    current = None
    for line in text.splitlines():
        if line.startswith(_MARKER):
            current = line[len(_MARKER) :].strip()
            continue
        if current in sections:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


_TEST_LOG_RE = re.compile(r"\.(test_[^.]+)\.test_log\.")


def _current_test(listing: str, recorded: set[str]) -> str:
    """Name the subtest that has a log but no result yet."""
    m = _TEST_LOG_RE.search(listing.strip().splitlines()[0] if listing else "")
    if not m:
        return ""
    name = m.group(1)
    return "" if name in recorded else name


def coverage_note(recorded: int, total: int, last: str, ended: bool) -> str:
    """Explain a run that recorded fewer subtests than the suite has.

    ``FAIL_ON_ERROR`` defaults to true (``cfg/local.sh``), so
    ``test-framework.sh`` exits the whole suite at the first real
    failure.  That is intended, but it makes "0 FAIL" over a third of a
    suite look like a clean pass unless the shortfall is stated.
    """
    if not total or recorded >= total or not ended:
        return ""
    missing = total - recorded
    where = f" after {last}" if last else ""
    return (
        f"stopped early{where}: {recorded} of {total} subtests ran, "
        f"{missing} never started (FAIL_ON_ERROR halts the suite on the "
        f"first real failure; auster -k would continue)"
    )


def build_progress(
    record: RunRecord,
    sections: dict[str, str],
    *,
    benign: dict[str, str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Turn one progress round trip into the dict agents consume.

    A partially written results.yml is normal here -- the file grows a
    record per subtest -- so a parse failure reports zero progress
    rather than raising.
    """
    now = time.time() if now is None else now
    try:
        subtests = parse_results(sections.get("results", ""), record.suite)
    except TestRunnerError:
        subtests = []

    # yaml.sh writes a subtest's name when it starts and its status when
    # it ends, so a record with no status is the test running right now.
    # Counting it would report the live subtest as an unknown-status
    # failure.
    running = [s for s in subtests if not str(s.get("status") or "").strip()]
    subtests = [s for s in subtests if str(s.get("status") or "").strip()]

    buckets = classify(subtests, benign)
    counts = {k: len(buckets[k]) for k in ("pass", "fail", "skip", "benign")}

    alive = sections.get("alive", "").strip() == "yes"
    total = parse_total(sections.get("total", ""))
    recorded = len(subtests)
    names = [str(s.get("name")) for s in subtests]
    last = names[-1] if names else ""

    if alive:
        state = "running"
    elif record.finished:
        state = "finished"
    else:
        # No auster on the node and no completion recorded: the starting
        # session died, or the run was killed.  Never call this finished.
        state = "ended"

    return {
        "cluster": record.cluster,
        "suite": record.suite,
        "node": record.node,
        "log_dir": record.log_dir,
        "state": state,
        "elapsed": max(0, int(now - record.started)) if record.started else 0,
        "counts": counts,
        "recorded": recorded,
        "total": total,
        # The unfinished results.yml record names the live subtest
        # exactly; the newest test_log is the fallback for the window
        # before that record is flushed.
        "current": (
            str(running[-1].get("name"))
            if running
            else _current_test(sections.get("current", ""), set(names))
        ),
        "last": last,
        "fail": buckets["fail"],
        "benign": buckets["benign"],
        "coverage_note": coverage_note(
            recorded, total, last, state != "running"
        ),
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


def _nid_family(nid: str) -> str:
    """Return "ipv6", "ipv4", or "" for a NID whose address is neither.

    The address is everything left of the last '@'; an IPv6 address is
    the one that can contain a ':', which no other NID form does.
    """
    addr = nid.rsplit("@", 1)[0].strip()
    if ":" in addr:
        return "ipv6"
    if addr.count(".") == 3:
        return "ipv4"
    return ""


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

        # The address family is the third statement that can drift.
        # test-framework.sh configures the NI from FORCE_LARGE_NID, so a
        # MGSNID of the other family names an address no node has.
        large = _cfg_value(p.cfg_text, "FORCE_LARGE_NID")
        fam = _nid_family(mgsnid) if mgsnid else ""
        if large and fam:
            want = "ipv6" if large.strip().lower() == "true" else "ipv4"
            if fam != want:
                errors.append(
                    f"{p.node}: cfg/{cfg}.sh has "
                    f"FORCE_LARGE_NID={large.strip()} but MGSNID={mgsnid} "
                    f"is {fam}; test-framework.sh will configure a {want} "
                    f"NI and every mount will target a NID no node "
                    f"advertises. Redeploy with ltvm deploy {cluster} "
                    f"--ip-family {want}"
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
