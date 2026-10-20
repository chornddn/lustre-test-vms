"""`ltvm install --serve`: make this machine a remote host.

The normal install runs first.  Then install() does the steps that ltvm
can do:

1. It makes the dedicated user (``ltvm``) if it is not there.  On macOS
   the user is hidden, has no password and is not an admin.  On Linux
   and WSL2 the password is locked.  The shell is ``/bin/sh``.  The user
   is in the ``ltvm`` group and has its own artifacts directory.
2. It runs ``serve init`` (the wizard) as that user.
3. It installs the reap job (ltvm_pkg/remote/reap_job.py).  On Linux
   (not WSL2), it installs a polkit rule that lets the user take the
   sleep lock of the power guard (power.py).
4. It checks sshd for the user, and it reads the host key.

It never changes sshd, Remote Login or a VPN.  For each item that the
owner must do, it prints the steps.  The last step is the `host add`
command for the controller.

Every command that needs root goes through Deps.run, so the tests stub
all of them.
"""

from __future__ import annotations

import grp
import os
import platform
import pwd
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

USER = "ltvm"
GROUP = "ltvm"
SHELL = "/bin/sh"
REAL_NAME = "ltvm remote host"
LAUNCHER = "/usr/local/bin/ltvm"
SSHD = "/usr/sbin/sshd"
HOSTKEY_PUB = Path("/etc/ssh/ssh_host_ed25519_key.pub")
# Hidden users on macOS have a UID below 500.
MACOS_UIDS = range(401, 500)
MACOS_STAFF_GID = 20
# The sshd PATH for a forced command on macOS has no /usr/local/bin.
# Homebrew puts sshpass and podman in /opt/homebrew/bin on Apple Silicon
# and in /usr/local/bin on Intel.  A directory that is not there does no
# harm, so the PATH has both.
HOMEBREW_BIN = "/opt/homebrew/bin"
FORCED_PATH = f"{HOMEBREW_BIN}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# Rootless podman needs subordinate UIDs and GIDs for the user.
SUBID_FILES = (Path("/etc/subuid"), Path("/etc/subgid"))
SUBID_FIRST = 100000
SUBID_COUNT = 65536
# On macOS, Remote Login with "Only these users" uses this group.
ACCESS_SSH_GROUP = "com.apple.access_ssh"

RunFn = Callable[..., "subprocess.CompletedProcess[str]"]
ProbeFn = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class ServeInstallError(RuntimeError):
    pass


def _probe(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a read-only command with no elevation."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def _sudo_run(
    cmd: list[str], *, check: bool = True, quiet: bool = False
) -> subprocess.CompletedProcess[str]:
    from ltvm_pkg import priv

    if cmd[:1] == ["sudo"]:
        # `sudo -u ltvm`: already the elevation, with the terminal.
        return priv._run(cmd, check=check, quiet=quiet)
    return priv.sudo_run(cmd, check=check, quiet=quiet)


def _address() -> str | None:
    from ltvm_pkg.remote import ssh

    # A UDP connect selects the route and sends no packet.
    return ssh.source_address("192.0.2.1", 22)


def _hostname() -> str:
    from ltvm_pkg.cli.host import _controller_name

    return _controller_name()


def _root_writer(run: RunFn, system: str) -> Callable[[Path, str, int], None]:
    """A writer for files that root owns (launchd, systemd, polkit)."""
    group = "wheel" if system == "Darwin" else "root"

    def write(path: Path, text: str, mode: int) -> None:
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.chmod(tmp, 0o644)
            run(
                [
                    "install",
                    "-m",
                    f"{mode:o}",
                    "-o",
                    "root",
                    "-g",
                    group,
                    tmp,
                    str(path),
                ],
                quiet=True,
            )
        finally:
            os.unlink(tmp)

    return write


