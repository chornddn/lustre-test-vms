"""The host registry on the controller.

The registry is one JSON file, ``$XDG_CONFIG_HOME/ltvm/hosts.json``.  JSON
and not TOML, because tomllib needs Python 3.11 and the floor is 3.10.

Each entry has a name and a URL.  The URL scheme selects the transport:

- ``exec://<path to the ltvm script>`` starts `serve` as a local child
  process.  It is for tests only.
- ``ssh://`` comes in a later change.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .protocol import RemoteError

if TYPE_CHECKING:
    from .transport import Transport

# A host name goes into `<host>/<vm>` on the controller, so it has no "/".
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass
class Host:
    name: str
    url: str
    # exec:// only: variables added to the environment of the child.
    # Tests use them to point the host at temporary state.
    env: dict[str, str] = field(default_factory=dict)
    # exec:// only: the public key that the child gets as its client
    # key, as sshd gives it with ExposeAuthInfo.
    pubkey: str = ""


def registry_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ltvm" / "hosts.json"


def _load() -> dict[str, Host]:
    path = registry_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    return {
        name: Host(name=name, **{k: v for k, v in entry.items() if k != "name"})
        for name, entry in data.get("hosts", {}).items()
    }


def _save(entries: dict[str, Host]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"hosts": {n: asdict(h) for n, h in sorted(entries.items())}}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def add(host: Host) -> None:
    """Add a host.  Raise ValueError for a bad or a duplicate name."""
    if not _NAME_RE.match(host.name):
        raise ValueError(f"bad host name {host.name!r}")
    entries = _load()
    if host.name in entries:
        raise ValueError(f"host {host.name!r} already exists")
    entries[host.name] = host
    _save(entries)


def list_hosts() -> list[Host]:
    return [h for _, h in sorted(_load().items())]


def remove(name: str) -> Host:
    """Remove a host and return its entry."""
    entries = _load()
    host = get(name, entries)
    del entries[name]
    _save(entries)
    return host


def get(name: str, entries: dict[str, Host] | None = None) -> Host:
    if entries is None:
        entries = _load()
    try:
        return entries[name]
    except KeyError:
        raise RemoteError(
            "unknown_host",
            f"no host named {name!r}",
            hint=f"The hosts are in {registry_path()}.",
        ) from None


def transport_for(name: str) -> Transport:
    """Resolve a host name to the transport that reaches it."""
    from .transport import for_host

    return for_host(get(name))
