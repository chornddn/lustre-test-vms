"""The owner network and the remote network of the guests.

A remote VM (owner ID ``remote:...``) gets its addresses, its bridge and
its socket from the remote network.  Every other VM gets them from the
owner network, as before.  No test runs a host tool.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import guest_net, vm_net

# The conftest stubs guest_net.remote_problems.  This name is the real one.
from ltvm_pkg.guest_net import remote_problems as _real_remote_problems
from ltvm_pkg.vm_state import VMInfo

REMOTE = "remote:SHA256:abc"


@pytest.fixture
def tmp_vmdir(tmp_path: Path) -> Iterator[Path]:
    sockets = tmp_path / "sockets"
    sockets.mkdir()
    (tmp_path / "overlays").mkdir()
    with (
        patch("ltvm_pkg.vm_state.OVERLAYS", tmp_path / "overlays"),
        patch("ltvm_pkg.vm_state.SUBNET", "192.168.100"),
        patch("ltvm_pkg.vm_state.EXTRA_SUBNET", "172.16.100"),
        patch("ltvm_pkg.vm_state.VM_DIR", tmp_path),
        patch("ltvm_pkg.vm_state.SOCKETS", sockets),
        patch("ltvm_pkg.vm_net._IP_LOCK_PATH", tmp_path / ".ip-alloc.lock"),
        patch("ltvm_pkg.guest_net._darwin", return_value=False),
        patch.dict("os.environ", {}, clear=False),
    ):
        for var in (
            "LTVM_REMOTE_SUBNET",
            "LTVM_REMOTE_EXTRA_SUBNET",
            "LTVM_REMOTE_VMNET_SOCKET",
        ):
            import os

            os.environ.pop(var, None)
        yield tmp_path


class TestNetworkChoice:
    def test_a_vm_with_no_remote_owner_is_on_the_owner_network(
        self, tmp_vmdir: Path
    ) -> None:
        for owner in (None, "", "pid:42", "session:x"):
            net = guest_net.for_owner(owner)
            assert net.kind == "owner"
            assert net.subnet == "192.168.100"
            assert net.gateway == "192.168.100.1"
            assert net.bridge == "fcbr0"
            assert net.subnet_for_nic(1) == "172.16.100"

    def test_a_remote_vm_is_on_the_remote_network(
        self, tmp_vmdir: Path
    ) -> None:
        net = guest_net.for_owner(REMOTE)
        assert net.kind == "remote"
        assert net.subnet == "192.168.101"
        assert net.gateway == "192.168.101.1"
        assert net.bridge == "fcbr1"
        assert net.subnet_for_nic(0) == "192.168.101"
        assert net.subnet_for_nic(2) == "172.16.101"
        assert net.nic_ip6("172.16.101.20") == (
            "fd17:2016:1000:f101:f172:f016:f101:f020"
        )

    def test_the_macos_remote_network_is_the_vmnet_host_network(
        self, tmp_vmdir: Path
    ) -> None:
        with patch("ltvm_pkg.guest_net._darwin", return_value=True):
            assert guest_net.remote().subnet == "192.168.106"

    def test_install_records_the_remote_subnet(self, tmp_vmdir: Path) -> None:
        (tmp_vmdir / "remote-subnet").write_text("192.168.150\n")
        assert guest_net.remote().subnet == "192.168.150"

    def test_the_env_wins_over_the_file(
        self, tmp_vmdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_vmdir / "remote-subnet").write_text("192.168.150\n")
        monkeypatch.setenv("LTVM_REMOTE_SUBNET", "192.168.160")
        assert guest_net.remote().subnet == "192.168.160"

    @pytest.mark.parametrize(
        "value",
        [
            "192.168.101.1/24 dev fcbr1'; touch /tmp/x; '",
            "192.168.101\nconf-file=/tmp/x",
            "0.0.0.0/0",
            "8.8.8",
            "192.168.101.5",
            "192.168",
            "192.168.300",
            "192.168.01",
        ],
    )
    @pytest.mark.parametrize("file", ["remote-subnet", "remote-extra-subnet"])
    def test_a_bad_record_is_refused(
        self, tmp_vmdir: Path, file: str, value: str
    ) -> None:
        """A member of group ltvm can write these files."""
        (tmp_vmdir / file).write_text(value + "\n")
        with pytest.raises(ValueError, match=file):
            guest_net.remote()

    def test_a_bad_env_value_is_refused(
        self, tmp_vmdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_REMOTE_SUBNET", "10.0.0;x")
        with pytest.raises(ValueError, match="LTVM_REMOTE_SUBNET"):
            guest_net.remote()

    def test_for_vm_reads_the_owner_id(self, tmp_vmdir: Path) -> None:
        vm = VMInfo(name="a", ip="192.168.101.20", owner_id=REMOTE)
        assert guest_net.for_vm(vm).kind == "remote"
        vm.owner_id = None
        assert guest_net.for_vm(vm).kind == "owner"


class TestPools:
    def test_a_remote_vm_gets_addresses_from_the_remote_pool_only(
        self, tmp_vmdir: Path
    ) -> None:
        net = guest_net.remote()
        with vm_net.alloc_ip("r1", count=3, net=net) as ips:
            assert ips[0].startswith("192.168.101.")
            assert all(ip.startswith("172.16.101.") for ip in ips[1:])

    def test_an_owner_vm_gets_addresses_from_the_owner_pool_only(
        self, tmp_vmdir: Path
    ) -> None:
        with vm_net.alloc_ip("o1", count=2) as ips:
            assert ips[0].startswith("192.168.100.")
            assert ips[1].startswith("172.16.100.")

    def test_the_pools_do_not_share_addresses(self, tmp_vmdir: Path) -> None:
        # The same name hashes to the same host octet in both pools, so
        # a shared pool would show a collision walk.  Two pools do not.
        with vm_net.alloc_ip("same") as owner_ips:
            VMInfo(name="other", ip=owner_ips[0]).save()
        with vm_net.alloc_ip("same", net=guest_net.remote()) as remote_ips:
            assert (
                remote_ips[0].rsplit(".", 1)[1]
                == (owner_ips[0].rsplit(".", 1)[1])
            )
            assert remote_ips[0].startswith("192.168.101.")

    def test_an_explicit_owner_address_is_refused_for_a_remote_vm(
        self, tmp_vmdir: Path
    ) -> None:
        with pytest.raises(SystemExit):
            with vm_net.alloc_ip(
                "r2", explicit_ip="192.168.100.77", net=guest_net.remote()
            ):
                pass

    def test_an_explicit_remote_address_is_kept_for_a_remote_vm(
        self, tmp_vmdir: Path
    ) -> None:
        with vm_net.alloc_ip(
            "r3", explicit_ip="192.168.101.77", net=guest_net.remote()
        ) as ips:
            assert ips == ["192.168.101.77"]


class TestCreate:
    """cmd_create takes the network from the owner ID."""

    def _create(self, tmp_vmdir: Path, owner_id: str | None) -> VMInfo:
        from ltvm_pkg import vm_commands
        from tests.test_vm_create_phases import _create_args, _create_env

        overlays = tmp_vmdir / "overlays"
        with (
            patch("ltvm_pkg.vm_commands.SOCKETS", tmp_vmdir / "sockets"),
            patch("ltvm_pkg.vm_commands.OVERLAYS", overlays),
            _create_env(tmp_vmdir) as env,
        ):
            vm_commands.cmd_create(
                _create_args(name="co1-net", nic=["tcp"], owner_id=owner_id)
            )
            nets = [c.kwargs["net"] for c in env["alloc_ip"].call_args_list]
        assert len(nets) == 1
        return VMInfo.load("co1-net")

    def test_a_remote_create_uses_the_remote_pool(
        self, tmp_vmdir: Path
    ) -> None:
        vm = self._create(tmp_vmdir, REMOTE)
        assert vm.ip == "192.168.101.50"
        assert vm.nic_ips == ["172.16.101.60"]
        assert vm.nic_ip6s == ["fd17:2016:1000:f101:f172:f016:f101:f060"]

    def test_an_owner_create_uses_the_owner_pool(self, tmp_vmdir: Path) -> None:
        vm = self._create(tmp_vmdir, "pid:7")
        assert vm.ip == "192.168.100.50"
        assert not vm.nic_ip6s[0].startswith("fd17:2016:1000:f101:")


class TestLaunch:
    """launch_qemu attaches each NIC to the network of the VM."""

    def _vm(self, tmp_vmdir: Path, owner_id: str | None, ip: str) -> VMInfo:
        from tests.test_qemu_run import _make_vm

        vm = _make_vm(tmp_vmdir, name="co1-r")
        vm.ip = ip
        vm.owner_id = owner_id
        vm.nics = ["tcp"]
        vm.nic_ips = ["172.16.101.20"]
        return vm

    def _launch_linux(self, vm: VMInfo) -> list[str]:
        from unittest.mock import MagicMock

        from ltvm_pkg import qemu_run
        from tests.test_qemu_run import _LaunchHarness, _run_launch

        h = _LaunchHarness()
        ready = MagicMock(
            ok=True, helper=Path("/usr/lib/qemu/qemu-bridge-helper")
        )
        with (
            patch("ltvm_pkg.qemu_run.is_macos", return_value=False),
            patch("ltvm_pkg.qemu_run.os.geteuid", return_value=1001),
            patch.object(qemu_run.rootless, "readiness", return_value=ready),
        ):
            _run_launch(vm, h)
        assert h.qemu_args is not None
        return h.qemu_args

    def test_a_remote_vm_is_on_the_remote_bridge(self, tmp_vmdir: Path) -> None:
        args = self._launch_linux(self._vm(tmp_vmdir, REMOTE, "192.168.101.20"))
        joined = " ".join(args)
        assert "br=fcbr0" not in joined
        assert joined.count("br=fcbr1") == 2
        assert "fc_gw=192.168.101.1 " in joined

    def test_an_owner_vm_stays_on_the_owner_bridge(
        self, tmp_vmdir: Path
    ) -> None:
        args = self._launch_linux(self._vm(tmp_vmdir, None, "192.168.100.20"))
        joined = " ".join(args)
        assert "br=fcbr1" not in joined
        assert joined.count("br=fcbr0") == 2

    def test_a_remote_vm_with_an_owner_address_does_not_boot(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A remote VM made before the remote network existed.
        vm = self._vm(tmp_vmdir, REMOTE, "192.168.100.20")
        with pytest.raises(SystemExit):
            self._launch_linux(vm)
        assert "not in the remote network" in capsys.readouterr().err

    def test_a_remote_vm_on_macos_uses_the_remote_socket(
        self, tmp_vmdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.test_qemu_run import TestLaunchQemuMacos, _LaunchHarness

        sock = tmp_vmdir / "remote.sock"
        monkeypatch.setenv("LTVM_REMOTE_VMNET_SOCKET", str(sock))
        vm = self._vm(tmp_vmdir, REMOTE, "192.168.101.20")
        calls: list[list[str]] = []

        class _All(_LaunchHarness):
            def subprocess_run(self, qemu_args, **kwargs):  # type: ignore[no-untyped-def]
                calls.append(list(qemu_args))
                return super().subprocess_run(qemu_args, **kwargs)

        with patch("ltvm_pkg.qemu_run._remote_vmnet_ready", return_value=None):
            TestLaunchQemuMacos()._launch_macos(vm, _All())
        [qemu] = [c for c in calls if "-netdev" in c]
        joined = " ".join(qemu)
        assert joined.count(f"addr.path={sock},") == 2
        assert TestLaunchQemuMacos._SOCK not in joined


class TestServeNeedsTheRemoteNetwork:
    """serve refuses a request that makes a VM without the remote network."""

    @pytest.fixture(autouse=True)
    def _restore(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # serve changes these for the process; give them back after.
        from ltvm_pkg import priv, vm_commands

        monkeypatch.setenv("LTVM_OWNER_ID", "controller-owner")
        monkeypatch.setattr(priv, "_sudo_forbidden", False)
        monkeypatch.setattr(vm_commands, "list_filter", None)
        monkeypatch.setattr(vm_commands, "list_lease", None)

    def _serve(
        self, tmp_path: Path, ltvm: Any, command: str, problems: list[str]
    ) -> Any:
        import io

        from ltvm_pkg.remote import protocol, serve, transport
        from tests.remote_helpers import write_serve_toml

        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        write_serve_toml(home)
        out = io.StringIO()
        ready = MagicMock(ok=True, problems=[])
        with (
            patch("ltvm_pkg.rootless.readiness", return_value=ready),
            patch("ltvm_pkg.guest_net.remote_problems", return_value=problems),
        ):
            rc = serve.serve_stdio(
                ltvm.run,
                parse=ltvm._parse_request,
                stdin=io.StringIO(protocol.hello_line("0") + "\n"),
                stdout=out,
                env={"HOME": str(home), "SSH_ORIGINAL_COMMAND": command},
            )
        return transport.parse_reply(rc, out.getvalue(), "")

    @pytest.mark.parametrize(
        "command", ["create co1-r", "cluster create co1 mgs+mds:co1-mds:1"]
    )
    def test_a_create_is_refused_without_it(
        self, tmp_vmdir: Path, ltvm: Any, command: str
    ) -> None:
        resp = self._serve(tmp_vmdir, ltvm, command, ["fcbr1 is missing"])
        assert resp.error is not None
        assert resp.error.code == "layout_unsupported"
        assert "fcbr1 is missing" in resp.error.message

    def test_a_list_does_not_need_it(
        self, tmp_vmdir: Path, ltvm: Any, capsys: Any
    ) -> None:
        resp = self._serve(tmp_vmdir, ltvm, "list --json", ["fcbr1 is missing"])
        assert resp.error is None


class TestRemoteProblems:
    def test_linux_ready(self, tmp_vmdir: Path) -> None:
        sysnet = tmp_vmdir / "sys-class-net"
        (sysnet / "fcbr1").mkdir(parents=True)
        acl = tmp_vmdir / "bridge.conf"
        acl.write_text("allow fcbr0\nallow fcbr1\n")
        with (
            patch.object(guest_net, "SYS_CLASS_NET", sysnet),
            patch("ltvm_pkg.rootless.BRIDGE_ACL", acl),
            patch.object(guest_net, "_active", return_value=True),
        ):
            assert _real_remote_problems() == []

    def test_linux_missing(self, tmp_vmdir: Path) -> None:
        acl = tmp_vmdir / "bridge.conf"
        acl.write_text("allow fcbr0\n")
        with (
            patch.object(guest_net, "SYS_CLASS_NET", tmp_vmdir / "none"),
            patch("ltvm_pkg.rootless.BRIDGE_ACL", acl),
            patch.object(guest_net, "_active", return_value=False),
        ):
            problems = _real_remote_problems()
        assert len(problems) == 4
        assert any("fcbr1 does not exist" in p for p in problems)
        assert any("does not allow fcbr1" in p for p in problems)

    def test_macos_needs_the_socket_and_the_jobs(
        self, tmp_vmdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_REMOTE_VMNET_SOCKET", str(tmp_vmdir / "s"))
        with (
            patch("ltvm_pkg.guest_net._darwin", return_value=True),
            patch.object(guest_net, "LAUNCH_DAEMONS", tmp_vmdir / "ld"),
        ):
            problems = _real_remote_problems()
        assert len(problems) == 4
        assert any("pf-remote" in p for p in problems)


class TestDoctor:
    def _vm(self, name: str, ip: str, owner: str | None) -> None:
        VMInfo(name=name, ip=ip, owner_id=owner).save()

    def test_a_remote_vm_on_the_owner_network_is_an_issue(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from ltvm_pkg import vm_commands

        self._vm("old-r", "192.168.100.30", REMOTE)
        self._vm("new-r", "192.168.101.30", REMOTE)
        with patch.object(guest_net, "remote_problems", return_value=[]):
            assert vm_commands._check_remote_guest_net() == 1
        out = capsys.readouterr().out
        assert "old-r is on the owner network" in out
        assert "new-r" not in out
        assert "remote guest network: ready (192.168.101.0/24)" in out

    def test_a_missing_network_is_an_issue_only_with_remote_vms(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from ltvm_pkg import vm_commands

        self._vm("own", "192.168.100.30", "pid:1")
        assert vm_commands._check_remote_guest_net() == 0
        assert "not set up" in capsys.readouterr().out
        self._vm("rvm", "192.168.101.31", REMOTE)
        assert vm_commands._check_remote_guest_net() == 1
        assert "unavailable: disabled in tests" in capsys.readouterr().out
