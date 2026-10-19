"""The power guard and the AC-only cap of a remote host.

While at least one lease is active, the host must not go to sleep.  A
lease is active when it has not expired.  Its VM or cluster must also
exist.  serve is not a daemon: each request is a short process.  So
the guard is a detached holder process that serve starts, and that
serve or `serve reap` stops when no lease is active.  A JSON file of
the dedicated user, next to the lease file, records the pid of the
holder.

- macOS: `caffeinate -s`, a PreventSystemSleep assertion.  It applies
  only on AC power.  It stops idle sleep, not a lid close: only
  `pmset disablesleep` (root) stops the sleep on a lid close, and
  serve never uses it.  Do not add -d, -u or -i: they keep the display
  on, or they also apply on battery.
- Linux: `systemd-inhibit --what=sleep --mode=block`.  logind ignores
  a sleep lock for the lid switch, because LidSwitchIgnoreInhibited=
  is "yes" by default.  The lock does not include handle-lid-switch.
- WSL2: nothing.  Windows decides when the host sleeps.

With `ac_only` in serve.toml, a create on battery gets on_battery
(admission.py calls on_battery()).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import platform
import signal
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

from . import leases

MACOS = "macos"
LINUX = "linux"
WSL2 = "wsl2"
OTHER = "other"

POWER_SUPPLY = Path("/sys/class/power_supply")

# A supply of one of these types that is online powers the host.
_AC_TYPES = ("Mains", "USB", "UPS", "Wireless")


def system() -> str:
    """The kind of host, for the inhibit command and the power source."""
    from ltvm_pkg import host_setup

    name = platform.system()
    if name == "Darwin":
        return MACOS
    if name == "Linux":
        return WSL2 if host_setup.is_wsl2() else LINUX
    return OTHER


def inhibit_argv(kind: str) -> list[str] | None:
    """The command that keeps the host awake while it runs, or None."""
    if kind == MACOS:
        return ["caffeinate", "-s"]
    if kind == LINUX:
        return [
            "systemd-inhibit",
            "--what=sleep",
            "--mode=block",
            "--who=ltvm serve",
            "--why=a remote VM lease is active",
            "sleep",
            "infinity",
        ]
    return None


# On Linux, logind asks polkit before it gives a sleep lock.  The
# default refuses it to a user with no active local session, such as
# the dedicated user over ssh or in the reap timer.  `install --serve`
# installs this rule (serve_install.py).
POLKIT_RULE = Path("/etc/polkit-1/rules.d/50-ltvm-serve.rules")


def polkit_rule(user: str) -> str:
    return f"""\
