"""Remote hosts: the caps in serve.toml and the admission check of serve.

The caps limit the vCPUs, RAM and disk of all remote VMs together.  A
request above a cap gets over_cap at once, and serve makes nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from tests.test_ltvm_cli import _load_ltvm

# ── serve.toml ───────────────────────────────────────────


class TestServeConfig:
    def test_missing_file_gives_no_caps(self, tmp_path: Path) -> None:
        from ltvm_pkg.remote import serve_config

        cfg = serve_config.load({"HOME": str(tmp_path)})
        assert cfg == serve_config.ServeConfig()
        assert cfg.max_vcpus is None
        assert cfg.enabled is True
        assert cfg.lease_hours == 8.0

    def test_path_is_under_the_config_dir_of_the_user(
        self, tmp_path: Path
    ) -> None:
        from ltvm_pkg.remote import serve_config

        home = {"HOME": str(tmp_path)}
        assert serve_config.config_path(home) == (
            tmp_path / ".config" / "ltvm" / "serve.toml"
        )
        xdg = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "x")}
        assert serve_config.config_path(xdg) == (
            tmp_path / "x" / "ltvm" / "serve.toml"
        )

    def test_save_then_load_gives_the_same_values(self, tmp_path: Path) -> None:
        from ltvm_pkg.remote import serve_config

        env = {"HOME": str(tmp_path)}
        cfg = serve_config.ServeConfig(
            enabled=False,
            max_vcpus=80,
            max_ram_gb=102.4,
            max_disk_gb=100,
            lease_hours=4.5,
            ac_only=True,
        )
        serve_config.save(env, cfg)
        assert serve_config.load(env) == cfg
        assert serve_config.config_path(env).stat().st_mode & 0o777 == 0o600

    def test_reads_a_file_that_the_owner_wrote(self, tmp_path: Path) -> None:
        from ltvm_pkg.remote import serve_config

        path = tmp_path / ".config" / "ltvm" / "serve.toml"
        path.parent.mkdir(parents=True)
        path.write_text(
            "# caps for remote VMs\n"
            "max_vcpus = 16\n"
            "max_ram_gb = 32.5   # a comment\n"
            'unknown_key = "kept out"\n'
            "ac_only = true\n"
        )
        cfg = serve_config.load({"HOME": str(tmp_path)})
        assert cfg.max_vcpus == 16
        assert cfg.max_ram_gb == 32.5
        assert cfg.max_disk_gb is None
        assert cfg.ac_only is True

    def test_flat_parser_reads_the_same_values_as_tomllib(self) -> None:
        # Python 3.10 has no tomllib.  The fallback parser must read the
        # file that save writes.
        from ltvm_pkg.remote import serve_config

        text = serve_config.dumps(
            serve_config.ServeConfig(max_vcpus=8, max_disk_gb=50.0)
        )
        assert serve_config._parse_flat(text) == {
            "enabled": True,
            "max_vcpus": 8,
            "max_disk_gb": 50.0,
            "lease_hours": 8.0,
            "ac_only": False,
        }

    def test_a_bad_value_is_an_error(self, tmp_path: Path) -> None:
        from ltvm_pkg.remote import serve_config

        path = tmp_path / ".config" / "ltvm" / "serve.toml"
        path.parent.mkdir(parents=True)
        path.write_text('max_vcpus = "many"\n')
        with pytest.raises(ValueError, match="max_vcpus"):
            serve_config.load({"HOME": str(tmp_path)})


# ── admission ────────────────────────────────────────────

REMOTE = "remote:SHA256:sentinel-key-c9"
GIB = 1 << 30


@pytest.fixture(scope="module")
def ltvm() -> Any:
    return _load_ltvm()


@pytest.fixture
def host(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """A host VM directory and a home for the dedicated user."""
    home = tmp_path / "home"
    home.mkdir()
    sockets = tmp_path / "vms" / "sockets"
    sockets.mkdir(parents=True)
    overlays = tmp_path / "vms" / "overlays"
    overlays.mkdir()
    with (
        patch("ltvm_pkg.vm_state.SOCKETS", sockets),
        patch("ltvm_pkg.vm_state.OVERLAYS", overlays),
    ):
        yield SimpleNamespace(home=home, env={"HOME": str(home)})


def _vm(name: str, *, vcpus: int = 2, mem: int = 2048, owner: str = REMOTE):
    from ltvm_pkg.vm_state import VMInfo

    VMInfo(
        name=name, ip="192.168.100.9", vcpus=vcpus, mem=mem, owner_id=owner
    ).save()


def _caps(host: SimpleNamespace, **caps: Any) -> None:
    from ltvm_pkg.remote import serve_config

    serve_config.save(host.env, serve_config.ServeConfig(**caps))


def _admit(ltvm: Any, host: SimpleNamespace, argv: list[str]) -> None:
    from ltvm_pkg.remote import admission

    admission.check(argv, ltvm._parse_request(argv), host.env)


def _refusal(ltvm: Any, host: SimpleNamespace, argv: list[str]) -> Any:
    from ltvm_pkg.remote.protocol import RemoteError

    with pytest.raises(RemoteError) as e:
        _admit(ltvm, host, argv)
    assert e.value.code == "over_cap"
    return e.value


class TestVcpuCap:
    def test_80_vms_with_1_vcpu_fit_a_cap_of_80(
        self, ltvm: Any, host: SimpleNamespace
    ) -> None:
        _caps(host, max_vcpus=80)
        for n in range(79):
            _vm(f"one-{n}", vcpus=1)
        _admit(ltvm, host, ["create", "one-79", "--vcpus", "1"])
        _vm("one-79", vcpus=1)
        _refusal(ltvm, host, ["create", "one-80", "--vcpus", "1"])

    def test_10_vms_with_8_vcpus_fit_a_cap_of_80(
        self, ltvm: Any, host: SimpleNamespace
    ) -> None:
        _caps(host, max_vcpus=80)
        for n in range(9):
            _vm(f"eight-{n}", vcpus=8)
        _admit(ltvm, host, ["create", "eight-9", "--vcpus", "8"])
        _vm("eight-9", vcpus=8)
        err = _refusal(ltvm, host, ["create", "eight-10", "--vcpus", "8"])
        assert err.details["requested"]["vcpus"] == 8
        assert err.details["used"]["vcpus"] == 80
        assert err.details["cap"]["vcpus"] == 80

    def test_owner_vms_do_not_count(
        self, ltvm: Any, host: SimpleNamespace
    ) -> None:
        _caps(host, max_vcpus=4)
        _vm("owner-sentinel-a3", vcpus=64, owner="pid:4242")
        _admit(ltvm, host, ["create", "small", "--vcpus", "4"])

    def test_create_of_an_existing_remote_vm_asks_for_nothing(
        self, ltvm: Any, host: SimpleNamespace
    ) -> None:
        # create of a stopped VM only starts it.  The VM is in "used".
        _caps(host, max_vcpus=8)
        _vm("again", vcpus=8)
        _admit(ltvm, host, ["create", "again", "--vcpus", "8"])

    def test_no_caps_admits_everything(
        self, ltvm: Any, host: SimpleNamespace
    ) -> None:
        _vm("big", vcpus=512)
        _admit(ltvm, host, ["create", "huge", "--vcpus", "512"])


class TestRamCap:
    def test_ram_counts_stopped_remote_vms_too(
        self, ltvm: Any, host: SimpleNamespace
    ) -> None:
        _caps(host, max_ram_gb=8)
        _vm("r1", mem=4096)
        _admit(ltvm, host, ["create", "r2", "--mem", "4096"])
        err = _refusal(ltvm, host, ["create", "r3", "--mem", "8192"])
        assert err.details["requested"]["ram_gb"] == 8
        assert err.details["used"]["ram_gb"] == 4
        assert err.details["cap"]["ram_gb"] == 8

    def test_no_mem_option_asks_for_the_target_default(
        self, ltvm: Any, host: SimpleNamespace
    ) -> None:
        from ltvm_pkg.target_config import TargetConfig

        # rocky10 needs more than the 2048 MiB fallback.
        default = TargetConfig("rocky10").default_mem
        assert default > 2048
        _caps(host, max_ram_gb=default / 1024)
        _admit(ltvm, host, ["create", "r10", "rocky10"])
        _vm("r10-other", mem=1024)
        err = _refusal(ltvm, host, ["create", "r10", "rocky10"])
        assert err.details["requested"]["ram_gb"] == default / 1024


class TestDiskCap:
    def test_jobs_artifacts_and_vm_disks_count(
        self, ltvm: Any, host: SimpleNamespace
    ) -> None:
        from ltvm_pkg import target_config
        from ltvm_pkg.remote import admission
        from ltvm_pkg.vm_state import OVERLAYS

        _vm("d1")
        (Path(OVERLAYS) / "d1.qcow2").write_bytes(b"x" * (1 << 20))
        (Path(OVERLAYS) / "d1-disk1.img").write_bytes(b"x" * (1 << 20))
        job = host.home / "jobs" / "job-1" / "tree"
        job.mkdir(parents=True)
        (job / "big.o").write_bytes(b"x" * (2 << 20))
        art = Path(target_config.ARTIFACTS_DIR)
        art.mkdir(parents=True, exist_ok=True)
        (art / "base.ext4").write_bytes(b"x" * (3 << 20))
        # An owner VM disk does not count.
        _vm("owner-sentinel-d2", owner="pid:4242")
        (Path(OVERLAYS) / "owner-sentinel-d2.qcow2").write_bytes(
            b"x" * (64 << 20)
        )

        # 1 + 1 + 2 + 3 MiB, and some blocks for the directories.
        used_mib = admission.usage(host.env).disk_gb * 1024
        assert 7 <= used_mib < 8

        _caps(host, max_disk_gb=1.0)
        err = _refusal(ltvm, host, ["create", "d3"])
        assert err.details["used"]["disk_gb"] > 0
        assert err.details["requested"]["disk_gb"] == 1.0

    def test_a_new_vm_fits_an_empty_host(
        self, ltvm: Any, host: SimpleNamespace
    ) -> None:
        _caps(host, max_disk_gb=1.5)
        _admit(ltvm, host, ["create", "d4"])

    def test_without_a_disk_cap_used_disk_is_not_counted(
        self, ltvm: Any, host: SimpleNamespace
    ) -> None:
        _caps(host, max_vcpus=1)
        err = _refusal(ltvm, host, ["create", "d5"])
        assert err.details["used"]["disk_gb"] is None
        assert err.details["cap"]["disk_gb"] is None


class TestClusterCap:
    def test_a_cluster_above_the_cap_is_refused_as_a_whole(
        self, ltvm: Any, host: SimpleNamespace
    ) -> None:
        _caps(host, max_vcpus=10)
        argv = [
            "cluster", "create", "c1", "rocky9",
            "mgs+mds:c1-mds:1", "oss:c1-oss:2", "client:c1-cl",
            "--vcpus", "4",
        ]  # fmt: skip
        err = _refusal(ltvm, host, argv)
        assert err.details["requested"]["vcpus"] == 12
        assert err.details["requested"]["disk_gb"] == 3.0

    def test_a_failover_partner_counts_as_a_node(
        self, ltvm: Any, host: SimpleNamespace
    ) -> None:
        _caps(host, max_vcpus=8)
        argv = [
            "cluster", "create", "c4", "rocky9",
            "mgs+mds:c4-mds:1", "client:c4-cl",
            "--failover", "c4-mds2=c4-mds",
            "--vcpus", "4",
        ]  # fmt: skip
        err = _refusal(ltvm, host, argv)
        assert err.details["requested"]["vcpus"] == 12

    def test_a_cluster_that_fits_is_admitted(
        self, ltvm: Any, host: SimpleNamespace
    ) -> None:
        _caps(host, max_vcpus=12, max_ram_gb=6)
        argv = [
            "cluster", "create", "c2",
            "mgs+mds:c2-mds:1", "oss:c2-oss:2", "client:c2-cl",
            "--vcpus", "4", "--mem", "2048",
        ]  # fmt: skip
        _admit(ltvm, host, argv)

    def test_node_target_sets_the_default_mem_of_one_node(
        self, ltvm: Any, host: SimpleNamespace
    ) -> None:
        from ltvm_pkg.target_config import TargetConfig

        mem9 = TargetConfig("rocky9").default_mem
        mem10 = TargetConfig("rocky10").default_mem
        _caps(host, max_ram_gb=0.5)
        argv = [
            "cluster", "create", "c3", "rocky9",
            "mgs+mds:c3-mds:1", "client:c3-cl",
            "--node-target", "c3-cl=rocky10",
        ]  # fmt: skip
        err = _refusal(ltvm, host, argv)
        assert err.details["requested"]["ram_gb"] == (mem9 + mem10) / 1024


class TestOtherVerbs:
    @pytest.mark.parametrize(
        "argv", [["start", "x"], ["list"], ["destroy", "x"]]
    )
    def test_verbs_that_make_no_vm_are_not_checked(
        self, ltvm: Any, host: SimpleNamespace, argv: list[str]
    ) -> None:
        _caps(host, max_vcpus=0, max_ram_gb=0, max_disk_gb=0)
        _vm("full", vcpus=4)
        _admit(ltvm, host, argv)

    def test_a_bad_serve_toml_refuses_with_bad_config(
        self, ltvm: Any, host: SimpleNamespace
    ) -> None:
        from ltvm_pkg.remote import serve_config
        from ltvm_pkg.remote.protocol import RemoteError

        path = serve_config.config_path(host.env)
        path.parent.mkdir(parents=True)
        path.write_text("max_vcpus = = 3\n")
        with pytest.raises(RemoteError) as e:
            _admit(ltvm, host, ["create", "x"])
        assert e.value.code == "bad_config"


# ── over the wire ────────────────────────────────────────


class TestWire:
    def test_details_go_over_the_wire(self) -> None:
        import json

        from ltvm_pkg.remote import protocol
        from ltvm_pkg.remote.protocol import RemoteError

        err = RemoteError(
            "over_cap",
            "too big",
            hint="less",
            details={"requested": {"vcpus": 8}},
        )
        line = protocol.reply_line("1.0", err)
        back = RemoteError.from_json(json.loads(line)["error"])
        assert back == err

    def test_a_reply_with_no_details_still_reads(self) -> None:
        from ltvm_pkg.remote.protocol import RemoteError

        back = RemoteError.from_json({"code": "verb_refused", "message": "m"})
        assert back.details == {}


@pytest.fixture
def child(tmp_path: Path) -> SimpleNamespace:
    """Host `t` as an exec:// child, with caps and one remote VM."""
    from ltvm_pkg.remote import hosts, serve_config
    from tests.test_ltvm_cli import _LTVM_PATH
    from tests.test_remote_serve import TEST_KEY_FP, TEST_PUBKEY

    home = tmp_path / "host-home"
    home.mkdir()
    sockets = tmp_path / "host-vms" / "sockets"
    sockets.mkdir(parents=True)
    with patch("ltvm_pkg.vm_state.SOCKETS", sockets):
        _vm("rvm-sentinel-c4", vcpus=6, owner=f"remote:{TEST_KEY_FP}")
    serve_config.save(
        {"HOME": str(home)}, serve_config.ServeConfig(max_vcpus=8)
    )
    env = {
        "HOME": str(home),
        "LTVM_VM_DIR": str(tmp_path / "host-vms"),
        "LTVM_ARTIFACTS_DIR": str(tmp_path / "host-artifacts"),
        "LTVM_SITE_CONFIG": str(tmp_path / "no-such-ltvm.conf"),
        "LTVM_SERVE_TEST_LAYOUT": "ready",
    }
    hosts.add(
        hosts.Host(
            name="t", url=f"exec://{_LTVM_PATH}", env=env, pubkey=TEST_PUBKEY
        )
    )
    return SimpleNamespace(home=home, sockets=sockets)


