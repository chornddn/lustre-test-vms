"""The settings of `serve` on the host: ``serve.toml``.

The file is ``$XDG_CONFIG_HOME/ltvm/serve.toml``, which is
``~/.config/ltvm/serve.toml`` of the dedicated user.  It is flat TOML:
one ``key = value`` on each line, with no tables.  A cap that is not in
the file has no limit.  A missing file gives the defaults.

Python 3.10 has no tomllib.  There, a small parser reads the same flat
format: true and false, integers, floats and quoted strings.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any


@dataclass
class ServeConfig:
    enabled: bool = True
    max_vcpus: int | None = None  # None: no cap
    max_ram_gb: float | None = None
    max_disk_gb: float | None = None
    lease_hours: float = 8.0
    ac_only: bool = False


# The type that each key must have in the file.  An int is also a
# float.
_TYPES: dict[str, tuple[type, ...]] = {
    "enabled": (bool,),
    "max_vcpus": (int,),
    "max_ram_gb": (int, float),
    "max_disk_gb": (int, float),
    "lease_hours": (int, float),
    "ac_only": (bool,),
}


def config_path(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or str(
        Path(env.get("HOME") or Path.home()) / ".config"
    )
    return Path(base) / "ltvm" / "serve.toml"


_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _value(text: str) -> Any:
    if text in ("true", "false"):
        return text == "true"
    m = _STRING_RE.match(text)
    if m:
        return m.group(1).encode().decode("unicode_escape")
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"cannot read the value {text!r}") from None


def _parse_flat(text: str) -> dict[str, Any]:
    """Read flat TOML when tomllib is not available."""
    data: dict[str, Any] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, rest = line.partition("=")
        if not sep:
            raise ValueError(f"line {n}: no '='")
        rest = rest.strip()
        m = _STRING_RE.match(rest)
        if m:
            value_text, tail = m.group(0), rest[m.end() :]
        else:
            value_text, _, tail = rest.partition("#")
            tail = "#" + tail if tail else ""
        if tail.strip() and not tail.strip().startswith("#"):
            raise ValueError(f"line {n}: text after the value")
        data[key.strip()] = _value(value_text.strip())
    return data


def loads(text: str) -> ServeConfig:
    if sys.version_info >= (3, 11):
        import tomllib

        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"serve.toml: {e}") from None
    else:
        data = _parse_flat(text)
    values: dict[str, Any] = {}
    for key, kinds in _TYPES.items():
        if key not in data:
            continue
        value = data[key]
        # bool is an int in Python.  A cap of true is an error.
        if isinstance(value, bool) != (kinds == (bool,)) or not isinstance(
            value, kinds
        ):
            raise ValueError(f"serve.toml: {key} has a bad value {value!r}")
        values[key] = value
    return ServeConfig(**values)


def load(env: Mapping[str, str]) -> ServeConfig:
    """Read serve.toml.  Raise ValueError for a file that is not valid."""
    try:
        text = config_path(env).read_text()
    except FileNotFoundError:
        return ServeConfig()
    return loads(text)


def dumps(cfg: ServeConfig) -> str:
    lines = []
    for f in fields(cfg):
        value = getattr(cfg, f.name)
        if value is None:
            continue
        if isinstance(value, bool):
            text = "true" if value else "false"
        else:
            text = repr(value)
        lines.append(f"{f.name} = {text}")
    return "\n".join(lines) + "\n"


def save(env: Mapping[str, str], cfg: ServeConfig) -> None:
    """Write serve.toml in one step, mode 0600."""
    path = config_path(env)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".serve.toml.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(dumps(cfg))
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