def _reap(*, ltvm: str, user: str, home: str, run: RunFn, system: str) -> str:
    from ltvm_pkg.remote import reap_job

    write = _root_writer(run, system)
    reap_job.install(ltvm, user, home, system=system, write=write, run=run)
    return "installed"


def _network(*, run: RunFn, system: str) -> str:
    from ltvm_pkg.remote import guest_net_install

    write = _root_writer(run, system)
    return guest_net_install.install(system=system, write=write, run=run)


def _polkit(deps: Deps, user: str) -> str:
    """Let the user take the sleep lock of the power guard (power.py).

    Only Linux needs the rule.  WSL2 has no power guard.
    """
    from ltvm_pkg.remote import power

    if deps.system != "Linux" or deps.wsl2:
        return "not needed"
    write = _root_writer(deps.run, deps.system)
    try:
        write(power.POLKIT_RULE, power.polkit_rule(user), 0o644)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as e:
        # priv.sudo_run raises RuntimeError for a failed command.
        return f"not installed: {e}"
    return "installed"


def _repo_read_paths() -> list[Path]:
    """The files that the launcher needs: the checkout and the Python."""
    repo = Path(__file__).resolve().parent.parent.parent
    paths = [repo, Path(sys.prefix), Path(sys.base_prefix)]
    paths.append(Path(os.path.realpath(sys.executable)).parent)
    seen: list[Path] = []
    for p in paths:
        if p not in seen:
            seen.append(p)
    return seen


def _brew_bin() -> str | None:
    """HOMEBREW_BIN if Homebrew is there (Apple Silicon), else None."""
    return HOMEBREW_BIN if os.path.isdir(HOMEBREW_BIN) else None


@dataclass
class Deps:
    """What install() uses from the host.  Tests give fakes."""

    system: str = field(default_factory=platform.system)
    wsl2: bool = False
    run: RunFn = _sudo_run
    probe: ProbeFn = _probe
    getpwnam: Callable[[str], Any] = pwd.getpwnam
    getgrnam: Callable[[str], Any] = grp.getgrnam
    uids: Callable[[], set[int]] = lambda: {p.pw_uid for p in pwd.getpwall()}
    ltvm: list[str] = field(
        default_factory=lambda: [
            sys.executable,
            str(Path(__file__).resolve().parent.parent.parent / "ltvm"),
        ]
    )
    launcher: str = LAUNCHER
    read_paths: list[Path] = field(default_factory=_repo_read_paths)
    hostkey_pub: Path = HOSTKEY_PUB
    subid_files: tuple[Path, Path] = SUBID_FILES
    address: Callable[[], str | None] = _address
    hostname: Callable[[], str] = _hostname
    reap: Callable[..., str] | None = None
    brew_bin: str | None = field(default_factory=_brew_bin)
    network: Callable[..., str] | None = None


# ── read access ──────────────────────────────────────────


def _bits(st: Any, uid: int, gids: set[int]) -> int:
    """The rwx bits of the mode that apply to uid and gids."""
    mode = int(st.st_mode)
    if st.st_uid == uid:
        return (mode >> 6) & 7
    if st.st_gid in gids:
        return (mode >> 3) & 7
    return mode & 7


def read_problems(
    paths: Iterable[Path],
    *,
    uid: int,
    gids: set[int],
    stat: Callable[[str], Any] = os.stat,
) -> list[str]:
    """The directories that a user with uid and gids cannot read.

    The user must enter every parent directory (x) and read each path
    (r and x).  Mode bits only: an ACL can give more access.
    """
    problems: list[str] = []
    for path in paths:
        parts = [*reversed(path.parents), path]
        for i, part in enumerate(parts):
            need = 5 if i == len(parts) - 1 else 1
            try:
                st = stat(str(part))
            except OSError:
                break
            if _bits(st, uid, gids) & need != need:
                text = (
                    f"{part} (mode {st.st_mode & 0o7777:04o}, "
                    f"uid {st.st_uid}, gid {st.st_gid})"
                )
                if text not in problems:
                    problems.append(text)
    return problems


