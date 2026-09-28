"""Tests for the `ltvm test` runner: argv building, results parsing,
benign-failure handling and preflight.

The behaviours guarded here are the ones that made hand-rolled test runs
untrustworthy:

  * `EXCEPT=...` in the environment is discarded by run_suites(), so the
    argv builder must only ever emit the suite-option form.
  * A green run that tested nothing (empty results, or a cluster whose
    nodes disagree about cfg/local.sh) must be an error, not a pass.
  * A known environmental failure must be visible as such -- neither
    hidden in the pass count nor re-diagnosed as a regression.
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import test_runner as tr
from ltvm_pkg.cli.test import _resolve_node, cmd_test

# ── fixtures ─────────────────────────────────────────────

# Shape taken verbatim from a real auster run on the co2 cluster: a
# TestGroup/project header, with the suite records under `Tests:`.
RESULTS_YML = """TestGroup:
    test_group: acc-sm-co2-cli
    testhost: co2-cli
    submission: Thu Aug  6 20:38:58 UTC 2026
    user_name: root
project: LUSTRE

Tests:
-
        name: sanity-lnet
        description: sanity-lnet sanity-lnet
        submission: Thu Aug  6 00:00:00 UTC 2026
        report_version: 2
        SubTests:
        -
            name: test_630
            status: PASS
            duration: 5
            return_code: 0
            error:
        -
            name: test_631
            status: PASS
            duration: 4
            return_code: 0
            error:
        -
            name: test_50
            status: FAIL
            duration: 12
            return_code: 1
            error: "Out\\ of\\ memory"
        -
            name: test_634
            status: SKIP
            duration: 0
            return_code: 0
            error: "Need\\ two\\ interfaces"
        duration: 21
        status: FAIL
