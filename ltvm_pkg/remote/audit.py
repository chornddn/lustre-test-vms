"""The append-only request log of `serve` on the host.

`serve` writes one JSON line for each request, refused requests too:

    {"time": "2026-10-19T14:00:00Z", "key": "SHA256:...", "verb": "list",
     "args": ["--json"], "result": "ok"}

"result" is "ok", "exit <N>" for a verb that failed, or the error code
of a refusal.  The log is ``$XDG_STATE_HOME/ltvm/serve.log``, which is
``~/.local/state/ltvm/serve.log`` under sshd.

The key fingerprint comes from the file that SSH_USER_AUTH names.  sshd
writes that file only with ``ExposeAuthInfo yes``.  Without it the key
is "-".
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path

NO_KEY = "-"


def log_path(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_STATE_HOME") or str(
        Path(env.get("HOME") or Path.home()) / ".local" / "state"
    )
    return Path(base) / "ltvm" / "serve.log"


def key_fingerprint(env: Mapping[str, str]) -> str:
    """The SHA256 fingerprint of the client key, as ssh-keygen -l shows it."""
    auth = env.get("SSH_USER_AUTH")
    if not auth:
        return NO_KEY
    try:
        lines = Path(auth).read_text().splitlines()
    except OSError:
        return NO_KEY
    for line in lines:
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "publickey":
            try:
                blob = base64.b64decode(parts[2], validate=True)
            except (binascii.Error, ValueError):
                return NO_KEY
            digest = base64.b64encode(hashlib.sha256(blob).digest())
            return "SHA256:" + digest.decode().rstrip("=")
    return NO_KEY


def append(path: Path, *, key: str, argv: list[str], result: str) -> None:
    """Add one line to the log.  Make the log 0600 if it is new."""
    entry = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "key": key,
        "verb": argv[0] if argv else "",
        "args": argv[1:],
        "result": result,
    }
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(entry) + "\n")
