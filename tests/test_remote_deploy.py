"""Remote hosts: send a Lustre tree, deploy it, mount it, run a suite.

The controller sends its Lustre tree to the host as a tar stream.
`serve` unpacks each stream into a new job directory under the home of
the dedicated user.  deploy-lustre, cluster deploy and the build verbs
then accept only a job tree as their Lustre tree.

The child host (exec://) receives real trees.  The in-process host runs
the verbs that need a VM, with the guest ssh and the build stubbed.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests import test_remote_lifecycle as _lifecycle
from tests.host_marks import needs_git
from tests.test_lustre_version import VERSION_GEN
from tests.test_remote_lifecycle import (
    OWNER_VM,
    REMOTE_OWNER,
    REMOTE_VM,
    _audit,
    _err,
)

child = _lifecycle.child
inproc = _lifecycle.inproc


def _git(tree: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(tree)]
        + list(args),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def lustre_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A small Lustre-like git checkout with a version tag.

    It has tracked files, an untracked file, ignored files, build
    output, ltvm staging and a stale LUSTRE-VERSION-FILE.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)
    tree = tmp_path / "controller" / "lustre-release"
    (tree / "lustre" / "tests").mkdir(parents=True)
    (tree / "lnet").mkdir()
    (tree / "configure.ac").write_text("AC_INIT\n")
    (tree / "lustre" / "tests" / "sanity.sh").write_text("#!/bin/bash\n")
    (tree / "lnet" / "lnet.c").write_text("int x;\n")
    (tree / "LUSTRE-VERSION-GEN").write_text(VERSION_GEN)
    (tree / ".gitignore").write_text("*.o\n/build-out/\n/LUSTRE-VERSION-FILE\n")
    _git(tree, "init", "-q")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", "base")
    _git(tree, "tag", "-a", "-m", "2.17.58", "2.17.58")
    (tree / "lnet" / "lnet.c").write_text("int x = 1;\n")
    _git(tree, "commit", "-q", "-am", "change")
    (tree / ".git" / "info" / "exclude").write_text("/.ltvm-*\n")
    (tree / "lnet" / "new-untracked.c").write_text("int new_sentinel;\n")
    (tree / "lnet" / "lnet.o").write_text("OBJECT-SENTINEL")
    (tree / "build-out").mkdir()
    (tree / "build-out" / "x.ko").write_text("KO-SENTINEL")
    (tree / ".ltvm-staging").mkdir()
    (tree / ".ltvm-staging" / "s").write_text("STAGING-SENTINEL")
    (tree / "LUSTRE-VERSION-FILE").write_text("LUSTRE_VERSION = 0.0.0_stale\n")
    return tree


def _files(root: Path) -> set[str]:
    return {
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() or p.is_symlink()
    }


def _version_gen(tree: Path) -> str:
    return subprocess.run(
        ["bash", "LUSTRE-VERSION-GEN"],
        cwd=tree,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@needs_git
class TestReceiveTree:
    def test_job_holds_the_git_files_and_no_build_output(
        self, child: SimpleNamespace, lustre_tree: Path
    ) -> None:
        from ltvm_pkg.remote import tree

        job = tree.send("t", lustre_tree)
        remote = Path(job.tree)
        assert remote.parent.parent == child.home / "jobs"
        assert _files(remote) == {
            ".gitignore",
            "LUSTRE-VERSION-FILE",
            "LUSTRE-VERSION-GEN",
            "configure.ac",
            "lnet/lnet.c",
            "lnet/new-untracked.c",
            "lustre/tests/sanity.sh",
        }
        assert (remote / "lnet" / "lnet.c").read_text() == "int x = 1;\n"
        assert _audit(child.home)[-1]["verb"] == "receive-tree"

    def test_remote_tree_gets_the_local_lustre_version(
        self, child: SimpleNamespace, lustre_tree: Path
    ) -> None:
        from ltvm_pkg.remote import tree

        (lustre_tree / "lnet" / "lnet.c").write_text("int x = 2;\n")
        (lustre_tree / "LUSTRE-VERSION-FILE").unlink()
        local = _version_gen(lustre_tree)
        assert local.startswith("2.17.58_1_g") and local.endswith("_dirty")
        job = tree.send("t", lustre_tree)
        assert _version_gen(Path(job.tree)) == local

    def test_each_stream_gets_a_new_job(
        self, child: SimpleNamespace, lustre_tree: Path
    ) -> None:
        from ltvm_pkg.remote import tree

        first = tree.send("t", lustre_tree)
        second = tree.send("t", lustre_tree)
        assert first.job != second.job
        assert Path(first.tree).is_dir() and Path(second.tree).is_dir()


def _tar(*members: tuple[str, str, str]) -> bytes:
    """A gzip tar stream.  Each member is (kind, name, data or target)."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for kind, name, value in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(value)
                tar.addfile(info, io.BytesIO(value.encode()))
                continue
            info.type = {
                "sym": tarfile.SYMTYPE,
                "hard": tarfile.LNKTYPE,
                "fifo": tarfile.FIFOTYPE,
            }[kind]
            info.linkname = value
            tar.addfile(info)
    return buf.getvalue()


