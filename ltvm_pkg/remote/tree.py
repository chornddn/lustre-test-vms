"""Send a local Lustre tree to a host (the controller side).

The verbs that build Lustre take a local tree.  With --host, the
controller first sends the tree to the host with `receive-tree`.  Then
it gives the verb the path of the job tree on the host in place of the
local path.

The stream is a gzip tar of these files:

- the files that git tracks, and the untracked files that git does not
  ignore (`git ls-files --cached --others --exclude-standard`).  So the
  build output, the ltvm staging and the other ignored files stay here.
- LUSTRE-VERSION-FILE with the `git describe` result of the local tree.
  The host gets no .git (the .git of a worktree points outside the
  tree), so LUSTRE-VERSION-GEN on the host reads this file.  The build
  on the host then has the same Lustre version as a local build.

Each request sends the full tree.  The host keeps no object cache.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

from ltvm_pkg.lustre_version import VERSION_FILE, git_version

from .protocol import RemoteError

RECEIVE_TREE = "receive-tree"
TREE_OPTION = "--lustre-tree"


@dataclass(frozen=True)
class Job:
    job: str
    tree: str


def _refuse(tree: Path, why: str) -> RemoteError:
    return RemoteError(
        "bad_request",
        f"cannot send the Lustre tree {tree}: {why}",
        hint="Give --lustre-tree a git checkout of Lustre.",
    )


def file_list(tree: Path) -> list[str]:
    """The relative paths that the stream holds, without the version."""
    try:
        r = subprocess.run(
            [
                "git",
                "-c",
                "safe.directory=*",
                "-C",
                str(tree),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except OSError as e:
        raise _refuse(tree, f"cannot run git: {e}") from None
    if r.returncode != 0:
        said = r.stderr.decode(errors="replace").strip().splitlines()
        raise _refuse(tree, said[-1] if said else "git ls-files failed")
    names = sorted({n for n in r.stdout.decode().split("\0") if n})
    # A deleted file is still in the index, and a submodule is a
    # directory.  Send neither.
    return [
        n
        for n in names
        if n != VERSION_FILE
        and os.path.lexists(tree / n)
        and not (tree / n).is_dir()
    ]


def _clean(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def make_stream(tree: Path) -> bytes:
    """The tar stream of tree, as receive-tree reads it."""
    tree = tree.resolve()
    if not tree.is_dir():
        raise _refuse(tree, "it is not a directory")
    names = file_list(tree)
    version, _ = git_version(tree)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=1) as tar:
        for name in names:
            tar.add(tree / name, arcname=name, recursive=False, filter=_clean)
        if version is not None:
            data = f"LUSTRE_VERSION = {version}\n".encode()
            info = _clean(tarfile.TarInfo(VERSION_FILE))
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
        elif (tree / VERSION_FILE).is_file():
            # No describe here: the local build reads this file too.
            tar.add(tree / VERSION_FILE, arcname=VERSION_FILE, filter=_clean)
    return buf.getvalue()


def send(host: str, tree: Path) -> Job:
    """Send tree to host.  Return the job that holds it on the host."""
    from . import hosts

    response = hosts.transport_for(host).request(
        [RECEIVE_TREE], make_stream(tree)
    )
    if response.error:
        raise response.error
    try:
        data = json.loads(response.stdout)
        return Job(job=str(data["job"]), tree=str(data["tree"]))
    except (ValueError, KeyError, TypeError):
        raise RemoteError(
            "bad_reply", "the host did not say where it put the tree"
        ) from None


def local_tree(argv: list[str], args: argparse.Namespace) -> str | None:
    """The local Lustre tree that argv takes, or None.

    deploy-lustre and cluster deploy build the tree in the current
    directory when no tree is given, so they always send one.  The build
    verbs send a tree only when --lustre-tree is given.
    """
    if argv[:1] == ["deploy-lustre"]:
        return getattr(args, "lustre_tree", None) or "."
    if argv[:2] == ["cluster", "deploy"]:
        return getattr(args, "lustre_source", None) or "."
    if argv[:1] == ["build"]:
        tree = getattr(args, "lustre_tree", None)
        return tree if isinstance(tree, str) else None
    return None


# The spellings of the tree option in the verbs that take a tree.
_TREE_OPTIONS = ("--lustre-tree", "--build")


def _drop_tree_option(argv: list[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for i, tok in enumerate(argv):
        if skip:
            skip = False
            continue
        if i > 0 and tok in _TREE_OPTIONS:
            skip = True
            continue
        if i > 0 and tok.startswith(tuple(o + "=" for o in _TREE_OPTIONS)):
            continue
        out.append(tok)
    return out


def attach(host: str, argv: list[str], args: argparse.Namespace) -> list[str]:
    """Send the local tree of argv, if it has one.  Return the new argv.

    The new argv names the job tree on the host.  argparse uses the last
    value of an option.  So an abbreviated local option that stays in
    argv has no effect.
    """
    local = local_tree(argv, args)
    if local is None:
        return argv
    job = send(host, Path(local))
    return [*_drop_tree_option(argv), TREE_OPTION, job.tree]
