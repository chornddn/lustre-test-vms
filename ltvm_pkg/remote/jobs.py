"""Job directories on the host: one for each tree that a controller sends.

`receive-tree` reads a tar stream from the stdin of serve and unpacks it
into a new job directory:

    ~/jobs/<job>/job.json   who sent the tree, and when
    ~/jobs/<job>/tree/      the Lustre tree

~ is the home of the dedicated user that runs serve.  The jobs directory
and each job directory have mode 0700.

The unpack refuses a stream that can write outside the tree.  So it
refuses an absolute name, a ".." component, a hard link and a device.
It also refuses a symbolic link whose target is outside the tree, and
a write through a symbolic link.  It refuses a ".git" entry too.  A refused stream leaves
no job directory.

A link target can go through other links of the stream.  A later link
can also change where an earlier link goes.  So after the unpack, the
check resolves each link in the tree to its real path.  Each real path
must be in the tree.

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


class TooBig(Exception):
    """The stream holds more bytes than the limit of receive."""

    def __init__(self, size: int) -> None:
        super().__init__(f"the tree stream holds more than {size} bytes")
        self.size = size


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
    if ".git" in parts:
        # git reads .git/config, which can name a program to run.
        raise _refuse(f"{name!r} is a git directory")
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


def _check_links(tree: Path) -> None:
    """Refuse the tree if a link in it resolves to a path outside it."""
    from ltvm_pkg.lustre_version import link_out_of

    bad = link_out_of(tree)
    if bad is not None:
        rel = bad.relative_to(tree)
        raise _refuse(f"link {str(rel)!r} points outside the tree")


def _unpack(
    stream: IO[bytes], tree: Path, limit: float | None
) -> tuple[int, int]:
    """Unpack the tar stream into tree.  Return (files, bytes).

    Raise TooBig before a file goes above limit bytes in total.
    """
    files = size = 0
    with tarfile.open(fileobj=stream, mode="r|*") as tar:
        for member in tar:
            if limit is not None and size + member.size > limit:
                raise TooBig(size + member.size)
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
    _check_links(tree)
    return files, size


def receive(
    stream: IO[bytes], home: Path, key: str, *, limit: float | None = None
) -> dict[str, Any]:
    """Unpack a tree stream into a new job.  Return what to print.

    limit is the most bytes of file data that the job can hold, or
    None for no limit.  Raise TooBig above it.
    """
    job = _new_job(home)
    (job / TREE_DIR).mkdir(mode=0o755)
    # The canonical path: job_tree accepts only this form.
    tree = Path(os.path.realpath(job / TREE_DIR))
    try:
        files, size = _unpack(stream, tree, limit)
    except (RemoteError, TooBig):
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

    The value must be the canonical path of the tree of a job.  That
    path is absolute.  It has no "..", no ".", no "/" at the end and no
    symbolic link.
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


def add_target(tree: str, name: str) -> None:
    """Record that a deploy of a job tree names a VM or a cluster.

    `pull <job>` reads the results of these targets.  Call it after
    job_tree accepted the tree.
    """
    path = Path(tree).parent / JOB_FILE
    info = json.loads(path.read_text())
    found = info.setdefault("targets", [])
    if name not in found:
        found.append(name)
        path.write_text(json.dumps(info, indent=2) + "\n")


def targets(home: Path, job: str) -> list[str] | None:
    """The targets of a job, or None if the job does not exist."""
    if not is_job_name(job):
        return None
    try:
        fd = os.open(root(home) / job / JOB_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd) as f:
        try:
            info = json.load(f)
        except ValueError:
            return None
    found = info.get("targets", []) if isinstance(info, dict) else []
    return [t for t in found if isinstance(t, str)]


# ── delete old jobs ──────────────────────────────────────


def all_jobs(home: Path) -> list[Path]:
    """Each job directory.  Other entries in the jobs directory stay."""
    try:
        entries = list(root(home).iterdir())
    except FileNotFoundError:
        return []
    return sorted(
        p
        for p in entries
        if is_job_name(p.name) and p.is_dir() and not p.is_symlink()
    )


def created(job: Path) -> float:
    """When receive-tree made the job.  The directory mtime if unknown."""
    try:
        info = json.loads((job / JOB_FILE).read_text())
        return datetime.datetime.fromisoformat(info["created"]).timestamp()
    except (OSError, ValueError, KeyError, TypeError):
        return job.lstat().st_mtime


def in_use(home: Path) -> set[str]:
    """The jobs that a remote VM was last deployed from, or that name a
    remote VM or cluster in "targets" (add_target).

    A remote VM exists while its lease (or the lease of its cluster)
    is live, so its job stays until reap destroys the VM.
    """
    from ltvm_pkg.vm_state import VMInfo

    from . import scope

    top = os.path.realpath(root(home)) + os.sep
    used: set[str] = set()
    for name in VMInfo.all_names():
        try:
            vm = VMInfo.load(name)
        except (ValueError, OSError, KeyError, TypeError):
            continue
        path = vm.build_path or ""
        if scope.is_remote(vm.owner_id) and path.startswith(top):
            used.add(path[len(top) :].split(os.sep, 1)[0])
    # `pull <job>` reads the results of the targets that the job names.
    for job in all_jobs(home):
        for target in targets(home, job.name) or []:
            try:
                scope.target_nodes(target)
            except RemoteError:
                continue  # destroyed: it does not hold the job
            used.add(job.name)
            break
    return used


def prune(home: Path, *, now: float, max_age: float | None) -> list[str]:
    """Delete the jobs older than max_age that no remote VM uses.

    With max_age None, delete every job.  Return the deleted job names.
    A job that cannot be deleted stays, with a warning on stderr.
    """
    import sys

    from ltvm_pkg import podman_run

    used = set() if max_age is None else in_use(home)
    deleted: list[str] = []
    for job in all_jobs(home):
        if max_age is not None and (
            job.name in used or now - created(job) < max_age
        ):
            continue
        try:
            podman_run.rmtree(job)
        except OSError as e:
            print(
                f"warning: cannot delete job {job.name}: {e}", file=sys.stderr
            )
            continue
        deleted.append(job.name)
    return deleted
