"""Protocol version, error codes and the version handshake.

The protocol version is separate from the ltvm version.  Increase
PROTOCOL_VERSION only for a change that an older peer cannot use, such
as a new request format or a new meaning for a reply field.  A new verb
on the allow-list does not change the protocol: an older host refuses
the verb with verb_refused.

The handshake is one JSON line in each direction:

- The controller writes ``{"protocol": N, "ltvm": "<version>"}`` to the
  stdin of `serve`.
- `serve` writes ``{"protocol": N, "ltvm": "<version>", "error": E}`` as
  the first line of its stdout.  E is null, or an object with "code",
  "message" and "hint", and "details" when the error has data, such as
  the requested, used and cap values of over_cap.  The verb output
  follows that line.

Do not change the shape of these two lines.  Peers with different
protocol versions must still read them to report version_mismatch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1

# Each code that a remote request can fail with, and its meaning.  The
# code goes in the "code" field of the JSON error envelope.
ERROR_CODES: dict[str, str] = {
    "version_mismatch": "the controller and the host use different "
    "protocol versions",
    "verb_refused": "the verb is not on the serve allow-list",
    "bad_request": "serve cannot read the request",
    "bad_reply": "the controller cannot read the reply from serve",
    "unknown_host": "the controller has no host with that name",
    "layout_unsupported": "the host does not have the shared layout, or "
    "serve runs as root",
    "needs_root": "the request needs root on the host",
    "path_refused": "a path argument is not allowed on a remote host",
    "vm_not_found": "no VM that a remote request made has that name",
    "cluster_not_found": "no cluster that a remote request made has that name",
    "name_in_use": "a VM or cluster that the host owner made has that name",
    "unreachable": "the controller cannot connect to the host",
    "timed_out": "the host did not finish the request in time",
    "host_key_mismatch": "the host key is not the key that the "
    "controller pinned",
    "over_cap": "the request goes above a cap that the host owner set",
    "bad_config": "serve cannot read its settings in serve.toml",
    "podman_unsafe": "podman on the host is not rootless and not in a VM",
}


@dataclass
class RemoteError(Exception):
    """A refusal or a failure of one remote request."""

    code: str
    message: str
    hint: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:
            raise ValueError(f"unknown remote error code {self.code!r}")
        super().__init__(self.message)

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
        }
        if self.details:
            # An older controller ignores the key that it does not know.
            data["details"] = self.details
        return data

    @classmethod
    def from_json(cls, data: Any) -> RemoteError:
        if not isinstance(data, dict):
            raise ValueError("error is not an object")
        return cls(
            code=str(data.get("code")),
            message=str(data.get("message", "")),
            hint=data.get("hint") or None,
            details=dict(data.get("details") or {}),
        )


def hello_line(ltvm_version: str) -> str:
    """The first line that the controller sends."""
    return json.dumps({"protocol": PROTOCOL_VERSION, "ltvm": ltvm_version})


def reply_line(ltvm_version: str, error: RemoteError | None) -> str:
    """The first line that serve sends."""
    return json.dumps(
        {
            "protocol": PROTOCOL_VERSION,
            "ltvm": ltvm_version,
            "error": error.to_json() if error else None,
        }
    )


def parse_line(line: str) -> dict[str, Any]:
    """Read a hello or a reply line.  Raise ValueError if it is bad."""
    data = json.loads(line)
    if not isinstance(data, dict) or not isinstance(data.get("protocol"), int):
        raise ValueError("no protocol version")
    return data
