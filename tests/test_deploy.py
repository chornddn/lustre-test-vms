"""Tests for ltvm_pkg/deploy.py: tar streaming, disk topology, mount."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import deploy
from ltvm_pkg.vm_state import VMInfo


def _ok(stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock()
    r.returncode = 0
    r.stdout = stdout
    r.stderr = stderr
    return r


def _fail(rc: int = 1, stdout: str = "", stderr: str = "boom") -> MagicMock:
    r = MagicMock()
    r.returncode = rc
    r.stdout = stdout
    r.stderr = stderr
    return r


@pytest.fixture
def tmp_sockets(tmp_path: Path) -> Path:
    """Redirect SOCKETS so VMInfo.save writes under tmp_path."""
    with patch("ltvm_pkg.vm_state.SOCKETS", tmp_path):
        yield tmp_path


@pytest.fixture
def staging(tmp_path: Path) -> Path:
    """Create a minimal staging tree."""
    s = tmp_path / "staging"
    (s / "usr/lib64/lustre/tests").mkdir(parents=True)
    (s / "lib/modules/5.14").mkdir(parents=True)
    (s / "lib/modules/5.14/lustre.ko").write_text("")
    return s


def _make_vm(
    name: str = "co1-single",
    ip: str = "192.168.100.50",
    mdt_disks: int = 0,
    ost_disks: int = 0,
    disk_size: int = 500 * 1024 * 1024,
) -> VMInfo:
    return VMInfo(
        name=name,
        ip=ip,
        mdt_disks=mdt_disks,
        ost_disks=ost_disks,
        disk_size=disk_size,
    )


# ── configure_test_disks ─────────────────────────────────


class TestConfigureTestDisks:
    """configure_test_disks writes a correct disk topology into local.sh."""

    def _capture_script(
        self,
        mdt: int,
        ost: int,
        disk_size: int = 0,
        os_family: str = "rhel",
    ) -> str:
        captured: dict = {}

        def fake_run_ssh(ip, script, timeout=30):
            captured["ip"] = ip
            captured["script"] = script
            return _ok()

        with patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_run_ssh):
            deploy.configure_test_disks(
                "10.0.0.1",
                mdt,
                ost,
                disk_size,
                os_family=os_family,
            )
        return captured["script"]

    def test_mdt_only_starts_at_vdb(self) -> None:
        """Single MDT disk maps to /dev/vdb (vda is rootfs)."""
        script = self._capture_script(mdt=1, ost=0)
        assert "MDSCOUNT=1" in script
        assert "MDSDEV1=/dev/vdb" in script
        assert "OSTCOUNT" not in script

    def test_mdt_and_ost_ordering(self) -> None:
        """MDT disks come first, then OST disks in allocation order."""
        # mdt=2, ost=3: MDSDEV1=vdb, MDSDEV2=vdc, OSTDEV1=vdd, vde, vdf
        script = self._capture_script(mdt=2, ost=3)
        assert "MDSCOUNT=2" in script
        assert "MDSDEV1=/dev/vdb" in script
        assert "MDSDEV2=/dev/vdc" in script
        assert "OSTCOUNT=3" in script
        assert "OSTDEV1=/dev/vdd" in script
        assert "OSTDEV2=/dev/vde" in script
        assert "OSTDEV3=/dev/vdf" in script

    def test_ost_only(self) -> None:
        """OST-only VMs get OSTDEV1=vdb (skipping the MDT range)."""
        script = self._capture_script(mdt=0, ost=2)
        assert "OSTCOUNT=2" in script
        assert "OSTDEV1=/dev/vdb" in script
        assert "OSTDEV2=/dev/vdc" in script
        assert "MDSCOUNT" not in script

    def test_disk_size_in_kb(self) -> None:
        """disk_size_bytes emits MDSSIZE/OSTSIZE in kilobytes."""
        # 500 MiB
        script = self._capture_script(
            mdt=1,
            ost=1,
            disk_size=500 * 1024 * 1024,
        )
        assert "MDSSIZE=512000" in script
        assert "OSTSIZE=512000" in script

    def test_no_size_when_zero(self) -> None:
        """disk_size=0 omits MDSSIZE/OSTSIZE entirely."""
        script = self._capture_script(mdt=1, ost=1, disk_size=0)
        assert "MDSSIZE" not in script
        assert "OSTSIZE" not in script

    def test_size_only_for_present_roles(self) -> None:
        """OSTSIZE is only written when there are OST disks."""
        script = self._capture_script(mdt=1, ost=0, disk_size=1024 * 1024)
        assert "MDSSIZE=1024" in script
        assert "OSTSIZE" not in script

    def test_reformatting_suites_reach_the_raw_devices(self) -> None:
        """stop() must drop llmount's dm-flakey mappers, or conf-sanity's
        own reformat finds /dev/vdb held and mkfs fails."""
        assert "CLEANUP_DM_DEV=true" in self._capture_script(mdt=1, ost=1)

    def test_markers_wrap_generated_block(self) -> None:
        """The generated snippet is wrapped in VM-disk sentinel markers."""
        script = self._capture_script(mdt=1, ost=1)
        assert "# --- VM disk configuration" in script
        assert "# --- END VM disk configuration" in script

    def test_script_rewrites_block_in_place(self) -> None:
        """Regenerating keeps the block where it is instead of appending."""
        script = self._capture_script(mdt=1, ost=1)
        # Appending would move the block past anything set after it, and
        # cfg/local.sh is sourced, so the last assignment wins.
        assert "VM disk configuration" in script
        assert "awk" in script
        assert not script.rstrip().endswith(">> {}".format("cfg/local.sh"))

    def test_regenerating_does_not_outrank_a_later_block(self) -> None:
        """A block written after ours keeps winning when we regenerate.

        This is the llmount-reverts-RAM-OSTs bug: configure_test_disks
        used to delete its block and re-append, which moved it past the
        RAM OST block and silently put the OSTs back on virtio disks.
        """
        script = self._capture_script(mdt=1, ost=1)
        with tempfile.TemporaryDirectory() as d:
            cfgdir = Path(d) / "usr/lib64/lustre/tests/cfg"
            cfgdir.mkdir(parents=True)
            cfg = cfgdir / "local.sh"
            cfg.write_text(
                "# --- VM disk configuration (generated by ltvm deploy) ---\n"
                "OSTCOUNT=1\n"
                "OSTDEV1=/dev/vdz\n"
                "# --- END VM disk configuration ---\n"
                "\n"
                "# --- RAM OST configuration (generated by ltvm deploy) ---\n"
                "OSTCOUNT=2\n"
                "OSTDEV1=/dev/ram0\n"
                "# --- END RAM OST configuration ---\n"
            )
            # run the generated script against the sandbox copy
            local = script.replace(
                "/usr/lib64/lustre/tests/cfg/local.sh", str(cfg)
            )
            subprocess.run(["bash", "-c", local], check=True)

            body = cfg.read_text()
            assert body.index("VM disk configuration") < body.index(
                "RAM OST configuration"
            ), "the regenerated block jumped past the RAM OST block"
            # and the virtio block really was refreshed in place
            assert "OSTDEV1=/dev/vdc" in body
            assert "/dev/vdz" not in body
            # sourcing it must still land on the ram device
            out = subprocess.run(
                ["bash", "-c", f". {cfg}; echo $OSTDEV1"],
                capture_output=True,
                text=True,
                check=True,
            )
            assert out.stdout.strip() == "/dev/ram0"

    def test_debian_libdir(self) -> None:
        """debian os_family writes to /usr/lib/lustre (not /usr/lib64)."""
        script = self._capture_script(
            mdt=1,
            ost=1,
            os_family="debian",
        )
        assert "/usr/lib/lustre/tests/cfg/local.sh" in script
        assert "/usr/lib64/lustre" not in script

    def test_rhel_libdir(self) -> None:
        """rhel os_family writes to /usr/lib64/lustre."""
        script = self._capture_script(
            mdt=1,
            ost=1,
            os_family="rhel",
        )
        assert "/usr/lib64/lustre/tests/cfg/local.sh" in script

    def test_failure_raises_runtimeerror(self) -> None:
        """A non-zero ssh result surfaces as RuntimeError with stderr."""
        with patch(
            "ltvm_pkg.deploy.run_ssh",
            return_value=_fail(rc=1, stderr="no such dir"),
        ):
            with pytest.raises(RuntimeError, match="local.sh"):
                deploy.configure_test_disks("10.0.0.1", 1, 1)


# ── deploy_to_vm ─────────────────────────────────────────


class TestDeployToVm:
    """deploy_to_vm streams tar over ssh, runs depmod, and configures disks."""

    def test_missing_staging_raises(self, tmp_path: Path) -> None:
        """Nonexistent staging dir raises before spawning subprocesses."""
        vm = _make_vm()
        with pytest.raises(RuntimeError, match="Staging directory not found"):
            deploy.deploy_to_vm(vm, tmp_path / "nope")

    def test_tar_failure_raises(self, staging: Path) -> None:
        """subprocess nonzero rc surfaces as RuntimeError with output."""
        vm = _make_vm()
        with (
            patch(
                "ltvm_pkg.deploy.subprocess.run",
                return_value=MagicMock(
                    returncode=2, stdout="", stderr="tar: boom"
                ),
            ),
            patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()),
        ):
            with pytest.raises(RuntimeError, match="tar deploy failed"):
                deploy.deploy_to_vm(vm, staging)

    def test_depmod_failure_raises(self, staging: Path) -> None:
        """A failed depmod after successful tar propagates with rc."""
        vm = _make_vm()
        with (
            patch(
                "ltvm_pkg.deploy.subprocess.run",
                return_value=_ok(),
            ),
            patch(
                "ltvm_pkg.deploy.run_ssh",
                return_value=_fail(rc=7, stderr="depmod: boom"),
            ),
        ):
            with pytest.raises(RuntimeError, match="depmod"):
                deploy.deploy_to_vm(vm, staging)

    def test_no_disks_skips_configure(self, staging: Path) -> None:
        """A VM with no data disks never calls configure_test_disks."""
        vm = _make_vm(mdt_disks=0, ost_disks=0)
        with (
            patch(
                "ltvm_pkg.deploy.subprocess.run",
                return_value=_ok(),
            ),
            patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()),
            patch("ltvm_pkg.deploy.configure_test_disks") as mock_cfg,
        ):
            deploy.deploy_to_vm(vm, staging)
            mock_cfg.assert_not_called()

    def test_disks_trigger_configure(self, staging: Path) -> None:
        """VMs with data disks forward topology to configure_test_disks."""
        vm = _make_vm(mdt_disks=1, ost_disks=2, disk_size=12345)
        with (
            patch(
                "ltvm_pkg.deploy.subprocess.run",
                return_value=_ok(),
            ),
            patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()),
            patch("ltvm_pkg.deploy.configure_test_disks") as mock_cfg,
        ):
            deploy.deploy_to_vm(vm, staging, os_family="rhel")
            mock_cfg.assert_called_once_with(
                vm.ip,
                1,
                2,
                12345,
                os_family="rhel",
            )

    def test_tar_command_targets_staging_and_vm_ip(self, staging: Path) -> None:
        """The bash tar|ssh pipeline references the staging dir and VM IP."""
        vm = _make_vm(ip="10.11.12.13")
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            return _ok()

        with (
            patch("ltvm_pkg.deploy.subprocess.run", side_effect=fake_run),
            patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()),
        ):
            deploy.deploy_to_vm(vm, staging)
        # bash -c "... tar --no-xattrs ... cf - -C <staging> . | ... ssh ... root@<ip> ..."
        assert captured["args"][0] == "bash"
        assert captured["args"][1] == "-c"
        pipeline = captured["args"][2]
        assert f"-C {staging}" in pipeline or str(staging) in pipeline
        assert "root@10.11.12.13" in pipeline
        assert "tar --no-xattrs" in pipeline
        assert "cf -" in pipeline
        assert "tar xf -" in pipeline

    def test_userspace_only_skips_modules_and_depmod(
        self, staging: Path
    ) -> None:
        """userspace_only excludes lib/modules and runs ldconfig only."""
        vm = _make_vm()
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            return _ok()

        ssh_cmds = []

        def fake_ssh(ip, cmd, timeout=60):
            ssh_cmds.append(cmd)
            return _ok()

        with (
            patch("ltvm_pkg.deploy.subprocess.run", side_effect=fake_run),
            patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_ssh),
        ):
            deploy.deploy_to_vm(vm, staging, userspace_only=True)
        assert "--exclude=./lib/modules" in captured["args"][2]
        # post-deploy should be ldconfig only (no depmod)
        assert ssh_cmds == ["ldconfig"]

    def test_full_deploy_runs_depmod(self, staging: Path) -> None:
        """Normal (kernel-module) deploy runs depmod -a && ldconfig."""
        vm = _make_vm()
        ssh_cmds = []

        def fake_ssh(ip, cmd, timeout=60):
            ssh_cmds.append(cmd)
            return _ok()

        with (
            patch("ltvm_pkg.deploy.subprocess.run", return_value=_ok()),
            patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_ssh),
        ):
            deploy.deploy_to_vm(vm, staging)
        assert ssh_cmds == [deploy._UNLOAD_SCRIPT, "depmod -a && ldconfig"]

    def test_a_lustre_that_will_not_unload_stops_the_deploy(
        self, staging: Path
    ) -> None:
        """Nothing is streamed: the new files would not take effect."""
        vm = _make_vm()
        with (
            patch("ltvm_pkg.deploy.subprocess.run") as tar,
            patch(
                "ltvm_pkg.deploy.run_ssh",
                return_value=_ok(stdout="unmounted\nmounted\nloaded libcfs\n"),
            ),
        ):
            with pytest.raises(RuntimeError, match="still loaded"):
                deploy.deploy_to_vm(vm, staging)
        tar.assert_not_called()


class TestUnloadLustre:
    """deploy unloads whatever Lustre is running, however it was mounted."""

    def _unload(self, **ssh: Any) -> None:
        with patch("ltvm_pkg.deploy.run_ssh", **ssh):
            deploy.unload_lustre(_make_vm(name="co9-a"))

    def test_nothing_loaded_is_quiet(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._unload(return_value=_ok(stdout=""))
        assert capsys.readouterr().err == ""

    def test_says_what_it_took_down(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._unload(
            return_value=_ok(
                stdout="unmounted /mnt/lustre /mnt/ost /mnt/mdt\n"
                "mounted\nloaded\n"
            )
        )
        err = capsys.readouterr().err
        assert "Unloaded the running Lustre on co9-a" in err
        assert "/mnt/lustre, /mnt/ost, /mnt/mdt" in err

    def test_still_mounted_is_an_error(self) -> None:
        with pytest.raises(RuntimeError) as exc:
            self._unload(
                return_value=_ok(
                    stdout="unmounted /mnt/mdt\nmounted /mnt/mdt\n"
                    "loaded libcfs\n"
                )
            )
        assert "still mounted on /mnt/mdt" in str(exc.value)
        assert "ltvm stop co9-a && ltvm start co9-a" in str(exc.value)

    def test_timeout_is_an_error(self) -> None:
        with pytest.raises(RuntimeError, match="timed out"):
            self._unload(side_effect=subprocess.TimeoutExpired("ssh", 180))

    def test_unreachable_vm_is_an_error(self) -> None:
        with pytest.raises(RuntimeError, match="rc=255"):
            self._unload(return_value=_fail(rc=255, stderr="no route"))

    def test_script_unmounts_by_hand_mounts_too(self) -> None:
        """Every lustre mount in /proc/mounts, not llmount.sh's own."""
        script = deploy._UNLOAD_SCRIPT
        assert "/proc/mounts" in script
        # llmount.sh's own targets, which a filter on "lustre" alone missed.
        assert '"lustre_tgt"' in script
        assert "llmountcleanup" not in script
        assert script.index("umount -f") < script.index("lustre_rmmod")


