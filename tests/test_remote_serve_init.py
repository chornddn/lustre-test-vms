"""Remote hosts: the `serve init` wizard and `install --serve`.

The wizard reads its answers from stdin.  The tests give the host facts
(CPUs, RAM, free disk, battery) directly, and they stub every command
that needs root.  No test creates a user or changes sshd.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from ltvm_pkg.remote import serve_config, serve_init
from ltvm_pkg.remote.serve_init import HostFacts
from tests.test_ltvm_cli import _load_ltvm

ltvm = _load_ltvm()

DESKTOP = HostFacts(cpus=10, ram_gb=16.0, free_disk_gb=1000.0, laptop=False)
LAPTOP = HostFacts(cpus=10, ram_gb=16.0, free_disk_gb=200.0, laptop=True)


class TestDefaults:
    def test_desktop_defaults(self) -> None:
        cfg = serve_init.defaults(DESKTOP)
        assert cfg.max_vcpus == 8
        assert cfg.max_ram_gb == 12.8
        assert cfg.max_disk_gb == 100.0
        assert cfg.lease_hours == 8.0
        assert cfg.ac_only is False

    def test_disk_is_a_quarter_of_free_space_when_smaller(self) -> None:
        assert serve_init.defaults(LAPTOP).max_disk_gb == 50.0

    def test_laptop_is_ac_only(self) -> None:
        assert serve_init.defaults(LAPTOP).ac_only is True

    def test_one_cpu_gives_one_vcpu(self) -> None:
        one = HostFacts(cpus=1, ram_gb=2.0, free_disk_gb=10.0, laptop=False)
        assert serve_init.defaults(one).max_vcpus == 1


def _wizard(
    facts: HostFacts,
    answers: str,
    *,
    yes: bool = False,
    overrides: dict[str, object] | None = None,
) -> tuple[serve_config.ServeConfig, str]:
    out = io.StringIO()
    cfg = serve_init.wizard(
        facts,
        overrides=overrides or {},
        yes=yes,
        stdin=io.StringIO(answers),
        stdout=out,
    )
    return cfg, out.getvalue()


class TestWizard:
    def test_enter_accepts_each_default(self) -> None:
        cfg, out = _wizard(DESKTOP, "\n\n\n\n")
        assert cfg == serve_init.defaults(DESKTOP)
        assert "[8]" in out

    def test_typed_values_replace_the_defaults(self) -> None:
        cfg, _ = _wizard(DESKTOP, "4\n8\n\n2\n")
        assert cfg.max_vcpus == 4
        assert cfg.max_ram_gb == 8.0
        assert cfg.max_disk_gb == 100.0
        assert cfg.lease_hours == 2.0

    def test_a_bad_answer_asks_again(self) -> None:
        cfg, out = _wizard(DESKTOP, "many\n0\n6\n\n\n\n")
        assert cfg.max_vcpus == 6
        assert out.count("vCPUs") == 3

    def test_none_removes_a_cap(self) -> None:
        cfg, _ = _wizard(DESKTOP, "\n\nnone\n\n")
        assert cfg.max_disk_gb is None

    def test_desktop_gets_no_ac_question(self) -> None:
        cfg, out = _wizard(DESKTOP, "\n\n\n\n")
        assert "AC" not in out
        assert cfg.ac_only is False

    def test_laptop_gets_the_ac_question_with_yes_as_default(self) -> None:
        cfg, out = _wizard(LAPTOP, "\n\n\n\n\n")
        assert "AC power" in out
        assert cfg.ac_only is True
        cfg, _ = _wizard(LAPTOP, "\n\n\n\nn\n")
        assert cfg.ac_only is False

    def test_yes_accepts_all_defaults_and_asks_nothing(self) -> None:
        cfg, out = _wizard(LAPTOP, "", yes=True)
        assert cfg == serve_init.defaults(LAPTOP)
        assert "]: " not in out

    def test_a_flag_sets_one_value_and_skips_its_question(self) -> None:
        cfg, out = _wizard(DESKTOP, "\n\n\n", overrides={"max_vcpus": 3})
        assert cfg.max_vcpus == 3
        assert "vCPUs" not in out
        assert cfg.max_ram_gb == 12.8

    def test_a_flag_with_yes_keeps_the_other_defaults(self) -> None:
        cfg, _ = _wizard(
            LAPTOP,
            "",
            yes=True,
            overrides={"lease_hours": 1.5, "ac_only": False},
        )
        assert cfg.lease_hours == 1.5
        assert cfg.ac_only is False
        assert cfg.max_vcpus == 8

    def test_end_of_input_accepts_the_defaults(self) -> None:
        cfg, _ = _wizard(DESKTOP, "5\n")
        assert cfg.max_vcpus == 5
        assert cfg.max_ram_gb == 12.8


class TestLaptop:
    def test_linux_battery_means_laptop(self, tmp_path: Path) -> None:
        bat = tmp_path / "BAT0"
        bat.mkdir()
        (bat / "type").write_text("Battery\n")
        ac = tmp_path / "AC"
        ac.mkdir()
        (ac / "type").write_text("Mains\n")
        assert serve_init.is_laptop("Linux", power_supply=tmp_path)

    def test_linux_without_battery_is_a_desktop(self, tmp_path: Path) -> None:
        ac = tmp_path / "AC"
        ac.mkdir()
        (ac / "type").write_text("Mains\n")
        assert not serve_init.is_laptop("Linux", power_supply=tmp_path)
        assert not serve_init.is_laptop(
            "Linux", power_supply=tmp_path / "missing"
        )

    def test_macos_reads_pmset(self) -> None:
        laptop = (
            "Now drawing from 'AC Power'\n"
            " -InternalBattery-0 (id=123)\t100%; charged; 0:00 remaining\n"
        )
        desktop = "Now drawing from 'AC Power'\n"
        assert serve_init.is_laptop("Darwin", pmset=lambda: laptop)
        assert not serve_init.is_laptop("Darwin", pmset=lambda: desktop)


class TestProbe:
    def test_free_disk_is_the_smaller_of_home_and_vm_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        free = {
            str(tmp_path / "home"): 400 * 2**30,
            str(tmp_path / "vms"): 80 * 2**30,
        }

        def usage(path: str) -> object:
            from collections import namedtuple

            u = namedtuple("u", "total used free")
            return u(0, 0, free[str(path)])

        (tmp_path / "home").mkdir()
        (tmp_path / "vms").mkdir()
        monkeypatch.setattr(serve_init.shutil, "disk_usage", usage)
        monkeypatch.setattr(serve_init.os, "cpu_count", lambda: 12)
        monkeypatch.setattr(serve_init, "_ram_mb", lambda: 32 * 1024)
        facts = serve_init.probe(
            [tmp_path / "home", tmp_path / "vms"], laptop=False
        )
        assert facts == HostFacts(
            cpus=12, ram_gb=32.0, free_disk_gb=80.0, laptop=False
        )


@pytest.fixture
def init_cli(monkeypatch: pytest.MonkeyPatch) -> Any:
    """`ltvm serve init` with fixed host facts and stdin answers."""
    from ltvm_pkg.cli import serve_setup

    monkeypatch.setattr(serve_setup, "host_facts", lambda: LAPTOP)

    def run(argv: list[str], answers: str = "") -> int:
        monkeypatch.setattr(sys, "stdin", io.StringIO(answers))
        return int(ltvm.run(["serve", "init", *argv], update_check=False))

    return run


class TestServeInitCommand:
    def test_writes_serve_toml_from_the_answers(
        self, init_cli: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import os

        assert init_cli([], "6\n\n\n4\nn\n") == 0
        cfg = serve_config.load(os.environ)
        assert cfg.max_vcpus == 6
        assert cfg.max_ram_gb == 12.8
        assert cfg.lease_hours == 4.0
        assert cfg.ac_only is False
        out = capsys.readouterr().out
        assert str(serve_config.config_path(os.environ)) in out

    def test_yes_and_one_flag(self, init_cli: Any) -> None:
        import os

        assert init_cli(["--yes", "--max-disk-gb", "20"]) == 0
        cfg = serve_config.load(os.environ)
        assert cfg.max_disk_gb == 20.0
        assert cfg.max_vcpus == 8
        assert cfg.ac_only is True

    def test_no_ac_only_flag(self, init_cli: Any) -> None:
        import os

        assert init_cli(["--yes", "--no-ac-only"]) == 0
        assert serve_config.load(os.environ).ac_only is False

    def test_json_asks_nothing_and_prints_the_caps(
        self, init_cli: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert init_cli(["--json", "--max-vcpus", "2"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["config"]["max_vcpus"] == 2
        assert data["config"]["max_ram_gb"] == 12.8
        assert data["path"].endswith("serve.toml")

    def test_a_bad_flag_value_is_a_parse_error(self, init_cli: Any) -> None:
        with pytest.raises(SystemExit):
            init_cli(["--max-vcpus", "0"])


# ── install --serve ──────────────────────────────────────

FP = "SHA256:" + "A" * 43


class FakeHost:
    """The host that install --serve sees.  It runs nothing for real.

    `elevated` records each command that needs root.  `probes` answers
    the read-only commands (launchctl print, systemctl, sshd -T).
    """

    def __init__(self, tmp_path: Path, system: str, *, wsl2: bool = False):
        import grp
        import pwd

        self.system = system
        self.wsl2 = wsl2
        self.elevated: list[list[str]] = []
        # What each `install SRC DST` wrote, by DST.
        self.files: dict[str, str] = {}
        self.as_user: list[list[str]] = []
        self.users: dict[str, Any] = {}
        self.groups = {
            "ltvm": grp.struct_group(("ltvm", "*", 501, [])),
            "kvm": grp.struct_group(("kvm", "*", 36, [])),
            "staff": grp.struct_group(("staff", "*", 20, [])),
        }
        self.sshd_running = True
        self.sshd_t = (
            "exposeauthinfo yes\nsetenv PATH=/usr/local/bin:/usr/bin:/bin\n"
        )
        self.tmp = tmp_path
        self.hostkey = tmp_path / "ssh_host_ed25519_key.pub"
        self.hostkey.write_text("ssh-ed25519 AAAA root@h\n")
        self.readable = tmp_path / "checkout"
        self.readable.mkdir()
        self.readable.chmod(0o755)
        self.read_paths: list[Path] = []
        self.subuid = tmp_path / "subuid"
        self.subgid = tmp_path / "subgid"
        self.subuid.write_text("alice:100000:65536\n")
        self.subgid.write_text("alice:100000:65536\nbob:165536:65536\n")
        self._pwd = pwd

    def getpwnam(self, name: str) -> Any:
        if name not in self.users:
            raise KeyError(name)
        return self.users[name]

    def getgrnam(self, name: str) -> Any:
        if name not in self.groups:
            raise KeyError(name)
        return self.groups[name]

    def run(
        self, cmd: list[str], *, check: bool = True, quiet: bool = False
    ) -> Any:
        import subprocess

        if cmd[:1] == ["install"] and Path(cmd[-2]).is_file():
            self.files[cmd[-1]] = Path(cmd[-2]).read_text()
        if cmd[:2] == ["sudo", "-u"]:
            self.as_user.append(cmd)
        else:
            self.elevated.append(cmd)
        out = ""
        if cmd[:2] == ["sshd", "-T"] or cmd[:1] == ["/usr/sbin/sshd"]:
            out = self.sshd_t
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def probe(self, cmd: list[str]) -> Any:
        import subprocess

        if cmd[0] == "ssh-keygen":
            return subprocess.CompletedProcess(
                cmd, 0, f"256 {FP} root@h (ED25519)\n", ""
            )
        rc = 0 if self.sshd_running else 1
        return subprocess.CompletedProcess(cmd, rc, "", "")

    def deps(self, **kw: Any) -> Any:
        from ltvm_pkg.remote import serve_install

        fields: dict[str, Any] = dict(
            system=self.system,
            wsl2=self.wsl2,
            run=self.run,
            probe=self.probe,
            getpwnam=self.getpwnam,
            getgrnam=self.getgrnam,
            uids=lambda: {0, 501, 400},
            ltvm=["/venv/bin/python", "/src/ltvm/ltvm"],
            launcher="/usr/local/bin/ltvm",
            read_paths=self.read_paths,
            hostkey_pub=self.hostkey,
            address=lambda: "192.0.2.10",
            hostname=lambda: "box",
            reap=lambda **k: "installed",
            subid_files=(self.subuid, self.subgid),
        )
        fields.update(kw)
        return serve_install.Deps(**fields)


def _install(
    host: FakeHost, cap_argv: list[str] | None = None, **kw: Any
) -> tuple[dict[str, Any], str]:
    from ltvm_pkg.remote import serve_install

    out = io.StringIO()
    report = serve_install.install(host.deps(**kw), cap_argv or [], stdout=out)
    return report, out.getvalue()


def _flat(cmds: list[list[str]]) -> str:
    return "\n".join(" ".join(c) for c in cmds)


class TestCreateUserMacOS:
    def test_hidden_user_with_no_password_and_no_admin(
        self, tmp_path: Path
    ) -> None:
        host = FakeHost(tmp_path, "Darwin")
        _install(host)
        cmds = _flat(host.elevated)
        assert "dscl . -create /Users/ltvm IsHidden 1" in cmds
        assert "dscl . -create /Users/ltvm Password *" in cmds
        assert "dscl . -create /Users/ltvm UserShell /bin/sh" in cmds
        assert "admin" not in cmds
        assert "sysadminctl" not in cmds

    def test_uid_is_a_free_hidden_uid(self, tmp_path: Path) -> None:
        host = FakeHost(tmp_path, "Darwin")
        _install(host)
        assert [
            "dscl",
            ".",
            "-create",
            "/Users/ltvm",
            "UniqueID",
            "401",
        ] in host.elevated

    def test_ltvm_group_staff_and_artifacts(self, tmp_path: Path) -> None:
        host = FakeHost(tmp_path, "Darwin")
        _install(host)
        cmds = host.elevated
        assert [
            "dscl",
            ".",
            "-create",
            "/Users/ltvm",
            "PrimaryGroupID",
            "20",
        ] in cmds
        assert [
            "dseditgroup",
            "-o",
            "edit",
            "-a",
            "ltvm",
            "-t",
            "user",
            "ltvm",
        ] in cmds
        assert [
            "install",
            "-d",
            "-o",
            "ltvm",
            "-g",
            "staff",
            "-m",
            "0755",
            "/Users/ltvm/artifacts",
        ] in cmds


class TestCreateUserLinux:
    def test_locked_password_and_sh(self, tmp_path: Path) -> None:
        host = FakeHost(tmp_path, "Linux")
        _install(host)
        useradd = next(c for c in host.elevated if c[0] == "useradd")
        assert useradd[useradd.index("--shell") + 1] == "/bin/sh"
        assert useradd[useradd.index("--gid") + 1] == "ltvm"
        assert useradd[useradd.index("--groups") + 1] == "kvm"
        assert ["usermod", "--lock", "ltvm"] in host.elevated
        assert [
            "install",
            "-d",
            "-o",
            "ltvm",
            "-g",
            "ltvm",
            "-m",
            "0755",
            "/home/ltvm/artifacts",
        ] in host.elevated

    def test_no_kvm_group_means_no_extra_group(self, tmp_path: Path) -> None:
        host = FakeHost(tmp_path, "Linux")
        del host.groups["kvm"]
        _install(host)
        useradd = next(c for c in host.elevated if c[0] == "useradd")
        assert "--groups" not in useradd

    def test_an_existing_user_is_kept(self, tmp_path: Path) -> None:
        import pwd

        host = FakeHost(tmp_path, "Linux")
        host.users["ltvm"] = pwd.struct_passwd(
            ("ltvm", "!", 990, 501, "", "/srv/ltvm", "/bin/sh")
        )
        report, out = _install(host)
        cmds = _flat(host.elevated)
        assert "useradd" not in cmds
        assert "usermod --append --groups ltvm,kvm ltvm" in cmds
        assert "/srv/ltvm/artifacts" in cmds
        assert report["user"]["created"] is False

    def test_no_ltvm_group_stops_before_any_change(
        self, tmp_path: Path
    ) -> None:
        from ltvm_pkg.remote import serve_install

        host = FakeHost(tmp_path, "Linux")
        del host.groups["ltvm"]
        with pytest.raises(serve_install.ServeInstallError, match="ltvm group"):
            _install(host)
        assert host.elevated == []


class TestServeInitAsTheUser:
    def test_runs_serve_init_as_the_user_with_the_flags(
        self, tmp_path: Path
    ) -> None:
        host = FakeHost(tmp_path, "Linux")
        _install(host, ["--yes", "--max-vcpus", "4"])
        assert host.as_user == [
            [
                "sudo",
                "-u",
                "ltvm",
                "-H",
                "/venv/bin/python",
                "/src/ltvm/ltvm",
                "serve",
                "init",
                "--yes",
                "--max-vcpus",
                "4",
            ]
        ]

    def test_unreadable_checkout_skips_init_and_names_the_dir(
        self, tmp_path: Path
    ) -> None:
        host = FakeHost(tmp_path, "Linux")
        private = tmp_path / "home"
        private.mkdir()
        checkout = private / "ltvm"
        checkout.mkdir()
        private.chmod(0o700)
        host.read_paths = [checkout]
        try:
            report, out = _install(host)
        finally:
            private.chmod(0o755)
        assert host.as_user == []
        assert str(private) in out
        assert "sudo -u ltvm -H /usr/local/bin/ltvm serve init" in out
        assert report["read_problems"]


class TestReadProblems:
    @staticmethod
    def _stat(modes: dict[str, tuple[int, int, int]]) -> Any:
        import stat as st
        from types import SimpleNamespace

        def fake(path: str) -> Any:
            mode, uid, gid = modes.get(str(path), (0o755, 0, 0))
            return SimpleNamespace(
                st_mode=st.S_IFDIR | mode, st_uid=uid, st_gid=gid
            )

        return fake

    def test_owner_group_and_other_bits(self) -> None:
        from ltvm_pkg.remote import serve_install

        d = Path("/home/alice/ltvm")
        stat = self._stat({"/home/alice": (0o750, 1000, 1000)})
        assert (
            serve_install.read_problems([d], uid=-1, gids={1000}, stat=stat)
            == []
        )
        assert (
            serve_install.read_problems([d], uid=1000, gids=set(), stat=stat)
            == []
        )
        problems = serve_install.read_problems([d], uid=-1, gids={5}, stat=stat)
        assert len(problems) == 1
        assert "/home/alice" in problems[0]
        assert "0750" in problems[0]

    def test_the_path_itself_needs_read(self) -> None:
        from ltvm_pkg.remote import serve_install

        d = Path("/opt/ltvm")
        stat = self._stat({"/opt/ltvm": (0o711, 0, 0)})
        assert serve_install.read_problems([d], uid=-1, gids=set(), stat=stat)


class TestSshd:
    def test_prints_the_pairing_command(self, tmp_path: Path) -> None:
        host = FakeHost(tmp_path, "Linux")
        report, out = _install(host)
        assert f"ltvm host add box ssh://ltvm@192.0.2.10 --hostkey {FP}" in out
        assert report["hostkey"] == FP

    def test_checks_sshd_for_the_dedicated_user(self, tmp_path: Path) -> None:
        host = FakeHost(tmp_path, "Linux")
        _install(host)
        sshd_t = next(c for c in host.elevated if "-T" in c)
        assert "user=ltvm" in " ".join(sshd_t)

    def test_no_expose_auth_info_prints_the_match_block(
        self, tmp_path: Path
    ) -> None:
        host = FakeHost(tmp_path, "Linux")
        host.sshd_t = "exposeauthinfo no\n"
        report, out = _install(host)
        assert "Match User ltvm" in out
        assert "ExposeAuthInfo yes" in out
        assert "SetEnv PATH" not in out
        assert report["sshd"]["problems"]

    def test_macos_block_sets_path_for_the_forced_command(
        self, tmp_path: Path
    ) -> None:
        host = FakeHost(tmp_path, "Darwin")
        host.sshd_t = "exposeauthinfo yes\n"
        _, out = _install(host)
        assert "SetEnv PATH=/usr/local/bin:" in out

    def test_deny_rule_is_a_problem(self, tmp_path: Path) -> None:
        host = FakeHost(tmp_path, "Linux")
        host.sshd_t = "exposeauthinfo yes\nallowusers alice bob\n"
        report, out = _install(host)
        assert any("AllowUsers" in p for p in report["sshd"]["problems"])

    def test_macos_without_remote_login_prints_the_steps(
        self, tmp_path: Path
    ) -> None:
        host = FakeHost(tmp_path, "Darwin")
        host.sshd_running = False
        _, out = _install(host)
        assert "Remote Login" in out
        assert "Only these users" in out

    def test_macos_access_group_without_ltvm(self, tmp_path: Path) -> None:
        import grp

        host = FakeHost(tmp_path, "Darwin")
        host.groups["com.apple.access_ssh"] = grp.struct_group(
            ("com.apple.access_ssh", "*", 399, ["alice"])
        )
        report, out = _install(host)
        assert any("Remote Login" in p for p in report["sshd"]["problems"])
        assert all("access_ssh" not in " ".join(c) for c in host.elevated)

    def test_wsl2_without_sshd_prints_the_wsl2_steps(
        self, tmp_path: Path
    ) -> None:
        host = FakeHost(tmp_path, "Linux", wsl2=True)
        host.sshd_running = False
        _, out = _install(host)
        assert "/etc/wsl.conf" in out
        assert "networkingMode=mirrored" in out

    def test_never_changes_sshd_or_a_vpn(self, tmp_path: Path) -> None:
        host = FakeHost(tmp_path, "Darwin")
        host.sshd_t = "exposeauthinfo no\n"
        host.sshd_running = False
        _install(host)
        cmds = _flat(host.elevated)
        for word in (
            "sshd_config",
            "systemsetup",
            "kickstart",
            "tailscale",
            "wg",
        ):
            assert word not in cmds


class TestReapHook:
    def test_reap_job_gets_the_launcher_user_and_home(
        self, tmp_path: Path
    ) -> None:
        host = FakeHost(tmp_path, "Darwin")
        seen: dict[str, Any] = {}

        def reap(**kw: Any) -> str:
            seen.update(kw)
            return "installed"

        report, _ = _install(host, reap=reap)
        assert seen["ltvm"] == "/usr/local/bin/ltvm"
        assert seen["user"] == "ltvm"
        assert seen["home"] == "/Users/ltvm"
        assert report["reap_job"] == "installed"

    def test_default_reap_writes_a_root_wheel_plist(
        self, tmp_path: Path
    ) -> None:
        from ltvm_pkg.remote import serve_install

        host = FakeHost(tmp_path, "Darwin")
        result = serve_install._reap(
            ltvm="/usr/local/bin/ltvm",
            user="ltvm",
            home="/Users/ltvm",
            run=host.run,
            system="Darwin",
        )
        assert result == "installed"
        install = next(c for c in host.elevated if c[0] == "install")
        assert install[1:7] == ["-m", "644", "-o", "root", "-g", "wheel"]
        assert install[-1].endswith("io.github.ltvm.serve-reap.plist")
        assert [
            "launchctl",
            "bootstrap",
            "system",
            install[-1],
        ] in host.elevated

    def test_a_failed_reap_job_is_a_step_left(self, tmp_path: Path) -> None:
        host = FakeHost(tmp_path, "Linux")

        def reap(**kw: Any) -> str:
            raise RuntimeError("systemctl failed")

        report, out = _install(host, reap=reap)
        assert report["reap_job"] == "not installed: systemctl failed"
        assert "Reap job: not installed" in out


class TestPolkitRule:
    RULE = "/etc/polkit-1/rules.d/50-ltvm-serve.rules"

    def test_linux_lets_the_user_block_sleep(self, tmp_path: Path) -> None:
        host = FakeHost(tmp_path, "Linux")
        report, _ = _install(host)
        assert report["polkit"] == "installed"
        install = next(c for c in host.elevated if c[-1] == self.RULE)
        assert install[1:7] == ["-m", "644", "-o", "root", "-g", "root"]
        rule = host.files[self.RULE]
        assert '"org.freedesktop.login1.inhibit-block-sleep"' in rule
        assert 'subject.user == "ltvm"' in rule
        assert "polkit.Result.YES" in rule

    @pytest.mark.parametrize(
        ("system", "wsl2"), [("Linux", True), ("Darwin", False)]
    )
    def test_wsl2_and_macos_get_no_rule(
        self, tmp_path: Path, system: str, wsl2: bool
    ) -> None:
        host = FakeHost(tmp_path, system, wsl2=wsl2)
        report, _ = _install(host)
        assert report["polkit"] == "not needed"
        assert self.RULE not in _flat(host.elevated)

    def test_a_failed_rule_is_a_step_left(self, tmp_path: Path) -> None:
        import subprocess

        host = FakeHost(tmp_path, "Linux")
        real = host.run

        def run(cmd: list[str], **kw: Any) -> Any:
            if cmd[-1] == self.RULE:
                raise subprocess.CalledProcessError(1, cmd, "", "no dir")
            return real(cmd, **kw)

        report, out = _install(host, run=run)
        assert report["polkit"].startswith("not installed")
        assert "polkit" in out


class TestInstallServeCommand:
    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        from ltvm_pkg import host_setup
        from ltvm_pkg.cli import setup
        from ltvm_pkg.remote import serve_install

        calls: dict[str, Any] = {}
        monkeypatch.setattr(
            host_setup, "run_setup", lambda **kw: calls.setdefault("setup", kw)
        )
        monkeypatch.setattr(setup, "_link_skills", lambda use_json: None)

        def fake_install(
            deps: Any, cap_argv: list[str], **kw: Any
        ) -> dict[str, Any]:
            calls["deps"] = deps
            calls["cap_argv"] = cap_argv
            return {"steps": []}

        monkeypatch.setattr(serve_install, "install", fake_install)
        return calls

    def test_runs_the_normal_install_then_the_serve_steps(
        self, calls: dict[str, Any]
    ) -> None:
        rc = ltvm.run(
            ["install", "--serve", "--yes", "--lease-hours", "2"],
            update_check=False,
        )
        assert rc == 0
        assert calls["setup"]["steps"] is None
        assert calls["cap_argv"] == ["--yes", "--lease-hours", "2.0"]

    def test_without_serve_there_are_no_serve_steps(
        self, calls: dict[str, Any]
    ) -> None:
        assert ltvm.run(["install"], update_check=False) == 0
        assert "deps" not in calls

    def test_a_serve_error_fails_the_install(
        self, calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg.remote import serve_install

        def fail(*a: Any, **k: Any) -> Any:
            raise serve_install.ServeInstallError("the ltvm group is not there")

        monkeypatch.setattr(serve_install, "install", fail)
        assert ltvm.run(["install", "--serve"], update_check=False) != 0


class TestInstallJson:
    def test_quiet_adds_yes_and_captures_the_wizard(
        self, tmp_path: Path
    ) -> None:
        from ltvm_pkg.remote import serve_install

        host = FakeHost(tmp_path, "Linux")
        seen: list[dict[str, Any]] = []
        real_run = host.run

        def run(cmd: list[str], **kw: Any) -> Any:
            if cmd[:2] == ["sudo", "-u"]:
                seen.append(kw)
            return real_run(cmd, **kw)

        serve_install.install(
            host.deps(run=run),
            ["--max-vcpus", "2"],
            stdout=io.StringIO(),
            quiet=True,
        )
        assert host.as_user[0][-3:] == ["--yes", "--max-vcpus", "2"]
        assert seen == [{"check": False, "quiet": True}]


class TestSubordinateIds:
    def test_new_linux_user_gets_free_ranges(self, tmp_path: Path) -> None:
        host = FakeHost(tmp_path, "Linux")
        _install(host)
        assert [
            "usermod",
            "--add-subuids",
            "165536-231071",
            "ltvm",
        ] in host.elevated
        assert [
            "usermod",
            "--add-subgids",
            "231072-296607",
            "ltvm",
        ] in host.elevated
        useradd = [i for i, c in enumerate(host.elevated) if c[0] == "useradd"]
        sub = [i for i, c in enumerate(host.elevated) if "--add-subuids" in c]
        assert useradd[0] < sub[0]

    def test_existing_ranges_are_kept(self, tmp_path: Path) -> None:
        host = FakeHost(tmp_path, "Linux")
        host.subuid.write_text("ltvm:200000:65536\n")
        host.subgid.write_text("ltvm:200000:65536\n")
        _install(host)
        assert "--add-sub" not in _flat(host.elevated)

    def test_missing_files_start_at_100000(self, tmp_path: Path) -> None:
        host = FakeHost(tmp_path, "Linux")
        host.subuid.unlink()
        host.subgid.unlink()
        _install(host)
        assert [
            "usermod",
            "--add-subuids",
            "100000-165535",
            "ltvm",
        ] in host.elevated

    def test_macos_gets_no_ranges(self, tmp_path: Path) -> None:
        host = FakeHost(tmp_path, "Darwin")
        _install(host)
        assert "--add-sub" not in _flat(host.elevated)
