"""Remote hosts: the power guard and the AC-only cap.

The tests simulate the power source (pmset output, a fake
/sys/class/power_supply) and the inhibit process.  They never run the
real caffeinate, systemd-inhibit or pmset.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

PMSET_AC = (
    "Now drawing from 'AC Power'\n"
    " -InternalBattery-0 (id=4653155)\t100%; charged; 0:00 remaining\n"
)
PMSET_BATTERY = (
    "Now drawing from 'Battery Power'\n"
    " -InternalBattery-0 (id=4653155)\t87%; discharging; 5:12 remaining\n"
)
PMSET_DESKTOP = "Now drawing from 'AC Power'\n"


def _supply(root: Path, name: str, kind: str, **attrs: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "type").write_text(kind + "\n")
    for key, value in attrs.items():
        (d / key).write_text(value + "\n")


@pytest.fixture
def sysfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from ltvm_pkg.remote import power

    root = tmp_path / "power_supply"
    root.mkdir()
    monkeypatch.setattr(power, "POWER_SUPPLY", root)
    return root


# ── the power source ─────────────────────────────────────


class TestOnBatteryMacos:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [(PMSET_AC, False), (PMSET_BATTERY, True), (PMSET_DESKTOP, False)],
    )
    def test_reads_pmset(
        self, monkeypatch: pytest.MonkeyPatch, text: str, expected: bool
    ) -> None:
        from ltvm_pkg.remote import power

        monkeypatch.setattr(power, "_pmset_batt", lambda: text)
        assert power.on_battery(power.MACOS) is expected

    def test_pmset_that_fails_is_not_battery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg.remote import power

        monkeypatch.setattr(power, "_pmset_batt", lambda: None)
        assert power.on_battery(power.MACOS) is False


class TestOnBatteryLinux:
    def test_mains_offline_with_a_battery_is_battery(self, sysfs: Path) -> None:
        from ltvm_pkg.remote import power

        _supply(sysfs, "AC", "Mains", online="0")
        _supply(sysfs, "BAT0", "Battery", status="Discharging")
        assert power.on_battery(power.LINUX) is True

    def test_mains_online_is_ac(self, sysfs: Path) -> None:
        from ltvm_pkg.remote import power

        _supply(sysfs, "AC", "Mains", online="1")
        _supply(sysfs, "BAT0", "Battery", status="Charging")
        assert power.on_battery(power.LINUX) is False

    def test_usb_c_charger_online_is_ac(self, sysfs: Path) -> None:
        from ltvm_pkg.remote import power

        _supply(sysfs, "ucsi-source-psy-USBC000:001", "USB", online="1")
        _supply(sysfs, "BAT0", "Battery")
        assert power.on_battery(power.LINUX) is False

    def test_desktop_with_no_supplies_is_ac(self, sysfs: Path) -> None:
        from ltvm_pkg.remote import power

        assert power.on_battery(power.LINUX) is False

    def test_battery_of_a_mouse_is_not_the_host_battery(
        self, sysfs: Path
    ) -> None:
        from ltvm_pkg.remote import power

        _supply(sysfs, "hidpp_battery_0", "Battery", scope="Device")
        assert power.on_battery(power.LINUX) is False

    def test_missing_sysfs_is_ac(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg.remote import power

        monkeypatch.setattr(power, "POWER_SUPPLY", tmp_path / "absent")
        assert power.on_battery(power.LINUX) is False


def test_other_systems_are_never_on_battery() -> None:
    from ltvm_pkg.remote import power

    assert power.on_battery(power.OTHER) is False


# ── the inhibit command ──────────────────────────────────


class TestInhibitArgv:
    def test_macos_uses_caffeinate_s(self) -> None:
        from ltvm_pkg.remote import power

        # -s only: it does not stop a lid close, and it does not keep
        # the display on.
        assert power.inhibit_argv(power.MACOS) == ["caffeinate", "-s"]

    def test_linux_uses_systemd_inhibit_for_sleep(self) -> None:
        from ltvm_pkg.remote import power

        argv = power.inhibit_argv(power.LINUX)
        assert argv is not None
        assert argv[0] == "systemd-inhibit"
        assert "--what=sleep" in argv
        assert "--mode=block" in argv
        # Not handle-lid-switch: a lid close still suspends.
        assert not any("lid" in a for a in argv)
        assert argv[-2:] == ["sleep", "infinity"]

    def test_wsl2_does_nothing(self) -> None:
        from ltvm_pkg.remote import power

        assert power.inhibit_argv(power.WSL2) is None
        assert power.inhibit_argv(power.OTHER) is None


# ── the guard ────────────────────────────────────────────

REMOTE = "remote:SHA256:sentinel-key-p1"
T0 = 1_800_000_000.0
HOUR = 3600.0


@pytest.fixture
def guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A macOS host with fake process control and a fixed lease clock."""
    from ltvm_pkg import vm_state
    from ltvm_pkg.remote import leases, power

    procs: dict[int, list[str]] = {}
    g = SimpleNamespace(
        env={"HOME": str(tmp_path / "home")},
        procs=procs,
        spawned=[],
        stopped=[],
        now=T0,
        sockets=vm_state.SOCKETS,
    )

    def spawn(argv: list[str], log: Path) -> int:
        pid = 50000 + len(g.spawned)
        g.spawned.append(argv)
        procs[pid] = argv
        return pid

    def alive(pid: int, argv: list[str]) -> bool:
        return procs.get(pid) == argv

    def stop(pid: int) -> None:
        g.stopped.append(pid)
        procs.pop(pid, None)

    monkeypatch.setattr(power, "system", lambda: power.MACOS)
    monkeypatch.setattr(power, "_spawn", spawn)
    monkeypatch.setattr(power, "_alive", alive)
    monkeypatch.setattr(power, "_stop", stop)
    monkeypatch.setattr(leases, "clock", lambda: g.now)
    return g


