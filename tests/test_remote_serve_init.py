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