# ── the user ─────────────────────────────────────────────


def _gid(deps: Deps, name: str) -> int | None:
    try:
        return int(deps.getgrnam(name).gr_gid)
    except KeyError:
        return None


def _free_uid(deps: Deps) -> int:
    used = deps.uids()
    for uid in MACOS_UIDS:
        if uid not in used:
            return uid
    raise ServeInstallError("no free UID for a hidden user below 500")


def _macos_user(user: str, home: str, uid: int) -> list[list[str]]:
    rec = f"/Users/{user}"
    attrs = [
        ("UniqueID", str(uid)),
        ("PrimaryGroupID", str(MACOS_STAFF_GID)),
        ("UserShell", SHELL),
        ("NFSHomeDirectory", home),
        ("RealName", REAL_NAME),
        # Not in the login window.
        ("IsHidden", "1"),
        # "*": no password can match, so no interactive login.
        ("Password", "*"),
    ]
    cmds = [["dscl", ".", "-create", rec]]
    cmds += [["dscl", ".", "-create", rec, k, v] for k, v in attrs]
    return cmds


def _linux_user(user: str, home: str, groups: list[str]) -> list[list[str]]:
    cmd = [
        "useradd",
        "--create-home",
        "--home-dir",
        home,
        "--shell",
        SHELL,
        "--gid",
        GROUP,
        "--comment",
        REAL_NAME,
    ]
    if groups:
        cmd += ["--groups", ",".join(groups)]
    # A locked password: only a key can log in.
    return [[*cmd, user], ["usermod", "--lock", user]]


def _subid_command(path: Path, flag: str, user: str) -> list[str] | None:
    """The usermod command that gives user a free range, or None.

    None when user has a range already.  The new range starts after
    the highest range in the file.
    """
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    end = SUBID_FIRST
    for line in lines:
        parts = line.strip().split(":")
        if len(parts) != 3:
            continue
        if parts[0] == user:
            return None
        try:
            end = max(end, int(parts[1]) + int(parts[2]))
        except ValueError:
            continue
    return ["usermod", flag, f"{end}-{end + SUBID_COUNT - 1}", user]


def _ensure_user(deps: Deps, user: str) -> dict[str, Any]:
    """Make the user, or keep the one that is there.  Return its facts."""
    macos = deps.system == "Darwin"
    ltvm_gid = _gid(deps, GROUP)
    if ltvm_gid is None:
        raise ServeInstallError(
            f"the {GROUP} group is not there: run the normal install first"
        )
    extra = [] if macos or _gid(deps, "kvm") is None else ["kvm"]
    try:
        pw = deps.getpwnam(user)
        created = False
        uid, home = int(pw.pw_uid), str(pw.pw_dir)
        primary = int(pw.pw_gid)
    except KeyError:
        created = True
        home = f"/Users/{user}" if macos else f"/home/{user}"
        if macos:
            uid = _free_uid(deps)
            primary = MACOS_STAFF_GID
            cmds = _macos_user(user, home, uid)
        else:
            uid = -1  # useradd picks it
            primary = ltvm_gid
            cmds = _linux_user(user, home, extra)
        for cmd in cmds:
            deps.run(cmd)
    if not macos:
        # Without these ranges rootless podman cannot run as the user.
        subuid, subgid = deps.subid_files
        for path, flag in (
            (subuid, "--add-subuids"),
            (subgid, "--add-subgids"),
        ):
            subid = _subid_command(path, flag, user)
            if subid is not None:
                deps.run(subid)
    group_name = "staff" if macos else GROUP
    if macos:
        deps.run(["dseditgroup", "-o", "edit", "-a", user, "-t", "user", GROUP])
        deps.run(
            ["install", "-d", "-o", user, "-g", "staff", "-m", "0700", home]
        )
    elif not created:
        deps.run(
            ["usermod", "--append", "--groups", ",".join([GROUP, *extra]), user]
        )
    artifacts = f"{home}/artifacts"
    # The user owns home and can put a link at artifacts.  install -d as
    # root follows a link on a path that exists, and then root changes
    # the owner of the target.  Thus the user makes the directory.
    deps.run(
        [
            "sudo",
            "-u",
            user,
            "install",
            "-d",
            "-g",
            group_name,
            "-m",
            "0755",
            artifacts,
        ]
    )
    gids = {primary, ltvm_gid}
    gids |= {g for g in (_gid(deps, n) for n in extra) if g is not None}
    return {
        "name": user,
        "created": created,
        "uid": uid,
        "gids": sorted(gids),
        "home": home,
        "artifacts": artifacts,
    }