def _vm(name: str) -> None:
    from ltvm_pkg.vm_state import VMInfo

    VMInfo(name=name, ip="192.168.100.9", owner_id=REMOTE).save()


def _cluster(name: str) -> None:
    from ltvm_pkg.vm_state import ClusterInfo

    ClusterInfo(name=name, nodes=[], owner_id=REMOTE).save()


def _lease(g: SimpleNamespace, kind: str, name: str, hours: float = 8) -> None:
    from ltvm_pkg.remote import leases

    leases.grant(g.env, kind, name, REMOTE, hours * HOUR)


class TestGuard:
    def test_no_lease_holds_nothing(self, guard: SimpleNamespace) -> None:
        from ltvm_pkg.remote import power

        assert power.sync(guard.env) is False
        assert guard.spawned == []

    def test_an_active_lease_holds_caffeinate(
        self, guard: SimpleNamespace
    ) -> None:
        from ltvm_pkg.remote import power

        _vm("pg-sentinel-a1")
        _lease(guard, "vm", "pg-sentinel-a1")
        assert power.sync(guard.env) is True
        assert guard.spawned == [["caffeinate", "-s"]]

    def test_a_second_request_does_not_start_a_second_holder(
        self, guard: SimpleNamespace
    ) -> None:
        from ltvm_pkg.remote import power

        _vm("pg-sentinel-a2")
        _lease(guard, "vm", "pg-sentinel-a2")
        power.sync(guard.env)
        power.sync(guard.env)
        assert len(guard.spawned) == 1
        assert guard.stopped == []

    def test_holder_stops_when_the_last_lease_expires(
        self, guard: SimpleNamespace
    ) -> None:
        from ltvm_pkg.remote import power

        _vm("pg-sentinel-a3")
        _lease(guard, "vm", "pg-sentinel-a3", hours=1)
        power.sync(guard.env)
        guard.now = T0 + 2 * HOUR
        assert power.sync(guard.env) is False
        assert guard.stopped == [50000]
        assert guard.procs == {}

    def test_a_lease_whose_vm_is_gone_does_not_count(
        self, guard: SimpleNamespace
    ) -> None:
        from ltvm_pkg.remote import power

        _lease(guard, "vm", "pg-sentinel-gone")
        assert power.sync(guard.env) is False
        assert guard.spawned == []

    def test_a_cluster_lease_counts_while_the_cluster_exists(
        self, guard: SimpleNamespace
    ) -> None:
        from ltvm_pkg.remote import power

        _lease(guard, "cluster", "pg-cl-sentinel")
        assert power.sync(guard.env) is False
        _cluster("pg-cl-sentinel")
        assert power.sync(guard.env) is True

    def test_a_holder_that_died_is_started_again(
        self, guard: SimpleNamespace
    ) -> None:
        from ltvm_pkg.remote import power

        _vm("pg-sentinel-a4")
        _lease(guard, "vm", "pg-sentinel-a4")
        power.sync(guard.env)
        guard.procs.clear()  # killed, or the host rebooted
        power.sync(guard.env)
        assert len(guard.spawned) == 2

    def test_a_reused_pid_is_never_stopped(
        self, guard: SimpleNamespace
    ) -> None:
        from ltvm_pkg.remote import power

        _vm("pg-sentinel-a5")
        _lease(guard, "vm", "pg-sentinel-a5", hours=1)
        power.sync(guard.env)
        guard.procs[50000] = ["some", "other", "process"]
        guard.now = T0 + 2 * HOUR
        power.sync(guard.env)
        assert guard.stopped == []

    def test_busy_holds_with_no_lease(self, guard: SimpleNamespace) -> None:
        """A create holds the guard while its VM does not exist yet."""
        from ltvm_pkg.remote import power

        assert power.sync(guard.env, busy=True) is True
        assert guard.spawned == [["caffeinate", "-s"]]
        assert power.sync(guard.env) is False
        assert guard.stopped == [50000]

    def test_wsl2_starts_nothing(
        self, guard: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg.remote import power

        monkeypatch.setattr(power, "system", lambda: power.WSL2)
        _vm("pg-sentinel-a6")
        _lease(guard, "vm", "pg-sentinel-a6")
        assert power.sync(guard.env) is False
        assert guard.spawned == []

    def test_linux_holds_systemd_inhibit(
        self, guard: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg.remote import power

        monkeypatch.setattr(power, "system", lambda: power.LINUX)
        _vm("pg-sentinel-a7")
        _lease(guard, "vm", "pg-sentinel-a7")
        power.sync(guard.env)
        assert guard.spawned[0][0] == "systemd-inhibit"

    def test_a_missing_tool_does_not_fail_the_request(
        self,
        guard: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        capsys: Any,
    ) -> None:
        from ltvm_pkg.remote import power

        def no_tool(argv: list[str], log: Path) -> int:
            raise FileNotFoundError(2, "No such file", argv[0])

        monkeypatch.setattr(power, "_spawn", no_tool)
        _vm("pg-sentinel-a8")
        _lease(guard, "vm", "pg-sentinel-a8")
        assert power.sync(guard.env) is False
        assert "caffeinate" in capsys.readouterr().err

    def test_a_holder_that_is_refused_is_reported(
        self,
        guard: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        capsys: Any,
    ) -> None:
        """polkit can refuse the sleep lock to the ltvm user."""
        from ltvm_pkg.remote import power

        def refused(argv: list[str], log: Path) -> int:
            raise RuntimeError(f"{argv[0]} stopped with exit 1: Access denied")

        monkeypatch.setattr(power, "system", lambda: power.LINUX)
        monkeypatch.setattr(power, "_spawn", refused)
        _vm("pg-sentinel-aa")
        _lease(guard, "vm", "pg-sentinel-aa")
        assert power.sync(guard.env) is False
        err = capsys.readouterr().err
        assert "systemd-inhibit stopped with exit 1: Access denied" in err
        assert not power.state_path(guard.env).exists()

    def test_a_test_host_never_runs_the_tool(
        self, guard: SimpleNamespace
    ) -> None:
        """An exec:// child in a test sees no conftest patch."""
        from ltvm_pkg.remote import power

        _vm("pg-sentinel-a9")
        _lease(guard, "vm", "pg-sentinel-a9")
        env = {**guard.env, "LTVM_SERVE_TEST_LAYOUT": "ready"}
        assert power.sync(env) is False
        assert guard.spawned == []


def test_conftest_keeps_the_real_tools_out() -> None:
    """With no opt-in, a test sees a system with no power control."""
    from ltvm_pkg.remote import power

    assert power.inhibit_argv(power.system()) is None
    assert power.on_battery() is False


# ── the AC-only cap ──────────────────────────────────────


@pytest.fixture
def admit(
    ltvm: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """The admission check of serve on a laptop with a fake power source."""
    from ltvm_pkg.remote import admission, power, serve_config

    a = SimpleNamespace(env={"HOME": str(tmp_path)}, battery=True)
    monkeypatch.setattr(power, "on_battery", lambda kind=None: a.battery)

    def check(argv: list[str]) -> None:
        admission.check(argv, ltvm._parse_request(argv), a.env)

    def caps(**kw: Any) -> None:
        serve_config.save(a.env, serve_config.ServeConfig(**kw))

    a.check = check
    a.caps = caps
    return a


class TestAcOnly:
    @pytest.mark.parametrize(
        "argv",
        [
            ["create", "ac-sentinel-b1"],
            ["cluster", "create", "ac-cl-b1", "mgs+mds:ac-b1-mds:1"],
        ],
    )
    def test_create_on_battery_is_refused(
        self, admit: SimpleNamespace, argv: list[str]
    ) -> None:
        from ltvm_pkg.remote.protocol import RemoteError

        admit.caps(ac_only=True)
        with pytest.raises(RemoteError) as e:
            admit.check(argv)
        assert e.value.code == "on_battery"
        assert "AC power" in (e.value.hint or "")

    def test_create_on_ac_is_admitted(self, admit: SimpleNamespace) -> None:
        admit.caps(ac_only=True)
        admit.battery = False
        admit.check(["create", "ac-sentinel-b2"])

    def test_without_the_cap_battery_does_not_matter(
        self, admit: SimpleNamespace
    ) -> None:
        admit.caps(max_vcpus=64)
        admit.check(["create", "ac-sentinel-b3"])

    def test_other_verbs_run_on_battery(self, admit: SimpleNamespace) -> None:
        admit.caps(ac_only=True)
        admit.check(["list"])


# ── serve ────────────────────────────────────────────────

from tests import test_remote_lifecycle as _lifecycle  # noqa: E402
from tests.test_remote_lifecycle import _audit, _err  # noqa: E402

inproc = _lifecycle.inproc
qemu = _lifecycle.qemu


@pytest.fixture(scope="module")
def ltvm() -> Any:
    from tests.test_ltvm_cli import _load_ltvm

    return _load_ltvm()


@pytest.fixture
def served(
    inproc: SimpleNamespace,
    guard: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Host `t` in this process, with the real guard and fake processes.

    The host of the tests sets the test layout switch, which turns the
    guard off.  Here sync sees the environment without it, and records
    each call.
    """
    from ltvm_pkg.remote import power, serve

    real = power.sync
    guard.calls = []
    guard.home = inproc.home

    def sync(env: Any, *, busy: bool = False) -> bool:
        env = {k: v for k, v in env.items() if k != serve.TEST_LAYOUT_ENV}
        guard.env = env
        held = real(env, busy=busy)
        guard.calls.append((busy, held))
        return held

    monkeypatch.setattr(power, "sync", sync)
    return guard


class TestServeHoldsTheGuard:
    def test_create_holds_the_guard_and_keeps_it(
        self, ltvm: Any, served: SimpleNamespace, qemu: dict[str, Any]
    ) -> None:
        assert ltvm.run(["--host", "t", "create", "new-p1"]) == 0
        # Held before the verb, and still held after: the VM exists.
        assert served.calls == [(True, True), (False, True)]
        assert served.spawned == [["caffeinate", "-s"]]

    def test_a_create_that_fails_releases_the_guard(
        self,
        ltvm: Any,
        served: SimpleNamespace,
        qemu: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        qemu["launch"].side_effect = RuntimeError("no qemu")
        ltvm.run(["--host", "t", "create", "new-p2"])
        assert served.calls[-1] == (False, False)
        assert served.stopped == [50000]

    def test_destroy_of_the_last_leased_vm_releases_the_guard(
        self, ltvm: Any, served: SimpleNamespace, qemu: dict[str, Any]
    ) -> None:
        assert ltvm.run(["--host", "t", "create", "new-p3"]) == 0
        with patch("ltvm_pkg.vm_commands.unregister_vm_dns", create=True):
            assert ltvm.run(["--host", "t", "destroy", "new-p3"]) == 0
        assert served.calls[-1] == (False, False)
        assert served.stopped == [50000]

    def test_a_refused_request_does_not_touch_the_guard(
        self, ltvm: Any, served: SimpleNamespace
    ) -> None:
        assert ltvm.run(["--host", "t", "destroy", "owner-sentinel-f6"]) == 1
        assert served.calls == []

    def test_create_on_battery_gives_on_battery(
        self,
        ltvm: Any,
        inproc: SimpleNamespace,
        qemu: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        capsys: Any,
    ) -> None:
        from ltvm_pkg.remote import power, serve_config

        serve_config.save(
            {"HOME": str(inproc.home)}, serve_config.ServeConfig(ac_only=True)
        )
        monkeypatch.setattr(power, "system", lambda: power.MACOS)
        monkeypatch.setattr(power, "_pmset_batt", lambda: PMSET_BATTERY)
        capsys.readouterr()
        rc = ltvm.run(["--host", "t", "create", "new-p4", "--json"])
        assert rc == 1
        err = _err(capsys.readouterr().err)
        assert err["code"] == "on_battery"
        assert not qemu["launch"].called
        assert _audit(inproc.home)[-1]["result"] == "on_battery"


def test_reap_releases_the_guard(
    guard: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ltvm_pkg import priv
    from ltvm_pkg.remote import leases, power

    # reap forbids sudo and sets the owner for the process: give them back.
    monkeypatch.setattr(priv, "_sudo_forbidden", False)
    monkeypatch.setenv("LTVM_OWNER_ID", "controller-owner")
    _vm("pg-sentinel-r1")
    _lease(guard, "vm", "pg-sentinel-r1", hours=1)
    power.sync(guard.env)
    guard.now = T0 + 2 * HOUR
    leases.reap(lambda *a, **k: 0, guard.env)
    assert guard.stopped == [50000]


class TestSpawn:
    """The real _spawn, with a Python child in place of the tool."""

    def test_a_holder_that_stops_at_once_raises_with_its_error(
        self, tmp_path: Path
    ) -> None:
        import sys

        from ltvm_pkg.remote import power

        argv = [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('Access denied\\n'); sys.exit(1)",
        ]
        with pytest.raises(RuntimeError) as e:
            power._spawn(argv, tmp_path / "guard.log")
        assert "exit 1" in str(e.value)
        assert "Access denied" in str(e.value)

    def test_a_holder_that_runs_gives_its_pid(self, tmp_path: Path) -> None:
        import os
        import sys

        from ltvm_pkg.remote import power

        argv = [sys.executable, "-c", "import time; time.sleep(60)"]
        pid = power._spawn(argv, tmp_path / "guard.log")
        try:
            assert os.getpgid(pid) == pid
        finally:
            power._stop(pid)
            os.waitpid(pid, 0)
