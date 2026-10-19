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