# ── sshd ─────────────────────────────────────────────────


def _sshd_running(deps: Deps) -> bool:
    if deps.system == "Darwin":
        cmds = [["launchctl", "print", "system/com.openssh.sshd"]]
    else:
        cmds = [
            ["systemctl", "is-active", "--quiet", "sshd"],
            ["systemctl", "is-active", "--quiet", "ssh"],
            ["pgrep", "-x", "sshd"],
        ]
    return any(deps.probe(c).returncode == 0 for c in cmds)


def _sshd_settings(deps: Deps, user: str) -> dict[str, str] | None:
    """sshd -T for the user (it needs root), or None."""
    try:
        r = deps.run(
            [SSHD, "-T", "-C", f"user={user},host=localhost,addr=127.0.0.1"],
            check=False,
            quiet=True,
        )
    except (OSError, RuntimeError):
        # As root, a missing sshd gives FileNotFoundError.
        return None
    if r.returncode != 0:
        return None
    settings: dict[str, str] = {}
    for line in (r.stdout or "").splitlines():
        key, _, value = line.strip().partition(" ")
        if key:
            settings.setdefault(key.lower(), value)
    return settings


def _match_block(user: str, macos: bool) -> str:
    lines = [
        f"Match User {user}",
        "    ExposeAuthInfo yes",
        "    PasswordAuthentication no",
        "    KbdInteractiveAuthentication no",
        "    AuthenticationMethods publickey",
        "    PermitTTY no",
        "    AllowTcpForwarding no",
        "    AllowAgentForwarding no",
        "    X11Forwarding no",
        "    PermitTunnel no",
        "    PermitUserRC no",
    ]
    if macos:
        lines.append(f"    SetEnv PATH={FORCED_PATH}")
    return "\n".join(lines)


def _check_sshd(deps: Deps, user: str, gids: list[int]) -> dict[str, Any]:
    macos = deps.system == "Darwin"
    running = _sshd_running(deps)
    problems: list[str] = []
    settings = _sshd_settings(deps, user)
    block = False
    if settings is None:
        problems.append("`sshd -T` failed: cannot read the sshd settings")
        block = True
    else:
        allow = settings.get("allowusers", "").split()
        if allow and user not in allow:
            problems.append(f"AllowUsers does not include {user}")
        if user in settings.get("denyusers", "").split():
            problems.append(f"DenyUsers includes {user}")
        groups = settings.get("allowgroups", "").split()
        names = set()
        for gid in gids:
            try:
                names.add(grp.getgrgid(gid).gr_name)
            except KeyError:
                continue
        if groups and not names & set(groups):
            problems.append(f"AllowGroups does not include a group of {user}")
        if settings.get("exposeauthinfo") != "yes":
            problems.append(
                f"ExposeAuthInfo is not on for {user}: the audit log "
                "cannot name the controller key"
            )
            block = True
        if macos:
            path = settings.get("setenv", "").partition("PATH=")[2]
            dirs = path.split()[0].split(":") if path else []
            for need in ("/usr/local/bin", deps.brew_bin):
                if need and need not in dirs:
                    problems.append(
                        f"the forced command of {user} has no {need} in PATH"
                    )
                    block = True
    if macos:
        try:
            members = deps.getgrnam(ACCESS_SSH_GROUP).gr_mem
        except KeyError:
            members = None
        if members is not None and user not in members:
            problems.append(f"Remote Login does not allow {user}")
    return {"running": running, "problems": problems, "match_block": block}


