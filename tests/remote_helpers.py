"""Helpers that the remote host tests share."""

from __future__ import annotations

from pathlib import Path


def write_serve_toml(home: Path, text: str = "") -> None:
    """Write serve.toml of the host user, as `serve init` does.

    serve refuses every request when the file is missing.
    """
    path = home / ".config" / "ltvm" / "serve.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
