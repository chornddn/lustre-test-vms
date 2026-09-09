"""``ltvm deploy`` and ``ltvm llmount``.

``deploy`` takes a VM **or** a cluster name and ships an already-built
Lustre staging tree to it.  It never builds: target, kernel and arch are
derived from what the named thing actually runs, and every build option
lives on ``ltvm build lustre``.  A deploy that shelled out to the build
had to mirror each build flag by hand, and the one it missed
(``--configure``) silently reconfigured the tree and shipped a cluster
without ``ko2iblnd``.

cmd_llmount is a thin wrapper around vm_commands.cmd_llmount.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

from ltvm_pkg.cli.util import (
    EXIT_ERROR,
    EXIT_OK,
    _error,
    _output,
)
from ltvm_pkg.lustre_build import StagingStatus, read_staging_meta


def _cli_attr(name: str) -> Any:
    """Look up ``name`` on ``ltvm_pkg.cli`` at call time."""
    import ltvm_pkg.cli as _cli

    return getattr(_cli, name)


def _staging_error(status: StagingStatus, name: str, use_json: bool) -> int:
    """Refuse a deploy that has nothing to deploy, naming the build."""
    return _error(
        f"no usable Lustre staging for {name} "
        f"({status.target}/{status.arch}/{status.kernel}): {status.reason}\n"
        f"  looked in: {status.path}\n"
        f"  build it first:\n"
        f"    {status.build_command()}",
        use_json,
    )


def cmd_deploy(args: argparse.Namespace) -> int:
    """Dispatch ``ltvm deploy <vm|cluster>`` to the right deployer."""
    use_json = args.json
    name = args.name

    # Deploy derives the arch from the target, so honouring one here
    # would let a caller state something the nodes contradict -- exactly
    # the mismatch this command was reshaped to make unexpressible.  The
    # flag is hidden from --help and exists only to be refused by name.
    if getattr(args, "arch", None):
        return _error(
            "deploy takes no --arch; it uses the arch the target runs",
            use_json,
            hint="pass --arch to `ltvm build lustre` instead",
        )

    from ltvm_pkg.vm_state import (
        ClusterInfo,
        ClusterNotFound,
        VMInfo,
        VMNotFound,
    )

    cluster: ClusterInfo | None = None
    vm: VMInfo | None = None
    try:
        cluster = ClusterInfo.load(name)
    except (ClusterNotFound, RuntimeError):
        cluster = None
    try:
        vm = VMInfo.load(name)
    except VMNotFound:
        vm = None

    if cluster is not None and vm is not None:
        # Whether the cluster or the VM should win is not ltvm's call to
        # make silently; either answer deploys somewhere the caller did
        # not mean.
        return _error(
            f"'{name}' names both a cluster and a VM; rename one",
            use_json,
        )
    if cluster is None and vm is None:
        return _error(
            f"no cluster and no VM named '{name}'",
            use_json,
            hint="`ltvm cluster list` / `ltvm list` show what exists",
        )

    if cluster is not None:
        return _deploy_cluster(cluster.name, args, use_json)
    assert vm is not None
    return _deploy_vm(vm, args, use_json)


def _deploy_cluster(name: str, args: argparse.Namespace, use_json: bool) -> int:
    """Hand a cluster deploy to vm_cluster, which checks the claims."""
    from ltvm_pkg.cli.util import _qemu_ns
    from ltvm_pkg.vm_cluster import cmd_cluster_deploy

    tree = getattr(args, "lustre_tree", None) or "."
    try:
        cmd_cluster_deploy(
            _qemu_ns(
                name=name,
                lustre_tree=tree,
                cfg_dir=getattr(args, "cfg_dir", None),
                fstype=getattr(args, "fstype", None),
                net=getattr(args, "net", None),
                ip_family=getattr(args, "ip_family", None),
            )
        )
        return EXIT_OK
    except SystemExit as e:
        return int(e.code) if e.code is not None else EXIT_ERROR


def _deploy_vm(vm: Any, args: argparse.Namespace, use_json: bool) -> int:
    """Deploy staging to one standalone VM (or one cluster node)."""
    from ltvm_pkg import vm_claim
    from ltvm_pkg.lustre_build import staging_status
    from ltvm_pkg.vm_state import VMNotFound

    # --net and --ip-family configure a whole cluster's LNet: local.sh
    # names one MGS NID that every node has to agree on, so neither can
    # be set one node at a time.  Deploying the cluster is how you
    # change them.
    for flag, dest in (("--net", "net"), ("--ip-family", "ip_family")):
        if getattr(args, dest, None):
            return _error(
                f"deploy {flag} names a cluster's network; "
                f"'{vm.name}' is a single VM",
                use_json,
                hint=(
                    f"deploy the cluster instead: "
                    f"ltvm deploy <cluster> {flag} ..."
                ),
            )

    try:
        vm_claim.check(vm.name, "deploy to")
    except vm_claim.ClaimError as e:
        return _error(str(e), use_json)
    tree = getattr(args, "lustre_tree", None)
    try:
        vm_claim.auto_claim(
            vm.name, str(Path(tree).resolve()) if tree else None
        )
    except vm_claim.ClaimHeld as e:
        return _error(str(e), use_json)

    # Derive the build key from what the VM runs.  Being able to pass a
    # kernel or arch that contradicts the target is the bug class this
    # command exists to remove.
    target = vm.os_id
    if not target:
        return _error(
            f"VM '{vm.name}' records no OS target; recreate it", use_json
        )
    vm_arch = vm.arch
    vm_variant = getattr(vm, "variant", "base") or "base"
    TargetConfig = _cli_attr("TargetConfig")
    try:
        tc = TargetConfig(target, arch=vm_arch, variant=vm_variant)
    except ValueError as e:
        return _error(
            f"Unknown target '{target}' for VM '{vm.name}': {e}",
            use_json,
            hint="Check `ltvm status` for valid targets.",
        )
    os_family = tc.os_family
    kernel = tc.resolve_kernel(
        Path(vm.kernel).parent.name if vm.kernel else None
    )

    # Resolve the tree holding the staging:
    #   1. --lustre-tree PATH wins.
    #   2. Otherwise a bundled snapshot from `ltvm fetch`, if present.
    #   3. Otherwise cwd.
    build_arg = getattr(args, "lustre_tree", None)
    bundled_snapshot: Path | None = None
    if build_arg is not None:
        build_path = Path(build_arg).expanduser().resolve()
    else:
        packaged = tc.output_dir / "kernels" / kernel / "lustre-artifacts"
        # A bundled snapshot is identified by the .ltvm-snapshot.json
        # marker written by snapshot_lustre.  It already has DESTDIR
        # layout (usr/, lib/modules/), so it deploys without a build.
        if packaged.is_dir() and (packaged / ".ltvm-snapshot.json").exists():
            bundled_snapshot = packaged
            build_path = packaged
            if not use_json:
                print("  Using bundled Lustre (from ltvm fetch)")
        else:
            build_path = Path(".").resolve()

    if not build_path.is_dir():
        return _error(f"Lustre tree not found: {build_path}", use_json)

    if bundled_snapshot is None:
        missing = [
            n
            for n in ("configure.ac", "lustre", "lnet")
            if not (build_path / n).exists()
        ]
        if missing:
            return _error(
                f"--lustre-tree:'{build_path}' does not look like a Lustre "
                f"source tree (missing: {', '.join(missing)})",
                use_json,
            )

    userspace_only = getattr(args, "userspace_only", False)

    # Which backend the VM runs is a deploy-time choice (it is written
    # into cfg/local.sh); whether ZFS is *available* is the build's, so
    # --zfs lives on `build lustre` and deploy only checks the staging.
    fstype = getattr(args, "fstype", None)
    if fstype == "zfs" and userspace_only:
        return _error(
            "--fstype zfs and --userspace-only are incompatible: a "
            "userspace-only deploy ships no kernel modules",
            use_json,
        )

    status = staging_status(
        build_path,
        target,
        arch=vm_arch,
        kernel=kernel,
        variant=vm_variant,
        build_tree=tc.kernel_output_dir(kernel=kernel) / "build-tree",
    )
    staging = status.path

    if bundled_snapshot is not None:
        # rsync --delete, unconditionally: the snapshot is the declared
        # source of truth here, and skipping the mirror when staging
        # already held .ko files used to ship whatever was last built
        # locally under the "Using bundled Lustre" banner.
        if not use_json:
            print(f"  Mirroring bundled snapshot into staging: {staging}")
        staging.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [
                "rsync",
                "-a",
                "--delete",
                str(bundled_snapshot) + "/",
                str(staging) + "/",
            ],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            return _error(
                f"Failed to mirror bundled snapshot: {r.stderr.strip()}",
                use_json,
            )
    elif userspace_only:
        # Userspace-only installs tools and tests, never modules, so a
        # staging dir without .ko files is fine here.
        if not staging.is_dir():
            return _staging_error(status, vm.name, use_json)
        if not use_json:
            print("  Userspace-only deploy (skipping kernel modules)")
    elif not status.usable:
        return _staging_error(status, vm.name, use_json)

    # Which ZFS to ship is the staged build's decision, not the
    # command line's: osd_zfs.ko is linked against one specific ZFS
    # build, so shipping any other would produce a module that won't
    # load.  build_lustre records the version it used.
    zfs_staging: Path | None = None
    staged_meta = read_staging_meta(staging)
    staged_zfs_version = (
        staged_meta.get("zfs_version")
        if isinstance(staged_meta, dict)
        else None
    )
    if staged_zfs_version and not userspace_only:
        from ltvm_pkg.zfs_build import zfs_staging_dir

        zfs_staging = zfs_staging_dir(tc, kernel, staged_zfs_version)
        if not any((zfs_staging / "lib" / "modules").rglob("zfs.ko*")):
            return _error(
                f"Lustre staging was built against ZFS "
                f"{staged_zfs_version}, but its build artifact is missing "
                f"from {zfs_staging}",
                use_json,
                hint=f"Run: ltvm build zfs {target} --kernel "
                f"{kernel} --zfs-version {staged_zfs_version}",
            )
        if not use_json:
            print(f"  Shipping ZFS {staged_zfs_version}")
    elif fstype == "zfs" and not userspace_only:
        return _error(
            "--fstype zfs, but the Lustre staging being deployed was not "
            "built with ZFS",
            use_json,
            hint=f"{status.build_command()} --zfs",
        )

    try:
        _cli_attr("deploy_to_vm")(
            vm,
            staging,
            os_family=os_family,
            userspace_only=userspace_only,
            ram_osts=getattr(args, "ram_osts", 0) or 0,
            ram_ost_size_gb=getattr(args, "ram_ost_size", 32),
            ram_mdt=getattr(args, "ram_mdt", False),
            zfs_staging=zfs_staging,
            fstype=fstype,
        )
    except RuntimeError as e:
        return _error(str(e), use_json)

    cfg_dir = getattr(args, "cfg_dir", None)
    if cfg_dir:
        rc = _distribute_cfg(vm, Path(cfg_dir), os_family, use_json)
        if rc != EXIT_OK:
            return rc

    # Record the kver just deployed, not vm.kver (the kernel running when
    # the VM booted): after a deploy the installed kernel may differ from
    # the running one.  Source of truth is the staging meta.
    import time as _time

    staging_meta = read_staging_meta(staging)
    kver = (
        staging_meta.get("kernel_version")
        if isinstance(staging_meta, dict)
        else None
    ) or vm.kver
    try:
        vm.update_deploy(int(_time.time()), str(build_path), kver)
    except PermissionError:
        # sockets/ is root-owned and sudo would need a password.  The
        # modules are already on the VM; only LAST_DEPLOY/BUILD_PATH/
        # KVER in the .info go unrecorded.  deploy must never prompt --
        # an unattended deploy+test loop would hang on it.
        if not use_json:
            print(
                "  Warning: deploy metadata not recorded (sockets dir "
                "is root-owned and sudo needs a password); the deploy "
                "itself succeeded.  Run `sudo -v` first to have it "
                "recorded.",
                file=sys.stderr,
            )
    except VMNotFound:
        # A concurrent `ltvm destroy` racing the final .info write must
        # not turn a successful deploy into a traceback.
        if not use_json:
            print(
                f"  Warning: VM '{vm.name}' was destroyed mid-deploy; "
                f"metadata not recorded",
                file=sys.stderr,
            )

    if not use_json:
        print(f"  Deployed Lustre to {vm.name}")
        print(f"  mount it with: ltvm llmount {vm.name}")
    else:
        _output(
            {
                "action": "deploy",
                "vm": vm.name,
                "target": target,
                "kernel": kernel,
                "kernel_version": kver,
                "build_path": str(build_path),
                "staging": str(staging),
                "os_family": os_family,
                "zfs": staged_zfs_version,
                "fstype": fstype,
            },
            use_json,
        )
    return EXIT_OK


def _distribute_cfg(
    vm: Any, cfg_dir: Path, os_family: str, use_json: bool
) -> int:
    """Copy every ``*.sh`` auster profile in *cfg_dir* onto one VM."""
    from ltvm_pkg.vm_cluster import _load_cfg_profiles, _write_cluster_cfg
    from ltvm_pkg.vm_net import SSH_OPTS

    try:
        profiles = _load_cfg_profiles(cfg_dir.expanduser().resolve())
    except SystemExit as e:
        return int(e.code) if e.code is not None else EXIT_ERROR
    for cfg_name, content in profiles:
        _, rc, out = _write_cluster_cfg(
            vm.name, vm.ip, cfg_name, content, SSH_OPTS, os_family
        )
        if rc != 0:
            return _error(
                f"{cfg_name}.sh distribution failed for {vm.name}: {out}",
                use_json,
            )
        if not use_json:
            print(f"  {cfg_name}.sh written to {vm.name}")
    return EXIT_OK


def cmd_llmount(args: argparse.Namespace) -> int:
    from ltvm_pkg.vm_commands import cmd_llmount as _qllmount

    try:
        _qllmount(args)
        return EXIT_OK
    except SystemExit as e:
        return int(e.code) if e.code is not None else EXIT_ERROR