def _hostkey(deps: Deps) -> str | None:
    r = deps.probe(["ssh-keygen", "-lf", str(deps.hostkey_pub)])
    if r.returncode != 0:
        return None
    fields = (r.stdout or "").split()
    return (
        fields[1]
        if len(fields) > 1 and fields[1].startswith("SHA256:")
        else None
    )


# ── the report ───────────────────────────────────────────


def _init_command(deps: Deps, user: str, cap_argv: list[str]) -> list[str]:
    return ["sudo", "-u", user, "-H", *deps.ltvm, "serve", "init", *cap_argv]


def _steps(deps: Deps, report: dict[str, Any], user: str) -> list[str]:
    """The steps that the owner must do, as text blocks."""
    macos = deps.system == "Darwin"
    steps: list[str] = []
    if report["read_problems"]:
        dirs = "\n".join(f"    {p}" for p in report["read_problems"])
        steps.append(
            f"{user} cannot read the ltvm checkout or its Python.  The "
            f"forced command runs them as {user}.  These directories stop "
            f"it:\n{dirs}\n"
            "  Move the checkout and its .venv out of your home (for "
            "example to /opt/ltvm) and run the install from there.  Or give "
            "the directories o+x (o+rx for the last one).  Then run the "
            f"wizard:\n    sudo -u {user} -H {deps.launcher} serve init"
        )
    if macos:
        steps.append(
            "macOS: every local user is in the staff group.  So "
            f"{user} can read files that staff or everyone can read in "
            "your home.  Check with `ls -la ~` and use chmod 0700 where "
            "you need it."
        )
    sshd = report["sshd"]
    if not sshd["running"]:
        if macos:
            steps.append(
                "Turn on Remote Login: System Settings > General > Sharing > "
                'Remote Login.  Under "Allow access for", select "Only '
                f'these users" and add {user}.  Keep your own account in '
                "the list only if you use ssh to this Mac."
            )
        elif deps.wsl2:
            steps.append(
                "WSL2: start sshd in this distro and make it reachable:\n"
                "  1. Install openssh-server (apt or dnf).\n"
                '  2. Put "[boot]" and "systemd=true" in /etc/wsl.conf.  '
                "Run `wsl --shutdown` in Windows, then start the distro.\n"
                "  3. sudo systemctl enable --now ssh (or sshd)\n"
                '  4. In Windows, put "[wsl2]" and '
                '"networkingMode=mirrored" in %UserProfile%\\.wslconfig.  '
                "Or forward port 22 with `netsh interface portproxy`.\n"
                "  5. Let port 22 in through the Windows firewall, from "
                "your private network only.\n"
                "  6. Set the Windows power plan so that the PC does not "
                "sleep while a VM runs."
            )
        else:
            steps.append(
                "Start sshd: sudo systemctl enable --now sshd (Debian and "
                "Ubuntu: ssh)"
            )
    elif macos and any("Remote Login" in p for p in sshd["problems"]):
        steps.append(
            "Remote Login allows only some users: System Settings > General > "
            f'Sharing > Remote Login > "Only these users".  Add {user}.'
        )
    other = [p for p in sshd["problems"] if "Remote Login" not in p]
    if other:
        steps.append("sshd: " + "\n  ".join(other))
    if sshd["match_block"]:
        reload = (
            "sudo launchctl kickstart -k system/com.openssh.sshd"
            if macos
            else "sudo systemctl reload sshd (or ssh)"
        )
        steps.append(
            "Add this block to /etc/ssh/sshd_config.d/ltvm.conf (or to the "
            "end of /etc/ssh/sshd_config).  Then run `sudo sshd -t` and "
            f"{reload}:\n\n{_match_block(user, macos)}"
        )
    if report["hostkey"] is None:
        steps.append(
            f"No ed25519 host key at {deps.hostkey_pub}.  Run "
            "`sudo ssh-keygen -A`, then run the install again."
        )
    if not report["network"].startswith("installed"):
        steps.append(
            f"Remote guest network: {report['network']}.  serve refuses "
            "to create VMs until it is installed.  Fix the cause and run "
            "`ltvm install --serve` again."
        )
    if report["reap_job"] != "installed":
        steps.append(f"Reap job: {report['reap_job']}")
    if report["polkit"].startswith("not installed"):
        from ltvm_pkg.remote import power

        steps.append(
            f"polkit rule {power.POLKIT_RULE}: {report['polkit']}.  "
            f"Without it, the host can sleep while a lease is active."
        )
    steps.append(
        "ltvm installs no VPN.  Use your own private network, and let "
        "only the controller reach port 22 (see docs/REMOTE_HOSTS.md)."
    )
    addr = report["address"] or "<addr>"
    fp = report["hostkey"] or "<fp>"
    steps.append(
        "On the controller, run:\n\n"
        f"    ltvm host add {report['name']} ssh://{user}@{addr} --hostkey {fp}"
        "\n\n  Use the address that the controller reaches (a VPN address, "
        "for example).  `host add` then prints a `serve authorize` "
        "command.  Run it here."
    )
    return steps