"""


def _fake_completed(stdout: str = "", rc: int = 0, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=rc, stdout=stdout, stderr=stderr
    )


def _probe_output(
    uname: str = "5.14.0-503.40.1.el9_5_lustre",
    kvers: str = "5.14.0-503.40.1.el9_5_lustre",
    lnet_conf: str = "",
    cfg: str = "FSNAME=lustre\nNETTYPE=tcp\n",
) -> str:
    m = "@@ltvm-preflight:"
    return (
        f"{m}uname\n{uname}\n"
        f"{m}modules\n{kvers}\n"
        f"{m}lnet_conf\n{lnet_conf}\n"
        f"{m}cfg\n{cfg}"
    )


# ── argv building ────────────────────────────────────────


class TestAusterArgv:
    """The suite-option spelling is the only one the framework honors."""

    def test_only_and_except_follow_the_suite_name(self) -> None:
        argv = tr.build_auster_argv(
            "sanity-lnet",
            log_dir="/tmp/x",
            only="630,631",
            except_="50,109",
        )
        suite_at = argv.index("sanity-lnet")
        assert argv.index("--only") > suite_at
        assert argv.index("--except") > suite_at
        assert argv[argv.index("--only") + 1] == "630,631"
        assert argv[argv.index("--except") + 1] == "50,109"

    def test_never_emits_the_environment_form(self) -> None:
        """run_suites() unsets ONLY/EXCEPT, so an env assignment is a
        silent no-op.  No argv element may look like one."""
        argv = tr.build_auster_argv(
            "sanity-lnet", log_dir="/tmp/x", only="1", except_="2"
        )
        cmd = tr.build_remote_command("/usr/lib64/lustre/tests", argv)
        for token in argv:
            assert not token.startswith("ONLY=")
            assert not token.startswith("EXCEPT=")
        assert "ONLY=" not in cmd
        assert "EXCEPT=" not in cmd

    def test_log_dir_is_passed_with_capital_d(self) -> None:
        """-d appends date/time subdirs; only -D is verbatim, which is
        what makes <dir>/results.yml a knowable path."""
        argv = tr.build_auster_argv("sanity-lnet", log_dir="/tmp/run1")
        assert "-D" in argv
        assert argv[argv.index("-D") + 1] == "/tmp/run1"
        assert "-d" not in argv

    def test_cfg_maps_to_dash_f(self) -> None:
        argv = tr.build_auster_argv("sanity", log_dir="/tmp/x", cfg="cluster")
        assert argv[argv.index("-f") + 1] == "cluster"

    def test_no_setup_precedes_the_suite_name(self) -> None:
        """-N is a global auster option, not a suite option."""
        argv = tr.build_auster_argv(
            "sanity-lnet", log_dir="/tmp/x", no_setup=True
        )
        assert "-N" in argv
        assert argv.index("-N") < argv.index("sanity-lnet")

    def test_no_setup_defaults_off(self) -> None:
        argv = tr.build_auster_argv("sanity-lnet", log_dir="/tmp/x")
        assert argv == [
            "./auster",
            "-r",
            "-v",
            "-D",
            "/tmp/x",
            "-f",
            "local",
            "sanity-lnet",
        ]

    def test_no_setup_composes_with_except(self) -> None:
        argv = tr.build_auster_argv(
            "sanity-lnet",
            log_dir="/tmp/x",
            except_="50,109",
            no_setup=True,
        )
        suite_at = argv.index("sanity-lnet")
        assert argv.index("-N") < suite_at
        assert argv.index("--except") > suite_at
        assert argv[argv.index("--except") + 1] == "50,109"

    def test_remote_command_quotes_arguments(self) -> None:
        argv = tr.build_auster_argv("sanity-lnet", log_dir="/tmp/a b", only="1")
        cmd = tr.build_remote_command("/usr/lib64/lustre/tests", argv)
        assert "'/tmp/a b'" in cmd
        assert cmd.startswith("mkdir -p '/tmp/a b' && cd ")


# ── results parsing ──────────────────────────────────────


class TestBuildReport:
    """results.yml, not stdout, decides what happened."""

    def test_buckets_by_status(self) -> None:
        rep = tr.build_report(
            RESULTS_YML, suite="sanity-lnet", cluster="co2", benign={}
        )
        assert rep["pass"] == ["test_630", "test_631"]
        assert rep["fail"] == [{"test": "test_50", "reason": "Out of memory"}]
        assert rep["skip"] == [
            {"test": "test_634", "reason": "Need two interfaces"}
        ]
        assert rep["counts"] == {
            "pass": 2,
            "fail": 1,
            "skip": 1,
            "benign": 0,
        }
        assert rep["duration"] == 21
        assert rep["suite"] == "sanity-lnet"
        assert rep["cluster"] == "co2"

    def test_benign_failure_moves_out_of_fail(self) -> None:
        rep = tr.build_report(
            RESULTS_YML,
            suite="sanity-lnet",
            cluster="co2",
            benign=tr.benign_for_suite("sanity-lnet"),
        )
        assert rep["fail"] == []
        assert len(rep["benign"]) == 1
        entry = rep["benign"][0]
        assert entry["test"] == "test_50"
        assert entry["reason"] == "Out of memory"
        assert "page-allocation" in entry["why"]
        assert rep["counts"]["fail"] == 0
        assert rep["counts"]["benign"] == 1

    def test_no_benign_puts_it_back_in_fail(self) -> None:
        rep = tr.build_report(
            RESULTS_YML,
            suite="sanity-lnet",
            cluster="co2",
            benign=tr.benign_for_suite("sanity-lnet", enabled=False),
        )
        assert [f["test"] for f in rep["fail"]] == ["test_50"]
        assert rep["benign"] == []

    def test_unknown_status_becomes_a_failure(self) -> None:
        """An unrecognized status is the framework telling us something
        we do not model; dropping it would inflate the pass rate."""
        text = RESULTS_YML.replace("status: SKIP", "status: WEIRD")
        rep = tr.build_report(
            text, suite="sanity-lnet", cluster="co2", benign={}
        )
        weird = [f for f in rep["fail"] if f["test"] == "test_634"]
        assert len(weird) == 1
        assert "unknown status 'WEIRD'" in weird[0]["reason"]
        assert rep["skip"] == []

    def test_headerless_suite_list_is_accepted(self) -> None:
        """Older/simpler files start straight at the suite list, with no
        TestGroup header."""
        bare = RESULTS_YML.split("Tests:\n", 1)[1]
        rep = tr.build_report(
            bare, suite="sanity-lnet", cluster="co2", benign={}
        )
        assert rep["counts"]["pass"] == 2

    def test_empty_subtests_is_an_error(self) -> None:
        """A results.yml with no subtests means the suite never ran."""
        text = (
            "-\n        name: sanity-lnet\n        SubTests:\n"
            "        duration: 0\n        status: PASS\n"
        )
        with pytest.raises(tr.TestRunnerError, match="never ran"):
            tr.build_report(text, suite="sanity-lnet", cluster="co2", benign={})

    def test_empty_file_is_an_error(self) -> None:
        with pytest.raises(tr.TestRunnerError):
            tr.build_report("", suite="sanity-lnet", cluster="co2", benign={})

    def test_unparseable_yaml_is_an_error(self) -> None:
        with pytest.raises(tr.TestRunnerError, match="not valid YAML"):
            tr.build_report(
                "\tname: [unclosed",
                suite="sanity-lnet",
                cluster="co2",
                benign={},
            )


class TestShellQuotedErrorMessages:
    """auster's error messages are shell-quoted inside YAML strings.

    ``yaml.sh`` writes ``printf 'error: "%q"' "$*"``.  ``%q`` escapes for
    the shell, not for YAML, so a message holding an apostrophe emits
    ``"Health\\ hasn\\'t\\ recovered"``.  ``\\'`` is not a YAML escape,
    and the whole results.yml became unreadable -- on exactly the runs
    that had something to report.
    """

    def _results(self, error_line: str) -> str:
        return (
            "Tests:\n"
            "-\n"
            "        name: sanity-lnet\n"
            "        SubTests:\n"
            "        -\n"
            "            name: test_218\n"
            "            status: FAIL\n"
            "            duration: 3\n"
            "            return_code: 1\n"
            f"            {error_line}\n"
            "        duration: 3\n"
            "        status: FAIL\n"
        )

    def test_the_apostrophe_message_from_the_field(self) -> None:
        text = self._results(r'error: "Health\ hasn\'t\ recovered"')
        subs = tr.parse_results(text, suite="sanity-lnet")
        assert [s["name"] for s in subs] == ["test_218"]
        assert subs[0]["error"] == "Health hasn't recovered"

    @pytest.mark.parametrize(
        "message",
        [
            "Health hasn't recovered",
            "can't open /mnt/lustre (rc = -22)",
            'unexpected "quoted" token',
            "cost $5 & 100% of budget; see <log>",
            "back\\slash and *glob* and [brackets]",
            "trailing backslash \\",
        ],
    )
    def test_bash_printf_q_round_trips(self, message: str) -> None:
        """Whatever %q emits for a message, the parser must read it back.

        Generated by the real producer rather than by hand, so this
        cannot drift from what bash actually writes.
        """
        quoted = subprocess.run(
            ["bash", "-c", 'printf "%q" "$1"', "_", message],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        subs = tr.parse_results(
            self._results(f'error: "{quoted}"'), suite="sanity-lnet"
        )
        assert subs and subs[0]["error"] == message, (
            f"%q wrote {quoted!r} for {message!r}; parsed "
            f"{subs[0]['error'] if subs else None!r}"
        )

    def test_real_yaml_escapes_survive(self) -> None:
        """The repair must not flatten escapes YAML does define."""
        text = self._results(r'error: "one\ntwo\tthree \"quoted\" back\\slash"')
        subs = tr.parse_results(text, suite="sanity-lnet")
        assert subs[0]["error"] == 'one\ntwo\tthree "quoted" back\\slash'

    def test_a_valid_file_is_not_rewritten(self) -> None:
        """Repair runs only after safe_load fails.

        A backslash in a plain scalar is already literal, so touching a
        file that parsed would corrupt it.
        """
        text = self._results(r"error: C:\path\to\file")
        subs = tr.parse_results(text, suite="sanity-lnet")
        assert subs[0]["error"] == r"C:\path\to\file"

    def test_genuinely_broken_yaml_still_raises(self) -> None:
        """The repair must not turn a structural error into silence."""
        with pytest.raises(tr.TestRunnerError, match="not valid YAML"):
            tr.parse_results("Tests:\n- [unclosed\n", suite="sanity-lnet")

    def test_the_failure_reaches_the_report(self) -> None:
        text = self._results(r'error: "Health\ hasn\'t\ recovered"')
        report = tr.build_report(text, suite="sanity-lnet", cluster="co2")
        assert report["counts"]["fail"] == 1
        assert report["fail"][0]["test"] == "test_218"
        assert "hasn't recovered" in report["fail"][0]["reason"]


class TestUnshellQuote:
    """Unit-level checks on the repair itself."""

    def test_only_double_quoted_scalars_are_touched(self) -> None:
        assert tr._unshell_quote(r"a: b\'c") == r"a: b\'c"
        assert tr._unshell_quote(r'a: "b\'"' + "c") == 'a: "b\'"c'

    def test_an_unterminated_quote_does_not_swallow_the_file(self) -> None:
        """Without the newline reset, one malformed line would put every
        later line inside a string and mangle the rest of the run."""
        out = tr._unshell_quote('a: "oops\n' + r"b: c\'d" + "\n")
        assert out.endswith("b: c\\'d\n")

    def test_a_backslash_at_end_of_input_is_kept(self) -> None:
        assert tr._unshell_quote('a: "b\\') == 'a: "b\\'


class TestBenignTable:
    """The benign list is data, and is overridable per invocation."""

    def test_table_is_keyed_by_suite_and_test(self) -> None:
        table = tr.BENIGN_FAILURES["sanity-lnet"]
        assert set(table) >= {"test_50", "test_109", "test_218", "test_634"}
        assert all(isinstance(v, str) and v for v in table.values())

    def test_override_accepts_bare_numbers(self) -> None:
        over = tr.parse_benign_overrides(["sanity-lnet:77,sanity:1"])
        assert "test_77" in over["sanity-lnet"]
        assert "test_1" in over["sanity"]

    def test_override_merges_into_the_suite_table(self) -> None:
        over = tr.parse_benign_overrides(["sanity-lnet:77"])
        table = tr.benign_for_suite("sanity-lnet", overrides=over)
        assert "test_77" in table and "test_50" in table

    def test_malformed_override_is_rejected(self) -> None:
        """A typo that silently disabled itself would be worse than an
        error: the caller would think a failure was excused."""
        with pytest.raises(tr.TestRunnerError):
            tr.parse_benign_overrides(["sanity-lnet-77"])

    def test_no_benign_empties_the_table(self) -> None:
        assert tr.benign_for_suite("sanity-lnet", enabled=False) == {}


# ── preflight ────────────────────────────────────────────


class TestPreflight:
    """Refuse to run rather than produce a green run that tested
    nothing."""

    def _probe(self, name: str, **kw: Any) -> tr.NodeProbe:
        return tr.parse_probe_output(name, _probe_output(**kw))

    def test_healthy_cluster_has_no_errors(self) -> None:
        probes = [self._probe("a"), self._probe("b")]
        assert tr.evaluate_preflight(probes, cluster="co2") == []

    def test_divergent_config_is_an_error(self) -> None:
        """The deploy bug fixed in d3b7487 left nodes with a single-node
        config; every multi-node test SKIPped and the run still said
        all-PASS."""
        probes = [
            self._probe("a"),
            self._probe("b", cfg="FSNAME=lustre\nNETTYPE=tcp\nOSTCOUNT=1\n"),
        ]
        errs = tr.evaluate_preflight(probes, cluster="co2")
        assert any("differs between nodes" in e for e in errs)
        assert any("a" in e and "b" in e for e in errs)

    def test_missing_config_names_the_node(self) -> None:
        probes = [self._probe("a"), self._probe("b", cfg="")]
        errs = tr.evaluate_preflight(probes, cluster="co2")
        assert any("cfg/local.sh missing on: b" in e for e in errs)

    def test_kernel_mismatch_names_for_cluster(self) -> None:
        probes = [self._probe("a", kvers="5.14.0-otherkernel")]
        errs = tr.evaluate_preflight(probes, cluster="co2")
        assert len(errs) == 1
        assert "--for-cluster co2" in errs[0]
        assert "5.14.0-otherkernel" in errs[0]

    def test_no_modules_at_all_is_an_error(self) -> None:
        probes = [self._probe("a", kvers="")]
        errs = tr.evaluate_preflight(probes, cluster="co2")
        assert any("no Lustre modules" in e for e in errs)

    def test_stale_lnet_conf_is_an_error(self) -> None:
        """A leftover SoftRoCE lnet.conf makes every test fail to load
        modules on a tcp-only cluster."""
        probes = [
            self._probe("a", lnet_conf='options lnet networks="o2ib0(eth0)"')
        ]
        errs = tr.evaluate_preflight(probes, cluster="co2")
        assert any("lnet.conf" in e and "o2ib" in e for e in errs)

    def test_matching_lnet_conf_is_accepted(self) -> None:
        probes = [
            self._probe("a", lnet_conf='options lnet networks="tcp0(eth0)"')
        ]
        assert tr.evaluate_preflight(probes, cluster="co2") == []

    def test_mgsnid_on_another_net_is_an_error(self) -> None:
        """The mismatch a net switch can leave behind.

        lnet.conf and NETTYPE both say o2ib, MGSNID still points at the
        old net's address.  Mount then fails with `no connections
        available: rc = -22`, which reads as a Lustre fault.
        """
        probes = [
            self._probe(
                "a",
                lnet_conf='options lnet networks="o2ib0(eth1)"',
                cfg="FSNAME=lustre\nNETTYPE=o2ib\nMGSNID=192.168.105.10@tcp\n",
            )
        ]
        errs = tr.evaluate_preflight(probes, cluster="co2")
        assert len(errs) == 1
        assert "MGSNID=192.168.105.10@tcp" in errs[0]
        assert "NETTYPE=o2ib" in errs[0]
        assert "cfg/local.sh" in errs[0]
        assert "--net o2ib" in errs[0]

    def test_matching_mgsnid_is_accepted(self) -> None:
        probes = [
            self._probe(
                "a",
                lnet_conf='options lnet networks="o2ib0(eth1)"',
                cfg="FSNAME=lustre\nNETTYPE=o2ib\nMGSNID=172.16.100.203@o2ib\n",
            )
        ]
        assert tr.evaluate_preflight(probes, cluster="co2") == []

    def test_indexed_mgsnid_matches_bare_nettype(self) -> None:
        """`@o2ib0` and `@o2ib` name the same net."""
        probes = [
            self._probe(
                "a",
                lnet_conf='options lnet networks="o2ib0(eth1)"',
                cfg="FSNAME=lustre\nNETTYPE=o2ib\n"
                "MGSNID=172.16.100.203@o2ib0\n",
            )
        ]
        assert tr.evaluate_preflight(probes, cluster="co2") == []

    def test_large_nid_with_ipv6_mgsnid_is_accepted(self) -> None:
        probes = [
            self._probe(
                "a",
                lnet_conf='options lnet networks="tcp0(eth1)"',
                cfg="FSNAME=lustre\nNETTYPE=tcp\n"
                "FORCE_LARGE_NID=true\n"
                "MGSNID=fd17:2016:1000:f100:f172:f016:f100:f203@tcp\n",
            )
        ]
        assert tr.evaluate_preflight(probes, cluster="co2") == []

    def test_small_nid_with_ipv4_mgsnid_is_accepted(self) -> None:
        probes = [
            self._probe(
                "a",
                lnet_conf='options lnet networks="tcp0(eth1)"',
                cfg="FSNAME=lustre\nNETTYPE=tcp\n"
                "FORCE_LARGE_NID=false\n"
                "MGSNID=172.16.100.203@tcp\n",
            )
        ]
        assert tr.evaluate_preflight(probes, cluster="co2") == []

    def test_large_nid_with_ipv4_mgsnid_is_an_error(self) -> None:
        """LNet gets an IPv6 NI and every mount targets an IPv4 MGS NID
        no node advertises."""
        probes = [
            self._probe(
                "a",
                lnet_conf='options lnet networks="tcp0(eth1)"',
                cfg="FSNAME=lustre\nNETTYPE=tcp\n"
                "FORCE_LARGE_NID=true\n"
                "MGSNID=172.16.100.203@tcp\n",
            )
        ]
        errs = tr.evaluate_preflight(probes, cluster="co2")
        assert len(errs) == 1
        assert "FORCE_LARGE_NID=true" in errs[0]
        assert "MGSNID=172.16.100.203@tcp" in errs[0]
        assert "--ip-family ipv6" in errs[0]

    def test_small_nid_with_ipv6_mgsnid_is_an_error(self) -> None:
        probes = [
            self._probe(
                "a",
                lnet_conf='options lnet networks="tcp0(eth1)"',
                cfg="FSNAME=lustre\nNETTYPE=tcp\n"
                "FORCE_LARGE_NID=false\n"
                "MGSNID=fd17:2016:1000:f100:f172:f016:f100:f203@tcp\n",
            )
        ]
        errs = tr.evaluate_preflight(probes, cluster="co2")
        assert len(errs) == 1
        assert "FORCE_LARGE_NID=false" in errs[0]
        assert "--ip-family ipv4" in errs[0]

    def test_cfg_without_force_large_nid_is_accepted(self) -> None:
        """A cluster deployed before the family flag existed."""
        probes = [
            self._probe(
                "a",
                lnet_conf='options lnet networks="tcp0(eth1)"',
                cfg="FSNAME=lustre\nNETTYPE=tcp\nMGSNID=172.16.100.203@tcp\n",
            )
        ]
        assert tr.evaluate_preflight(probes, cluster="co2") == []

    @pytest.mark.parametrize("large", ["true", "false"])
    def test_hostname_mgsnid_is_accepted(self, large: str) -> None:
        """A NID that names a host has no family to disagree with."""
        probes = [
            self._probe(
                "a",
                lnet_conf='options lnet networks="tcp0(eth1)"',
                cfg="FSNAME=lustre\nNETTYPE=tcp\n"
                f"FORCE_LARGE_NID={large}\n"
                "MGSNID=co1-mds@tcp\n",
            )
        ]
        assert tr.evaluate_preflight(probes, cluster="co2") == []

    @pytest.mark.parametrize(
        "mgsnid",
        [
            "172.16.100.203@tcp",
            "fd17:2016:1000:f100:f172:f016:f100:f203@tcp",
        ],
    )
    def test_net_check_reads_the_net_after_the_last_at(
        self, mgsnid: str
    ) -> None:
        """An IPv6 address carries colons but no '@', so the net split
        stays right."""
        probes = [
            self._probe(
                "a",
                lnet_conf='options lnet networks="tcp0(eth1)"',
                cfg=f"FSNAME=lustre\nNETTYPE=tcp\nMGSNID={mgsnid}\n",
            )
        ]
        assert tr.evaluate_preflight(probes, cluster="co2") == []

    def test_unreachable_node_is_an_error(self) -> None:
        probes = [tr.NodeProbe(node="a", reachable=False, error="timed out")]
        errs = tr.evaluate_preflight(probes, cluster="co2")
        assert any("unreachable" in e for e in errs)

    def test_probe_script_exits_zero(self) -> None:
        """A missing config makes the probe's last `cat` fail, which
        would otherwise be reported as an unreachable node and hide the
        real finding."""
        assert tr.probe_script("/cfg").rstrip().endswith("exit 0")

    def test_probe_script_quotes_the_cfg_path(self) -> None:
        script = tr.probe_script("/usr/lib64/lustre/tests/cfg/lo cal.sh")
        assert "'/usr/lib64/lustre/tests/cfg/lo cal.sh'" in script

    def test_gather_probes_marks_failed_ssh_unreachable(self) -> None:
        def runner(ip: str, cmd: str, timeout: int = 60) -> Any:
            if ip == "2":
                return _fake_completed(rc=255, stderr="no route")
            return _fake_completed(_probe_output())

        probes = tr.gather_probes([("a", "1"), ("b", "2")], "/cfg", runner)
        by_name = {p.node: p for p in probes}
        assert by_name["a"].reachable
        assert not by_name["b"].reachable
        assert "no route" in by_name["b"].error


# ── command wiring ───────────────────────────────────────


class _Node:
    def __init__(self, name: str, roles: list[str]) -> None:
        self.name = name
        self.roles = roles


class TestResolveNode:
    """auster runs from the client when there is one."""

    def test_prefers_client(self) -> None:
        nodes = [_Node("mds", ["mgs", "mds"]), _Node("cli", ["client"])]
        assert _resolve_node(nodes, None).name == "cli"

    def test_falls_back_to_mgs_without_a_client(self) -> None:
        nodes = [_Node("oss", ["oss"]), _Node("mds", ["mgs", "mds"])]
        assert _resolve_node(nodes, None).name == "mds"

    def test_explicit_node_matches_name_or_role(self) -> None:
        nodes = [_Node("mds", ["mgs", "mds"]), _Node("cli", ["client"])]
        assert _resolve_node(nodes, "mds").name == "mds"
        assert _resolve_node(nodes, "oss") is None


def _test_args(**kw: Any) -> argparse.Namespace:
    ns = argparse.Namespace(
        cluster="co2",
        suite="sanity-lnet",
        json=True,
        only=None,
        cfg="local",
        node=None,
        timeout=60,
        log_dir="/tmp/ltvm-test/x",
        skip_preflight=True,
        benign=None,
        no_benign=False,
        no_setup=False,
    )
    setattr(ns, "except", None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


class TestCmdTest:
    """End-to-end exit-code behaviour with ssh mocked at the boundary."""

    def _run(
        self,
        args: argparse.Namespace,
        results: str = RESULTS_YML,
        probe: str | None = None,
    ) -> tuple[int, list[Any]]:
        import ltvm_pkg.vm_net as vm_net
        import ltvm_pkg.vm_state as vm_state

        cluster = MagicMock()
        cluster.name = "co2"
        cluster.get_nodes.return_value = [
            _Node("co2-mds", ["mgs", "mds"]),
            _Node("co2-cli", ["client"]),
        ]
        vm = MagicMock()
        vm.ip = "10.0.0.1"

        ssh_calls: list[Any] = []

        def fake_run_ssh(ip: str, cmd: str, timeout: int = 120) -> Any:
            ssh_calls.append(cmd)
            return _fake_completed(probe or _probe_output())

        def fake_scp_run(argv: Any, **kw: Any) -> Any:
            dst = argv[-1]
            with open(dst, "w") as fh:
                fh.write(results)
            return subprocess.CompletedProcess(argv, 0, "", "")

        params = MagicMock()
        params.os_family = "rhel"
        # cmd_test records the run beside the cluster state; keep that
        # write off the host's real state dir.
        state = tempfile.TemporaryDirectory()
        with (
            patch("ltvm_pkg.vm_state.SOCKETS", Path(state.name)),
            patch.object(vm_state.ClusterInfo, "load", return_value=cluster),
            patch.object(vm_state.VMInfo, "load", return_value=vm),
            patch.object(vm_net, "run_ssh", fake_run_ssh),
            patch(
                "ltvm_pkg.vm_cluster.cluster_build_params",
                return_value=params,
            ),
            patch("subprocess.run", fake_scp_run),
        ):
            rc = cmd_test(args)
        state.cleanup()
        return rc, ssh_calls

    def test_real_failure_exits_nonzero(self) -> None:
        rc, _ = self._run(_test_args(no_benign=True))
        assert rc != 0

    def test_benign_failure_exits_zero(self) -> None:
        rc, _ = self._run(_test_args())
        assert rc == 0

    def test_preflight_refuses_and_never_runs_auster(self) -> None:
        rc, calls = self._run(
            _test_args(skip_preflight=False),
            probe=_probe_output(kvers="some-other-kernel"),
        )
        assert rc != 0
        assert not any("auster" in c for c in calls)

    def test_only_reaches_the_remote_command(self) -> None:
        _, calls = self._run(_test_args(skip_preflight=True, only="630"))
        auster = [c for c in calls if "auster" in c]
        assert len(auster) == 1
        assert "sanity-lnet --only 630" in auster[0]


# ------------------------------------------------------------------
# Progress: answering "how far in is it?" from any session
# ------------------------------------------------------------------

# results.yml as it looks mid-run: two finished subtests and a third
# whose name is written but whose status is not, because it is running.
PARTIAL_YML = """Tests:
    -
        name: sanity-lnet
        SubTests:
        -
            name: test_1
            status: PASS
            duration: 3
            return_code: 0
            error:
        -
            name: test_218
            status: FAIL
            duration: 7
            return_code: 1
            error: "Health hasn't recovered"
        -
            name: test_219
"""


def _record(**kw: Any) -> Any:
    from ltvm_pkg import test_runner as tr

    base: dict[str, Any] = dict(
        cluster="co2",
        suite="sanity-lnet",
        node="co2-cli",
        ip="10.0.0.1",
        log_dir="/tmp/ltvm-test/co2-sanity-lnet-1",
        tests_dir="/usr/lib64/lustre/tests",
        started=1000,
    )
    base.update(kw)
    return tr.RunRecord(**base)


def _sections(**kw: str) -> dict[str, str]:
    base = {
        "alive": "yes",
        "total": "172",
        "current": "",
        "results": PARTIAL_YML,
    }
    base.update(kw)
    return base


class TestRunRecord:
    """The record is the handle a second session has on a run."""

    def test_round_trips_through_the_state_dir(self, tmp_path: Path) -> None:
        from ltvm_pkg import test_runner as tr

        with patch("ltvm_pkg.vm_state.SOCKETS", tmp_path):
            _record(only="630").save()
            got = tr.RunRecord.load("co2")
        assert got is not None
        assert (got.suite, got.node, got.only) == (
            "sanity-lnet",
            "co2-cli",
            "630",
        )

    def test_absent_record_is_none_not_an_error(self, tmp_path: Path) -> None:
        from ltvm_pkg import test_runner as tr

        with patch("ltvm_pkg.vm_state.SOCKETS", tmp_path):
            assert tr.RunRecord.load("nosuch") is None

    def test_corrupt_record_is_none_not_a_traceback(
        self, tmp_path: Path
    ) -> None:
        from ltvm_pkg import test_runner as tr

        with patch("ltvm_pkg.vm_state.SOCKETS", tmp_path):
            tr.run_record_path("co2").write_text("{not json")
            assert tr.RunRecord.load("co2") is None

    def test_a_record_from_a_newer_ltvm_still_loads(
        self, tmp_path: Path
    ) -> None:
        """An unknown key must not make an existing run unreadable."""
        from ltvm_pkg import test_runner as tr

        with patch("ltvm_pkg.vm_state.SOCKETS", tmp_path):
            _record().save()
            path = tr.run_record_path("co2")
            import json as _json

            data = _json.loads(path.read_text())
            data["something_new"] = 1
            path.write_text(_json.dumps(data))
            assert tr.RunRecord.load("co2") is not None


class TestProgressScript:
    """The liveness check every hand-rolled version gets wrong."""

    def test_pgrep_pattern_cannot_match_its_own_shell(self) -> None:
        from ltvm_pkg import test_runner as tr

        script = tr.progress_script(_record())
        pgrep = next(ln for ln in script.splitlines() if ln.startswith("pgrep"))
        # The bracket makes the pattern text differ from what it matches,
        # so the process running it is not itself a hit.
        assert "[a]uster" in pgrep
        assert "auster" not in pgrep.replace("[a]uster", "")

    def test_pattern_identifies_this_run_not_any_auster(self) -> None:
        from ltvm_pkg import test_runner as tr

        script = tr.progress_script(_record(log_dir="/tmp/ltvm-test/mine"))
        assert (
            "/tmp/ltvm\\-test/mine" in script or "/tmp/ltvm-test/mine" in script
        )

    def test_results_section_is_last(self) -> None:
        """A newline inside results.yml must not open a later section."""
        from ltvm_pkg import test_runner as tr

        script = tr.progress_script(_record())
        markers = [
            ln for ln in script.splitlines() if ln.startswith("echo '@@")
        ]
        assert markers[-1].endswith("results'")

    def test_sections_split_and_keep_result_newlines(self) -> None:
        from ltvm_pkg import test_runner as tr

        text = (
            "@@ltvm-preflight:alive\nyes\n"
            "@@ltvm-preflight:total\n172\n"
            "@@ltvm-preflight:current\n\n"
            "@@ltvm-preflight:results\na\nb\n"
        )
        got = tr.parse_progress_output(text)
        assert got["alive"] == "yes"
        assert got["total"] == "172"
        assert got["results"] == "a\nb"

    def test_total_comes_from_the_suite_script(self) -> None:
        from ltvm_pkg import test_runner as tr

        assert "run_test" in tr.suite_total_script("/t", "sanity-lnet")
        assert "sanity-lnet.sh" in tr.suite_total_script("/t", "sanity-lnet")
        assert tr.parse_total("172\n") == 172
        assert tr.parse_total("grep: no such file") == 0


class TestBuildProgress:
    """What a watcher is told about a run in flight."""

    def test_running_run_reports_counts_and_position(self) -> None:
        from ltvm_pkg import test_runner as tr

        p = tr.build_progress(_record(), _sections(), now=1060)
        assert p["state"] == "running"
        assert p["elapsed"] == 60
        assert (p["recorded"], p["total"]) == (2, 172)

    def test_the_live_subtest_is_not_counted_as_a_failure(self) -> None:
        """yaml.sh writes the name at begin and the status at end."""
        from ltvm_pkg import test_runner as tr

        p = tr.build_progress(_record(), _sections())
        assert p["current"] == "test_219"
        assert [f["test"] for f in p["fail"]] == ["test_218"]
        assert p["counts"]["fail"] == 1

    def test_benign_table_applies_while_the_run_is_live(self) -> None:
        from ltvm_pkg import test_runner as tr

        p = tr.build_progress(
            _record(), _sections(), benign={"test_218": "needs two interfaces"}
        )
        assert p["counts"]["fail"] == 0
        assert p["benign"][0]["why"] == "needs two interfaces"

    def test_a_half_written_results_file_reports_zero_not_an_error(
        self,
    ) -> None:
        from ltvm_pkg import test_runner as tr

        p = tr.build_progress(_record(), _sections(results="Tests:\n  - {oops"))
        assert p["recorded"] == 0
        assert p["state"] == "running"

    def test_gone_without_a_completion_is_ended_not_finished(self) -> None:
        """A dead starting session must never read as a clean finish."""
        from ltvm_pkg import test_runner as tr

        p = tr.build_progress(_record(), _sections(alive="no"))
        assert p["state"] == "ended"

    def test_completion_recorded_reads_as_finished(self) -> None:
        from ltvm_pkg import test_runner as tr

        p = tr.build_progress(_record(finished=2000), _sections(alive="no"))
        assert p["state"] == "finished"

    def test_current_falls_back_to_the_newest_test_log(self) -> None:
        from ltvm_pkg import test_runner as tr

        done = PARTIAL_YML.rsplit("        -\n", 1)[0]
        p = tr.build_progress(
            _record(),
            _sections(
                results=done,
                current="/tmp/x/sanity-lnet.test_240.test_log.co2-cli.log",
            ),
        )
        assert p["current"] == "test_240"

    def test_a_finished_subtest_is_not_reported_as_current(self) -> None:
        from ltvm_pkg import test_runner as tr

        done = PARTIAL_YML.rsplit("        -\n", 1)[0]
        p = tr.build_progress(
            _record(),
            _sections(
                results=done,
                current="/tmp/x/sanity-lnet.test_218.test_log.co2-cli.log",
            ),
        )
        assert p["current"] == ""


class TestCoverageNote:
    """A suite halted early must not read as a clean pass."""

    def test_shortfall_is_stated_with_the_numbers(self) -> None:
        from ltvm_pkg import test_runner as tr

        note = tr.coverage_note(83, 172, "test_218", ended=True)
        assert "83 of 172" in note
        assert "89 never started" in note
        assert "test_218" in note

    def test_a_complete_run_says_nothing(self) -> None:
        from ltvm_pkg import test_runner as tr

        assert tr.coverage_note(172, 172, "test_999", ended=True) == ""

    def test_a_live_run_is_not_called_short(self) -> None:
        from ltvm_pkg import test_runner as tr

        assert tr.coverage_note(83, 172, "test_218", ended=False) == ""

    def test_no_total_means_no_claim(self) -> None:
        from ltvm_pkg import test_runner as tr

        assert tr.coverage_note(83, 0, "test_218", ended=True) == ""
