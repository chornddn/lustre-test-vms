"""`ltvm serve init`: the wizard that writes serve.toml.

The dedicated user runs it (`install --serve` starts it with `sudo -u`).
It finds the CPUs, the RAM and the free disk of the host, and it
suggests the default caps.  Enter accepts a default.  `--yes` accepts
all of them, and a flag such as `--max-vcpus` sets one value with no
question.  The AC power question comes only on a laptop.
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TextIO

from ltvm_pkg.remote.serve_config import ServeConfig

# The default caps, from the spec.
CPU_SHARE = 0.8
RAM_SHARE = 0.8
DISK_SHARE = 0.25
DISK_MAX_GB = 100.0
LEASE_HOURS = 8.0
MAX_LEASE_HOURS = 24.0


@dataclass(frozen=True)
class HostFacts:
    cpus: int
    ram_gb: float
    free_disk_gb: float
    laptop: bool


def defaults(facts: HostFacts) -> ServeConfig:
    """The caps that the wizard suggests for this host."""
    return ServeConfig(
        max_vcpus=max(1, math.floor(facts.cpus * CPU_SHARE)),
        max_ram_gb=round(facts.ram_gb * RAM_SHARE, 1),
        max_disk_gb=round(min(DISK_MAX_GB, facts.free_disk_gb * DISK_SHARE), 1),
        lease_hours=LEASE_HOURS,
        max_lease_hours=MAX_LEASE_HOURS,
        ac_only=facts.laptop,
    )


POWER_SUPPLY = Path("/sys/class/power_supply")
GIB = 2**30


def _pmset() -> str:
    try:
        r = subprocess.run(
            ["pmset", "-g", "batt"], capture_output=True, text=True, check=False
        )
    except OSError:
        return ""
    return r.stdout


def is_laptop(
    system: str,
    *,
    pmset: Callable[[], str] = _pmset,
    power_supply: Path = POWER_SUPPLY,
) -> bool:
    """True when the host has a battery."""
    if system == "Darwin":
        return "InternalBattery" in pmset()
    try:
        supplies = list(power_supply.iterdir())
    except OSError:
        return False
    for supply in supplies:
        try:
            if (supply / "type").read_text().strip() == "Battery":
                return True
        except OSError:
            continue
    return False


def _ram_mb() -> int:
    from ltvm_pkg.qemu_run import _read_meminfo_mb

    return _read_meminfo_mb("MemTotal")


def probe(disk_paths: Iterable[Path], *, laptop: bool) -> HostFacts:
    """Find the CPUs, the RAM and the free disk of this host.

    The free disk is the smallest free space of the disk_paths that
    exist.  Remote VMs use the VM directory and the home of the
    dedicated user.
    """
    free = [
        shutil.disk_usage(str(p)).free for p in disk_paths if Path(p).exists()
    ]
    return HostFacts(
        cpus=os.cpu_count() or 1,
        ram_gb=round(_ram_mb() / 1024, 1),
        free_disk_gb=round(min(free) / GIB, 1) if free else 0.0,
        laptop=laptop,
    )


def _cap(kind: Callable[[str], Any]) -> Callable[[str], Any]:
    """Parse a cap: a number above 0, or "none" for no cap."""

    def parse(text: str) -> Any:
        if text.lower() == "none":
            return None
        value = kind(text)
        if value <= 0:
            raise ValueError
        return value

    return parse


def _positive(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise ValueError
    return value


def _yes_no(text: str) -> bool:
    if text.lower() in ("y", "yes"):
        return True
    if text.lower() in ("n", "no"):
        return False
    raise ValueError


def _show(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "y" if value else "n"
    return f"{value:g}" if isinstance(value, float) else str(value)


def _questions(
    facts: HostFacts,
) -> list[tuple[str, str, Callable[[str], Any], str]]:
    """(field, question, parser, hint for a bad answer) for each value."""
    questions = [
        (
            "max_vcpus",
            f"Max vCPUs for all remote VMs (this host has {facts.cpus})",
            _cap(int),
            "Give a whole number above 0, or none.",
        ),
        (
            "max_ram_gb",
            f"Max RAM in GB (this host has {facts.ram_gb:g})",
            _cap(float),
            "Give a number above 0, or none.",
        ),
        (
            "max_disk_gb",
            f"Max disk in GB (this host has {facts.free_disk_gb:g} free)",
            _cap(float),
            "Give a number above 0, or none.",
        ),
        (
            "lease_hours",
            "Default lease in hours",
            _positive,
            "Give a number above 0.",
        ),
        (
            "max_lease_hours",
            "Longest lease in hours that a remote request can ask for",
            _positive,
            "Give a number above 0.",
        ),
    ]
    if facts.laptop:
        questions.append(
            (
                "ac_only",
                "Run remote VMs only on AC power (y/n)",
                _yes_no,
                "Give y or n.",
            )
        )
    return questions


def wizard(
    facts: HostFacts,
    *,
    overrides: Mapping[str, Any],
    yes: bool,
    stdin: TextIO,
    stdout: TextIO,
) -> ServeConfig:
    """Ask for each cap and return the result.

    A value in overrides (from a flag) gets no question.  With yes, no
    value gets a question.  An empty answer or the end of stdin accepts
    the default.
    """
    cfg = replace(defaults(facts), **overrides)
    if yes:
        return cfg
    for field, question, parse, hint in _questions(facts):
        if field in overrides:
            continue
        default = getattr(cfg, field)
        while True:
            stdout.write(f"{question} [{_show(default)}]: ")
            stdout.flush()
            answer = stdin.readline()
            if not answer:  # end of input
                stdout.write("\n")
                value = default
                break
            answer = answer.strip()
            if not answer:
                value = default
                break
            try:
                value = parse(answer)
                break
            except ValueError:
                stdout.write(f"{hint}\n")
        cfg = replace(cfg, **{field: value})
    return cfg


# The serve.toml fields that a flag can set, with the flag name.
CAP_FLAGS = {
    "max_vcpus": "--max-vcpus",
    "max_ram_gb": "--max-ram-gb",
    "max_disk_gb": "--max-disk-gb",
    "lease_hours": "--lease-hours",
    "max_lease_hours": "--max-lease-hours",
}


def _arg(kind: Callable[[str], Any]) -> Callable[[str], Any]:
    def parse(text: str) -> Any:
        try:
            value = kind(text)
        except ValueError:
            value = 0
        if value <= 0:
            raise argparse.ArgumentTypeError(f"{text!r} is not above 0")
        return value

    return parse


def add_cap_args(parser: argparse.ArgumentParser) -> None:
    """The wizard flags, for `serve init` and `install --serve`."""
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Accept every default cap and ask nothing",
    )
    parser.add_argument(
        "--max-vcpus",
        type=_arg(int),
        metavar="N",
        help="The vCPU cap for all remote VMs (default: 80%% of the CPUs)",
    )
    parser.add_argument(
        "--max-ram-gb",
        type=_arg(float),
        metavar="GB",
        help="The RAM cap (default: 80%% of the RAM)",
    )
    parser.add_argument(
        "--max-disk-gb",
        type=_arg(float),
        metavar="GB",
        help="The disk cap (default: 100 GB or 25%% of the free disk, "
        "the smaller)",
    )
    parser.add_argument(
        "--lease-hours",
        type=_arg(float),
        metavar="H",
        help="The default lease (default: 8)",
    )
    parser.add_argument(
        "--max-lease-hours",
        type=_arg(float),
        metavar="H",
        help="The longest lease that a remote request can ask for "
        "(default: 24)",
    )
    parser.add_argument(
        "--ac-only",
        dest="ac_only",
        action="store_true",
        default=None,
        help="Run remote VMs only on AC power (default on a laptop)",
    )
    parser.add_argument(
        "--no-ac-only",
        dest="ac_only",
        action="store_false",
        help="Run remote VMs on battery power too",
    )


def overrides_from(args: argparse.Namespace) -> dict[str, Any]:
    """The values that the flags set."""
    found = {
        field: getattr(args, field, None) for field in (*CAP_FLAGS, "ac_only")
    }
    return {k: v for k, v in found.items() if v is not None}


def cap_argv(args: argparse.Namespace) -> list[str]:
    """The same flags again, for a `serve init` child."""
    argv = ["--yes"] if getattr(args, "yes", False) else []
    values = overrides_from(args)
    for field, flag in CAP_FLAGS.items():
        if field in values:
            argv += [flag, str(values[field])]
    if "ac_only" in values:
        argv.append("--ac-only" if values["ac_only"] else "--no-ac-only")
    return argv
