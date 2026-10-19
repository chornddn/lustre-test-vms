"""Leases: a time limit on each VM and cluster that a remote request makes.

A remote `create` or `cluster create` gives the new VM or cluster a
lease.  The length comes from `--lease`, or from `lease_hours` in
serve.toml.  `lease renew` makes a lease longer.  `serve reap` destroys
each VM and cluster whose lease has expired, so a controller that
crashes does not leave VMs on the host.

The leases are in one JSON file of the dedicated user,
``$XDG_STATE_HOME/ltvm/leases.json``, next to the audit log.  A lease is
not a claim (vm_claim).  An expired claim lets another session use the
VM; an expired lease destroys the VM.  The claim code also rewrites or
clears the expiry time of a claim in places where a lease must stay.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import io
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, dataclass
from pathlib import Path

from .protocol import RemoteError

# The lease code reads the time only through this name, so that the
# tests can fix the clock.
clock: Callable[[], float] = time.time

VM = "vm"
CLUSTER = "cluster"


@dataclass
class Lease:
    kind: str  # VM or CLUSTER
    name: str
    owner: str
    since: float
    expires: float

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}"

    def expired(self, now: float) -> bool:
        return now >= self.expires


def store_path(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_STATE_HOME") or str(
        Path(env.get("HOME") or Path.home()) / ".local" / "state"
    )
    return Path(base) / "ltvm" / "leases.json"


@contextlib.contextmanager
def _locked(env: Mapping[str, str]) -> Iterator[Path]:
    """Hold the lock of the lease file.  serve and reap run at once."""
    path = store_path(env)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(
        path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        os.close(fd)


def _read(path: Path) -> dict[str, Lease]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    out: dict[str, Lease] = {}
    for d in data.values():
        lease = Lease(
            kind=str(d["kind"]),
            name=str(d["name"]),
            owner=str(d["owner"]),
            since=float(d["since"]),
            expires=float(d["expires"]),
        )
        out[lease.key] = lease
    return out


def _write(path: Path, leases: dict[str, Lease]) -> None:
    body = json.dumps(
        {k: asdict(v) for k, v in sorted(leases.items())}, indent=1
    )
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".leases.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_all(env: Mapping[str, str]) -> dict[str, Lease]:
    """Every lease, live or expired, by "<kind>:<name>"."""
    with _locked(env) as path:
        return _read(path)


def grant(
    env: Mapping[str, str], kind: str, name: str, owner: str, seconds: float
) -> Lease:
    """Give kind/name a lease that ends `seconds` from now."""
    now = clock()
    lease = Lease(kind, name, owner, since=now, expires=now + seconds)
    with _locked(env) as path:
        leases = _read(path)
        prev = leases.get(lease.key)
        if prev is not None and not prev.expired(now):
            lease.since = prev.since
        leases[lease.key] = lease
        _write(path, leases)
    return lease


def drop(env: Mapping[str, str], key: str) -> None:
    with _locked(env) as path:
        leases = _read(path)
        if leases.pop(key, None) is not None:
            _write(path, leases)


# ── lengths ──────────────────────────────────────────────


def default_seconds(env: Mapping[str, str]) -> float:
    from . import serve_config

    return serve_config.load(env).lease_hours * 3600


def parse_length(text: str) -> float:
    """``90``, ``30m``, ``4h`` or ``2d`` as seconds.  Raise bad_request."""
    from ltvm_pkg.vm_claim import parse_ttl

    try:
        return parse_ttl(text)
    except ValueError as e:
        raise RemoteError("bad_request", str(e)) from None


# ── lease renew ──────────────────────────────────────────


def _target(env: Mapping[str, str], name: str) -> tuple[str, str, str]:
    """(kind, name, owner) of the lease that `lease renew <name>` changes.

    A cluster node has no lease of its own: its name renews the lease
    of its cluster.  Raise VMNotFound when nothing has the name.
    """
    from ltvm_pkg.vm_state import ClusterInfo, ClusterNotFound, VMInfo

    unreadable = (RuntimeError, ValueError, KeyError, TypeError, OSError)
    try:
        cluster = ClusterInfo.load(name)
        return CLUSTER, name, cluster.owner_id or "-"
    except (ClusterNotFound, *unreadable):
        pass
    for lease in load_all(env).values():
        if lease.kind != CLUSTER:
            continue
        try:
            nodes = [n.name for n in ClusterInfo.load(lease.name).get_nodes()]
        except (ClusterNotFound, *unreadable):
            continue
        if name in nodes:
            return CLUSTER, lease.name, lease.owner
    vm = VMInfo.load(name)
    return VM, name, vm.owner_id or "-"


def cmd_renew(args: argparse.Namespace) -> int:
    """`ltvm lease renew <vm|cluster> [--for TIME]`."""
    from ltvm_pkg.cli.util import _emit_error
    from ltvm_pkg.vm_state import VMNotFound

    env = os.environ
    try:
        seconds = (
            parse_length(args.for_time)
            if args.for_time
            else default_seconds(env)
        )
        kind, name, owner = _target(env, args.name)
    except RemoteError as e:
        return _emit_error(e.message, args.json, error_code=e.code)
    except VMNotFound:
        return _emit_error(
            f"no VM or cluster named {args.name!r}",
            args.json,
            error_code="vm_not_found",
        )
    lease = grant(env, kind, name, owner, seconds)
    if args.json:
        print(json.dumps(asdict(lease), indent=2))
    else:
        until = time.strftime("%F %T %Z", time.localtime(lease.expires))
        print(f"{kind} {name}: the lease ends at {until}")
    return 0


# ── serve ────────────────────────────────────────────────


def on_request(
    argv: list[str],
    parse: Callable[[list[str]], argparse.Namespace],
    env: Mapping[str, str],
    owner: str,
) -> None:
    """Give a lease for a remote create.

    The verb ignores --lease.  run() refuses it only for a local command.

    serve calls this after its checks and before the verb.  The lease
    starts before the VM exists, so a create that serve does not finish
    still leaves a lease that reap acts on.
    """
    kind = None
    if argv[:1] == ["create"]:
        kind = VM
    elif argv[:2] == ["cluster", "create"]:
        kind = CLUSTER
    if kind is None:
        return
    sink = io.StringIO()
    try:
        with redirect_stdout(sink), redirect_stderr(sink):
            args = parse(argv)
    except SystemExit:
        # The verb gets the same parse error and makes nothing.
        return
    text = getattr(args, "lease", None)
    seconds = parse_length(text) if text else default_seconds(env)
    if not getattr(args, "dry_run", False):
        grant(env, kind, args.name, owner, seconds)


# ── reap ─────────────────────────────────────────────────


def _destroy_argv(lease: Lease) -> list[str]:
    if lease.kind == CLUSTER:
        return ["cluster", "destroy", lease.name]
    return ["destroy", lease.name]


def _check_in_scope(lease: Lease) -> None:
    """Raise RemoteError unless serve may destroy what the lease names."""
    from . import scope

    if lease.kind == CLUSTER:
        scope._check_clusters([lease.name], lifecycle=True)
    else:
        scope._check_existing([lease.name])


def _still_expired(env: Mapping[str, str], key: str, now: float) -> bool:
    """A `lease renew` can come between the scan and the destroy."""
    lease = load_all(env).get(key)
    return lease is not None and lease.expired(now)


def _audit_key(owner: str) -> str:
    from . import audit, scope

    if owner.startswith(scope.REMOTE_OWNER_PREFIX):
        return owner[len(scope.REMOTE_OWNER_PREFIX) :]
    return audit.NO_KEY


def _reap_one(
    run: Callable[..., int], env: Mapping[str, str], lease: Lease
) -> str:
    """Destroy what an expired lease names.  Return the audit result."""
    from . import serve

    try:
        _check_in_scope(lease)
    except RemoteError as e:
        # Absent, or not a remote VM: never destroy it.
        drop(env, lease.key)
        return e.code
    # The destroy acts as the owner of the lease, so that a claim that
    # the remote controller holds does not stop it.
    os.environ["LTVM_OWNER_ID"] = lease.owner
    with redirect_stdout(sys.stderr):
        rc, result = serve._run_verb(run, _destroy_argv(lease))
    if rc != 0:
        # Keep the lease: the next reap tries again.
        return result
    drop(env, lease.key)
    return "reaped"


def reap(
    run: Callable[..., int],
    env: Mapping[str, str],
    *,
    script: str | None = None,
) -> list[dict[str, object]]:
    """Destroy each VM and cluster whose lease has expired.

    run is ltvm.run.  Each destroy runs in this process, with sudo
    forbidden, as a remote request does.  Return one entry for each
    expired lease, with its audit result: "reaped", "exit <N>", or the
    error code of a lease whose VM serve may not destroy.
    """
    from ltvm_pkg import priv

    from . import audit, serve

    serve._guard_children(script)
    priv.forbid_sudo()
    now = clock()
    log = audit.log_path(env)
    out: list[dict[str, object]] = []
    for key, lease in sorted(load_all(env).items()):
        if not lease.expired(now) or not _still_expired(env, key, now):
            continue
        result = _reap_one(run, env, lease)
        audit.append(
            log,
            key=_audit_key(lease.owner),
            argv=_destroy_argv(lease),
            result=result,
        )
        out.append({**asdict(lease), "result": result})
    return out
