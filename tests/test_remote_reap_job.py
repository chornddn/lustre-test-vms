"""The job that runs `ltvm serve reap`: a LaunchDaemon or a systemd timer.

`install --serve` calls reap_job.install.  These tests give
it a fake writer and a fake runner, so no test reaches launchctl,
systemctl or sudo.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

LTVM = "/opt/ltvm-sentinel/ltvm"
USER = "ltvm-user-sentinel"
HOME = "/var/ltvm-home-sentinel"


class Recorder:
    def __init__(self) -> None:
        self.files: dict[Path, tuple[str, int]] = {}
        self.commands: list[tuple[list[str], bool]] = []

    def write(self, path: Path, text: str, mode: int) -> None:
        self.files[path] = (text, mode)

    def run(self, cmd: list[str], *, check: bool) -> None:
        self.commands.append((cmd, check))


def test_launchd_job_runs_reap_as_the_dedicated_user() -> None:
    from ltvm_pkg.remote import reap_job

    plist = plistlib.loads(reap_job.launchd_plist(LTVM, USER, HOME).encode())
    assert plist["Label"] == reap_job.LABEL
    assert plist["UserName"] == USER
    assert plist["ProgramArguments"] == [LTVM, "serve", "reap"]
    assert plist["StartInterval"] == reap_job.INTERVAL_S
    assert plist["EnvironmentVariables"]["HOME"] == HOME


def test_launchd_job_path_has_the_homebrew_bin_directories() -> None:
    # launchd gives a job only /usr/bin:/bin:/usr/sbin:/sbin.  The reap
    # needs podman and sshpass from Homebrew.
    from ltvm_pkg.remote import reap_job

    plist = plistlib.loads(reap_job.launchd_plist(LTVM, USER, HOME).encode())
    dirs = plist["EnvironmentVariables"]["PATH"].split(":")
    assert "/opt/homebrew/bin" in dirs
    assert "/usr/local/bin" in dirs
    assert "/usr/bin" in dirs


def test_systemd_timer_runs_reap_as_the_dedicated_user() -> None:
    from ltvm_pkg.remote import reap_job

    units = reap_job.systemd_units(LTVM, USER)
    service = units[f"{reap_job.UNIT}.service"]
    timer = units[f"{reap_job.UNIT}.timer"]
    assert f"User={USER}\n" in service
    assert f"ExecStart={LTVM} serve reap\n" in service
    assert "Type=oneshot\n" in service
    assert f"OnUnitActiveSec={reap_job.INTERVAL_S}s\n" in timer
    assert f"Unit={reap_job.UNIT}.service\n" in timer
    assert "WantedBy=timers.target\n" in timer


def test_systemd_service_keeps_the_sleep_lock_holder() -> None:
    # power.sync() can start a systemd-inhibit holder that must live
    # after `serve reap` exits.  The default KillMode=control-group of a
    # oneshot service stops every process of the unit.
    from ltvm_pkg.remote import reap_job

    service = reap_job.systemd_units(LTVM, USER)[f"{reap_job.UNIT}.service"]
    assert "KillMode=process\n" in service


def test_install_on_macos_writes_the_daemon_and_loads_it() -> None:
    from ltvm_pkg.remote import reap_job

    rec = Recorder()
    reap_job.install(
        LTVM, USER, HOME, system="Darwin", write=rec.write, run=rec.run
    )
    path = Path("/Library/LaunchDaemons") / f"{reap_job.LABEL}.plist"
    assert list(rec.files) == [path]
    text, mode = rec.files[path]
    assert mode == 0o644
    assert plistlib.loads(text.encode())["UserName"] == USER
    assert rec.commands == [
        (["launchctl", "bootout", f"system/{reap_job.LABEL}"], False),
        (["launchctl", "bootstrap", "system", str(path)], True),
    ]


def test_install_on_linux_writes_the_units_and_starts_the_timer() -> None:
    from ltvm_pkg.remote import reap_job

    rec = Recorder()
    reap_job.install(
        LTVM, USER, HOME, system="Linux", write=rec.write, run=rec.run
    )
    unit_dir = Path("/etc/systemd/system")
    assert sorted(rec.files) == [
        unit_dir / f"{reap_job.UNIT}.service",
        unit_dir / f"{reap_job.UNIT}.timer",
    ]
    assert rec.commands == [
        (["systemctl", "daemon-reload"], True),
        (["systemctl", "enable", "--now", f"{reap_job.UNIT}.timer"], True),
    ]


def test_install_refuses_another_system() -> None:
    from ltvm_pkg.remote import reap_job

    rec = Recorder()
    with pytest.raises(ValueError, match="FreeBSD"):
        reap_job.install(
            LTVM, USER, HOME, system="FreeBSD", write=rec.write, run=rec.run
        )
    assert (rec.files, rec.commands) == ({}, [])
