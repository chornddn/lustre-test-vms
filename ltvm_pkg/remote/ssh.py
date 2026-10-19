"""SSH for remote hosts: keys, host key pins, authorized_keys and ssh://.

Pairing pins each side to the other:

- The controller pins the host key.  `host add` takes the SHA256
  fingerprint of the host key from the owner.  The controller gets the
  key with ssh-keyscan, and keeps it only if its fingerprint is the
  pinned one.  Then ssh trusts only that key (StrictHostKeyChecking=yes,
  a known_hosts file for this host only).  If the host is not reachable
  at `host add`, the first request pins the key in the same way.
- The host pins the controller key.  `serve authorize` writes one
  authorized_keys line that always starts with AUTHORIZED_PREFIX.  The
  `restrict` option turns off every forward, the pty and ~/.ssh/rc.

Every ssh, ssh-keygen and ssh-keyscan command goes through run(), so the
tests can replace it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import shlex
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .hosts import Host, registry_path
from .protocol import RemoteError
from .transport import Response, parse_reply, stdin_bytes, text

SERVE_COMMAND = "ltvm serve --stdio"
AUTHORIZED_PREFIX = f'restrict,command="{SERVE_COMMAND}",from='
DEFAULT_USER = "ltvm"
CONNECT_TIMEOUT = 10
# ssh sends a keepalive after this many seconds with no data from the
# host.  After SERVER_ALIVE_COUNT keepalives with no answer, ssh stops.
# So a dead host or a dead network stops a request in about 2 minutes.
SERVER_ALIVE_INTERVAL = 30
SERVER_ALIVE_COUNT = 4
# The longest time that one request can take, in seconds.  A deploy
# builds Lustre and `suite run --wait` runs a full suite, so the default
# is long.  0 sets no limit.
TIMEOUT_ENV = "LTVM_REMOTE_TIMEOUT"
DEFAULT_REQUEST_TIMEOUT = 8 * 3600.0

_FP_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
_KEY_TYPES = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    }
)
# A comment has no quote, no space and no control character, so it
# cannot change the meaning of the line.
_COMMENT_RE = re.compile(r"^[A-Za-z0-9._:@+=/-]*$")
# One from= pattern: an address, a CIDR range or a host name, with the
# ? and * wildcards and an optional ! in front.
_FROM_RE = re.compile(r"^!?[A-Za-z0-9.:/*?_-]+$")
_URL_PART_RE = re.compile(r"^[A-Za-z0-9._:%-]+$")


def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run one ssh tool.  The tests replace this function."""
    return subprocess.run(argv, text=True, check=False, **kwargs)


