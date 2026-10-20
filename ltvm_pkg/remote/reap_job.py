"""The job that runs `ltvm serve reap` on a remote host.

On macOS it is a LaunchDaemon in the system domain with ``UserName`` set
to the dedicated user.  A LaunchAgent does not run for a user who never
logs in, and the dedicated user never logs in.  On Linux it is a systemd
timer with a oneshot service that runs as the dedicated user.  WSL2 runs
systemd only when /etc/wsl.conf has ``[boot] systemd=true``.

`install --serve` calls install().  It writes the files with
priv.atomic_write and runs launchctl or systemctl with priv.sudo_run,
unless the caller gives other functions.
"""

from __future__ import annotations

import platform
import plistlib
from collections.abc import Callable
from pathlib import Path

LABEL = "io.github.ltvm.serve-reap"
UNIT = "ltvm-serve-reap"
INTERVAL_S = 300

LAUNCH_DAEMONS = Path("/Library/LaunchDaemons")
SYSTEMD_DIR = Path("/etc/systemd/system")

WriteFn = Callable[[Path, str, int], None]
RunFn = Callable[..., object]


def launchd_plist(
    ltvm: str, user: str, home: str, interval: int = INTERVAL_S
) -> str:
    """The LaunchDaemon plist.

    launchd does not set HOME for UserName.  The launchd PATH has no
    Homebrew directory, so the job gets the PATH of the forced command.
    """
    from ltvm_pkg.remote.serve_install import FORCED_PATH

    job = {
        "Label": LABEL,
        "UserName": user,
        "ProgramArguments": [ltvm, "serve", "reap"],
        "EnvironmentVariables": {"HOME": home, "PATH": FORCED_PATH},
        "StartInterval": interval,
        "RunAtLoad": True,
    }
    return plistlib.dumps(job).decode()


def systemd_units(
    ltvm: str, user: str, interval: int = INTERVAL_S
) -> dict[str, str]:
    """The service and timer units, by file name."""
    service = (
        "[Unit]\n"
        "Description=ltvm: destroy remote VMs with an expired lease\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"User={user}\n"
        f"ExecStart={ltvm} serve reap\n"
    )
    timer = (
        "[Unit]\n"
        "Description=ltvm: run serve reap\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=60s\n"
        f"OnUnitActiveSec={interval}s\n"
        f"Unit={UNIT}.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return {f"{UNIT}.service": service, f"{UNIT}.timer": timer}


def _default_write(path: Path, text: str, mode: int) -> None:
    from ltvm_pkg import priv

    priv.atomic_write(path, text, mode)


def _default_run(cmd: list[str], *, check: bool) -> object:
    from ltvm_pkg import priv

    return priv.sudo_run(cmd, check=check)


def install(
    ltvm: str,
    user: str,
    home: str,
    *,
    system: str | None = None,
    write: WriteFn | None = None,
    run: RunFn | None = None,
) -> None:
    """Install and start the reap job for `user`.

    ltvm is the absolute path of the ltvm that the job runs.  home is the
    home directory of user.  Raise ValueError on a system other than
    macOS and Linux.
    """
    system = system or platform.system()
    write = write or _default_write
    run = run or _default_run
    if system == "Darwin":
        path = LAUNCH_DAEMONS / f"{LABEL}.plist"
        write(path, launchd_plist(ltvm, user, home), 0o644)
        # bootstrap refuses a job that is loaded.  Remove the old one.
        run(["launchctl", "bootout", f"system/{LABEL}"], check=False)
        run(["launchctl", "bootstrap", "system", str(path)], check=True)
    elif system == "Linux":
        for name, text in systemd_units(ltvm, user).items():
            write(SYSTEMD_DIR / name, text, 0o644)
        run(["systemctl", "daemon-reload"], check=True)
        run(["systemctl", "enable", "--now", f"{UNIT}.timer"], check=True)
    else:
        raise ValueError(f"no reap job for {system}")
