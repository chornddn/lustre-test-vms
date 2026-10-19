"""`ltvm host add|list|remove` and `ltvm serve authorize`: pairing.

The controller user runs `host add`.  It prints the `serve authorize`
command, which the host owner runs on the host.  See
ltvm_pkg/remote/ssh.py for what each side pins.
"""

from __future__ import annotations

import argparse
import re
import shlex
import socket
from pathlib import Path
from typing import Any

from ltvm_pkg.cli.util import EXIT_OK, _emit_error, _error, _output
from ltvm_pkg.remote import hosts, ssh
from ltvm_pkg.remote.protocol import RemoteError


def cmd_serve_authorize(args: argparse.Namespace) -> int:
    path = ssh.authorized_keys_path()
    try:
        line = ssh.authorize(args.pubkey, args.from_addr, path)
    except ValueError as e:
        return _error(str(e), args.json)
    if args.json:
        _output({"authorized_keys": str(path), "line": line}, True)
    else:
        print(f"Added this line to {path}:\n\n  {line}")
    return EXIT_OK


def cmd_host(args: argparse.Namespace) -> int:
    action = getattr(args, "host_action", None)
    if action == "add":
        return _add(args)
    if action == "list":
        return _list(args)
    if action == "remove":
        return _remove(args)
    return _error("host needs a subcommand: add, list or remove", args.json)


def _controller_name() -> str:
    name = socket.gethostname().split(".")[0]
    return re.sub(r"[^A-Za-z0-9_-]", "-", name) or "controller"


def authorize_command(user: str, key: ssh.PublicKey, from_addr: str) -> str:
    """The command that the host owner runs, as the dedicated user."""
    return (
        f"sudo -u {shlex.quote(user)} -H ltvm serve authorize "
        f"{shlex.quote(str(key))} --from {shlex.quote(from_addr)}"
    )


def _delete_key_files(key: str) -> list[str]:
    """Delete a host key and its files, only inside the keys directory."""
    path = Path(key)
    if path.parent.resolve() != ssh.keys_dir().resolve():
        return []
    deleted = []
    for p in (path, Path(key + ".pub"), ssh.known_hosts_path(key)):
        try:
            p.unlink()
        except FileNotFoundError:
            continue
        deleted.append(str(p))
    return deleted


def _add(args: argparse.Namespace) -> int:
    name = args.name
    try:
        hosts.check_new_name(name)
        target = ssh.parse_url(args.url)
        fp = ssh.check_fingerprint(args.hostkey)
        if args.from_addr:
            ssh.check_from(args.from_addr)
    except ValueError as e:
        return _error(str(e), args.json)
    from_addr = args.from_addr or ssh.source_address(target.addr, target.port)
    if not from_addr:
        return _error(
            f"cannot find the address of this machine on the route to "
            f"{target.addr}",
            args.json,
            hint="Give the address that the host sees with --from.",
        )
    key = ssh.key_path(name)
    if key.exists() or Path(f"{key}.pub").exists():
        return _error(
            f"a key for host {name!r} is already at {key}",
            args.json,
            hint="Delete it, or use a different host name.",
        )
    try:
        pub = ssh.make_key(key, f"ltvm:{_controller_name()}")
    except (OSError, RuntimeError, ValueError) as e:
        return _error(f"cannot make a key for {name}: {e}", args.json)
    host = hosts.Host(
        name=name, url=args.url, key=str(key), hostkey=fp, from_addr=from_addr
    )
    pinned = True
    try:
        ssh.pin_host_key(host)
    except RemoteError as e:
        if e.code != "unreachable":
            _delete_key_files(str(key))
            return _emit_error(
                e.message, args.json, hint=e.hint, error_code=e.code
            )
        # The first request pins the key, against the same fingerprint.
        pinned = False
    hosts.add(host)
    command = authorize_command(target.user, pub, from_addr)
    if args.json:
        _output(
            {
                "name": name,
                "url": args.url,
                "key": str(key),
                "hostkey": fp,
                "hostkey_pinned": pinned,
                "from": from_addr,
                "authorize_command": command,
            },
            True,
        )
        return EXIT_OK
    print(f"Added host {name} ({args.url}).  Key: {key}")
    if not pinned:
        print(
            f"The host is not reachable now.  The first request gets its "
            f"host key and checks it against {fp}."
        )
    print(
        f"\nAsk the owner of {name} to run this command on the host:\n\n"
        f"  {command}\n\n"
        f"{from_addr} is the address of this machine on the route to the "
        f"host.  If the host sees a different address (NAT, VPN), change "
        f"--from before you send the command."
    )
    return EXIT_OK


def _probe(host: hosts.Host) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": host.name,
        "url": host.url,
        "status": "ok",
        "message": None,
        "protocol": None,
        "ltvm": None,
    }
    try:
        response = hosts.transport_for(host.name).request([])
    except RemoteError as e:
        row.update(status=e.code, message=e.message)
        return row
    except ValueError as e:
        row.update(status="bad_entry", message=str(e))
        return row
    row["protocol"] = response.peer.get("protocol")
    row["ltvm"] = response.peer.get("ltvm")
    if response.error:
        row.update(status=response.error.code, message=response.error.message)
    return row


def _list(args: argparse.Namespace) -> int:
    rows = [_probe(h) for h in hosts.list_hosts()]
    if args.json:
        _output({"hosts": rows}, True)
        return EXIT_OK
    if not rows:
        print(
            f"No hosts.  Add one with `ltvm host add`.  ({hosts.registry_path()})"
        )
        return EXIT_OK
    table = [("NAME", "URL", "STATUS", "PROTOCOL", "LTVM")] + [
        (
            r["name"],
            r["url"],
            r["status"],
            str(r["protocol"] if r["protocol"] is not None else "-"),
            str(r["ltvm"] or "-"),
        )
        for r in rows
    ]
    widths = [max(len(row[i]) for row in table) for i in range(4)]
    for row in table:
        cells = [c.ljust(w) for c, w in zip(row, widths)] + [row[4]]
        print("  ".join(cells))
    for r in rows:
        if r["message"]:
            print(f"{r['name']}: {r['message']}")
    return EXIT_OK


def _remove(args: argparse.Namespace) -> int:
    try:
        host = hosts.remove(args.name)
    except RemoteError as e:
        return _emit_error(e.message, args.json, hint=e.hint, error_code=e.code)
    line = None
    if host.key:
        try:
            pub = ssh.parse_public_key(Path(host.key + ".pub").read_text())
            line = ssh.authorized_line(pub, host.from_addr)
        except (OSError, ValueError):
            line = None
    deleted = _delete_key_files(host.key) if host.key else []
    if args.json:
        _output(
            {"removed": host.name, "deleted": deleted, "authorized_line": line},
            True,
        )
        return EXIT_OK
    print(f"Removed host {host.name}.")
    for p in deleted:
        print(f"Deleted {p}")
    if line:
        user = ssh.parse_url(host.url).user
        print(
            f"\nAsk the owner of {host.name} to delete this line from "
            f"~{user}/.ssh/authorized_keys on the host:\n\n  {line}"
        )
    return EXIT_OK