def _receive(data: bytes) -> Any:
    from ltvm_pkg.remote import hosts

    return hosts.transport_for("t").request(["receive-tree"], data)


class TestUnsafeStreams:
    @pytest.mark.parametrize(
        "members",
        [
            [("file", "../escape-sentinel", "x")],
            [("file", "a/../../escape-sentinel", "x")],
            [("file", "/tmp/escape-sentinel", "x")],
            [("sym", "lnk", "/etc")],
            [("sym", "lnk", "../../..")],
            [("sym", "a/lnk", "../../x")],
            [("hard", "h", "/etc/passwd")],
            [("fifo", "f", "")],
            [("file", "a", "x"), ("sym", "a", "b")],
            # Each link passes a check of its member name.  Through the
            # earlier links, tree/q resolves to the serve home.
            [
                ("sym", "a", "."),
                ("sym", "a/l", ".."),
                ("sym", "a/p", "l/.."),
                ("sym", "a/q", "p/.."),
            ],
            # A later link moves an earlier link out of the tree.
            [("sym", "up", "d/.."), ("sym", "d", ".")],
            # git on the host reads .git/config, which can run a program.
            [("file", ".git/config", "[core]\n\tfsmonitor = x\n")],
            [("file", "lnet/.git", "gitdir: /tmp\n")],
        ],
    )
    def test_unsafe_stream_is_refused_and_leaves_no_job(
        self, child: SimpleNamespace, members: list[tuple[str, str, str]]
    ) -> None:
        resp = _receive(_tar(("file", "ok", "fine"), *members))
        assert resp.error is not None
        assert resp.error.code == "path_refused"
        assert list((child.home / "jobs").iterdir()) == []
        assert not list(child.tmp.rglob("escape-sentinel"))
        assert _audit(child.home)[-1]["result"] == "path_refused"

    def test_link_inside_the_tree_is_kept(self, child: SimpleNamespace) -> None:
        resp = _receive(
            _tar(("file", "lustre/a.c", "a"), ("sym", "lustre/b.c", "a.c"))
        )
        assert resp.error is None
        tree = Path(json.loads(resp.stdout)["tree"])
        assert os.readlink(tree / "lustre" / "b.c") == "a.c"
        assert (tree / "lustre" / "b.c").read_text() == "a"

    def test_link_up_to_a_place_inside_the_tree_is_kept(
        self, child: SimpleNamespace
    ) -> None:
        resp = _receive(
            _tar(
                ("file", "lnet/a.c", "a"),
                ("sym", "lustre/b.c", "../lnet/a.c"),
                ("sym", "here", "."),
            )
        )
        assert resp.error is None
        tree = Path(json.loads(resp.stdout)["tree"])
        assert (tree / "lustre" / "b.c").read_text() == "a"

    def test_garbage_stream_is_a_bad_request(
        self, child: SimpleNamespace
    ) -> None:
        resp = _receive(b"not a tar stream")
        assert resp.error is not None
        assert resp.error.code == "bad_request"
        assert list((child.home / "jobs").iterdir()) == []

    def test_receive_tree_takes_no_arguments(
        self, child: SimpleNamespace
    ) -> None:
        from ltvm_pkg.remote import hosts

        resp = hosts.transport_for("t").request(
            ["receive-tree", "/etc"], _tar(("file", "ok", "x"))
        )
        assert resp.error is not None
        assert resp.error.code == "bad_request"


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make this process run as serve does for a remote request."""
    from ltvm_pkg.remote import scope

    monkeypatch.setenv("LTVM_OWNER_ID", scope.owner_id("SHA256:k"))


def _hostile_tree(tmp_path: Path) -> tuple[Path, Path]:
    """A job-like tree whose git and version script make a sentinel.

    A container build can add a .git to a job tree.  Return (tree,
    sentinel).
    """
    tree = tmp_path / "tree"
    tree.mkdir()
    sentinel = tmp_path / "host-code-sentinel"
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    with (tree / ".git" / "config").open("a") as f:
        f.write(f"[core]\n\tfsmonitor = touch {sentinel}; false\n")
    gen = tree / "LUSTRE-VERSION-GEN"
    gen.write_text(f"#!/bin/sh\ntouch {sentinel}\necho 9.9.9\n")
    gen.chmod(0o755)
    (tree / "LUSTRE-VERSION-FILE").write_text("LUSTRE_VERSION = 2.17.58\n")
    return tree, sentinel


@needs_git
class TestJobTreeOnTheHost:
    """Under serve, the host runs no code from a job tree."""

    def test_pin_version_runs_no_git(
        self, tmp_path: Path, served: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg.lustre_version import pin_version

        tree, sentinel = _hostile_tree(tmp_path)
        monkeypatch.setattr(
            subprocess, "run", _forbid_tree_commands(subprocess.run, tree)
        )
        pin = pin_version(tree)
        assert not sentinel.exists()
        assert (pin.version, pin.source) == ("2.17.58", "file")

    def test_pin_version_does_not_write_through_a_link(
        self, tmp_path: Path, served: None
    ) -> None:
        from ltvm_pkg.lustre_version import pin_version

        tree, _ = _hostile_tree(tmp_path)
        outside = tmp_path / "outside"
        outside.write_text("OUTSIDE\n")
        (tree / "LUSTRE-VERSION-FILE").unlink()
        (tree / "LUSTRE-VERSION-FILE").symlink_to(outside)
        pin = pin_version(tree)
        assert outside.read_text() == "OUTSIDE\n"
        assert pin.source == "default"

    def test_deploy_source_check_runs_no_git(
        self, tmp_path: Path, served: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg import staging_sources

        tree, sentinel = _hostile_tree(tmp_path)
        monkeypatch.setattr(
            subprocess, "run", _forbid_tree_commands(subprocess.run, tree)
        )
        assert staging_sources._source_files(tree) is None
        assert not sentinel.exists()

    def test_header_version_runs_no_tree_script(
        self, tmp_path: Path, served: None
    ) -> None:
        from ltvm_pkg.cli.util import _lustre_tree_version

        tree, sentinel = _hostile_tree(tmp_path)
        (tree / "LUSTRE-VERSION-FILE").unlink()
        assert _lustre_tree_version(tree) is None
        assert not sentinel.exists()

    def test_build_lock_adds_no_git_exclude(
        self, tmp_path: Path, served: None
    ) -> None:
        from ltvm_pkg.lustre_build import _tree_build_lock

        tree, _ = _hostile_tree(tmp_path)
        exclude = tree / ".git" / "info" / "exclude"
        before = exclude.read_text()
        with _tree_build_lock(tree):
            pass
        assert exclude.read_text() == before

    def test_build_lock_is_not_a_link(
        self, tmp_path: Path, served: None
    ) -> None:
        from ltvm_pkg.lustre_build import _tree_build_lock

        tree, _ = _hostile_tree(tmp_path)
        outside = tmp_path / "outside"
        (tree / ".ltvm-build-lock").symlink_to(outside)
        with pytest.raises(OSError):
            with _tree_build_lock(tree):
                pass
        assert not outside.exists()

    @pytest.mark.parametrize("link", [".ltvm-staging", ".ltvm-staging/rocky9"])
    def test_staging_under_a_link_is_refused(
        self, tmp_path: Path, served: None, link: str
    ) -> None:
        from ltvm_pkg.lustre_build import staging_path

        tree, _ = _hostile_tree(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (tree / link).parent.mkdir(parents=True, exist_ok=True)
        (tree / link).symlink_to(outside)
        with pytest.raises(RuntimeError, match="link"):
            staging_path(tree, "rocky9", kernel="k")

    def test_staging_link_out_of_the_staging_is_refused(
        self, tmp_path: Path, served: None
    ) -> None:
        from ltvm_pkg.lustre_build import staging_path

        tree, _ = _hostile_tree(tmp_path)
        staging = staging_path(tree, "rocky9", kernel="k")
        (staging / "usr" / "lib64").mkdir(parents=True)
        (staging / "usr" / "lib64" / "liblustreapi.so").symlink_to(
            "liblustreapi.so.1"
        )
        assert staging_path(tree, "rocky9", kernel="k") == staging
        (staging / "etc").symlink_to(tmp_path)
        with pytest.raises(RuntimeError, match="link"):
            staging_path(tree, "rocky9", kernel="k")

    def test_local_staging_may_be_a_link(self, tmp_path: Path) -> None:
        """Negative control: a local tree keeps its staging link."""
        from ltvm_pkg.lustre_build import staging_path

        tree, _ = _hostile_tree(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (tree / ".ltvm-staging").symlink_to(outside)
        assert staging_path(
            tree, "rocky9", kernel="k"
        ).parent.parent.parent == (tree / ".ltvm-staging")

    def test_tree_file_write_does_not_follow_a_link(
        self, tmp_path: Path
    ) -> None:
        from ltvm_pkg.lustre_version import write_tree_file

        outside = tmp_path / "outside"
        outside.write_text("OUTSIDE\n")
        link = tmp_path / ".ltvm-kernel-x"
        link.symlink_to(outside)
        with pytest.raises(OSError):
            write_tree_file(link, "k\n")
        assert outside.read_text() == "OUTSIDE\n"
        write_tree_file(tmp_path / "new", "k\n")
        assert (tmp_path / "new").read_text() == "k\n"

    def test_local_build_still_uses_git(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative control: the same tree runs git when not served."""
        from ltvm_pkg.lustre_version import pin_version

        monkeypatch.delenv("LTVM_OWNER_ID", raising=False)
        tree, sentinel = _hostile_tree(tmp_path)
        subprocess.run(
            [
                "git",
                "-C",
                str(tree),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "x",
            ],
            check=True,
        )
        pin_version(tree)
        assert sentinel.exists()