class TestOverCapThroughServe:
    def test_json_error_has_requested_used_and_cap(
        self, child: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        import json

        rc = ltvm.run(
            ["--host", "t", "create", "big-sentinel-e1", "--vcpus", "4",
             "--mem", "1024", "--json"]
        )  # fmt: skip
        err = json.loads(capsys.readouterr().err)
        assert rc == 1
        assert err["code"] == "over_cap"
        assert err["requested"] == {"vcpus": 4, "ram_gb": 1.0, "disk_gb": 1.0}
        assert err["used"] == {"vcpus": 6, "ram_gb": 2.0, "disk_gb": None}
        assert err["cap"] == {"vcpus": 8, "ram_gb": None, "disk_gb": None}
        assert not (child.sockets / "big-sentinel-e1.info").exists()
        log = child.home / ".local" / "state" / "ltvm" / "serve.log"
        assert json.loads(log.read_text().splitlines()[-1])["result"] == (
            "over_cap"
        )

    def test_text_error_shows_the_values(
        self, child: SimpleNamespace, ltvm: Any, capsys: Any
    ) -> None:
        rc = ltvm.run(
            ["--host", "t", "cluster", "create", "cl-sentinel-e2",
             "mgs+mds:cl-e2-mds:1", "client:cl-e2-cl", "--vcpus", "2"]
        )  # fmt: skip
        err = capsys.readouterr().err
        assert rc == 1
        assert "above the host cap for vcpus" in err
        assert "requested: vcpus=4" in err
        assert "used: vcpus=6" in err
        assert "cap: vcpus=8" in err
        assert not list(child.sockets.glob("*e2*"))
