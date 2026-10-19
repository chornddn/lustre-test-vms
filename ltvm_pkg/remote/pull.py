"""`pull`: copy the results of a remote VM, cluster or job to the controller.

The controller sends ``pull <name> <what>``.  <what> is one of TYPES.
<name> is a remote VM, a remote cluster, or a job that `receive-tree`
made.  A job selects the VMs and clusters that a deploy of its tree
named (jobs.add_target).  For a cluster, crash, dk and console select
the nodes.

serve finds the results only in fixed places in the VM state:

    suite    VM_DIR/suites/ltvm-suite/<vm or cluster>/   (suite run --wait,
                                                         suite collect)
    crash    VM_DIR/crashes/<vm>/                        (vm crash-collect)
    dk       VM_DIR/dk/<vm>/                             (vm dk-collect)
    console  SOCKETS/<vm>.log

The request has no path.  serve opens each component below the base
directory with O_NOFOLLOW, so a symbolic link in a place refuses the
request.  In a result directory, serve sends only directories and
regular files, and it does not follow a symbolic link: a guest can put
links in the suite results.

After the reply line, serve writes an uncompressed tar stream.  Each
member name starts with the VM or the cluster: `<vm>/crash-<vm>-<ts>/vmcore`,
`<vm>/console.log`.  The controller unpacks it into the destination
(default ./ltvm-pull/<host>/<what>/).  It accepts only directories and
regular files with relative names, and it writes no file through a
symbolic link.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from . import jobs, scope
from .protocol import RemoteError

VERB = "pull"
TYPES = ("suite", "crash", "dk", "console")
DEFAULT_ROOT = "ltvm-pull"

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_CHUNK = 1 << 20


@dataclass
class Source:
    """One place to send: a directory or a file, as an open descriptor."""

    fd: int
    arcname: str
    is_dir: bool


# ── host ─────────────────────────────────────────────────


def _refuse_link(where: str) -> RemoteError:
    return RemoteError(
        "path_refused",
        f"the results in {where} are behind a symbolic link",
        hint="serve reads results only from its own directories.",
    )


def _open_below(base: Path, parts: list[str], *, is_dir: bool) -> int | None:
    """Open base/parts with no symbolic link below base.

    Return None if the place does not exist.  Raise path_refused for a
    symbolic link or for a file of the wrong type.
    """
    where = "/".join(parts)
    try:
        fd = os.open(os.path.realpath(base), _DIR_FLAGS)
    except FileNotFoundError:
        return None
    try:
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            flags = _FILE_FLAGS if last and not is_dir else _DIR_FLAGS
            try:
                nfd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                return None
            except OSError:
                # ELOOP for a link, ENOTDIR for a file in the way.
                raise _refuse_link(where) from None
            os.close(fd)
            fd = nfd
        if not is_dir and not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _refuse_link(where)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _targets(name: str, home: Path) -> list[str]:
    """The remote VMs and clusters that name selects."""
    if jobs.is_job_name(name):
        found = jobs.targets(home, name)
        if found is None:
            raise RemoteError(
                "vm_not_found",
                f"this host has no job named {name!r}",
                hint="Use the job that a deploy with --host printed.",
            )
        out = []
        for target in found:
            try:
                scope.target_nodes(target)
            except RemoteError:
                continue  # destroyed since the deploy
            out.append(target)
        return out
    scope.target_nodes(name)
    return [name]


def plan(argv: list[str], home: Path) -> list[Source]:
    """Check a pull request and open what it sends.  Raise RemoteError."""
    if len(argv) != 3 or argv[2] not in TYPES:
        raise RemoteError(
            "bad_request",
            f"use: pull <vm|cluster|job> <{'|'.join(TYPES)}>",
            hint="pull takes no path and no option.",
        )
    _, name, what = argv
    from ltvm_pkg import vm_state

    vm_dir = Path(vm_state.VM_DIR)
    sources: list[Source] = []
    try:
        for target in _targets(name, home):
            if what == "suite":
                places = [
                    (target, vm_dir, ["suites", "ltvm-suite", target], True)
                ]
            else:
                places = []
                for vm in scope.target_nodes(target):
                    if what == "console":
                        log = (Path(vm_state.SOCKETS), [f"{vm}.log"], False)
                        places.append((f"{vm}/console.log", *log))
                    else:
                        sub = "crashes" if what == "crash" else "dk"
                        places.append((vm, vm_dir, [sub, vm], True))
            for arcname, base, parts, is_dir in places:
                fd = _open_below(base, parts, is_dir=is_dir)
                if fd is not None:
                    sources.append(Source(fd, arcname, is_dir))
    except BaseException:
        close(sources)
        raise
    return sources


def close(sources: list[Source]) -> None:
    for s in sources:
        os.close(s.fd)


def _walk(fd: int, arcname: str) -> Iterator[tuple[int, str, bool]]:
    """Yield (fd, arcname, is_dir) under a directory.  Skip links."""
    for entry in sorted(os.scandir(fd), key=lambda e: e.name):
        name = f"{arcname}/{entry.name}"
        if entry.is_symlink():
            print(f"pull: skipped the symbolic link {name}", file=sys.stderr)
            continue
        is_dir = entry.is_dir(follow_symlinks=False)
        if not is_dir and not entry.is_file(follow_symlinks=False):
            continue
        try:
            sub = os.open(
                entry.name, _DIR_FLAGS if is_dir else _FILE_FLAGS, dir_fd=fd
            )
        except OSError:
            continue  # gone, or changed to a link since the scan
        try:
            if is_dir:
                yield sub, name, True
                yield from _walk(sub, name)
            elif stat.S_ISREG(os.fstat(sub).st_mode):
                yield sub, name, False
        finally:
            os.close(sub)


def _add(tar: tarfile.TarFile, fd: int, arcname: str, is_dir: bool) -> None:
    st = os.fstat(fd)
    info = tarfile.TarInfo(arcname)
    info.mtime = int(st.st_mtime)
    if is_dir:
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        tar.addfile(info)
        return
    info.size = st.st_size
    info.mode = 0o644
    with os.fdopen(os.dup(fd), "rb") as f:
        tar.addfile(info, f)


def write(sources: list[Source], out: IO[bytes]) -> None:
    """Write the tar stream of the sources to out, and close them."""
    try:
        with tarfile.open(
            fileobj=out, mode="w|", format=tarfile.PAX_FORMAT
        ) as tar:
            for s in sources:
                _add(tar, s.fd, s.arcname, s.is_dir)
                if s.is_dir:
                    for fd, name, is_dir in _walk(s.fd, s.arcname):
                        _add(tar, fd, name, is_dir)
    finally:
        close(sources)
    out.flush()


# ── controller ───────────────────────────────────────────


def request_argv(argv: list[str], args: argparse.Namespace) -> list[str]:
    """The argv that goes to the host.  --dest and --json stay here."""
    if argv[:1] != [VERB]:
        return argv
    # names.to_host already checked the `<host>/` prefix.
    name = str(args.name).split("/", 1)[-1]
    return [VERB, name, args.what]


def _unsafe(message: str) -> RemoteError:
    return RemoteError("bad_reply", f"the pull stream is refused: {message}")


def _inside(top: str, path: str) -> bool:
    real = os.path.realpath(path)
    return real == top or real.startswith(top + os.sep)


def unpack(data: bytes, dest: Path) -> tuple[list[str], int]:
    """Unpack a pull stream into dest.  Return (files, bytes).

    Accept only directories and regular files with relative names and no
    "..".  Write no file through a symbolic link.
    """
    import io

    dest.mkdir(parents=True, exist_ok=True)
    top = os.path.realpath(dest)
    files: list[str] = []
    size = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r|") as tar:
            for m in tar:
                parts = m.name.split("/")
                if (
                    m.name.startswith("/")
                    or "\\" in m.name
                    or not all(p not in ("", ".", "..") for p in parts)
                ):
                    raise _unsafe(f"{m.name!r} is not a plain relative name")
                path = os.path.join(top, *parts)
                if m.isdir():
                    os.makedirs(path, mode=0o755, exist_ok=True)
                    if not _inside(top, path):
                        raise _unsafe(f"{m.name!r} is under a link")
                    continue
                if not m.isreg():
                    raise _unsafe(f"{m.name!r} is not a file or a directory")
                parent = os.path.dirname(path)
                os.makedirs(parent, mode=0o755, exist_ok=True)
                if not _inside(top, parent):
                    raise _unsafe(f"{m.name!r} is under a link")
                src = tar.extractfile(m)
                assert src is not None
                fd = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                    0o644,
                )
                with os.fdopen(fd, "wb") as f:
                    shutil.copyfileobj(src, f, _CHUNK)
                files.append(m.name)
                size += m.size
    except (tarfile.TarError, EOFError) as e:
        raise _unsafe(str(e)) from None
    except OSError as e:
        raise RemoteError(
            "bad_reply", f"cannot write the pulled files: {e}"
        ) from None
    return files, size


def save(
    host: str, args: argparse.Namespace, body: bytes, use_json: bool
) -> int:
    """Unpack the reply of a pull and report it.  Return the exit status."""
    from ltvm_pkg.cli.util import EXIT_ERROR, EXIT_OK, _emit_error

    if args.dest:
        dest = Path(os.path.abspath(os.path.expanduser(args.dest)))
    else:
        dest = Path.cwd() / DEFAULT_ROOT / host / args.what
    try:
        files, size = unpack(body, dest)
    except RemoteError as e:
        _emit_error(e.message, use_json, hint=e.hint, error_code=e.code)
        return EXIT_ERROR
    if use_json:
        print(
            json.dumps(
                {
                    "host": host,
                    "name": args.name,
                    "what": args.what,
                    "dest": str(dest),
                    "files": files,
                    "bytes": size,
                },
                indent=2,
            )
        )
    elif files:
        print(f"pulled {len(files)} files ({size} bytes) to {dest}")
    else:
        print(f"no {args.what} results for {args.name} on host {host}")
    return EXIT_OK
