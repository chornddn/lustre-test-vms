"""install --serve sets up the remote guest network.

Every write and every command is stubbed.  No test changes the host.
"""

from __future__ import annotations

import os
import plistlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ltvm_pkg import guest_net
from ltvm_pkg.remote import guest_net_install as gni


class _Host:
    def __init__(self) -> None:
        self.files: dict[Path, str] = {}
        self.cmds: list[list[str]] = []

    def write(self, path: Path, text: str, mode: int) -> None:
        self.files[path] = text

    def run(self, cmd: list[str], **_kw: Any) -> None:
        self.cmds.append(cmd)


@pytest.fixture
def host(tmp_path: Path) -> Iterator[_Host]:
    acl = tmp_path / "bridge.conf"
    acl.write_text("allow fcbr0\n")
    with (
        patch("ltvm_pkg.vm_state.VM_DIR", tmp_path),
        patch("ltvm_pkg.vm_state.SUBNET", "192.168.100"),
        patch("ltvm_pkg.rootless.BRIDGE_ACL", acl),
        patch("ltvm_pkg.guest_net._darwin", return_value=False),
        patch.object(gni, "HOST_DNSMASQ_DIR", tmp_path / "no-dnsmasq.d"),
        patch("ltvm_pkg.host_firewall.trust_bridge") as fw,
        patch.dict("os.environ", {}, clear=False),
    ):
        import os

        os.environ.pop("LTVM_REMOTE_SUBNET", None)
        h = _Host()
        h.fw = fw  # type: ignore[attr-defined]
        h.acl = acl  # type: ignore[attr-defined]
        h.vm_dir = tmp_path  # type: ignore[attr-defined]
        yield h


