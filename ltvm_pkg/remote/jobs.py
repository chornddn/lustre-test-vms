"""Job directories on the host: one for each tree that a controller sends.

`receive-tree` reads a tar stream from the stdin of serve and unpacks it
into a new job directory:

    ~/jobs/<job>/job.json   who sent the tree, and when
    ~/jobs/<job>/tree/      the Lustre tree

~ is the home of the dedicated user that runs serve.  The jobs directory
and each job directory have mode 0700.

The unpack refuses a stream that can write outside the tree: an
absolute name, a ".." component, a symbolic link whose target is outside
the tree, a hard link or a device.  It also refuses to write through a
symbolic link.  A refused stream leaves no job directory.

A verb takes a job tree only as its canonical path, which is the path
that `receive-tree` printed (job_tree).  So ".." and symbolic links in a
path argument give path_refused.
"""

from __future__ import annotations

import datetime
import json
import os
import posixpath
import re
import secrets
import shutil
import tarfile
from pathlib import Path
from typing import IO, Any

from .protocol import RemoteError

JOBS_DIR = "jobs"
TREE_DIR = "tree"
JOB_FILE = "job.json"

_JOB_RE = re.compile(r"j-\d{8}-\d{6}-[0-9a-f]{8}")
_COPY_CHUNK = 1 << 20


def root(home: Path) -> Path:
    """The directory that holds the job directories."""
    return home / JOBS_DIR


def is_job_name(name: str) -> bool:
    return bool(_JOB_RE.fullmatch(name))


def _new_job(home: Path) -> Path:
    top = root(home)
    top.mkdir(mode=0o700, exist_ok=True)
    while True:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        job = top / f"j-{stamp}-{secrets.token_hex(4)}"
        try:
            job.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return job


def _refuse(message: str) -> RemoteError:
    return RemoteError(
        "path_refused",
        f"the tree stream is refused: {message}",
        hint="Send a tree with no absolute paths, no '..', and no "
        "links that point outside the tree.",
    )


def _member_path(name: str) -> str:
    """The safe relative path of a member.  Raise for an unsafe name."""
    if name.startswith("/") or "\\" in name:
        raise _refuse(f"{name!r} is not a relative path")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise _refuse(f"{name!r} is outside the tree")
    return "/".join(parts)


def _check_link(rel: str, target: str) -> None:
    if not target or target.startswith("/"):
        raise _refuse(f"link {rel!r} points to {target!r}")
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
    if joined == ".." or joined.startswith("../"):
        raise _refuse(f"link {rel!r} points outside the tree")


def _inside(tree: Path, path: Path) -> bool:
    real = os.path.realpath(path)
    return real == str(tree) or real.startswith(str(tree) + os.sep)


def _unpack(stream: IO[bytes], tree: Path) -> tuple[int, int]:
    """Unpack the tar stream into tree.  Return (files, bytes)."""
    files = size = 0
    with tarfile.open(fileobj=stream, mode="r|*") as tar:
        for member in tar:
            rel = _member_path(member.name)
            dest = tree / rel
            if not _inside(tree, dest.parent):
                raise _refuse(f"{rel!r} is under a link")
            if os.path.lexists(dest) and not (
                member.isdir() and dest.is_dir() and not dest.is_symlink()
            ):
                raise _refuse(f"{rel!r} is in the stream two times")
            if member.isdir():
                dest.mkdir(mode=0o755, parents=True, exist_ok=True)
            elif member.issym():
                _check_link(rel, member.linkname)
                dest.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                os.symlink(member.linkname, dest)
                files += 1
            elif member.isreg():
                dest.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                src = tar.extractfile(member)
                assert src is not None
                # No set-ID bits; the owner can always read and write.
                mode = 0o644 | (member.mode & 0o111)
                fd = os.open(
                    dest,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    mode,
                )
                with os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(src, out, _COPY_CHUNK)
                files += 1
                size += member.size
            else:
                raise _refuse(f"{rel!r} is not a file, a directory or a link")
    return files, size


def receive(stream: IO[bytes], home: Path, key: str) -> dict[str, Any]:
    """Unpack a tree stream into a new job.  Return what to print."""
    job = _new_job(home)
    (job / TREE_DIR).mkdir(mode=0o755)
    # The canonical path: job_tree accepts only this form.
    tree = Path(os.path.realpath(job / TREE_DIR))
    try:
        files, size = _unpack(stream, tree)
    except RemoteError:
        shutil.rmtree(job, ignore_errors=True)
        raise
    except (tarfile.TarError, OSError, EOFError) as e:
        shutil.rmtree(job, ignore_errors=True)
        raise RemoteError(
            "bad_request", f"cannot read the tree stream: {e}"
        ) from None
    info = {
        "job": job.name,
        "tree": str(tree),
        "files": files,
        "bytes": size,
        "key": key,
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    (job / JOB_FILE).write_text(json.dumps(info, indent=2) + "\n")
    return info


def job_tree(home: Path, value: str, option: str) -> Path:
    """The job tree that a path argument names.  Raise path_refused.

    The value must be the canonical path of the tree of a job: absolute,
    with no "..", no ".", no "/" at the end and no symbolic link.
    """
    refused = RemoteError(
        "path_refused",
        f"{option} {value!r} is not a tree that receive-tree made",
        hint="Give a local Lustre tree with --host: the controller sends "
        "it to the host first.",
    )
    if not os.path.isabs(value):
        raise refused
    real = Path(os.path.realpath(value))
    top = Path(os.path.realpath(root(home)))
    if (
        str(real) != value
        or real.name != TREE_DIR
        or real.parent.parent != top
        or not is_job_name(real.parent.name)
        or not real.is_dir()
    ):
        raise refused
    return real
