"""Every ltvm command shown in the docs must still parse.

The docs drifted far enough that a fresh agent following them ran
commands that no longer exist: `ltvm vm ensure`, `ltvm deploy --mount`,
`ltvm deploy --kernel`, `ltvm cluster deploy`.  Each had been retired or
renamed, and nothing failed at commit time -- only the reader failed,
later, with an argparse error and no idea which doc lied.

This walks the shipped Markdown, pulls every `ltvm ...` line out of the
```bash blocks, and feeds it to the real parser.  A renamed action or a
dropped option now breaks the build instead of the next reader.

Scope is deliberately the parser, not the runtime: this answers "can
this command line still be typed", not "does it do the right thing".
That is the failure mode the docs actually had.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import re
import shlex
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).parent.parent

# Docs a reader is pointed at.  Anything listed here is load-bearing;
# add a doc when it starts carrying commands.
DOCS = [
    "README.md",
    "CLAUDE.md",
    "docs/GETTING_STARTED.md",
    "docs/SOFTROCE_SETUP.md",
    "docs/SYSTEM_TEST_PLAN.md",
    "docs/IPV6.md",
]

# Notation that means "fill this in", not a literal argument.  A line
# carrying any of it is a reference entry, not a runnable command:
#   <name>        placeholder
#   a|b           alternation
#   [--cleanup]   optional
_PLACEHOLDER = re.compile(r"[<>\[\]|]")


def _load_ltvm() -> Any:
    """Import the `ltvm` entry point, which has no .py extension."""
    path = str(_REPO / "ltvm")
    loader = importlib.machinery.SourceFileLoader("ltvm_doccheck", path)
    spec = importlib.util.spec_from_loader("ltvm_doccheck", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


_ltvm = _load_ltvm()


def _bash_blocks(text: str) -> list[tuple[int, str]]:
    """Yield (1-based line number, line) for every line in a ```bash block.

    Only ```bash blocks are checked.  Bare ``` blocks hold command
    reference tables and sample output -- prose in a monospace font, not
    commands anybody is meant to paste.
    """
    out: list[tuple[int, str]] = []
    in_bash = False
    for n, line in enumerate(text.splitlines(), start=1):
        if line.startswith("```"):
            in_bash = line.strip() == "```bash"
            continue
        if in_bash:
            out.append((n, line))
    return out


def _strip_comment(line: str) -> str:
    """Drop a trailing `# ...` comment, respecting quotes."""
    lex = shlex.shlex(line, posix=True)
    lex.whitespace_split = True
    kept: list[str] = []
    try:
        for tok in lex:
            if tok.startswith("#"):
                break
            kept.append(tok)
    except ValueError:
        # Unbalanced quote across a continuation; caller skips it.
        return ""
    return shlex.join(kept)


def _commands(doc: str) -> list[tuple[int, list[str]]]:
    """Every runnable `ltvm ...` invocation in one doc, as argv."""
    text = (_REPO / doc).read_text()
    found: list[tuple[int, list[str]]] = []

    pending = ""
    pending_line = 0
    for n, raw in _bash_blocks(text):
        line = raw.strip()
        if pending:
            line = pending + " " + line
        else:
            pending_line = n
        if line.endswith("\\"):
            pending = line[:-1].strip()
            continue
        pending = ""

        if not line or _PLACEHOLDER.search(line):
            continue
        # Normalise the two ways the docs spell the entry point, and
        # the sudo the VM-lifecycle commands need.
        line = re.sub(r"^sudo\s+", "", line)
        line = re.sub(r"^\./ltvm\b", "ltvm", line)
        if not line.startswith("ltvm "):
            continue

        stripped = _strip_comment(line)
        if not stripped:
            continue
        argv = shlex.split(stripped)[1:]
        if argv:
            found.append((pending_line, argv))
    return found


def _all_commands() -> list[tuple[str, int, list[str]]]:
    return [(doc, n, argv) for doc in DOCS for n, argv in _commands(doc)]


def _parses(argv: list[str]) -> str | None:
    """Return None if the parser accepts argv, else the error text."""
    parser = _ltvm.build_parser()
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code:
            return err.getvalue().strip().splitlines()[-1]
    return None


class TestDocsAreExtractable:
    """Guard the extractor itself.

    A regex that silently matches nothing would make every other test
    in this file pass while checking no commands at all.
    """

    def test_every_doc_exists(self) -> None:
        missing = [d for d in DOCS if not (_REPO / d).is_file()]
        assert not missing, f"DOCS lists files that do not exist: {missing}"

    def test_each_doc_yields_commands(self) -> None:
        empty = [doc for doc in DOCS if not _commands(doc)]
        assert not empty, (
            f"No ltvm commands extracted from {empty}.  Either the doc "
            f"lost its ```bash blocks or the extractor stopped matching."
        )

    def test_placeholders_are_skipped(self) -> None:
        """Reference notation must not reach the parser."""
        assert _PLACEHOLDER.search("ltvm llmount <vm> [--cleanup]")
        assert not _PLACEHOLDER.search("ltvm llmount co1-mds")

    def test_a_retired_command_would_be_caught(self) -> None:
        """The check has teeth: the commands that prompted it now fail."""
        for argv in (
            ["vm", "ensure", "co1-single"],
            ["deploy", "co1", "--mount"],
            ["deploy", "co1", "--kernel", "5.14-rhel9.5"],
        ):
            assert _parses(argv) is not None, (
                f"ltvm {' '.join(argv)} parses, so this test can no "
                f"longer prove the doc check would catch it"
            )


