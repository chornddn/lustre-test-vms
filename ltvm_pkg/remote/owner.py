"""The commands of the host owner: `serve enable`, `disable`, `status`.

The owner runs them as the dedicated user (for example
``sudo -u ltvm -H ltvm serve disable``), because serve.toml, the leases
and the audit log are in the home directory of that user, and because
only that user can destroy the remote VMs without sudo.

- `serve disable` sets ``enabled = false`` in serve.toml.  serve then
  refuses each request with `disabled`, before all other checks.
  With --destroy, it also destroys each VM and cluster that remote
  requests made, and no VM of the owner.
- `serve enable` sets ``enabled = true``.
- `serve status` shows the leases, the resource use against each cap,
  and the most recent requests in the audit log.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Mapping

from . import serve_config
from .protocol import RemoteError


def _bad_config(e: Exception) -> RemoteError:
    return RemoteError(
        "bad_config",
        f"serve cannot read serve.toml: {e}",
        hint="The host owner corrects serve.toml.",
    )


def check_enabled(env: Mapping[str, str]) -> None:
    """Raise `disabled` if the owner disabled remote use.

    serve.toml that serve cannot read refuses every request too, so
    that a bad edit of `enabled` never lets requests in.
    """
    try:
        cfg = serve_config.load(env)
    except (OSError, ValueError) as e:
        raise _bad_config(e) from None
    if not cfg.enabled:
        raise RemoteError(
            "disabled",
            "the host owner disabled remote use of this host",
            hint="Use local VMs.  Do not try again until the owner runs "
            "`ltvm serve enable`.",
        )


def _set_enabled(env: Mapping[str, str], enabled: bool) -> None:
    try:
        cfg = serve_config.load(env)
    except (OSError, ValueError) as e:
        raise _bad_config(e) from None
    cfg.enabled = enabled
    serve_config.save(env, cfg)


def cmd_enable(args: argparse.Namespace) -> int:
    """`ltvm serve enable`: accept remote requests again."""
    from ltvm_pkg.cli.util import _emit_error

    try:
        _set_enabled(os.environ, True)
    except RemoteError as e:
        return _emit_error(e.message, args.json, hint=e.hint, error_code=e.code)
    if args.json:
        print(json.dumps({"enabled": True}))
    else:
        print("Remote use of this host is enabled.")
    return 0


def cmd_disable(
    args: argparse.Namespace, run: Callable[..., int], script: str
) -> int:
    """`ltvm serve disable [--destroy]`: refuse all remote requests.

    serve.toml changes first, so no new request starts while the
    destroys run.  A request that serve started before the change can
    still finish.
    """
    from ltvm_pkg.cli.util import EXIT_ERROR, _emit_error

    from . import leases

    env = os.environ
    try:
        _set_enabled(env, False)
    except RemoteError as e:
        return _emit_error(e.message, args.json, hint=e.hint, error_code=e.code)
    destroyed: list[dict[str, object]] = []
    if args.destroy:
        destroyed = leases.reap(run, env, script=script, everything=True)
    if args.json:
        print(json.dumps({"enabled": False, "destroyed": destroyed}, indent=2))
    else:
        print("Remote use of this host is disabled.")
        for r in destroyed:
            print(f"{r['kind']} {r['name']}: {r['result']}")
    failed = any(r["result"] != "reaped" for r in destroyed)
    return EXIT_ERROR if failed else 0


# ── serve status ─────────────────────────────────────────

ACTIVE = "active"
EXPIRED = "expired"
GONE = "gone"


def _exists(kind: str, name: str) -> bool:
    from ltvm_pkg.vm_state import ClusterInfo, VMInfo

    from . import leases

    if kind == leases.CLUSTER:
        return name in ClusterInfo.all_names()
    return name in VMInfo.all_names()


def _lease_rows(env: Mapping[str, str]) -> list[dict[str, object]]:
    """Each lease, with its state: active, expired or gone.

    A lease is gone when its VM or cluster does not exist (a destroy by
    the agent, or a create that failed).  The next reap drops it.
    """
    from dataclasses import asdict

    from . import leases

    now = leases.clock()
    rows: list[dict[str, object]] = []
    for _, lease in sorted(leases.load_all(env).items()):
        if lease.expired(now):
            state = EXPIRED
        elif _exists(lease.kind, lease.name):
            state = ACTIVE
        else:
            state = GONE
        rows.append({**asdict(lease), "state": state})
    return rows


def _recent(env: Mapping[str, str], count: int) -> list[dict[str, object]]:
    """The last `count` entries of the audit log.  Skip bad lines."""
    from . import audit

    try:
        lines = audit.log_path(env).read_text().splitlines()
    except FileNotFoundError:
        return []
    out: list[dict[str, object]] = []
    for line in reversed(lines):
        if len(out) >= count:
            break
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out[::-1]


def status(env: Mapping[str, str], recent: int) -> dict[str, object]:
    """The status document of `serve status --json`.

    "used" and "cap" have the keys of the over_cap error.  serve counts
    the disk only when a disk cap is set, so with no disk cap the used
    disk is null.
    """
    from . import admission

    try:
        cfg = serve_config.load(env)
    except (OSError, ValueError) as e:
        raise _bad_config(e) from None
    disk = cfg.max_disk_gb is not None
    used = admission._as_json(admission.usage(env, disk=disk))
    if not disk:
        used["disk_gb"] = None
    return {
        "enabled": cfg.enabled,
        "lease_hours": cfg.lease_hours,
        "ac_only": cfg.ac_only,
        "leases": _lease_rows(env),
        "used": used,
        "cap": admission._as_json(cfg),
        "recent": _recent(env, recent),
    }


def _when(epoch: object) -> str:
    import time

    if not isinstance(epoch, (int, float)):
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def _show(value: object) -> str:
    return "-" if value is None else str(value)


def print_status(data: Mapping[str, object]) -> None:
    enabled = "enabled" if data["enabled"] else "DISABLED"
    print(f"Remote use: {enabled}")
    print(
        f"Default lease: {data['lease_hours']} h   AC only: {data['ac_only']}"
    )
    print()
    print("Leases:")
    rows = data["leases"]
    assert isinstance(rows, list)
    if not rows:
        print("  (none)")
    for r in rows:
        print(
            f"  {r['kind']:<8} {r['name']:<24} {r['state']:<8} "
            f"until {_when(r['expires'])}  owner={r['owner']}"
        )
    print()
    print("Use (used / cap, - is no cap):")
    used, cap = data["used"], data["cap"]
    assert isinstance(used, dict) and isinstance(cap, dict)
    for key in ("vcpus", "ram_gb", "disk_gb"):
        print(f"  {key}: {_show(used.get(key))} / {_show(cap.get(key))}")
    print()
    print("Recent requests:")
    entries = data["recent"]
    assert isinstance(entries, list)
    if not entries:
        print("  (none)")
    for e in entries:
        argv = " ".join([str(e.get("verb", "")), *map(str, e.get("args", []))])
        print(
            f"  {e.get('time', '-')} {e.get('key', '-')} {argv} -> {e.get('result', '-')}"
        )


def cmd_status(args: argparse.Namespace) -> int:
    """`ltvm serve status`: leases, use against the caps, recent requests."""
    from ltvm_pkg.cli.util import _emit_error

    try:
        data = status(os.environ, args.recent)
    except RemoteError as e:
        return _emit_error(e.message, args.json, hint=e.hint, error_code=e.code)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print_status(data)
    return 0
