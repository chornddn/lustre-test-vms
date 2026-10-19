"""Refuse a remote build when podman does not isolate it.

A remote build runs code from the controller in the build container.
The container is the isolation, so the container must not run as root
on the host:

- macOS: podman runs the containers in the podman machine, a VM.
- Linux and WSL2: podman must be rootless.  `podman info` tells.  A
  podman as the dedicated user is usually rootless, but CONTAINER_HOST
  or a podman wrapper can point it at a rootful service.

A request that cannot get an answer from podman is refused too.
"""

from __future__ import annotations

import subprocess
import sys

from .protocol import RemoteError

# The requests that run the build container.
_BUILD_REQUESTS = (
    ["build", "all"],
    ["build", "container"],
    ["build", "kernel"],
    ["build", "image"],
    ["build", "zfs"],
    ["build", "mofed-kmods"],
    ["deploy-lustre"],
    ["cluster", "deploy"],
)


def _platform() -> str:
    return sys.platform


def _rootless() -> str:
    """The answer of podman to "rootless?", or its error."""
    try:
        r = subprocess.run(
            ["podman", "info", "--format", "{{.Host.Security.Rootless}}"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"cannot run podman: {e}"
    if r.returncode != 0:
        lines = r.stderr.strip().splitlines()
        return lines[-1] if lines else f"podman info exit {r.returncode}"
    return r.stdout.strip()


def needs_build(argv: list[str]) -> bool:
    return any(argv[: len(r)] == r for r in _BUILD_REQUESTS)


def check(argv: list[str]) -> None:
    """Raise podman_unsafe if argv builds and podman is not isolated."""
    if not needs_build(argv) or _platform() == "darwin":
        return
    answer = _rootless()
    if answer == "true":
        return
    raise RemoteError(
        "podman_unsafe",
        "podman on this host is not rootless and not in a VM, so a "
        "remote build cannot run"
        + ("" if answer in ("false", "") else f" ({answer})"),
        hint="The host owner makes podman rootless for the ltvm user, "
        "and removes CONTAINER_HOST from its environment.",
    )