def _forbid_tree_commands(real: Any, tree: Path) -> Any:
    def run(cmd: Any, *a: Any, **kw: Any) -> Any:
        assert str(tree) not in " ".join(map(str, cmd)), cmd
        assert str(kw.get("cwd", "")) != str(tree), cmd
        return real(cmd, *a, **kw)

    return run


class TestReceiveTreeDiskCap:
    """receive-tree counts against max_disk_gb, before and in the unpack."""

    def _cap(self, child: SimpleNamespace, gb: float) -> None:
        from tests.remote_helpers import write_serve_toml

        write_serve_toml(child.home, f"max_disk_gb = {gb!r}\n")

    def test_a_stream_above_the_cap_is_refused_and_leaves_no_job(
        self, child: SimpleNamespace
    ) -> None:
        self._cap(child, 1 / 1024)
        resp = _receive(_tar(("file", "big.o", "x" * (2 << 20))))
        assert resp.error is not None
        assert resp.error.code == "over_cap"
        assert list((child.home / "jobs").iterdir()) == []
        assert _audit(child.home)[-1]["result"] == "over_cap"

    def test_a_full_disk_refuses_the_stream(
        self, child: SimpleNamespace
    ) -> None:
        self._cap(child, 1 / 1024)
        full = child.home / "jobs" / "j-old" / "tree"
        full.mkdir(parents=True)
        (full / "big.o").write_bytes(b"x" * (2 << 20))
        resp = _receive(_tar(("file", "ok", "x")))
        assert resp.error is not None
        assert resp.error.code == "over_cap"

    def test_a_stream_that_fits_is_kept(self, child: SimpleNamespace) -> None:
        """Negative control: a stream below the cap makes a job."""
        self._cap(child, 1 / 1024)
        resp = _receive(_tar(("file", "small.c", "x" * (64 << 10))))
        assert resp.error is None