# ── lustre_mount_vm ──────────────────────────────────────


class TestLustreMountVm:
    """lustre_mount_vm cleans up state then runs llmount.sh."""

    def test_vm_not_found_returns_not_found_exit(
        self,
        tmp_sockets: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from ltvm_pkg.vm_state import EXIT_NOT_FOUND

        rc = deploy.lustre_mount_vm("ghost", os_family="rhel")
        assert rc == EXIT_NOT_FOUND

    def test_mount_success_returns_zero(self, tmp_sockets: Path) -> None:
        """Happy path: cleanup + llmount.sh both return 0."""
        vm = _make_vm(name="mount-ok", ip="10.0.0.5")
        vm.save()
        with patch("ltvm_pkg.deploy.run_ssh", return_value=_ok(stdout="")):
            rc = deploy.lustre_mount_vm("mount-ok", os_family="rhel")
        assert rc == 0

    def test_mount_calls_cleanup_then_llmount(self, tmp_sockets: Path) -> None:
        """lustre_mount_vm runs llmountcleanup first, then llmount."""
        vm = _make_vm(name="mount-order", ip="10.0.0.6")
        vm.save()
        calls = []

        def fake_ssh(ip, cmd, timeout=0):
            calls.append(cmd)
            return _ok()

        with patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_ssh):
            deploy.lustre_mount_vm("mount-order", os_family="rhel")
        assert len(calls) == 2
        assert "llmountcleanup.sh" in calls[0]
        assert "dmsetup remove_all" in calls[0]
        assert "llmount.sh" in calls[1]
        assert "llmountcleanup.sh" not in calls[1]

    def test_mount_uses_debian_libdir(self, tmp_sockets: Path) -> None:
        """debian os_family passes /usr/lib/lustre into the mount commands."""
        vm = _make_vm(name="mount-deb", ip="10.0.0.7")
        vm.save()
        calls = []

        def fake_ssh(ip, cmd, timeout=0):
            calls.append(cmd)
            return _ok()

        with patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_ssh):
            deploy.lustre_mount_vm("mount-deb", os_family="debian")
        assert all("/usr/lib/lustre" in c for c in calls)
        assert all("/usr/lib64/lustre" not in c for c in calls)

    def test_mount_failure_returns_rc(
        self,
        tmp_sockets: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A failing llmount.sh surfaces its rc from lustre_mount_vm."""
        vm = _make_vm(name="mount-fail", ip="10.0.0.8")
        vm.save()
        results = iter([_ok(), _fail(rc=5, stderr="mount broken")])
        with patch(
            "ltvm_pkg.deploy.run_ssh",
            side_effect=lambda *a, **k: next(results),
        ):
            rc = deploy.lustre_mount_vm("mount-fail", os_family="rhel")
        assert rc == 5

    def test_mount_ssh_exception_returns_error(self, tmp_sockets: Path) -> None:
        """A run_ssh exception becomes EXIT_ERROR (not a traceback)."""
        from ltvm_pkg.vm_state import EXIT_ERROR

        vm = _make_vm(name="mount-exc", ip="10.0.0.9")
        vm.save()
        with patch(
            "ltvm_pkg.deploy.run_ssh",
            side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=60),
        ):
            rc = deploy.lustre_mount_vm("mount-exc", os_family="rhel")
        assert rc == EXIT_ERROR


# --------------------------------------------------------------------------
# cmd_deploy: target resolution, staging refusal, no build
# --------------------------------------------------------------------------


def _setup_lustre_tree(build_path: Path) -> None:
    (build_path / "lustre").mkdir(parents=True)
    (build_path / "lnet").mkdir()
    (build_path / "configure.ac").write_text("")


def _seed_staging(
    build_path: Path,
    kernel: str = "5.14-rhel9.7",
    variant: str = "base",
    module: str = "lustre.ko",
) -> Path:
    """Lay down a staging tree that reads as freshly built."""
    from ltvm_pkg.lustre_build import staging_path

    staging = staging_path(
        build_path, "rocky9", arch="x86_64", kernel=kernel, variant=variant
    )
    staging.mkdir(parents=True)
    (staging / module).write_text("")
    (staging / ".ltvm-staging-stamp").write_text("5.14.0-foo\n")
    return staging


def _deploy_args(
    name: str = "co-test",
    lustre_tree: str | None = None,
    *,
    json: bool = False,
    userspace_only: bool = False,
    cfg_dir: str | None = None,
    arch: str | None = None,
    net: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        name=name,
        lustre_tree=lustre_tree,
        json=json,
        userspace_only=userspace_only,
        cfg_dir=cfg_dir,
        arch=arch,
        net=net,
        as_owner=None,
        force=False,
    )


def _mark_staging_fresh(
    staging: Path,
    build_path: Path,
    tc: Any,
    *,
    kernel: str = "5.14-rhel9.7",
    target: str = "rocky9",
) -> None:
    """Make a staging dir pass cmd_deploy's fast-path freshness check.

    The check now verifies that the staging was built against the
    kernel ABI and configure flags currently in play, not just that no
    source file is newer than the stamp -- so a test that wants "this
    staging is up to date" has to record those too.  Creates the
    kernel build-tree's Module.symvers and the tree's configure stamp
    to match what .ltvm-staging-meta.json claims.
    """
    from ltvm_pkg.lustre_build import _hash_file, _stamp_suffix

    build_tree = Path(tc.kernel_output_dir(kernel=kernel)) / "build-tree"
    build_tree.mkdir(parents=True, exist_ok=True)
    symvers = build_tree / "Module.symvers"
    if not symvers.exists():
        symvers.write_text("dummy symvers\n")
    cfg_hash = "deadbeef" * 8

    meta_file = staging / ".ltvm-staging-meta.json"
    meta = {}
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text())
        except ValueError:
            meta = {}
    meta.setdefault("kernel_version", "5.14.0-fake")
    meta["module_symvers_sha256"] = _hash_file(symvers)
    meta["configure_sha256"] = cfg_hash
    meta_file.write_text(json.dumps(meta))

    (
        build_path / f".ltvm-configure-{_stamp_suffix(target, tc.arch)}"
    ).write_text(cfg_hash + "\n")
    # Writing into the tree root bumps its mtime, and the fast path's
    # `find -newer` compares against the staging stamp -- so re-touch
    # the stamp last or the setup makes itself look stale.
    (staging / ".ltvm-staging-stamp").touch()


# cmd_deploy's freshness check reads the kernel build-tree's
# Module.symvers, so the stub's kernel_output_dir has to be a path
# tests can actually write to -- "/fake/kernels/..." cannot be created.
_STUB_KERNELS_ROOT = Path(tempfile.mkdtemp(prefix="ltvm-test-kernels-"))


def _stub_tc() -> MagicMock:
    """Standard TargetConfig stub used by cmd_deploy tests."""
    tc = MagicMock()
    tc.os_family = "rhel"
    tc.arch = "x86_64"
    tc.resolve_kernel.side_effect = lambda k: k or "5.14-rhel9.7"
    tc.kernel_output_dir.side_effect = lambda kernel=None: (
        _STUB_KERNELS_ROOT / f"{kernel or '5.14-rhel9.7'}"
    )
    return tc


class TestCmdDeployNameResolution:
    """`deploy <name>` takes a VM or a cluster and says so when neither."""

    def test_resolves_a_vm_name(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = _seed_staging(build_path)

        vm = _make_vm(name="co1-vm", ip="10.0.1.1")
        vm.os_id = "rocky9"
        vm.save()

        captured: dict = {}
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch(
                "ltvm_pkg.cli.deploy_to_vm",
                side_effect=lambda v, s, **kw: captured.update(staging=Path(s)),
            ),
        ):
            rc = cli_mod.cmd_deploy(
                _deploy_args(name="co1-vm", lustre_tree=str(build_path))
            )

        assert rc == 0
        assert captured["staging"] == staging

    def test_resolves_a_cluster_name(self, tmp_sockets: Path) -> None:
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.vm_state import ClusterInfo

        ClusterInfo(
            name="co9",
            nodes=[{"name": "co9-mds", "roles": ["mgs", "mds"]}],
        ).save()

        seen: dict = {}
        with patch(
            "ltvm_pkg.vm_cluster.cmd_cluster_deploy",
            side_effect=lambda ns: seen.update(
                name=ns.name, tree=ns.lustre_tree
            ),
        ):
            rc = cli_mod.cmd_deploy(
                _deploy_args(name="co9", lustre_tree="/some/tree")
            )

        assert rc == 0
        assert seen == {"name": "co9", "tree": "/some/tree"}

    def test_unknown_name_names_both_lookups(
        self, tmp_sockets: Path, capsys
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        rc = cli_mod.cmd_deploy(_deploy_args(name="ghost"))
        assert rc == 1
        err = capsys.readouterr().err
        assert "cluster" in err and "VM" in err

    def test_name_that_is_both_is_refused(self, tmp_sockets: Path) -> None:
        """Picking a winner silently would deploy somewhere unintended."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.vm_state import ClusterInfo

        vm = _make_vm(name="twin", ip="10.0.1.2")
        vm.os_id = "rocky9"
        vm.save()
        ClusterInfo(
            name="twin", nodes=[{"name": "twin-mds", "roles": ["mgs"]}]
        ).save()

        with patch("ltvm_pkg.cli.deploy_to_vm") as deploy_mock:
            rc = cli_mod.cmd_deploy(_deploy_args(name="twin"))
        assert rc == 1
        deploy_mock.assert_not_called()


class TestCmdDeployDerivesBuildInputs:
    """Kernel, arch and variant come from the target, never from a flag."""

    def test_vm_kernel_beats_target_default(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        _seed_staging(build_path, kernel="5.14-rhel9.5")

        vm = _make_vm(name="co1-altkern", ip="10.0.1.3")
        vm.os_id = "rocky9"
        vm.kernel = "/fake/artifacts/rocky9/x86_64/kernels/5.14-rhel9.5/vmlinux"
        vm.save()

        captured: dict = {}
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch(
                "ltvm_pkg.cli.deploy_to_vm",
                side_effect=lambda v, s, **kw: captured.update(staging=Path(s)),
            ),
        ):
            rc = cli_mod.cmd_deploy(
                _deploy_args(name="co1-altkern", lustre_tree=str(build_path))
            )

        assert rc == 0
        assert captured["staging"].name == "5.14-rhel9.5"

    def test_mofed_variant_routes_to_mofed_staging(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        _seed_staging(build_path, variant="mofed-24", module="ko2iblnd.ko")

        vm = _make_vm(name="co1-mofed", ip="10.0.1.4")
        vm.os_id = "rocky9"
        vm.variant = "mofed-24"
        vm.save()

        captured: dict = {}
        with (
            patch.object(
                cli_mod, "TargetConfig", return_value=_stub_tc()
            ) as tc_mock,
            patch(
                "ltvm_pkg.cli.deploy_to_vm",
                side_effect=lambda v, s, **kw: captured.update(staging=Path(s)),
            ),
        ):
            rc = cli_mod.cmd_deploy(
                _deploy_args(name="co1-mofed", lustre_tree=str(build_path))
            )

        assert rc == 0
        assert tc_mock.call_args.kwargs.get("variant") == "mofed-24"
        assert captured["staging"].name == "mofed-24"

    def test_arch_flag_is_refused(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """--arch rides in from the shared parent parser; deploy rejects it."""
        from ltvm_pkg import cli as cli_mod

        vm = _make_vm(name="co1-arch", ip="10.0.1.5")
        vm.os_id = "rocky9"
        vm.save()

        with patch("ltvm_pkg.cli.deploy_to_vm") as deploy_mock:
            rc = cli_mod.cmd_deploy(
                _deploy_args(name="co1-arch", arch="aarch64")
            )
        assert rc == 1
        deploy_mock.assert_not_called()

    def test_net_flag_is_refused_for_a_single_vm(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """--net names a whole cluster's LNet: every node has to agree
        on one MGS NID, so it cannot be set one node at a time."""
        from ltvm_pkg import cli as cli_mod

        vm = _make_vm(name="co1-net", ip="10.0.1.7")
        vm.os_id = "rocky9"
        vm.save()

        with patch("ltvm_pkg.cli.deploy_to_vm") as deploy_mock:
            rc = cli_mod.cmd_deploy(_deploy_args(name="co1-net", net="o2ib"))
        assert rc == 1
        deploy_mock.assert_not_called()


class TestCmdDeployNeverBuilds:
    """Missing or stale staging is a hard error, not an implicit build."""

    def test_no_staging_names_the_build_and_spawns_nothing(
        self, tmp_sockets: Path, tmp_path: Path, capsys
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)

        vm = _make_vm(name="co1-nostaging", ip="10.0.1.6")
        vm.os_id = "rocky9"
        vm.save()

        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm") as deploy_mock,
            patch("subprocess.run") as run_mock,
        ):
            rc = cli_mod.cmd_deploy(
                _deploy_args(name="co1-nostaging", lustre_tree=str(build_path))
            )

        assert rc == 1
        deploy_mock.assert_not_called()
        run_mock.assert_not_called()
        err = capsys.readouterr().err
        assert "ltvm build lustre" in err
        assert "--configure" in err

    def test_stale_staging_is_refused(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """A source edit after the build stamp refuses rather than ships."""
        import os as _os
        import time as _time

        from ltvm_pkg import cli as cli_mod

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        _seed_staging(build_path)
        edited = build_path / "lustre" / "later.c"
        edited.write_text("")
        future = _time.time() + 60
        _os.utime(edited, (future, future))

        vm = _make_vm(name="co1-stale", ip="10.0.1.7")
        vm.os_id = "rocky9"
        vm.save()

        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm") as deploy_mock,
        ):
            rc = cli_mod.cmd_deploy(
                _deploy_args(name="co1-stale", lustre_tree=str(build_path))
            )

        assert rc == 1
        deploy_mock.assert_not_called()


class TestCmdDeployErrorPaths:
    """Error-path branches of cmd_deploy that gate downstream work."""

    def test_no_os_id_errors(self, tmp_sockets: Path) -> None:
        from ltvm_pkg import cli as cli_mod

        vm = _make_vm(name="co1-no-target", ip="10.0.1.8")
        vm.os_id = ""
        vm.save()

        assert cli_mod.cmd_deploy(_deploy_args(name="co1-no-target")) == 1

    def test_unknown_target_yields_targetconfig_error(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        vm = _make_vm(name="co1-unknown", ip="10.0.1.9")
        vm.os_id = "bogusos"
        vm.save()

        with patch.object(
            cli_mod, "TargetConfig", side_effect=ValueError("nope")
        ):
            rc = cli_mod.cmd_deploy(
                _deploy_args(name="co1-unknown", lustre_tree=str(tmp_path))
            )
        assert rc == 1

    def test_lustre_tree_missing_errors(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        vm = _make_vm(name="co1-nodir", ip="10.0.1.10")
        vm.os_id = "rocky9"
        vm.save()

        with patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()):
            rc = cli_mod.cmd_deploy(
                _deploy_args(
                    name="co1-nodir",
                    lustre_tree=str(tmp_path / "does-not-exist"),
                )
            )
        assert rc == 1

    def test_lustre_tree_not_a_tree_errors(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        bad = tmp_path / "not-a-lustre-tree"
        bad.mkdir()
        vm = _make_vm(name="co1-bad", ip="10.0.1.11")
        vm.os_id = "rocky9"
        vm.save()

        with patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()):
            rc = cli_mod.cmd_deploy(
                _deploy_args(name="co1-bad", lustre_tree=str(bad))
            )
        assert rc == 1

    def test_userspace_only_no_staging_errors(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        vm = _make_vm(name="co1-uspace", ip="10.0.1.12")
        vm.os_id = "rocky9"
        vm.save()

        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm") as deploy_mock,
        ):
            rc = cli_mod.cmd_deploy(
                _deploy_args(
                    name="co1-uspace",
                    lustre_tree=str(build_path),
                    userspace_only=True,
                )
            )
        assert rc == 1
        deploy_mock.assert_not_called()

    def test_deploy_to_vm_runtimeerror_returns_error(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        _seed_staging(build_path)

        vm = _make_vm(name="co1-rterr", ip="10.0.1.13")
        vm.os_id = "rocky9"
        vm.save()

        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch(
                "ltvm_pkg.cli.deploy_to_vm",
                side_effect=RuntimeError("ssh died"),
            ),
        ):
            rc = cli_mod.cmd_deploy(
                _deploy_args(name="co1-rterr", lustre_tree=str(build_path))
            )
        assert rc == 1


class TestCmdDeployUserspaceOnly:
    """--userspace-only happy path skips kernel modules and forwards flag."""

    def test_userspace_only_forwards_flag_to_deploy(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = _seed_staging(build_path)

        vm = _make_vm(name="co1-uspace-ok", ip="10.0.1.14")
        vm.os_id = "rocky9"
        vm.save()

        captured: dict = {}

        def fake_deploy_to_vm(vm_arg, staging_arg, **kwargs):
            captured.update(kwargs)
            captured["staging"] = Path(staging_arg)

        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm", side_effect=fake_deploy_to_vm),
        ):
            rc = cli_mod.cmd_deploy(
                _deploy_args(
                    name="co1-uspace-ok",
                    lustre_tree=str(build_path),
                    userspace_only=True,
                )
            )

        assert rc == 0
        assert captured.get("userspace_only") is True
        assert captured["staging"] == staging


class TestCmdDeployCfgDir:
    """--cfg-dir distributes auster profiles to a standalone VM."""

    def test_profiles_are_written(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        _seed_staging(build_path)
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        (cfg_dir / "co1sn.sh").write_text(". local.sh\n")

        vm = _make_vm(name="co1-cfg", ip="10.0.1.15")
        vm.os_id = "rocky9"
        vm.save()

        writes: list = []
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm"),
            patch(
                "ltvm_pkg.vm_cluster._write_cluster_cfg",
                side_effect=lambda n, ip, cfg, content, opts, fam: (
                    writes.append((n, cfg, content)) or (n, 0, "ok")
                ),
            ),
        ):
            rc = cli_mod.cmd_deploy(
                _deploy_args(
                    name="co1-cfg",
                    lustre_tree=str(build_path),
                    cfg_dir=str(cfg_dir),
                )
            )

        assert rc == 0
        assert writes == [("co1-cfg", "co1sn", ". local.sh\n")]

    def test_a_failed_write_is_fatal(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        _seed_staging(build_path)
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        (cfg_dir / "co1sn.sh").write_text("x\n")

        vm = _make_vm(name="co1-cfg-fail", ip="10.0.1.16")
        vm.os_id = "rocky9"
        vm.save()

        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm"),
            patch(
                "ltvm_pkg.vm_cluster._write_cluster_cfg",
                return_value=("co1-cfg-fail", 1, "no space"),
            ),
        ):
            rc = cli_mod.cmd_deploy(
                _deploy_args(
                    name="co1-cfg-fail",
                    lustre_tree=str(build_path),
                    cfg_dir=str(cfg_dir),
                )
            )
        assert rc == 1


class TestCmdDeployBundledSnapshot:
    """Bundled-snapshot detection and rsync mirroring path."""

    def _make_snapshot(
        self, tc_output_dir: Path, kernel: str = "5.14-rhel9.7"
    ) -> Path:
        """Lay down a snapshot dir with the marker file."""
        snap = tc_output_dir / "kernels" / kernel / "lustre-artifacts"
        snap.mkdir(parents=True)
        (snap / ".ltvm-snapshot.json").write_text("{}")
        (snap / "usr").mkdir()
        (snap / "lib").mkdir()
        (snap / "marker.ko").write_text("from-snapshot")
        return snap

    def test_bundled_snapshot_used_when_no_tree_given(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc_out = tmp_path / "tc-out"
        snap = self._make_snapshot(tc_out)

        vm = _make_vm(name="co1-bundled", ip="10.0.1.17")
        vm.os_id = "rocky9"
        vm.save()

        tc = _stub_tc()
        tc.output_dir = tc_out

        captured: dict = {}
        rsync_calls: list = []

        def fake_run(cmd, *args, **kwargs):
            rsync_calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        import os as _os

        old = _os.getcwd()
        try:
            _os.chdir(tmp_path)
            with (
                patch.object(cli_mod, "TargetConfig", return_value=tc),
                patch(
                    "ltvm_pkg.cli.deploy_to_vm",
                    side_effect=lambda v, s, **kw: captured.update(
                        staging=Path(s)
                    ),
                ),
                patch("subprocess.run", side_effect=fake_run),
            ):
                rc = cli_mod.cmd_deploy(
                    _deploy_args(name="co1-bundled", lustre_tree=None)
                )
        finally:
            _os.chdir(old)

        assert rc == 0
        assert rsync_calls, "expected rsync to be invoked"
        rsync = rsync_calls[0]
        assert rsync[0] == "rsync"
        assert "--delete" in rsync
        assert str(snap) + "/" in rsync
        staging = captured["staging"]
        assert ".ltvm-staging" in str(staging)
        assert staging.name == "5.14-rhel9.7"

    def test_bundled_snapshot_rsync_failure_returns_error(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc_out = tmp_path / "tc-out"
        self._make_snapshot(tc_out)

        vm = _make_vm(name="co1-rsync-fail", ip="10.0.1.18")
        vm.os_id = "rocky9"
        vm.save()

        tc = _stub_tc()
        tc.output_dir = tc_out

        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch("ltvm_pkg.cli.deploy_to_vm") as deploy_mock,
            patch(
                "subprocess.run",
                return_value=MagicMock(
                    returncode=23, stdout="", stderr="rsync: nope"
                ),
            ),
        ):
            rc = cli_mod.cmd_deploy(
                _deploy_args(name="co1-rsync-fail", lustre_tree=None)
            )

        assert rc == 1
        deploy_mock.assert_not_called()

    def test_bundled_snapshot_skips_lustre_tree_validation(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """Snapshot DESTDIR layout has no configure.ac -- must not error."""
        from ltvm_pkg import cli as cli_mod

        tc_out = tmp_path / "tc-out"
        snap = self._make_snapshot(tc_out)
        assert not (snap / "configure.ac").exists()

        vm = _make_vm(name="co1-snap-ok", ip="10.0.1.19")
        vm.os_id = "rocky9"
        vm.save()

        tc = _stub_tc()
        tc.output_dir = tc_out

        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch("ltvm_pkg.cli.deploy_to_vm"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout="", stderr=""),
            ),
        ):
            rc = cli_mod.cmd_deploy(
                _deploy_args(name="co1-snap-ok", lustre_tree=None)
            )

        assert rc == 0


class TestCmdDeployKverRecording:
    """Staging meta drives the recorded kver; bookkeeping never fails a
    deploy that already happened."""

    def test_kver_from_staging_meta_recorded(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.vm_state import VMInfo as _VMI

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = _seed_staging(build_path)
        (staging / ".ltvm-staging-meta.json").write_text(
            '{"kernel_version": "5.14.0-from-staging"}'
        )
        # After the meta write: _mark_staging_fresh merges the
        # freshness fields into whatever meta is already there.
        _mark_staging_fresh(staging, build_path, _stub_tc())

        vm = _make_vm(name="co1-kver", ip="10.0.1.20")
        vm.os_id = "rocky9"
        vm.save()

        update_calls: list = []
        orig = _VMI.update_deploy

        def capture(self, epoch, build_path_arg, kver):
            update_calls.append({"kver": kver})
            return orig(self, epoch, build_path_arg, kver)

        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm"),
            patch.object(_VMI, "update_deploy", capture),
        ):
            cli_mod.cmd_deploy(
                _deploy_args(name="co1-kver", lustre_tree=str(build_path))
            )

        assert update_calls
        assert update_calls[0]["kver"] == "5.14.0-from-staging"

    def test_update_deploy_permissionerror_warns_not_fail(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.vm_state import VMInfo as _VMI

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        _seed_staging(build_path)

        vm = _make_vm(name="co1-perm", ip="10.0.1.21")
        vm.os_id = "rocky9"
        vm.save()

        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm"),
            patch.object(
                _VMI, "update_deploy", side_effect=PermissionError("nope")
            ),
        ):
            rc = cli_mod.cmd_deploy(
                _deploy_args(name="co1-perm", lustre_tree=str(build_path))
            )
        assert rc == 0


# --------------------------------------------------------------------------
# cmd_llmount: thin wrapper over vm_commands.cmd_llmount
# --------------------------------------------------------------------------


class TestCmdLlmount:
    """cmd_llmount returns SystemExit codes from the underlying handler."""

    def test_no_cleanup_passes_flag_through(self, tmp_sockets: Path) -> None:
        import argparse as ap

        from ltvm_pkg import cli as cli_mod

        captured: dict = {}

        def fake(args):
            captured["cleanup"] = getattr(args, "cleanup", None)
            captured["vm"] = args.vm
            # mimic vm_commands.cmd_llmount calling sys.exit on success
            raise SystemExit(0)

        with patch("ltvm_pkg.vm_commands.cmd_llmount", side_effect=fake):
            args = ap.Namespace(
                vm="co1-mnt", json=False, timeout=300, cleanup=False
            )
            rc = cli_mod.cmd_llmount(args)

        assert rc == 0
        assert captured["cleanup"] is False
        assert captured["vm"] == "co1-mnt"

    def test_cleanup_flag_propagates(self, tmp_sockets: Path) -> None:
        import argparse as ap

        from ltvm_pkg import cli as cli_mod

        captured: dict = {}

        def fake(args):
            captured["cleanup"] = getattr(args, "cleanup", None)
            raise SystemExit(0)

        with patch("ltvm_pkg.vm_commands.cmd_llmount", side_effect=fake):
            args = ap.Namespace(
                vm="co1-clean", json=False, timeout=300, cleanup=True
            )
            rc = cli_mod.cmd_llmount(args)
        assert rc == 0
        assert captured["cleanup"] is True

    def test_systemexit_nonzero_propagates_rc(self, tmp_sockets: Path) -> None:
        import argparse as ap

        from ltvm_pkg import cli as cli_mod

        with patch(
            "ltvm_pkg.vm_commands.cmd_llmount",
            side_effect=SystemExit(5),
        ):
            args = ap.Namespace(
                vm="co1-fail", json=False, timeout=300, cleanup=False
            )
            rc = cli_mod.cmd_llmount(args)
        assert rc == 5

    def test_systemexit_none_code_maps_to_exit_error(
        self, tmp_sockets: Path
    ) -> None:
        """A bare `sys.exit()` (no code) maps to EXIT_ERROR == 1."""
        import argparse as ap

        from ltvm_pkg import cli as cli_mod

        with patch(
            "ltvm_pkg.vm_commands.cmd_llmount",
            side_effect=SystemExit(None),
        ):
            args = ap.Namespace(
                vm="co1-bare", json=False, timeout=300, cleanup=False
            )
            rc = cli_mod.cmd_llmount(args)
        assert rc == 1

    def test_normal_return_yields_exit_ok(self, tmp_sockets: Path) -> None:
        """If the underlying handler returns normally (no SystemExit), rc=0."""
        import argparse as ap

        from ltvm_pkg import cli as cli_mod

        with patch("ltvm_pkg.vm_commands.cmd_llmount", return_value=None):
            args = ap.Namespace(
                vm="co1-ret", json=False, timeout=300, cleanup=False
            )
            rc = cli_mod.cmd_llmount(args)
        assert rc == 0


class TestCmdDeployVariantPropagation:
    """Variant-aware staging path resolution."""

    def test_mofed_variant_routes_to_mofed_staging(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """A VM with variant=mofed-24 deploys from the mofed-24 staging dir."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path,
            "rocky9",
            arch="x86_64",
            kernel="5.14-rhel9.7",
            variant="mofed-24",
        )
        staging.mkdir(parents=True)
        (staging / "ko2iblnd.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")

        vm = _make_vm(name="co1-mofed", ip="10.0.0.20")
        vm.os_id = "rocky9"
        vm.variant = "mofed-24"
        vm.save()

        captured: dict = {}

        def fake_deploy_to_vm(vm_arg, staging_arg, **kwargs):
            captured["staging"] = Path(staging_arg)

        args = _deploy_args(name="co1-mofed", lustre_tree=str(build_path))
        with (
            patch.object(
                cli_mod, "TargetConfig", return_value=_stub_tc()
            ) as tc_mock,
            patch(
                "ltvm_pkg.cli.deploy_to_vm",
                side_effect=fake_deploy_to_vm,
            ),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=""),
            ),
        ):
            rc = cli_mod.cmd_deploy(args)

        assert rc == 0
        # TargetConfig must be invoked with variant=mofed-24
        kwargs = tc_mock.call_args.kwargs
        assert kwargs.get("variant") == "mofed-24"
        # Staging is the variant's own dir, a sibling of the base
        # kernel dir rather than nested inside it.
        assert captured["staging"].name == "5.14-rhel9.7__mofed-24"
        assert captured["staging"].parent.name == "x86_64"


class TestVerifyDeployedModules:
    """A deploy that ships stale modules must not pass silently."""

    def _staging(self, tmp_path, mods):
        d = tmp_path / "staging"
        d.mkdir()
        for name in mods:
            (d / name).write_bytes(b"\x7fELF fake")
        return d

    def _vm(self):
        class V:
            ip = "10.0.0.1"
            name = "co1-test"

        return V()

    def test_warns_on_mismatch(self, tmp_path, capsys):
        from unittest.mock import MagicMock, patch

        from ltvm_pkg.deploy import verify_deployed_modules

        staging = self._staging(tmp_path, ["osc.ko", "lov.ko"])
        with (
            patch(
                "ltvm_pkg.deploy.read_modinfo_field",
                side_effect=lambda p, f: "STAGED_" + p.name,
            ),
            patch("ltvm_pkg.deploy.run_ssh") as ssh,
        ):
            ssh.return_value = MagicMock(
                returncode=0, stdout="osc STAGED_osc.ko\nlov OLDBUILD\n"
            )
            verify_deployed_modules(self._vm(), staging)
        err = capsys.readouterr().err
        assert "do not match" in err
        assert "lov.ko" in err
        assert "osc.ko" not in err

    def test_silent_when_all_match(self, tmp_path, capsys):
        from unittest.mock import MagicMock, patch

        from ltvm_pkg.deploy import verify_deployed_modules

        staging = self._staging(tmp_path, ["osc.ko"])
        with (
            patch(
                "ltvm_pkg.deploy.read_modinfo_field",
                side_effect=lambda p, f: "SAME",
            ),
            patch("ltvm_pkg.deploy.run_ssh") as ssh,
        ):
            ssh.return_value = MagicMock(returncode=0, stdout="osc SAME\n")
            verify_deployed_modules(self._vm(), staging)
        assert capsys.readouterr().err == ""

    def test_module_absent_on_vm_is_not_stale(self, tmp_path, capsys):
        """A staged module the VM does not have is not evidence of staleness."""
        from unittest.mock import MagicMock, patch

        from ltvm_pkg.deploy import verify_deployed_modules

        staging = self._staging(tmp_path, ["osc.ko"])
        with (
            patch(
                "ltvm_pkg.deploy.read_modinfo_field",
                side_effect=lambda p, f: "SAME",
            ),
            patch("ltvm_pkg.deploy.run_ssh") as ssh,
        ):
            ssh.return_value = MagicMock(returncode=0, stdout="osc \n")
            verify_deployed_modules(self._vm(), staging)
        assert capsys.readouterr().err == ""

    def test_ssh_failure_warns_but_does_not_raise(self, tmp_path, capsys):
        from unittest.mock import MagicMock, patch

        from ltvm_pkg.deploy import verify_deployed_modules

        staging = self._staging(tmp_path, ["osc.ko"])
        with (
            patch(
                "ltvm_pkg.deploy.read_modinfo_field",
                side_effect=lambda p, f: "SAME",
            ),
            patch("ltvm_pkg.deploy.run_ssh") as ssh,
        ):
            ssh.return_value = MagicMock(returncode=255, stdout="")
            verify_deployed_modules(self._vm(), staging)
        assert "could not verify" in capsys.readouterr().err


# ── configure_ram_osts ───────────────────────────────────


class TestConfigureRamOsts:
    """Ram-backed OSTs take the OST's backing store out of a benchmark."""

    def _capture_script(
        self,
        count: int,
        size_gb: int = 32,
        ram_mdt: bool = False,
        os_family: str = "rhel",
    ) -> str:
        captured: dict = {}

        def fake_run_ssh(ip, script, timeout=30):
            captured["script"] = script
            return _ok()

        with patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_run_ssh):
            deploy.configure_ram_osts(
                "10.0.0.1",
                count,
                size_gb,
                ram_mdt=ram_mdt,
                os_family=os_family,
            )
        return captured["script"]

    def test_osts_map_to_ram_devices_from_zero(self) -> None:
        script = self._capture_script(count=4)
        for n, dev in enumerate(("ram0", "ram1", "ram2", "ram3"), start=1):
            assert f"OSTDEV{n}=/dev/{dev}" in script
        assert "OSTCOUNT=4" in script

    def test_mdt_untouched_by_default(self) -> None:
        """Without --ram-mdt the MDT keeps whatever it had; emitting an
        MDSDEV here would silently override the virtio mapping."""
        script = self._capture_script(count=4)
        assert "MDSDEV" not in script
        assert "MDSCOUNT" not in script

    def test_ram_mdt_takes_the_device_after_the_osts(self) -> None:
        script = self._capture_script(count=4, ram_mdt=True)
        assert "MDSDEV1=/dev/ram4" in script  # 0-3 are the OSTs
        assert "rd_nr=$want_nr" in script

    def test_size_is_converted_to_kib_for_brd(self) -> None:
        """brd's rd_size is KiB; OSTSIZE is KB for the test framework."""
        script = self._capture_script(count=2, size_gb=32)
        assert "want_kb=33554432" in script  # 32 GiB in KiB
        assert "OSTSIZE=33554432" in script

    def test_stale_signatures_are_wiped(self) -> None:
        """A ram device reloaded with a new geometry can still carry an
        ldiskfs superblock, which mkfs.lustre then refuses."""
        script = self._capture_script(count=2)
        assert "wipefs -a /dev/ram$i" in script

    def test_brd_only_reloaded_when_geometry_differs(self) -> None:
        """An unconditional rmmod would fail on a mounted filesystem for
        no reason."""
        script = self._capture_script(count=2)
        assert "have_nr" in script and "have_kb" in script
        assert "-lt" in script  # count compare
        assert "-ne" in script  # size compare

    def test_in_use_brd_gives_an_actionable_message(self) -> None:
        script = self._capture_script(count=2)
        assert "unmount Lustre first" in script

    def test_block_is_appended_not_replacing_the_virtio_one(self) -> None:
        """cfg/local.sh is sourced, so later wins.  Appending keeps both
        blocks visible, which matters when working out why an OST is not
        where it was expected."""
        script = self._capture_script(count=2)
        assert ">> " in script  # append
        # Only our own block is removed first, never ltvm's virtio block.
        assert "RAM OST configuration" in script
        assert "VM disk configuration" not in script

    def test_failure_is_raised_with_output(self) -> None:
        with patch(
            "ltvm_pkg.deploy.run_ssh",
            return_value=_fail(stderr="brd is in use; unmount Lustre first"),
        ):
            with pytest.raises(RuntimeError, match="unmount Lustre first"):
                deploy.configure_ram_osts("10.0.0.1", 4, 32)


class TestDeployRamOstWiring:
    """deploy_to_vm must apply ram OSTs after the virtio block."""

    def test_not_called_when_zero(self, staging: Path) -> None:
        vm = _make_vm(mdt_disks=1, ost_disks=2)
        with (
            patch("ltvm_pkg.deploy.subprocess.run", return_value=_ok()),
            patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()),
            patch("ltvm_pkg.deploy.configure_test_disks"),
            patch("ltvm_pkg.deploy.configure_ram_osts") as mock_ram,
        ):
            deploy.deploy_to_vm(vm, staging)
            mock_ram.assert_not_called()

    def test_forwarded_when_requested(self, staging: Path) -> None:
        vm = _make_vm(mdt_disks=1, ost_disks=0)
        with (
            patch("ltvm_pkg.deploy.subprocess.run", return_value=_ok()),
            patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()),
            patch("ltvm_pkg.deploy.configure_test_disks"),
            patch("ltvm_pkg.deploy.configure_ram_osts") as mock_ram,
        ):
            deploy.deploy_to_vm(
                vm, staging, ram_osts=8, ram_ost_size_gb=16, ram_mdt=True
            )
            mock_ram.assert_called_once_with(
                vm.ip, 8, 16, ram_mdt=True, os_family="rhel"
            )

    def test_ram_runs_after_virtio(self, staging: Path) -> None:
        """Order is load-bearing: the ram block must be appended after
        the virtio one or `source cfg/local.sh` takes the wrong devices."""
        calls: list[str] = []
        vm = _make_vm(mdt_disks=1, ost_disks=2)
        with (
            patch("ltvm_pkg.deploy.subprocess.run", return_value=_ok()),
            patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()),
            patch(
                "ltvm_pkg.deploy.configure_test_disks",
                side_effect=lambda *a, **k: calls.append("virtio"),
            ),
            patch(
                "ltvm_pkg.deploy.configure_ram_osts",
                side_effect=lambda *a, **k: calls.append("ram"),
            ),
        ):
            deploy.deploy_to_vm(vm, staging, ram_osts=4)
        assert calls == ["virtio", "ram"], calls