class TestDocumentedCommandsParse:
    @pytest.mark.parametrize(
        "doc,line,argv",
        _all_commands(),
        ids=lambda v: v if isinstance(v, str) else None,
    )
    def test_command_parses(
        self, doc: str, line: int, argv: list[str]
    ) -> None:
        error = _parses(argv)
        assert error is None, (
            f"{doc}:{line} documents a command the CLI rejects:\n"
            f"    ltvm {shlex.join(argv)}\n"
            f"  {error}"
        )


class TestSuggestedAgentsIsGone:
    """SUGGESTED-AGENTS.md was a second copy of the canonical flow.

    It drifted: no `ltvm test`, no claim, no `--for-cluster`, and it
    still described `llmount --cleanup` as a plain llmountcleanup.sh
    wrapper.  Because CLAUDE.md told the reader to append it to their
    own instructions, the stale copy is what a fresh session actually
    read.  One canonical section, linked to, cannot drift from itself.
    """

    def test_file_is_not_reintroduced(self) -> None:
        assert not (_REPO / "SUGGESTED-AGENTS.md").exists(), (
            "SUGGESTED-AGENTS.md is back.  Point readers at the "
            "'One Way to Build, Deploy, Mount and Test' section of "
            "CLAUDE.md instead of copying it."
        )

    def test_nothing_still_points_at_it(self) -> None:
        dangling = [
            doc
            for doc in DOCS
            if "SUGGESTED-AGENTS" in (_REPO / doc).read_text()
        ]
        assert not dangling, (
            f"{dangling} still reference the deleted SUGGESTED-AGENTS.md"
        )


class TestWorkspaceEntryPoint:
    """The flow must also reach a session that never opens this repo.

    ltvm's own CLAUDE.md loads only when the cwd is inside ltvms/.  Work
    on Lustre itself happens in a sibling checkout, so the workspace
    CLAUDE.md one level up is the only file such a session reads.  When
    it named ltvms without a single command, every one of those sessions
    started by guessing.

    Skipped where that file does not exist -- it belongs to the
    surrounding workspace, not to this repo.
    """

    _WORKSPACE = _REPO.parent / "CLAUDE.md"

    @pytest.fixture(autouse=True)
    def _skip_without_workspace(self) -> None:
        if not self._WORKSPACE.is_file():
            pytest.skip(f"no workspace CLAUDE.md at {self._WORKSPACE}")

    def test_it_carries_the_four_verbs(self) -> None:
        text = self._WORKSPACE.read_text()
        missing = [
            v
            for v in ("build lustre", "deploy", "cluster llmount", "test")
            if f"ltvm {v}" not in text
        ]
        assert not missing, (
            f"{self._WORKSPACE} names ltvms but never shows {missing}; a "
            f"session working in a Lustre checkout reads this file and "
            f"nothing else from ltvm"
        )

    def test_it_says_to_use_for_cluster(self) -> None:
        text = self._WORKSPACE.read_text()
        assert "--for-cluster" in text, (
            f"{self._WORKSPACE} omits --for-cluster, so the flow it "
            f"teaches builds against the wrong kernel"
        )

    def test_its_commands_parse(self) -> None:
        bad = []
        for n, argv in _commands(str(self._WORKSPACE)):
            error = _parses(argv)
            if error:
                bad.append(f"{self._WORKSPACE}:{n}: ltvm {shlex.join(argv)}")
        assert not bad, "workspace CLAUDE.md documents dead commands:\n  " + (
            "\n  ".join(bad)
        )


class TestCanonicalFlowIsFindable:
    """The four verbs must appear together, in order, in one place.

    Scattered across sections, a reader assembles a flow that builds
    without --for-cluster or never runs the suite -- which is what the
    older docs taught.
    """

    _HEADING = "## One Way to Build, Deploy, Mount and Test"

    def test_claude_md_has_the_canonical_section(self) -> None:
        assert self._HEADING in (_REPO / "CLAUDE.md").read_text()

    def _section(self) -> str:
        text = (_REPO / "CLAUDE.md").read_text()
        return text.split(self._HEADING, 1)[1].split("\n## ", 1)[0]

    def test_the_section_shows_all_four_verbs_in_order(self) -> None:
        section = self._section()
        verbs = ("build lustre", "deploy", "cluster llmount", "test")
        missing = [v for v in verbs if f"ltvm {v}" in section]
        assert len(missing) == len(verbs), (
            f"canonical section is missing "
            f"{[v for v in verbs if f'ltvm {v}' not in section]}"
        )
        # Compare first appearance, so a section that merely mentions
        # all four out of sequence still fails.
        where = [section.index(f"ltvm {v}") for v in verbs]
        assert where == sorted(where), (
            f"canonical section shows the verbs out of order: "
            f"{[v for _, v in sorted(zip(where, verbs))]}; they must "
            f"appear in the order they are run"
        )

    def test_the_section_releases_the_claim(self) -> None:
        """Two sessions deploying to one cluster corrupt each other's
        runs silently.  deploy claims the nodes for an agent, so the
        flow an agent copies must end by releasing them."""
        section = self._section()
        assert "ltvm release" in section, (
            "canonical section never runs ltvm release"
        )

    def test_the_canonical_build_is_cluster_scoped(self) -> None:
        text = (_REPO / "CLAUDE.md").read_text()
        section = text.split(self._HEADING, 1)[1].split("\n## ", 1)[0]
        assert "--for-cluster" in section, (
            "the canonical build must use --for-cluster: a plain "
            "`build lustre <target>` builds against the target's "
            "default kernel, which is often not the cluster's"
        )