def install(
    deps: Deps,
    cap_argv: list[str],
    *,
    stdout: TextIO,
    user: str = USER,
    quiet: bool = False,
) -> dict[str, Any]:
    """Do the --serve steps.  Return a report and print the steps left.

    With quiet, the wizard asks nothing and its output is captured.
    """
    if quiet and "--yes" not in cap_argv:
        cap_argv = ["--yes", *cap_argv]
    facts = _ensure_user(deps, user)
    problems = read_problems(
        deps.read_paths, uid=facts["uid"], gids=set(facts["gids"])
    )
    init_cmd = _init_command(deps, user, cap_argv)
    wizard = "skipped"
    if not problems:
        stdout.write(f"Running the wizard as {user}:\n")
        stdout.flush()
        try:
            r = deps.run(init_cmd, check=False, quiet=quiet)
            wizard = "ok" if r.returncode == 0 else f"exit {r.returncode}"
        except (OSError, RuntimeError) as e:
            wizard = f"failed: {e}"
    reap = deps.reap or _reap
    try:
        reap_result = reap(
            ltvm=deps.launcher,
            user=user,
            home=facts["home"],
            run=deps.run,
            system=deps.system,
        )
    except (RuntimeError, ValueError, OSError) as e:
        reap_result = f"not installed: {e}"
    network = deps.network or _network
    try:
        network_result = network(run=deps.run, system=deps.system)
    except (
        RuntimeError,
        ValueError,
        OSError,
        subprocess.CalledProcessError,
    ) as e:
        network_result = f"not installed: {e}"
    report: dict[str, Any] = {
        "network": network_result,
        "user": facts,
        "read_problems": problems,
        "wizard": wizard,
        "wizard_command": shlex.join(init_cmd),
        "reap_job": reap_result,
        "polkit": _polkit(deps, user),
        "sshd": _check_sshd(deps, user, facts["gids"]),
        "hostkey": _hostkey(deps),
        "address": deps.address() or None,
        "name": deps.hostname(),
    }
    report["steps"] = _steps(deps, report, user)
    verb = "Made" if facts["created"] else "Kept"
    stdout.write(
        f"\n{verb} the user {user} (home {facts['home']}).  "
        f"Wizard: {wizard}.  Reap job: {reap_result}.  "
        f"Remote guest network: {network_result}.\n\nSteps left:\n"
    )
    for n, step in enumerate(report["steps"], 1):
        stdout.write(f"\n{n}. {step}\n")
    return report