def request_timeout() -> float | None:
    """The time limit of one ssh:// request, from TIMEOUT_ENV."""
    raw = os.environ.get(TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_REQUEST_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_REQUEST_TIMEOUT
    return value if value > 0 else None


# -- keys -------------------------------------------------------------------


def fingerprint(blob_b64: str) -> str:
    """The SHA256 fingerprint of a key, as `ssh-keygen -l` shows it."""
    try:
        blob = base64.b64decode(blob_b64, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("the key is not base64") from None
    digest = base64.b64encode(hashlib.sha256(blob).digest())
    return "SHA256:" + digest.decode().rstrip("=")


@dataclass
class PublicKey:
    type: str
    blob: str
    comment: str = ""

    def __str__(self) -> str:
        return " ".join(p for p in (self.type, self.blob, self.comment) if p)


def parse_public_key(text: str) -> PublicKey:
    """Read one OpenSSH public key line.  Raise ValueError if it is bad.

    The line must start with the key type.  A line with options in
    front of the key is refused, because options there would replace
    the restrictions that `serve authorize` writes.
    """
    if "\n" in text.strip() or "\r" in text:
        raise ValueError("the key must be one line")
    parts = text.split()
    if len(parts) < 2:
        raise ValueError("the key needs a type and a base64 blob")
    key_type, blob = parts[0], parts[1]
    comment = " ".join(parts[2:])
    if key_type not in _KEY_TYPES:
        raise ValueError(f"unknown key type {key_type!r}")
    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("the key is not base64") from None
    # The blob starts with its own type, as a length and a string.
    n = int.from_bytes(raw[:4], "big")
    if raw[4 : 4 + n].decode("ascii", "replace") != key_type:
        raise ValueError(f"the key data is not a {key_type} key")
    if not _COMMENT_RE.match(comment):
        raise ValueError(
            "the key comment has a character that is not permitted "
            "(use letters, digits and ._:@+=/-)"
        )
    return PublicKey(key_type, blob, comment)


def check_from(addr: str) -> str:
    """Check a from= value: one or more patterns, with commas between."""
    if not addr or not all(_FROM_RE.match(p) for p in addr.split(",")):
        raise ValueError(
            f"bad address {addr!r}: use addresses, CIDR ranges or host "
            "names, with commas between and no spaces"
        )
    return addr


def authorized_line(key: PublicKey, from_addr: str) -> str:
    return f'{AUTHORIZED_PREFIX}"{check_from(from_addr)}" {key}'


def authorized_keys_path() -> Path:
    return Path.home() / ".ssh" / "authorized_keys"


def authorize(pubkey: str, from_addr: str, path: Path) -> str:
    """Add the line for pubkey to path and return it.

    Raise ValueError for a bad key or address, or for a key that has a
    line in the file.
    """
    key = parse_public_key(pubkey)
    line = authorized_line(key, from_addr)
    try:
        old = path.read_text()
    except FileNotFoundError:
        old = ""
    for n, existing in enumerate(old.splitlines(), 1):
        if key.blob in existing.split():
            raise ValueError(f"this key already has a line: {path}:{n}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as f:
        if old and not old.endswith("\n"):
            f.write("\n")
        f.write(line + "\n")
    return line


def keys_dir() -> Path:
    return registry_path().parent / "keys"


def key_path(name: str) -> Path:
    return keys_dir() / name


def known_hosts_path(key: str) -> Path:
    return Path(key + ".known_hosts")


def make_key(path: Path, comment: str) -> PublicKey:
    """Make an ed25519 key with no passphrase.  Agents use it unattended."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    proc = run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            comment,
            "-f",
            str(path),
        ],
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ssh-keygen failed: {proc.stderr.strip()}")
    return parse_public_key(Path(str(path) + ".pub").read_text())


# -- host URL and host key --------------------------------------------------


@dataclass
class Target:
    user: str
    addr: str
    port: int


def parse_url(url: str) -> Target:
    """Read ssh://[user@]addr[:port].  Raise ValueError if it is bad."""
    parts = urlsplit(url)
    if parts.scheme != "ssh" or not parts.hostname:
        raise ValueError(f"not an ssh:// URL: {url!r}")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(f"an ssh:// URL has no path: {url!r}")
    if parts.password is not None:
        raise ValueError("an ssh:// URL has no password")
    user = parts.username or DEFAULT_USER
    try:
        port = parts.port or 22
    except ValueError:
        raise ValueError(f"bad port in {url!r}") from None
    for part in (user, parts.hostname):
        # No leading "-", so no part reads as an ssh option.
        if not _URL_PART_RE.match(part) or part.startswith("-"):
            raise ValueError(f"bad user or address in {url!r}")
    return Target(user, parts.hostname, port)


def check_fingerprint(fp: str) -> str:
    if not _FP_RE.match(fp):
        raise ValueError(
            f"bad host key fingerprint {fp!r}: use the SHA256:... value "
            "from `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`"
        )
    return fp


def host_alias(name: str) -> str:
    return f"ltvm-{name}"


def pin_host_key(host: Host) -> None:
    """Get the host key and keep it if its fingerprint is the pinned one.

    Raise RemoteError(unreachable) when ssh-keyscan gets no key, and
    RemoteError(host_key_mismatch) when no key has the pinned fingerprint.
    """
    target = parse_url(host.url)
    try:
        proc = run(
            [
                "ssh-keyscan",
                "-T",
                str(CONNECT_TIMEOUT),
                "-t",
                "ed25519",
                "-p",
                str(target.port),
                target.addr,
            ],
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise RemoteError(
            "unreachable", "ssh-keyscan is not installed"
        ) from None
    seen = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0].startswith("#"):
            continue
        try:
            fp = fingerprint(parts[2])
        except ValueError:
            continue
        seen.append(fp)
        if fp == host.hostkey and parts[1] == "ssh-ed25519":
            path = known_hosts_path(host.key)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(f"{host_alias(host.name)} {parts[1]} {parts[2]}\n")
            os.replace(tmp, path)
            return
    if not seen:
        tail = proc.stderr.strip().splitlines()[-1:] or ["no host key"]
        raise RemoteError(
            "unreachable",
            f"cannot get the host key of {target.addr}:{target.port}",
            hint=f"ssh-keyscan said: {tail[0]}",
        )
    raise RemoteError(
        "host_key_mismatch",
        f"the host key of {target.addr}:{target.port} is {seen[0]}, "
        f"not the pinned {host.hostkey}",
        hint="Do not use this host.  Ask its owner for the correct "
        "fingerprint.",
    )


def source_address(addr: str, port: int) -> str | None:
    """The local address that this machine uses to reach addr.

    This sends no packet: a UDP connect only selects a route.  Behind a
    NAT the host sees a different address, so the owner can change it.
    """
    try:
        infos = socket.getaddrinfo(addr, port, type=socket.SOCK_DGRAM)
    except OSError:
        return None
    for family, kind, proto, _, sockaddr in infos:
        try:
            with socket.socket(family, kind, proto) as s:
                s.connect(sockaddr)
                return str(s.getsockname()[0])
        except OSError:
            continue
    return None


# -- the ssh:// transport ---------------------------------------------------


def _opt(name: str, value: str) -> list[str]:
    if any(c in value for c in ' "\t'):
        value = '"' + value.replace('"', "") + '"'
    return ["-o", f"{name}={value}"]


class SshTransport:
    """Send a request to `ltvm serve --stdio` on the host over SSH."""

    def __init__(self, host: Host) -> None:
        self.host = host
        self.target = parse_url(host.url)
        if not host.key or not host.hostkey:
            raise ValueError(f"host {host.name!r} has no key or no host key")

    def ssh_argv(self, command: str) -> list[str]:
        """The ssh command line.  It reads no ssh_config file."""
        kh = str(known_hosts_path(self.host.key))
        argv = [
            "ssh",
            "-F",
            "/dev/null",
            "-T",
            "-i",
            self.host.key,
            "-p",
            str(self.target.port),
            "-l",
            self.target.user,
        ]
        argv += _opt("IdentitiesOnly", "yes")
        argv += _opt("IdentityAgent", "none")
        argv += _opt("BatchMode", "yes")
        argv += _opt("StrictHostKeyChecking", "yes")
        argv += _opt("UserKnownHostsFile", kh)
        argv += _opt("GlobalKnownHostsFile", "/dev/null")
        argv += _opt("HostKeyAlias", host_alias(self.host.name))
        argv += _opt("HostKeyAlgorithms", "ssh-ed25519")
        argv += _opt("UpdateHostKeys", "no")
        argv += _opt("ForwardAgent", "no")
        argv += _opt("ForwardX11", "no")
        argv += _opt("ClearAllForwardings", "yes")
        argv += _opt("PermitLocalCommand", "no")
        argv += _opt("ControlMaster", "no")
        argv += _opt("ControlPath", "none")
        argv += _opt("ConnectTimeout", str(CONNECT_TIMEOUT))
        argv += _opt("ServerAliveInterval", str(SERVER_ALIVE_INTERVAL))
        argv += _opt("ServerAliveCountMax", str(SERVER_ALIVE_COUNT))
        argv += ["--", self.target.addr]
        if command:
            argv.append(command)
        return argv

    def request(self, argv: list[str], data: bytes = b"") -> Response:
        return self.send(shlex.join(argv), data)

    def send(self, command: str, data: bytes = b"") -> Response:
        if not known_hosts_path(self.host.key).exists():
            pin_host_key(self.host)
        limit = request_timeout()
        try:
            proc = run(
                self.ssh_argv(command),
                input=stdin_bytes(data),
                capture_output=True,
                text=False,
                timeout=limit,
            )
        except FileNotFoundError:
            raise RemoteError("unreachable", "ssh is not installed") from None
        except subprocess.TimeoutExpired:
            return Response(
                1,
                "",
                "",
                RemoteError(
                    "timed_out",
                    f"the host did not finish the request in {limit:g} s",
                    hint=f"Set {TIMEOUT_ENV} to a longer time in seconds "
                    "(0 for no limit).",
                ),
            )
        out, err = text(proc.stdout), text(proc.stderr)
        if proc.returncode == 255 and not out:
            # The hint holds the ssh message, so stderr stays clean
            # for --json.
            return Response(255, "", "", _ssh_error(err))
        raw: object = proc.stdout  # bytes: run() got text=False
        return parse_reply(
            proc.returncode,
            out,
            err,
            raw if isinstance(raw, bytes) else None,
        )


def _ssh_error(stderr: str) -> RemoteError:
    """Make a RemoteError from the stderr of an ssh that failed."""
    if (
        "Host key verification failed" in stderr
        or "REMOTE HOST IDENTIFICATION HAS CHANGED" in stderr
    ):
        return RemoteError(
            "host_key_mismatch",
            "the host key is not the pinned key",
            hint="Do not use this host.  Ask its owner for the correct "
            "fingerprint.",
        )
    lines = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    said = lines[-1] if lines else "no output"
    hint = f"ssh said: {said}"
    if "Permission denied" in stderr:
        hint += (
            ".  The host refused the key: the owner must run `ltvm serve "
            "authorize` for it, with a --from address that the host sees "
            "for this machine."
        )
    return RemoteError("unreachable", "cannot connect to the host", hint=hint)