def _send(argv: list[str]) -> Any:
    from ltvm_pkg.remote import hosts

    return hosts.transport_for("t").request(argv)


@pytest.fixture
def job(child: SimpleNamespace) -> Path:
    """A job tree that receive-tree made on the child host."""
    resp = _receive(_tar(("file", "configure.ac", "AC_INIT\n")))
    assert resp.error is None
    return Path(json.loads(resp.stdout)["tree"])


class TestBuildNeedsATree:
    """A build verb that uses a Lustre tree must name a job tree."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["build", "all", "rocky9"],
            ["build", "kernel", "rocky9"],
            ["build", "image", "rocky9"],
        ],
    )
    def test_no_tree_is_refused(
        self, child: SimpleNamespace, argv: list[str]
    ) -> None:
        resp = _send(argv)
        assert resp.error is not None
        assert resp.error.code == "path_refused"
        assert "--lustre-tree" in resp.error.message

    @pytest.mark.parametrize(
        "argv",
        [
            ["build", "image", "rocky9", "--no-lustre"],
            ["build", "kernel", "ubuntu2404"],
            ["build", "kernel", "mainline"],
            ["build", "container", "rocky9"],
            ["build", "status"],
        ],
    )
    def test_a_verb_with_no_tree_passes(
        self, ltvm: Any, tmp_path: Path, argv: list[str]
    ) -> None:
        """Negative control: the check refuses only verbs that use a tree."""
        from ltvm_pkg.remote import scope

        args = ltvm._parse_request(argv)
        assert scope.check(argv, args, home=tmp_path) == argv


class TestPathArguments:
    @pytest.mark.parametrize(
        "make",
        [
            lambda job, home: "/etc",
            lambda job, home: str(home),
            lambda job, home: str(job.parent),
            lambda job, home: f"{job}/..",
            lambda job, home: f"{job}/../../{job.parent.name}/tree",
            lambda job, home: f"{job}/.",
            lambda job, home: f"{job}/configure.ac",
            lambda job, home: os.path.relpath(job, home),
            lambda job, home: "",
        ],
        ids=[
            "host-path",
            "home",
            "job-dir",
            "dotdot",
            "dotdot-back-in",
            "dot",
            "file-in-tree",
            "relative",
            "empty",
        ],
    )
    def test_deploy_tree_outside_a_job_tree_is_refused(
        self, child: SimpleNamespace, job: Path, make: Any
    ) -> None:
        value = make(job, child.home)
        resp = _send(["deploy-lustre", REMOTE_VM, "--lustre-tree", value])
        assert resp.error is not None
        assert resp.error.code == "path_refused"
        assert _audit(child.home)[-1]["result"] == "path_refused"

    def test_symlink_to_a_job_tree_is_refused(
        self, child: SimpleNamespace, job: Path
    ) -> None:
        link = child.home / "tree-link"
        link.symlink_to(job)
        resp = _send(["deploy-lustre", REMOTE_VM, "--lustre-tree", str(link)])
        assert resp.error is not None
        assert resp.error.code == "path_refused"

    def test_job_that_is_a_symlink_out_is_refused(
        self, child: SimpleNamespace, job: Path
    ) -> None:
        outside = child.tmp / "outside-sentinel"
        (outside / "tree").mkdir(parents=True)
        planted = job.parent.parent / "j-20260101-000000-deadbeef"
        planted.symlink_to(outside)
        resp = _send(
            ["deploy-lustre", REMOTE_VM, "--lustre-tree", str(planted / "tree")]
        )
        assert resp.error is not None
        assert resp.error.code == "path_refused"

    def test_deploy_needs_a_tree(self, child: SimpleNamespace) -> None:
        resp = _send(["deploy-lustre", REMOTE_VM])
        assert resp.error is not None
        assert resp.error.code == "path_refused"

    @pytest.mark.parametrize(
        "argv",
        [
            ["deploy-lustre", REMOTE_VM, "--build", "/etc"],
            ["deploy-lustre", REMOTE_VM, "--zfs-source", "/etc"],
            ["build", "kernel", "rocky9", "--lustre-tree", "/etc"],
            [
                "suite",
                "run",
                REMOTE_VM,
                "sanity",
                "--wait",
                "--results-dir",
                "/tmp/x",
            ],
        ],
    )
    def test_other_host_paths_are_refused(
        self, child: SimpleNamespace, job: Path, argv: list[str]
    ) -> None:
        if "--zfs-source" in argv:
            argv = [*argv, "--lustre-tree", str(job)]
        resp = _send(argv)
        assert resp.error is not None
        assert resp.error.code == "path_refused"

    def test_cluster_deploy_of_the_cwd_is_refused(
        self, child: SimpleNamespace
    ) -> None:
        from tests.test_remote_clusters import (
            REMOTE_CL,
            REMOTE_NODE,
            _save_cluster,
        )

        _save_cluster(child.sockets, REMOTE_CL, REMOTE_NODE, REMOTE_OWNER)
        resp = _send(["cluster", "deploy", REMOTE_CL])
        assert resp.error is not None
        assert resp.error.code == "path_refused"

    @pytest.mark.parametrize(
        "argv",
        [
            ["llmount", OWNER_VM],
            ["llumount", OWNER_VM],
            ["vm", "exec", OWNER_VM, "true"],
            ["suite", "run", OWNER_VM, "sanity"],
            ["suite", "status", OWNER_VM],
            ["suite", "status", "no-such-vm"],
        ],
    )
    def test_owner_vm_is_not_found(
        self, child: SimpleNamespace, argv: list[str]
    ) -> None:
        resp = _send(argv)
        assert resp.error is not None
        assert resp.error.code == "vm_not_found"

    def test_deploy_to_an_owner_vm_is_not_found(
        self, child: SimpleNamespace, job: Path
    ) -> None:
        resp = _send(["deploy-lustre", OWNER_VM, "--lustre-tree", str(job)])
        assert resp.error is not None
        assert resp.error.code == "vm_not_found"

    def test_interactive_cluster_ssh_stays_refused(
        self, child: SimpleNamespace
    ) -> None:
        resp = _send(["cluster", "ssh", "c", "mds"])
        assert resp.error is not None
        assert resp.error.code == "verb_refused"


class TestPodmanSafety:
    @pytest.fixture
    def linux(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        from ltvm_pkg.remote import podman

        answers: list[str] = []
        monkeypatch.setattr(podman, "_platform", lambda: "linux")
        monkeypatch.setattr(podman, "_rootless", lambda: answers[0])
        return answers

    @pytest.mark.parametrize(
        "argv",
        [
            # `build all` with no tree gets path_refused first.
            ["build", "container", "nosuch-x9"],
            ["build", "kernel", "nosuch-x9"],
            ["build", "image", "nosuch-x9", "--no-lustre"],
            ["build", "zfs", "nosuch-x9"],
            ["build", "mofed-kmods", "nosuch-x9"],
        ],
    )
    @pytest.mark.parametrize("answer", ["false", "", "Error: no podman"])
    def test_rootful_podman_refuses_a_build(
        self,
        inproc: SimpleNamespace,
        linux: list[str],
        ltvm: Any,
        capsys: Any,
        argv: list[str],
        answer: str,
    ) -> None:
        linux.append(answer)
        rc = ltvm.run(["--host", "t", *argv, "--json"])
        assert rc == 1
        assert _err(capsys.readouterr().err)["code"] == "podman_unsafe"
        assert _audit(inproc.home)[-1]["result"] == "podman_unsafe"

    def test_rootless_podman_reaches_the_verb(
        self, inproc: SimpleNamespace, linux: list[str], ltvm: Any, capsys: Any
    ) -> None:
        linux.append("true")
        rc = ltvm.run(["--host", "t", "build", "container", "nosuch-x9"])
        assert rc != 0
        assert "nosuch-x9" in capsys.readouterr().err
        assert _audit(inproc.home)[-1]["result"] == f"exit {rc}"

    def test_macos_podman_is_vm_backed(
        self,
        inproc: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        ltvm: Any,
        capsys: Any,
    ) -> None:
        from ltvm_pkg.remote import podman

        def no_probe() -> str:
            raise AssertionError("podman info ran on macOS")

        monkeypatch.setattr(podman, "_platform", lambda: "darwin")
        monkeypatch.setattr(podman, "_rootless", no_probe)
        rc = ltvm.run(["--host", "t", "build", "container", "nosuch-x9"])
        capsys.readouterr()
        assert _audit(inproc.home)[-1]["result"] == f"exit {rc}"

    def test_status_needs_no_podman(
        self, inproc: SimpleNamespace, linux: list[str], ltvm: Any, capsys: Any
    ) -> None:
        linux.append("false")
        rc = ltvm.run(["--host", "t", "build", "status", "--json"])
        capsys.readouterr()
        assert rc == 0


# ── deploy, mount, suite and exec on the in-process host ──


@pytest.fixture
def ihost(
    inproc: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """The in-process host, with rootless podman and a remote cluster."""
    from ltvm_pkg import vm_cluster
    from ltvm_pkg.remote import podman
    from tests.test_remote_clusters import REMOTE_CL, REMOTE_NODE, _save_cluster

    monkeypatch.setattr(podman, "_platform", lambda: "linux")
    monkeypatch.setattr(podman, "_rootless", lambda: "true")
    monkeypatch.setattr(vm_cluster, "SOCKETS", inproc.sockets)
    _save_cluster(inproc.sockets, REMOTE_CL, REMOTE_NODE, REMOTE_OWNER)
    inproc.cluster = REMOTE_CL
    inproc.node = REMOTE_NODE
    return inproc


class _Verb:
    """Stands in for a verb handler.  Keeps the args and the cwd."""

    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
        self.calls: list[tuple[Any, str]] = []

    def __call__(self, args: Any) -> int:
        self.calls.append((args, os.getcwd()))
        return self.rc


@needs_git
class TestDeploy:
    def test_deploy_sends_the_tree_and_builds_the_job(
        self,
        ihost: SimpleNamespace,
        lustre_tree: Path,
        ltvm: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: Any,
    ) -> None:
        verb = _Verb()
        monkeypatch.setattr(ltvm, "cmd_deploy", verb)
        rc = ltvm.run(
            ["--host", "t", "deploy-lustre", f"t/{REMOTE_VM}"]
            + ["--lustre-tree", str(lustre_tree), "--mount"]
        )
        capsys.readouterr()
        assert rc == 0
        [(args, _)] = verb.calls
        assert args.vm == REMOTE_VM
        assert args.mount is True
        tree = Path(args.lustre_tree)
        assert tree.parent.parent == (ihost.home / "jobs").resolve()
        assert (tree / "lnet" / "new-untracked.c").exists()
        assert not (tree / "lnet" / "lnet.o").exists()
        assert str(lustre_tree) not in _audit(ihost.home)[-1]["args"]

    def test_deploy_from_the_cwd_sends_the_cwd(
        self,
        ihost: SimpleNamespace,
        lustre_tree: Path,
        ltvm: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: Any,
    ) -> None:
        verb = _Verb()
        monkeypatch.setattr(ltvm, "cmd_deploy", verb)
        monkeypatch.chdir(lustre_tree)
        rc = ltvm.run(["--host", "t", "deploy-lustre", REMOTE_VM])
        capsys.readouterr()
        assert rc == 0
        [(args, _)] = verb.calls
        assert (Path(args.lustre_tree) / "configure.ac").exists()

    def test_build_alias_sends_the_tree(
        self,
        ihost: SimpleNamespace,
        lustre_tree: Path,
        ltvm: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: Any,
    ) -> None:
        verb = _Verb()
        monkeypatch.setattr(ltvm, "cmd_deploy", verb)
        rc = ltvm.run(
            [
                "--host",
                "t",
                "deploy-lustre",
                REMOTE_VM,
                "--build",
                str(lustre_tree),
            ]
        )
        capsys.readouterr()
        assert rc == 0
        [(args, _)] = verb.calls
        assert Path(args.lustre_tree).name == "tree"

    def test_cluster_deploy_sends_the_tree(
        self,
        ihost: SimpleNamespace,
        lustre_tree: Path,
        ltvm: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: Any,
    ) -> None:
        verb = _Verb()
        monkeypatch.setattr(ltvm, "cmd_cluster_deploy", verb)
        rc = ltvm.run(
            ["--host", "t", "cluster", "deploy", f"t/{ihost.cluster}"]
            + ["--build", str(lustre_tree), "--mount"]
        )
        capsys.readouterr()
        assert rc == 0
        [(args, _)] = verb.calls
        assert args.name == ihost.cluster
        assert (Path(args.lustre_source) / "lnet" / "lnet.c").exists()

    def test_build_kernel_sends_its_lustre_tree(
        self,
        ihost: SimpleNamespace,
        lustre_tree: Path,
        ltvm: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: Any,
    ) -> None:
        verb = _Verb()
        monkeypatch.setattr(ltvm, "cmd_build_kernel", verb)
        rc = ltvm.run(
            [
                "--host",
                "t",
                "build",
                "kernel",
                "rocky9",
                f"--lustre-tree={lustre_tree}",
            ]
        )
        capsys.readouterr()
        assert rc == 0
        [(args, _)] = verb.calls
        assert (Path(args.lustre_tree) / "configure.ac").exists()

    def test_a_tree_that_is_not_git_is_refused_here(
        self,
        ihost: SimpleNamespace,
        tmp_path: Path,
        ltvm: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: Any,
    ) -> None:
        verb = _Verb()
        monkeypatch.setattr(ltvm, "cmd_deploy", verb)
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        rc = ltvm.run(
            [
                "--host",
                "t",
                "deploy-lustre",
                REMOTE_VM,
                "--json",
                "--lustre-tree",
                str(plain),
            ]
        )
        assert rc == 1
        assert _err(capsys.readouterr().err)["code"] == "bad_request"
        assert verb.calls == []


class TestMountSuiteExec:
    @pytest.mark.parametrize("verb", ["llmount", "llumount"])
    def test_mount_verbs_reach_the_vm(
        self,
        ihost: SimpleNamespace,
        ltvm: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: Any,
        verb: str,
    ) -> None:
        handler = _Verb()
        monkeypatch.setattr(ltvm, "cmd_llmount", handler)
        rc = ltvm.run(["--host", "t", verb, f"t/{REMOTE_VM}"])
        capsys.readouterr()
        assert rc == 0
        [(args, _)] = handler.calls
        assert args.vm == REMOTE_VM
        assert args.cleanup is (verb == "llumount")

    @pytest.mark.parametrize("verb", ["llmount", "llumount"])
    def test_cluster_mount_verbs_reach_the_cluster(
        self,
        ihost: SimpleNamespace,
        ltvm: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: Any,
        verb: str,
    ) -> None:
        handler = _Verb()
        monkeypatch.setattr(ltvm, "cmd_cluster_llmount", handler)
        rc = ltvm.run(["--host", "t", "cluster", verb, f"t/{ihost.cluster}"])
        capsys.readouterr()
        assert rc == 0
        [(args, _)] = handler.calls
        assert args.name == ihost.cluster

    def test_suite_run_puts_results_in_the_vm_state(
        self,
        ihost: SimpleNamespace,
        ltvm: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: Any,
    ) -> None:
        from ltvm_pkg.remote import scope

        handler = _Verb()
        monkeypatch.setattr(ltvm, "cmd_suite_run", handler)
        before = os.getcwd()
        rc = ltvm.run(
            [
                "--host",
                "t",
                "suite",
                "run",
                f"t/{REMOTE_VM}",
                "sanity",
                "--wait",
            ]
        )
        capsys.readouterr()
        assert rc == 0
        [(args, cwd)] = handler.calls
        assert args.target == REMOTE_VM
        assert Path(cwd) == scope.suite_dir().resolve()
        assert os.getcwd() == before

    def test_suite_status_of_a_remote_cluster(
        self,
        ihost: SimpleNamespace,
        ltvm: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: Any,
    ) -> None:
        from ltvm_pkg import suite_run

        monkeypatch.setattr(suite_run, "_require_up", lambda *a: None)
        monkeypatch.setattr(suite_run, "query", lambda *a: [])
        rc = ltvm.run(
            ["--host", "t", "suite", "status", f"t/{ihost.cluster}", "--json"]
        )
        data = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert data["runs"] == []
        assert data["node"] == ihost.node

    def test_vm_exec_runs_in_the_guest(
        self,
        ihost: SimpleNamespace,
        ltvm: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: Any,
    ) -> None:
        from ltvm_pkg import vm_commands

        ran: list[str] = []

        def fake_ssh(ip: str, command: str, **kw: Any) -> Any:
            ran.append(command)
            return subprocess.CompletedProcess([], 0, "guest-sentinel-k2\n", "")

        monkeypatch.setattr(vm_commands, "run_ssh", fake_ssh)
        monkeypatch.setattr(vm_commands, "is_running", lambda vm: True)
        rc = ltvm.run(
            ["--host", "t", "vm", "exec", "--json", f"t/{REMOTE_VM}"]
            + ["lctl", "dl"]
        )
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert ran == ["lctl dl"]
        assert "guest-sentinel-k2" in out["stdout"]

    def test_cluster_exec_headers_name_the_host(
        self,
        ihost: SimpleNamespace,
        ltvm: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: Any,
    ) -> None:
        from ltvm_pkg import vm_cluster
        from ltvm_pkg.vm_state import ClusterInfo, VMInfo

        second = f"{ihost.node}-2"
        VMInfo(name=second, ip="192.168.100.79", owner_id=REMOTE_OWNER).save()
        cl = ClusterInfo.load(ihost.cluster)
        cl.nodes.append({"name": second, "roles": ["mgs", "mds"]})
        cl.save()
        done = subprocess.CompletedProcess
        monkeypatch.setattr(
            vm_cluster,
            "run_ssh",
            lambda ip, cmd, **kw: done(
                [], 1 if ip.endswith(".79") else 0, f"out-{ip}\n", ""
            ),
        )
        rc = ltvm.run(
            [
                "--host",
                "t",
                "cluster",
                "exec",
                f"t/{ihost.cluster}",
                "mds",
                "uptime",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 1
        assert f"--- t/{ihost.node} ---\nout-192.168.100.77\n" in out
        assert f"--- t/{second} ---\nout-192.168.100.79\n" in out

    def test_cluster_exec_json_names_the_host(
        self,
        ihost: SimpleNamespace,
        ltvm: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: Any,
    ) -> None:
        from ltvm_pkg import vm_cluster

        done = subprocess.CompletedProcess
        monkeypatch.setattr(
            vm_cluster, "run_ssh", lambda ip, cmd, **kw: done([], 0, "x\n", "")
        )
        rc = ltvm.run(
            [
                "--host",
                "t",
                "cluster",
                "exec",
                "--json",
                ihost.cluster,
                "mds",
                "uptime",
                "--json",
            ]
        )
        data = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert data["cluster"] == f"t/{ihost.cluster}"
        assert data["nodes"][0]["node"] == f"t/{ihost.node}"
        # The --json after the role is part of the guest command.
        assert data["command"] == "uptime --json"


class TestSshTimeout:
    @pytest.fixture
    def ssh_host(self, tmp_path: Path) -> Any:
        from ltvm_pkg.remote.hosts import Host
        from ltvm_pkg.remote.ssh import SshTransport

        key = tmp_path / "k"
        Path(str(key) + ".known_hosts").write_text("pinned\n")
        return SshTransport(
            Host(
                name="s",
                url="ssh://ltvm@192.0.2.9",
                key=str(key),
                hostkey="SHA256:" + "A" * 43,
            )
        )

    def test_a_hung_request_times_out(
        self, ssh_host: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg.remote import ssh

        seen: dict[str, Any] = {}

        def hung(argv: list[str], **kw: Any) -> Any:
            seen.update(kw)
            raise subprocess.TimeoutExpired(argv, kw["timeout"])

        monkeypatch.setattr(ssh, "run", hung)
        monkeypatch.setenv("LTVM_REMOTE_TIMEOUT", "90")
        resp = ssh_host.request(["suite", "run", "x", "sanity", "--wait"])
        assert resp.error is not None
        assert resp.error.code == "timed_out"
        assert seen["timeout"] == 90.0

    @pytest.mark.parametrize(
        ("value", "limit"),
        [("", 8 * 3600.0), ("0", None), ("x", 8 * 3600.0), ("30", 30.0)],
    )
    def test_timeout_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, value: str, limit: Any
    ) -> None:
        from ltvm_pkg.remote import ssh

        monkeypatch.setenv("LTVM_REMOTE_TIMEOUT", value)
        assert ssh.request_timeout() == limit

    def test_keepalives_stop_a_dead_connection(self, ssh_host: Any) -> None:
        argv = ssh_host.ssh_argv("list")
        assert "ServerAliveInterval=30" in argv
        assert "ServerAliveCountMax=4" in argv

    def test_tree_stream_goes_after_the_handshake(
        self, ssh_host: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg.remote import ssh

        seen: dict[str, Any] = {}

        def capture(argv: list[str], **kw: Any) -> Any:
            seen.update(kw)
            return subprocess.CompletedProcess(argv, 255, b"", b"refused")

        monkeypatch.setattr(ssh, "run", capture)
        ssh_host.request(["receive-tree"], b"TAR-SENTINEL")
        hello, _, data = seen["input"].partition(b"\n")
        assert json.loads(hello)["protocol"] >= 1
        assert data == b"TAR-SENTINEL"