class TestLinux:
    def _install(self, host: _Host, in_use: Any = lambda s: False) -> str:
        return gni.install(
            system="Linux",
            write=host.write,
            run=host.run,
            in_use=in_use,
            dnsmasq="/usr/sbin/dnsmasq",
        )

    def test_the_bridge_unit_loads_the_drop_rules_before_the_bridge(
        self, host: _Host
    ) -> None:
        self._install(host)
        unit = host.files[gni.SYSTEMD_DIR / guest_net.REMOTE_NET_UNIT]
        starts = [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")]
        assert "nft -f /etc/ltvm/remote-net.nft" in starts[0]
        assert any("ip link add name fcbr1 type bridge" in ln for ln in starts)
        assert any("192.168.101.1/24 dev fcbr1" in ln for ln in starts)
        assert "MASQUERADE" not in unit and "iptables" not in unit

    def test_the_nft_table_drops_forwarding_both_ways(
        self, host: _Host
    ) -> None:
        self._install(host)
        rules = host.files[gni.NFT_FILE]
        assert "hook forward" in rules
        assert 'iifname "fcbr1" oifname != "fcbr1" drop' in rules
        assert 'oifname "fcbr1" iifname != "fcbr1" drop' in rules
        assert "table inet ltvm_remote" in rules

    def test_the_remote_dnsmasq_has_its_own_bridge_and_no_upstream(
        self, host: _Host
    ) -> None:
        self._install(host)
        conf = host.files[gni.DNSMASQ_CONF_LINUX]
        assert "interface=fcbr1\n" in conf
        assert "listen-address=192.168.101.1\n" in conf
        assert "server=" not in conf and "dhcp-range" not in conf
        unit = host.files[gni.SYSTEMD_DIR / guest_net.REMOTE_DNSMASQ_UNIT]
        assert f"--conf-file={gni.DNSMASQ_CONF_LINUX}" in unit

    def test_the_helper_acl_gets_the_remote_bridge(self, host: _Host) -> None:
        self._install(host)
        assert host.files[host.acl] == "allow fcbr0\nallow fcbr1\n"  # type: ignore[attr-defined]

    def test_the_units_start_and_the_firewall_routes_nothing(
        self, host: _Host
    ) -> None:
        self._install(host)
        assert ["systemctl", "daemon-reload"] in host.cmds
        assert [
            "systemctl",
            "enable",
            "--now",
            guest_net.REMOTE_NET_UNIT,
            guest_net.REMOTE_DNSMASQ_UNIT,
        ] in host.cmds
        host.fw.assert_called_once_with("fcbr1", route=False)  # type: ignore[attr-defined]

    def test_a_used_subnet_moves_the_remote_network_and_is_recorded(
        self, host: _Host
    ) -> None:
        line = self._install(host, in_use=lambda s: s == "192.168.101")
        record = host.vm_dir / "remote-subnet"  # type: ignore[attr-defined]
        assert host.files[record] == "192.168.150\n"
        assert "192.168.150.0/24" in line

    def test_the_bridge_unit_runs_no_shell(self, host: _Host) -> None:
        self._install(host)
        unit = host.files[gni.SYSTEMD_DIR / guest_net.REMOTE_NET_UNIT]
        assert "sh -c" not in unit and "/bin/sh" not in unit
        execs = [ln for ln in unit.splitlines() if ln.startswith("Exec")]
        assert all(ln.split("=", 1)[1].lstrip("-")[:1] == "/" for ln in execs)

    def test_a_planted_record_is_not_trusted(self, host: _Host) -> None:
        """A group member owns a file that it makes in VM_DIR."""
        record = host.vm_dir / "remote-subnet"  # type: ignore[attr-defined]
        record.write_text("192.168.170\n")
        line = self._install(host)
        assert "192.168.101.0/24" in line
        assert host.files[record] == "192.168.101\n"

    def test_a_root_record_is_kept_and_written_again(self, host: _Host) -> None:
        record = host.vm_dir / "remote-subnet"  # type: ignore[attr-defined]
        record.write_text("192.168.170\n")
        with patch.object(gni, "TRUSTED_UID", os.getuid()):
            line = self._install(host)
        assert "192.168.170.0/24" in line
        assert host.files[record] == "192.168.170\n"

    def test_the_records_are_written_as_root_under_the_env(
        self, host: _Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_REMOTE_SUBNET", "192.168.180")
        self._install(host)
        vm_dir = host.vm_dir  # type: ignore[attr-defined]
        assert host.files[vm_dir / "remote-subnet"] == "192.168.180\n"
        assert host.files[vm_dir / "remote-extra-subnet"] == "172.16.101\n"

    def test_the_owner_subnet_is_never_the_remote_one(
        self, host: _Host
    ) -> None:
        assert (
            gni.choose_subnet("192.168.100", "192.168.100", lambda s: False)
            == "192.168.101"
        )


class TestMacos:
    def _install(self, host: _Host, tmp_path: Path) -> None:
        with (
            patch("ltvm_pkg.guest_net._darwin", return_value=True),
            patch.dict(
                "os.environ",
                {"LTVM_REMOTE_VMNET_SOCKET": str(tmp_path / "remote.sock")},
            ),
        ):
            gni.install(
                system="Darwin",
                write=host.write,
                run=host.run,
                socket_vmnet="/opt/homebrew/opt/socket_vmnet/bin/socket_vmnet",
                dnsmasq="/opt/homebrew/sbin/dnsmasq",
            )

    def _job(self, host: _Host, label: str) -> dict[str, Any]:
        text = host.files[guest_net.launchd_plist(label)]
        job: dict[str, Any] = plistlib.loads(text.encode())
        return job

    def test_the_second_socket_vmnet_is_host_mode_on_its_own_socket(
        self, host: _Host, tmp_path: Path
    ) -> None:
        self._install(host, tmp_path)
        args = self._job(host, guest_net.SOCKET_VMNET_REMOTE_LABEL)[
            "ProgramArguments"
        ]
        assert "--vmnet-mode=host" in args
        assert "--vmnet-gateway=192.168.106.1" in args
        assert "--socket-group=ltvm" in args
        assert args[-1] == str(tmp_path / "remote.sock")

    def test_the_pf_anchor_blocks_every_other_address(
        self, host: _Host, tmp_path: Path
    ) -> None:
        self._install(host, tmp_path)
        rules = host.files[gni.PF_CONF]
        assert "192.168.106.0/24" in rules and "172.16.101.0/24" in rules
        assert "fd17:2016:1000:f101::/64" in rules
        assert "block drop quick from <ltvm_remote> to ! <ltvm_remote>" in rules
        assert "block drop quick from ! <ltvm_remote> to <ltvm_remote>" in rules
        args = self._job(host, guest_net.PF_REMOTE_LABEL)["ProgramArguments"]
        assert args == [
            "/sbin/pfctl",
            "-E",
            "-a",
            "com.apple/ltvm-remote",
            "-f",
            str(gni.PF_CONF),
        ]

    def test_the_pf_anchor_loads_before_the_network_comes_up(
        self, host: _Host, tmp_path: Path
    ) -> None:
        self._install(host, tmp_path)
        boots = [
            c[-1] for c in host.cmds if c[:2] == ["launchctl", "bootstrap"]
        ]
        assert boots[0].endswith(f"{guest_net.PF_REMOTE_LABEL}.plist")
        assert boots[1].endswith(f"{guest_net.SOCKET_VMNET_REMOTE_LABEL}.plist")

    def test_the_remote_dnsmasq_listens_on_the_remote_gateway(
        self, host: _Host, tmp_path: Path
    ) -> None:
        self._install(host, tmp_path)
        conf = host.files[gni.DNSMASQ_CONF_MACOS]
        assert "listen-address=192.168.106.1\n" in conf
        assert "server=" not in conf

    def test_planted_records_do_not_reach_root_config(
        self, host: _Host, tmp_path: Path
    ) -> None:
        """A group member can make these files at any time on macOS."""
        (tmp_path / "remote-subnet").write_text("192.168.170\n")
        (tmp_path / "remote-extra-subnet").write_text("10.0.0\n")
        self._install(host, tmp_path)
        rules = host.files[gni.PF_CONF]
        assert "192.168.106.0/24" in rules and "172.16.101.0/24" in rules
        assert "192.168.170" not in rules and "10.0.0" not in rules
        assert host.files[tmp_path / "remote-subnet"] == "192.168.106\n"
        assert host.files[tmp_path / "remote-extra-subnet"] == "172.16.101\n"

    def test_the_owner_jobs_are_not_touched(
        self, host: _Host, tmp_path: Path
    ) -> None:
        from ltvm_pkg import host_setup, rootless

        self._install(host, tmp_path)
        assert host_setup.SOCKET_VMNET_PLIST_PATH not in host.files
        assert host_setup.DNSMASQ_PLIST_PATH not in host.files
        assert rootless.DNSMASQ_RELOAD_PLIST not in host.files


class TestInstallServe:
    """install --serve runs the network step and reports it."""

    def test_the_report_names_the_network(self, tmp_path: Path) -> None:
        from tests.test_remote_serve_init import FakeHost, _install

        report, out = _install(FakeHost(tmp_path, "Linux"))
        assert report["network"] == "installed (stub)"
        assert "Remote guest network: installed (stub)" in out

    def test_a_failed_network_is_a_step_left(self, tmp_path: Path) -> None:
        from tests.test_remote_serve_init import FakeHost, _install

        def fail(**_kw: Any) -> str:
            raise RuntimeError("nft is not installed")

        report, out = _install(FakeHost(tmp_path, "Linux"), network=fail)
        assert report["network"] == "not installed: nft is not installed"
        assert any("serve refuses" in s for s in report["steps"])

    def test_the_default_step_writes_as_root_through_run(
        self, tmp_path: Path
    ) -> None:
        from ltvm_pkg.remote import serve_install

        calls: list[dict[str, Any]] = []

        def fake(**kw: Any) -> str:
            calls.append(kw)
            return "installed (x)"

        with patch.object(gni, "install", fake):
            got = serve_install._network(run=print, system="Linux")
        assert got == "installed (x)"
        assert calls[0]["system"] == "Linux" and calls[0]["run"] is print
        assert callable(calls[0]["write"])