// ltvm serve: the user {user} may block sleep while a remote VM
// lease is active.  `ltvm install --serve` installs this file.
polkit.addRule(function(action, subject) {{
    if (action.id == "org.freedesktop.login1.inhibit-block-sleep" &&
        subject.user == "{user}") {{
        return polkit.Result.YES;
    }}
}});
"""


# ── the power source ─────────────────────────────────────


def _pmset_batt() -> str | None:
    try:
        return subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _attr(supply: Path, name: str) -> str:
    try:
        return (supply / name).read_text().strip()
    except OSError:
        return ""


def _linux_on_battery() -> bool:
    try:
        supplies = sorted(POWER_SUPPLY.iterdir())
    except OSError:
        return False
    battery = False
    for supply in supplies:
        kind = _attr(supply, "type")
        if kind in _AC_TYPES and _attr(supply, "online") == "1":
            return False
        # A battery with scope "Device" is in a mouse or a keyboard.
        if kind == "Battery" and _attr(supply, "scope") != "Device":
            battery = True
    return battery


def on_battery(kind: str | None = None) -> bool:
    """True when the host runs on its battery.

    A host with no battery, or a probe that fails, counts as AC power.
    """
    kind = kind or system()
    if kind == MACOS:
        text = _pmset_batt()
        return text is not None and "'Battery Power'" in text
    if kind in (LINUX, WSL2):
        return _linux_on_battery()
    return False


# ── the holder process ───────────────────────────────────


# A holder that polkit refuses stops in less than this time.
SETTLE_S = 0.5


def _spawn(argv: list[str], log: Path) -> int:
    """Start the holder detached.  Return its pid.

    The holder must not keep the stdio of serve open: sshd closes the
    session only when every copy of the channel is closed.  Its stderr
    goes to log.  Raise RuntimeError when the holder stops at once, for
    example when polkit refuses the sleep lock.
    """
    with open(log, "w") as err:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err,
            close_fds=True,
            start_new_session=True,
        )
    try:
        rc = proc.wait(timeout=SETTLE_S)
    except subprocess.TimeoutExpired:
        return proc.pid
    try:
        why = log.read_text().strip()
    except OSError:
        why = ""
    raise RuntimeError(f"{argv[0]} stopped with exit {rc}: {why or '-'}")


def _alive(pid: int, argv: list[str]) -> bool:
    """True when pid is still the holder that serve started.

    The holder leads its own process group.  The name check stops a
    pid that the kernel gave to another process.
    """
    try:
        os.kill(pid, 0)
        if os.getpgid(pid) != pid:
            return False
        name = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return Path(name).name == Path(argv[0]).name


def _stop(pid: int) -> None:
    # The process group also holds the `sleep` of systemd-inhibit.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGTERM)


def state_path(env: Mapping[str, str]) -> Path:
    return leases.store_path(env).with_name("power-guard.json")


@contextlib.contextmanager
def _locked(env: Mapping[str, str]) -> Iterator[Path]:
    """Two serve processes must not start two holders."""
    path = state_path(env)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(
        path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        os.close(fd)


def _holder(path: Path) -> tuple[int, list[str]] | None:
    try:
        data = json.loads(path.read_text())
        return int(data["pid"]), [str(a) for a in data["argv"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ── the guard ────────────────────────────────────────────


def _exists(lease: leases.Lease) -> bool:
    from ltvm_pkg.vm_state import ClusterInfo, VMInfo

    if lease.kind == leases.CLUSTER:
        return lease.name in ClusterInfo.all_names()
    return lease.name in VMInfo.all_names()


def active_leases(env: Mapping[str, str]) -> list[leases.Lease]:
    """The leases that have not expired and whose VM or cluster exists."""
    now = leases.clock()
    return [
        lease
        for lease in leases.load_all(env).values()
        if not lease.expired(now) and _exists(lease)
    ]


def sync(env: Mapping[str, str], *, busy: bool = False) -> bool:
    """Start or stop the holder to agree with the active leases.

    busy holds the guard with no active lease, for a create whose VM
    does not exist yet.  Return True when a holder runs after the call.
    A failure prints a warning to stderr (the controller sees it after
    the verb, and the journal keeps it for `serve reap`).  It never
    fails the request.
    """
    from .serve import test_layout

    # An exec:// child in the tests gets none of the conftest patches.
    if test_layout(env):
        return False
    argv = inhibit_argv(system())
    if argv is None:
        return False
    try:
        want = busy or bool(active_leases(env))
        with _locked(env) as path:
            held = _holder(path)
            running = held is not None and _alive(*held)
            if want and running:
                return True
            if held is not None and running:
                _stop(held[0])
            path.unlink(missing_ok=True)
            if not want:
                return False
            pid = _spawn(argv, path.with_suffix(".log"))
            path.write_text(json.dumps({"pid": pid, "argv": argv}) + "\n")
            return True
    except (OSError, ValueError, RuntimeError) as e:
        print(
            f"ltvm serve: cannot change the sleep guard: {e}", file=sys.stderr
        )
        return False


def makes_vm(argv: list[str]) -> bool:
    """True for a request that makes a VM or a cluster."""
    return argv[:1] == ["create"] or argv[:2] == ["cluster", "create"]
