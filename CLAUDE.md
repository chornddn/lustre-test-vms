# lustre-test-vms-v2 -- Agent and Developer Reference

Build infrastructure for Lustre development/testing using
QEMU microVMs. Produces four cacheable artifacts per
target OS: build container, kernel, VM base image, and
Lustre staging (userland + modules per kernel), plus an
optional fifth -- ZFS -- when a build asks for it.

## LLM: Start Here

To build, deploy, mount or test, go straight to
[One Way to Build, Deploy, Mount and Test](#one-way-to-build-deploy-mount-and-test).
Those four verbs are the whole day-to-day flow, and the
sections above them are background.  Do not assemble a
flow from other sections: a build without `--for-cluster`
targets the wrong kernel, and that only surfaces at insmod.

If the user has just opened this repo, walk them through
installation proactively:

```bash
ltvm doctor                    # already installed?
sudo ./ltvm install            # if not: QEMU + bridge + dnsmasq + SSH
ltvm target fetch rocky9       # pre-built artifacts (fastest)
# or: ltvm build all rocky9 --lustre-tree ~/lustre-release
```

On macOS run `./ltvm install` *without* sudo: it refuses to run
as root there (Homebrew will not) and elevates only the steps
that need it -- see "Running on macOS" in README.md.

Ask: **"Where is your Lustre source checkout?"**  The usage
guidance an agent needs is the `ltvm` skill, which `ltvm
install` links into their skill directories.  If their
workspace instructions need the flow too, link to the
canonical section above rather than copying it -- a copy
drifts, and the reader cannot tell which version they have.

## Versioning and git hooks

`pyproject.toml` carries the full version; `BASE_VERSION` in
`ltvm_pkg/__init__.py` carries its major.minor, and `ltvm --version`
reports that plus the short commit hash.

The tracked hooks in `.githooks/` keep both honest, and are enabled per
clone:

```bash
make hooks        # git config core.hooksPath .githooks
```

`pre-commit` runs ruff and mypy, then `.githooks/bump-version` bumps the
patch version when the staged commit touches `ltvm`, `ltvm_pkg/` or
`targets/` -- docs-, test- and hook-only commits do not move it, and a
version edited by hand in the same commit is left alone. It refuses the
commit when `BASE_VERSION` and `pyproject.toml` disagree, and carries
the new version into `uv.lock` so the commit does not end dirty. `post-commit`
bakes the new hash into `ltvm_pkg/_build_info.py`.

## Agent Skills

`skills/ltvm/` is the skill that teaches an agent to use this
tool: VM and cluster lifecycle, deploying a Lustre tree, the root rules,
crash collection. `ltvm install` links it into `~/.claude/skills` (and
`~/.codex/skills` when Codex is installed) for the invoking user -- under
sudo that is `$SUDO_USER`, not root. `ltvm skills` does only the linking
and `ltvm skills --uninstall` removes it. `ltvm doctor` reports links
that are missing and makes them with `--fix`, which is what catches a
host installed before the skills existed. A skill directory that is not
a symlink is someone else's: doctor names it and `--fix` leaves it
alone.

Links, not copies: `git pull` or `ltvm update` updates the skill with the
ltvm it describes. It covers *using* ltvm; target configuration, artifact
internals and release mechanics stay in this file.

## Tab Completion

Two modules, split along the line between *what* to offer and
*how a shell gets asked*:

- `ltvm_pkg/completion.py` -- the argcomplete completers.  Every one is
  wrapped in `_safe`, because an exception here lands as a traceback in
  the user's prompt.  They read live state (targets.yaml, VM and
  cluster files, snapshot tags via `qemu-img snapshot -l -U`), so they
  stay right without a word list to maintain.
- `ltvm_pkg/shell_completion.py` -- generating and installing the
  per-shell registration, via `argcomplete.shellcode()` in-process (not
  the `register-python-argcomplete` script, which lives in the venv's
  `bin/` that `sudo ltvm install` has no PATH for).

`ltvm install` installs for every shell on the host; `ltvm completion`
prints or installs it by hand, and `ltvm doctor` reports missing or
stale files and writes them with `--fix`.  Writes go through
`priv.atomic_write`, so only the write elevates -- the command itself
does not need root.

`ltvm install --verify` reports it too (`host_setup.verify`), but
deliberately *not* as part of `all_ok`: a file gone stale across a
version bump is normal and self-heals on the next install, so failing
that command's exit code over it would cry wolf.  `ltvm doctor` is the
one that exits non-zero and fixes it.

Wiring lives in `ltvm`: `_COMPLETERS_BY_OPTION` is attached across the
whole subparser tree by `_attach_completers(p)` at the end of
`build_parser`, so an option that means the same thing everywhere
(`--arch`, `--lustre-tree`) is covered once and a new subcommand is
covered for free.  It only fills arguments with no completer, so a
per-command assignment always wins.

Three traps worth knowing:

- **`create --kernel` is a version name**, despite reading like a
  path: `_resolve_os_and_kernel` hands it to `resolve_os_artifacts`,
  which matches it against artifact *directory* names, so a real path
  gets "No kernel matching".  It is in the by-option table with every
  other `--kernel` for that reason.  `create --image` genuinely is a
  path and stays out, where argcomplete's default `FilesCompleter` is
  the right answer -- as it is for `--ssh-key`, `--tarball`, `--output`.
- **zsh needs the `#compdef` wrapper.**  An autoloaded `_ltvm`'s body
  *is* the completion function, so the bare shellcode would define
  `_python_argcomplete` and return -- first TAB empty, second one works.
  `shellcode("zsh")` adds the header and a trailing call.
- **`completion` is excluded from telemetry and the update check** in
  `main()`.  The documented usage is `eval "$(ltvm completion)"` from a
  shell rc file, so it runs once per terminal opened.

`LTVM_COMPLETION_ROOT` prefixes every system path -- for staging into an
image, and for the test suite, which sets it in `tests/conftest.py` so
`ltvm doctor --fix` under pytest cannot rewrite the developer's real
`/etc/bash_completion.d`.

macOS takes its directories from the Homebrew prefix instead
(`_SEARCH_MACOS`): `/usr/share` there is on the SIP-sealed system
volume, which not even root can write.  The prefix is the user's, so
nothing elevates, and it is found without running `brew`.

## Repository Layout

- `targets/` -- `targets.yaml` (source of truth), shared
  `common/` files (kernel fragment, package lists, setup
  scripts), and per-target dirs with `container.Dockerfile` +
  `image.Dockerfile` + `packages-os.txt`.  Per-target
  `variants/` dirs hold optional overlay Dockerfiles.
- `ltvm_pkg/` -- Python package; `cli/` subpackage holds
  per-area dispatch (`build.py`, `clean.py`, `cluster.py`,
  `deploy.py`, `fetch.py`, `make.py`, `setup.py`,
  `targets.py`, `telemetry.py`, `vm.py`) plus shared
  `util.py`; rest is implementation.  `ltvm` script at repo
  root is the CLI.
- `artifacts/<target>/<arch>/{container,kernels/<kver>,images/<kver>[/<variant>]}/`
  -- gitignored build artifacts with a `meta.json` each.
  ZFS, when built, lands at `kernels/<kver>/zfs/<version>/`.
- `docs/` -- operator notes (getting started, releasing
  prebuilt QEMU, nested virtualization, SoftRoCE setup,
  system test plan, VM ownership).

## Quick Start

```bash
sudo ./ltvm install                # macOS: no sudo (see above)
ltvm target fetch rocky9
ltvm build status
```

## Artifacts

Four cacheable artifacts per (target, arch, variant):
**build container**, **kernel**, **VM base image**, and
**Lustre staging** (userland + modules per kernel,
written into the Lustre tree's `.ltvm-staging/`).  The
first three each track an `input_hash` in their
`meta.json`; `ltvm build status` reports staleness, and
`--why` names the input that moved.
Images are keyed per-kernel (so multiple kernel minors
can coexist).

### input_hash is load-bearing -- do not perturb it

`input_hash` *is* the staleness decision, so producing a
different value for unchanged inputs invalidates every
artifact on every machine at once, costing a kernel rebuild
per target.  Nothing warns you; builds just start running.

`TargetConfig._hash_parts` feeds the bytes through
`_HashParts`, which records them under labels while
concatenating them unchanged -- so `input_hash()` (the
digest) and `input_components()` (the per-input digests
`--why` diffs, also stored in `meta.json`) come from one
code path.  Keep it that way: an explanation that disagrees
with the rebuild decision is worse than none.

[tests/test_input_hash_stability.py](tests/test_input_hash_stability.py)
pins the digest for every target and artifact.  If it fails,
assume you changed the hash by accident.  When the change is
deliberate, update the goldens in the same commit -- that
test failing is the one signal anybody gets before the
rebuilds start.

Three cases `--why` answers honestly rather than
plausibly, all worth preserving: an artifact built before
the per-input digests existed reports the cause as unknown
(not "nothing changed"); the kernel's `lustre-tree-inputs`
component is never blamed, because `build status` has no
Lustre tree to recompute it from; and a hash that moved with
no component accounting for it says exactly that.

```bash
ltvm build kernel rocky9 --lustre-tree ~/lustre-release
ltvm build image rocky9 --kernel 5.14-rhel9.5         # default kernel if omitted
ltvm build all rocky9 --lustre-tree ~/lustre-release  # stale only; --force for all
ltvm build mofed-kmods rocky9 --variant mofed-24
```

`ltvm clean` previews superseded kernel builds, off-list
kernel groups (dropped from `kernels.available`), and orphan
images.  Dry-run by default; `--apply` deletes, `--keep N`
and `--older-than DAYS` scope it.  The target's default
kernel and any variant-pinned kernel survive unless
`--force`.  Distinct from `ltvm target clean`, which wipes a
target's whole arch dir in one shot.

**The kernel** is built from the Lustre tree's
`lustre/kernel_patches/` slice for the target -- `.target`
(SRPM version), `kernel_configs/*.config`, and
`series/*.series` + `patches/` -- merged with
[targets/common/kernel-config.fragment](targets/common/kernel-config.fragment)
and the target's `kernels.config`.  Its SRPMs cache under
`artifacts/<target>/<arch>/cache/`, with a Rocky-vault
fallback for older minors.  **The image** is built
as a container via podman, exported to ext4 with `mke2fs -d`
under fakeroot, and carries the package lists, source-built
tools (IOR, mdtest, iozone, pjdfstest, FlameGraph, drgn,
Lustre-patched e2fsprogs), passwordless root SSH, serial
autologin and kdump.  No kernel inside it -- QEMU passes
that via `-kernel`.

`ltvm clean` walks `artifacts/` and previews superseded
kernel builds, off-list kernel groups (no longer in
`kernels.available`), and orphan images (no matching
kernel).  Default is dry-run; pass `--apply` to delete.

```bash
ltvm clean                      # dry-run, all targets/arches
ltvm clean rocky9               # one target
ltvm clean --older-than 30 --apply
ltvm clean --keep 2 --apply     # keep 2 most recent per group
```

Always preserved (unless `--force`): the target's default
kernel and any variant-pinned kernel.  Distinct from
`ltvm target clean`, which wipes a single target's whole
arch dir in one shot.

### Kernel build inputs (from Lustre tree)

`ltvm build kernel` parses the Lustre tree for the
target's slice: `lustre/kernel_patches/`
- `targets/<lustre_target>.target` -- SRPM version
- `kernel_configs/kernel-<ver>-<target>-<arch>.config`
- `series/<target>.series` + `patches/` -- patch series

then merges [targets/common/kernel-config.fragment](targets/common/kernel-config.fragment)
(plus the per-target `kernels.config` from `targets.yaml`)
and builds vmlinux/vmlinuz/modules/build-tree inside the
build container.  SRPMs cache under `artifacts/<target>/<arch>/cache/`
with a Rocky-vault fallback for older minors.

### Image

Built as a container via podman, exported to ext4 with
`mke2fs -d` under fakeroot (rootless).  Installs
`packages-{base,server,test,debug}.txt` +
`packages-os.txt`, source-built tools (IOR, mdtest,
iozone, pjdfstest, FlameGraph, drgn, Lustre-patched
e2fsprogs), passwordless root SSH, serial console
autologin, kdump pre-configured (vmlinuz + initramfs
baked in at build time).  No kernel inside the image --
QEMU passes it via `-kernel`.

## Exporting Images

`ltvm target export` repackages a built `base.ext4` + its
kernel + GRUB2 into one self-contained bootable disk -- for
people (or clouds) that don't have the ltvm runtime.

The disk is GPT and boots under both BIOS and UEFI: a BIOS
boot partition holding GRUB's core.img, an EFI system
partition holding a `BOOTX64.EFI` at the removable-media
path (so no NVRAM entry is needed), then root, last so it
can grow.  Both loaders read the same `grub.cfg`.  UEFI is
not optional on GCE's H4D: it falls back to legacy BIOS, and
that BIOS cannot read its NVMe boot disk.  The UEFI loader is
built with `grub-mkimage`, not `grub-install`, whose EFI mode
is distro-patched both ways (Ubuntu's switches to a Secure
Boot shim layout when `grub-efi-amd64-signed` is installed;
RHEL's refuses without `--force`).  Host needs `dosfstools`
and GRUB's x86_64-efi modules (`grub-efi-amd64-bin` /
`grub2-efi-x64-modules`); `ltvm install` and `ltvm doctor`
cover both.  Export needs `losetup` and `mount`, so it is
Linux-only: on macOS doctor prints a note instead of counting
the missing tools as issues.

```bash
ltvm target export rocky9                      # bootable qcow2
ltvm target export rocky9 --format raw
ltvm target export rocky9 --format gce \
    --disk-size-gb 20 --ssh-key ~/.ssh/id_ed25519.pub
```

`--format gce` writes the `disk.raw` tar.gz (oldgnu format,
whole-GiB disk) that Google Compute Engine's custom-image
import requires, and fixes up the guest for it: an explicit
NetworkManager DHCP profile for eth0, because ltvm's own
images take their address from the `fc_ip=` kernel cmdline
that GCE never passes.  The fstab `/` entry is rewritten to
`UUID=` for *every* format -- the image ships `/dev/vda`,
which is right only for ltvm's unpartitioned microvm boot.

The printed `gcloud compute images create` line carries
`--guest-os-features=UEFI_COMPATIBLE,GVNIC` (GVNIC only when
the image's kernel has `gve`).  Leave `UEFI_COMPATIBLE` off
and GCE boots the image through legacy BIOS; GVNIC is the NIC
the newer families (C4D, H4D) use.

Every format also gets `ltvm-growroot.service`, which grows
the root partition and its ext4 at boot to fill the disk.
The export sizes the partition to the image, and the disk it
boots from -- a GCE boot disk sized at instance creation, a
`qemu-img resize`d qcow2 -- is usually bigger.  It needs
`sfdisk`, which Debian and Ubuntu ship in the separate
`fdisk` package; the export warns when the image lacks it.

A base image ships no Google guest agent, so GCE cannot
inject SSH keys: export rocky10's `gce` variant
(`--variant gce`), which carries it, or pass `--ssh-key` to
bake a key in.  `--format gce` turns off password SSH and
locks root, but the image is otherwise ltvm's lab build --
don't open port 22 to the world.

## Lustre/Kernel Compatibility Gate

`ltvm` checks Lustre tree compatibility with the target's
`lustre.mode` (`server_ldiskfs` / `server_zfs` / `client`)
before any Lustre-involving build.

```bash
ltvm target validate rocky9 --lustre-tree ~/lustre-release
# Exit: 0 compatible (or warning), 1 refused, 2 could not tell

# Bypass a refusal (not hard errors):
ltvm build all rocky9 --lustre-tree ~/lustre-release --force-compat
```

## ZFS

ZFS is a per-build option, not a property of a target.  It
is entirely out-of-tree, so it sits between the kernel and
Lustre: it *reads* the kernel build-tree and changes
nothing about it, and it is installed into a VM at deploy
time rather than baked into the image.  Nothing about a
target's container, kernel or image artifacts depends on
it -- which is what lets it be a flag.

```bash
ltvm build zfs rocky9                                  # standalone
ltvm build lustre rocky9 --lustre-tree ~/lustre-release --zfs
ltvm deploy-lustre co1-zfs --lustre-tree ~/lustre-release --zfs --mount
ltvm cluster deploy co2 --build ~/lustre-release --zfs --mount
```

`--zfs` on a **build** means "build the ZFS OSD".  The
result has *both* backends: `--enable-server --with-zfs`
produces osd-ldiskfs and osd-zfs from one build, and
`FSTYPE` picks between them at test time.

`--zfs` on a **deploy** additionally means "run this VM on
ZFS", so it implies `--fstype zfs`.  Pass `--fstype
ldiskfs` alongside it to stage ZFS on a VM you want to
keep running ldiskfs.  `--fstype` writes the setting into
the VM's `cfg/local.sh`, which is what `llmount.sh`,
`auster` and a bare `sanity.sh` all read.

For ZFS the test framework takes `MDSDEV*`/`OSTDEV*` as
the **vdevs** to build pools on and derives the dataset
names itself (`$FSNAME-mdt1/mdt1`), so the `/dev/vd*`
mapping deploy already writes is what ZFS wants too --
nothing else in cfg/local.sh changes.

### Version

`--zfs-version VER` (implies `--zfs`) overrides the
target's `zfs.version` in targets.yaml, which in turn
overrides `DEFAULT_ZFS_VERSION` in
[ltvm_pkg/zfs_build.py](ltvm_pkg/zfs_build.py).  Release
tarballs come from openzfs/zfs and cache globally under
`artifacts/cache/zfs/`.

rocky8 pins 2.3.4 rather than 2.4.0: 2.4 dropped support
for the 4.18 EL8 kernel.

### Artifact

`kernels/<kver>/zfs/<version>/` holds `src/` (configured
and built in place, for Lustre's `--with-zfs`) and
`staging/` (a `make install DESTDIR=` tree, for
deploy-lustre to stream into a VM).  It is keyed on the
kernel's release string **and** the kernel artifact's
`input_hash`, because zfs.ko links against Module.symvers
-- which moves when a kernel patch changes without
kernel.release changing.

The ZFS a VM receives is never chosen by the command line:
`build lustre` records the version in the staging meta and
deploy ships exactly that one, since osd_zfs.ko is linked
against one specific ZFS build.

Both rhel and debian build containers work; the inner
script picks dnf or apt for its extra build deps and takes
the library directory from `rpm --eval %{_libdir}` or the
Debian multiarch triplet, which is where the VM's ldconfig
will look for libzfs.  Any other os_family is refused up
front rather than inside the container.

`ltvm build zfs` works on a **client** target too -- ZFS is
a filesystem, not a server component.  What a client target
cannot do is `--with-zfs`, which needs `--enable-server` to
have an OSD to build; nothing in ltvm consumes a ZFS
artifact built for a client target, so it is only useful if
you are going to install it yourself.

Built per (target, arch, kernel, version):

| target | kernel | ZFS |
|---|---|---|
| rocky8 | 4.18 | 2.3.4 |
| rocky9 | 5.14 | 2.4.0, 2.3.4 |
| rocky10 | 6.12 | 2.4.0 |
| ubuntu2404 | 6.8 | 2.4.0 |

### Publishing

`ltvm target publish` emits a `zfs` asset only when the
Lustre being published was itself built with ZFS.  It
carries `staging/` and not `src/`: staging is what a
fetcher installs into a VM (~48 MB compressed), while
`src/` is only needed to *build* Lustre `--with-zfs`, adds
~180 MB, and `ltvm build zfs` reproduces it in well under
two minutes.  A fetched ZFS therefore reads as stale to
`ltvm build zfs`, which is correct -- it has no source to
configure against.

The kernel asset excludes `zfs/` for the same reason it
excludes `mofed-kmods/`: a fetcher who never passes
`--zfs` should not pay for it.

## VM Management

Lifecycle and inspection only.  Building, deploying,
mounting and testing are the four verbs in
[One Way to Build, Deploy, Mount and Test](#one-way-to-build-deploy-mount-and-test).

```bash
ltvm create co1-single --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3
ltvm create co1-single --root-size 20G   # OS disk (default 8G)
ltvm create co1-single rocky9 --dry-run  # resolve + validate, write nothing
ssh co1-single 'lctl dl'
ltvm vm console-log co1-single
ltvm vm console-log co1-single -f     # keep streaming (tail -F semantics)
ltvm vm nmi co1-single                # inject NMI -> kdump
ltvm vm snapshot co1-single [tag]     # snapshot the overlay disk
ltvm vm snapshot co1-single --delete tag
ltvm vm restore co1-single [tag]      # restore (no tag: list them)
ltvm vm set co1-single --vcpus 2 --mem 4096   # resize a stopped VM
ltvm vm crash-collect co1-single --mod-dir $CO/1
ltvm destroy co1-single
```

The VM commands answer under `vm` too: `ltvm vm create` is `ltvm
create`, and likewise for `destroy`, `start`, `stop`, `list`,
`deploy-lustre`, `llmount`, `llumount` and `doctor`.  One parser
registered under both groups, so there is nothing to keep in sync.

**Owner/session metadata:** New VMs persist an advisory opaque `owner_id`.
Agent controllers should export `LTVM_OWNER_ID=<durable-session-id>` before
running normal create commands.  (If you do run create under sudo
anyway, use `sudo -E`: plain sudo drops the variable and the VM silently
takes the `pid:` fallback.) `--owner ID` / `--owner-id ID` override the
environment; otherwise LTVM uses `pid:<invoking-ltvm-pid>`. Cluster create
resolves once and applies the same owner to every member. Discover it through
`ltvm list --json`; legacy VMs report `owner_id: null`. See
[docs/VM_OWNERSHIP.md](docs/VM_OWNERSHIP.md).

**Claims:** `ltvm_pkg/vm_claim.py` records which session is using a VM
(`ltvm claim`/`release`, files in `VM_DIR/claims/`, mode 1777, rewritten
in place under flock).  `vm_claim.require()`/`check()` gate deploy,
llmount, the lifecycle commands (in `_require_manageable`, and in the CLI
wrappers ahead of `_vm_privileges` so a refusal never prompts for sudo),
the `vm` actions and the cluster commands; `auto_claim()` claims for a
session owner on deploy.  Tests: `tests/conftest.py` points `CLAIMS_DIR`
at a tmpdir and drops the session variables, since the suite often runs
under an agent.  Contract: docs/VM_OWNERSHIP.md#claims.

**Disks:** `--disk-size` sizes the MDT/OST scratch disks; `--root-size`
sizes the VM's own OS disk (default 8G, floor 1G, and never smaller than
the base image it overlays).  The guest's rc.local grows the root
filesystem into whatever the overlay is, so the flag is the whole
mechanism.  Both are fixed at create time -- `ltvm create` against an
existing VM warns that a differing value was ignored.  `cluster create`
takes `--root-size` too and applies it to every node.

**Resizing:** vCPUs and memory are not fixed: `ltvm vm set <name>
--vcpus N --mem MB` rewrites them in the stopped VM's `.info`, which
`launch_qemu` reads on every start, so the VM keeps its disks and
installed state.  It refuses a running VM, whose QEMU would keep the
old size while `list` showed the new one.  The host memory check runs
at the next `start`, as for any VM.

**Host memory:** `create` and `start` admit a VM only when the running
VMs' `-m` plus its own fit MemTotal less a reserve
(`qemu_run._memory_shortfall`).  `[memory] overcommit` in
`/etc/ltvm.conf` (`site_config.path()`; `LTVM_SITE_CONFIG` moves it, and
the tests point it at a file that does not exist) multiplies that budget
for the sum, never for one VM.  It is 1.0 unless set, for hosts running
KSM; a value below 1.0 or not a number warns once per process and counts
as 1.0.

**Kernel arguments:** `--kernel-args 'slub_debug=FZPU page_owner=on'`
is stored in the `.info` file as `KERNEL_ARGS` and appended after
ltvm's own parameters on every boot, so a parameter whose last
occurrence wins (`crashkernel=`) takes the user's value.  `root=`,
`console=` and `fc_*` are refused: the VM boots and logs through
ltvm's values, and rc.local configures the guest from `fc_*`.  Fixed at
create time like the disks; `cluster create` applies it to every node.

**Naming:** always include the checkout number: `co<N>-<role>`.

**Root:** a host has one of two layouts, and
[ltvm_pkg/rootless.py](ltvm_pkg/rootless.py) says which applies to the
current user.

*Shared* (what `ltvm install` sets up now): `/opt/qemu-vms`, its
`overlays/`, `sockets/` and `hosts.d/` are `root:ltvm` 3775, QEMU's
bridge helper is setuid root (a `dpkg-statoverride` on apt hosts, so
package upgrades keep it) and `/etc/qemu/bridge.conf` allows `fcbr0`.
Members of the `ltvm` group then run the whole VM lifecycle, clusters
included, with no root: QEMU runs as the user with `-netdev bridge`,
disks and state files are created directly, and VM names go to
`hosts.d/`, which dnsmasq watches (`hostsdir=`), instead of
`/etc/hosts`.  `/etc/hosts` is still updated when that needs no password.
Root must not create files in a directory every group member can plant
symlinks in, so under `sudo` a `create` continues as `$SUDO_USER` and a
`start` as the VM's owner, whose QEMU it is; `stop` and `destroy` only
signal and unlink, and stay root.  Root code that does write there opens
files `O_NOFOLLOW` (`priv.chmod_regular`, `priv.ensure_lock_file`,
`host_setup._write_root_file`).  A VM with a
`passthrough` NIC still needs a root QEMU, so it takes the classic path
below; on a shared host that trusts the group with root.

*Shared* on macOS: `ltvm install` makes the same directories and the
`ltvm` group (`dseditgroup`), but there is no helper -- QEMU connects
to socket_vmnet's socket, which launchd creates `root:staff` 0770.
Homebrew's dnsmasq has no inotify for `hostsdir=`, so it reads
`hosts.d/` as `addn-hosts=`, and the `io.github.ltvm.dnsmasq-reload`
LaunchDaemon, with `WatchPaths` on the directory, runs `launchctl kill
SIGHUP` on the dnsmasq job when a file comes or goes.  It runs a fixed
command and reads nothing a user wrote.  macOS caps `setgroups()` at 16
groups, so dropping from root there goes through `initgroups()`.

*Classic* (no bridge helper, or the user is not in the group): only
`update`, `cluster create` and `cluster destroy` need the whole command
under root -- and not `cluster create --dry-run`, which only reads.
Single-VM lifecycle -- `create`, `start`, `stop`, `destroy`, `doctor` --
runs as the invoking user and elevates the individual operations that
need it.  QEMU itself is launched under sudo, because it writes its
pidfile and QMP socket into the root-owned `/opt/qemu-vms/sockets`
(0755); the log is created there as root once and handed to the user,
and the pidfile and QMP socket are handed over after launch.
Everything the user then touches is theirs: `.info` and `.log` 0644,
`.pid` and `.qmp` 0600.

`doctor` reports which layout applies and why the shared one is
unavailable.  Once sudo has refused, later elevations in the same
process use `sudo -n`.

`build *`, `target *`, `deploy`, `llmount`, `list`, `vm *` and
the remaining `cluster` actions need nothing.

Verified 2026-09-11 by running the whole lifecycle as a non-root user.

## One Way to Build, Deploy, Mount and Test

Four verbs, one job each.  `deploy` claims every node for
an agent session, and the other commands refuse a node
that another live session claimed (see
[docs/VM_OWNERSHIP.md](docs/VM_OWNERSHIP.md#claims)):

```bash
ltvm build lustre --for-cluster co1 --lustre-tree ~/lustre-release
ltvm deploy co1 --lustre-tree ~/lustre-release --net o2ib
ltvm cluster llmount co1
ltvm test co1 sanity-lnet --except 50,109
ltvm cluster llumount co1
ltvm release co1-mds co1-oss co1-client
```

Nothing here creates or destroys a cluster.  Ask the
operator for one; both need root, and destroying somebody's
cluster to work around dirty state loses their logs.

**Build options exist only on `build lustre`.**  `--configure`,
`--kernel`, `--arch` and `--force-compat` have exactly one
home.  A build flag on `deploy` would mean deploy is building
again -- and a deploy that re-runs configure without the
`--configure` args it cannot forward once shipped a cluster
`lnet.ko` and `ksocklnd.ko` but no `ko2iblnd.ko`, silently.

**`deploy` takes a VM or a cluster** and asks it what it runs:
target, kernel, arch and variant come from the node metadata,
so a deploy cannot contradict the nodes.

**`deploy` never builds.**  Missing or stale staging is a hard
error naming the exact `build lustre --for-cluster` line to
run.  Staging counts as stale when the source tree is newer
than the `.ltvm-staging-stamp` written at the end of the build.

**`llmount` is the mount command.**  Neither `build` nor
`deploy` mounts anything.

**Ask `ltvm test` how a run is going; do not guess.**  A suite
runs for tens of minutes inside one blocking call, so the
answer needs a second command:

```bash
ltvm test co1 --status        # or --follow, which repeats until the run ends
```

`--status` reports state, elapsed time, the subtest running
now, the counts so far, and how many of the suite's subtests
have been recorded (`87/172`).  It reads the run record
`ltvm test` writes before it starts, so it works from
**another session**, and after the session that started the
run has died.

Do not reach for `pgrep`, a console tail, or a timer
instead.  All three have failed here: `pgrep -f auster`
matches the shell running the `pgrep`; a run backgrounded
with `&` over ssh dies with the session; and `cluster exec`
gives up after 120s on a run that is still perfectly alive.

**`llmount --cleanup` leaves the node with no Lustre
resident, or fails.**  `llmountcleanup.sh` on its own does
not get there: it stops the nodes named in the test config
but not a client the node mounted on itself, and that one
mount keeps `mdd` busy so `lustre_rmmod` fails.  It also
leaves the dm-flakey targets behind.  Cleanup therefore
escalates -- force-unmount, drop the dm targets, unload
again -- and exits non-zero naming what is still held.
Stale state is silent when it is created and reappears
later as `mkfs.lustre: Unable to build fs (256)`, or as a
whole suite that never runs.

**`deploy --net {tcp,o2ib}` picks the cluster's LNet net.**
A cluster runs **one** net at a time, because the Lustre
test suites all assume one.  `deploy` is the only command
that sets it; `test` has no `--net`.  Switching nets means
another deploy, which is cheap: `llmount.sh` reformats, so
a changed `MGSNID` needs no `writeconf`.  Omitting `--net`
keeps the net last deployed, or tcp for a cluster that
never had one.  `--net o2ib` on a cluster whose NICs cannot
carry it fails before any node is touched.  Both nets run
on the extra NICs and their `172.16.100.x` addresses; only
a cluster created with no `--nic` at all falls back to the
mgmt NIC (`eth0`).  `--ip-family {ipv4,ipv6}` picks which
of the extra NIC's two addresses the NIDs use, defaults to
`ipv4`, and is recorded like `--net`; see
[docs/IPV6.md](docs/IPV6.md), which also covers why a
filesystem does not yet mount over an IPv6 NID.
For a real-HCA
(`passthrough`) cluster the boot-time emitter owns the
config, so deploy refuses `--net o2ib` and refuses a bare
deploy that would overwrite its `lnet.conf`.

Deploy writes `cfg/local.sh` (`NETTYPE`, `MGSNID`) and
`/etc/modprobe.d/lnet.conf` together, from one resolved
net.  That is not a nicety: a node holding one file for
one net and the other for another mounts nothing, and
says `no connections available: rc = -22` while doing it
-- which reads as a Lustre fault.  `rc.local` composes an
`lnet.conf` only when none exists, so what deploy writes
survives reboot.

`tcp` runs on the mgmt NIC (`eth0`) and uses the mgmt
address; `o2ib` runs on the extra NICs and uses their
`172.16.100.x` addresses.  A real-HCA (`passthrough`)
cluster is left to the boot-time emitter: deploy refuses
`--net o2ib` for it, and refuses a bare deploy that would
overwrite its `lnet.conf`.

### Running ltvm inside a VM it built

`deploy` runs on the build host and pushes Lustre
*into* a VM over ssh.  The `make-*` commands are the other
direction: ltvm running **inside** a machine it produced --
an ltvm VM, or a cloud node booted from `ltvm target export
--format gce` -- installing Lustre onto that machine's own
root filesystem.

```bash
# on the node, from a Lustre checkout:
ltvm make-install --lustre-tree ~/lustre-release
ltvm make-uninstall
ltvm make-reinstall --lustre-tree ~/lustre-release
```

The build still happens in the target's build container
(the VM image ships the runtime packages, not the
toolchain), so the node needs podman plus a
`ltvm target fetch <target>` first.

On an EL8/EL9 image the distro `python3` (3.6 / 3.9) is
below ltvm's 3.10 floor, so run ltvm with the 3.11 the
image now ships:

```bash
dnf install -y podman            # not in the image
python3.11 /path/to/ltvm make-install --lustre-tree .
```

Images built before this was added need
`dnf install -y python3.11 python3.11-pyyaml` too.

**Two preconditions**, both enforced for all three
commands: you must be on a machine ltvm built, and inside
a Lustre source tree.  They unpack a tree onto `/`, and
the machine where you'd most likely type one by accident
is your build host.  `--force` overrides the first.

Being an ltvm machine is established by any of three, in
order:

1. `/etc/ltvm-image.json`, baked in by `ltvm build image`
   -- authoritative, and the only one that names the
   variant and kernel;
2. ltvm's kernel cmdline (`fc_ip=` / `fc_name=`), which
   `qemu_run` passes -- this is what identifies every VM
   from an image built before the stamp existed;
3. ltvm's setup scripts under `/usr/local/sbin` -- what
   identifies a cloud node, which gets no ltvm cmdline.

Target resolution prefers the stamp, then `--target`, then
an `/etc/os-release` guess -- and an ambiguous guess
(rocky9 vs rocky9-64k) is an error asking for `--target`,
not a coin flip.  Without a stamp the kernel is matched to
whatever is **running** rather than the target's default,
since modules built for the wrong release won't load.

`make-uninstall` works from the manifest at
`/var/lib/ltvm/lustre-install.json` that install writes, not
from `make uninstall` (the node has no configured source
tree), so it removes exactly what ltvm put there.  It
unloads Lustre modules first and refuses if they won't
unload; `--no-unload` and `--force` override.

Verified end-to-end by the three scripts under `tests/e2e/`
(deliberately not named `test_*.py`, and excluded from pytest
collection -- they write to `/`, so run them only in a
throwaway machine):
[local_install_rootfs.py](tests/e2e/local_install_rootfs.py)
(any Linux rootfs),
[make_install_vm.py](tests/e2e/make_install_vm.py) (a real
Lustre DESTDIR in a real ltvm VM: depmod, `modprobe
lustre`, then unloading it again), and
[export_pipeline_rootfs.py](tests/e2e/export_pipeline_rootfs.py)
(the real `target export` pipeline).

### Clusters

```bash
sudo ltvm cluster create co2 mgs+mds:co2-mds:1 oss:co2-oss:3
ltvm cluster status co2
ltvm cluster exec co2 oss 'lctl dl'
sudo ltvm cluster destroy co2
```

`cluster exec <role>` fans out across every node holding the role and
exits non-zero if any node did; `cluster ssh <role>` opens a session on
the first, since it execs a single interactive ssh.
`cluster status` reports what a build and a deploy have to
match -- target, arch, kernel and net -- so those facts come
from the cluster rather than from a note that goes stale.
When the nodes disagree on any of the three build fields the
line reads `-` and a warning on stderr names the value per
node.  That is worth surfacing: one staging tree serves at
most one kernel, and the other nodes fail at insmod, far
from the build that chose it.

**`cluster create --nic` defaults to `softroce`**, so a new
cluster can run either LNet net: a softroce NIC is an
ordinary virtio-net device with an rxe link on top, and
socklnd binds that same netdev.  `--nic tcp` still gives a
tcp-only cluster -- one `deploy --net o2ib` correctly
refuses.

Extra NICs share a network of their own (`172.16.100.0/24`
by default, `$LTVM_EXTRA_SUBNET` to change it), separate
from mgmt.  That network is dual-stack: every extra NIC
also gets a static ULA derived from its IPv4 address
(`fd17:2016:1000:f100::/64`, `$LTVM_EXTRA_SUBNET6`), while
mgmt stays IPv4 only -- see
[docs/IPV6.md](docs/IPV6.md).  Repeating `--nic` gives several rails on one
LNet net -- `--nic tcp --nic tcp` yields
`tcp0(eth1,eth2)` -- and `rc.local` routes each rail by
source address so a NI bound to one rail egresses on it.
For o2iblnd over SoftRoCE see
[docs/SOFTROCE_SETUP.md](docs/SOFTROCE_SETUP.md); it needs a
kernel with InfiniBand enabled, and `ko2iblnd.ko` is built
by default.

#### Distributing extra test-config profiles

Deploying to a cluster always adds the generated cluster
block to `<lustre libdir>/tests/cfg/local.sh` on every
node.  Pass `--cfg-dir DIR` to distribute additional
auster profiles alongside it:

```bash
ltvm deploy co2 --lustre-tree ~/lustre-release \
    --cfg-dir ~/lustre-dev/test-scripts/clusters/co2/cfg
cd /usr/lib64/lustre/tests && NAME=co2sn ./auster -r -v conf-sanity --only 57c
```

Every `*.sh` in `DIR` goes to `tests/cfg/<basename>` on
every node, after `local.sh`, so a profile that sources
`local.sh` finds it in place.  A failed write is fatal --
a partly-distributed config is the failure mode this area
already suffered from.  A profile named `local.sh` is
rejected: it would clobber the tree's `local.sh` and the
cluster block in it.  Profiles must source it, not
replace it.

**Profile content is not ltvm's.** ltvm distributes files;
`test-scripts` decides what is in them.  Profiles live in
`~/lustre-dev/test-scripts/clusters/<cluster>/cfg/` -- see
that repo's `clusters/README.md`.

Each action is a real subparser, so `ltvm cluster <action> --help`
works and every action's flags validate and tab-complete.  Two
consequences worth knowing:

- **`cluster exec` takes its command as a REMAINDER**, so everything
  after the role is passed through untouched (`lctl dl -t` keeps its
  `-t`).  The price is that ltvm's own flags must come *before* the
  role: `cluster exec co2 --timeout 30 oss uptime`, not after it.
- **An option between two node specs does not parse.**  `create`'s specs
  are one `nargs="+"` positional -- they have to be, or argparse would
  assign a bare positional TARGET the first spec -- and argparse matches
  positionals in contiguous runs, so an option in the middle ends the
  run and the rest come back as "unrecognized arguments".  Before or
  after the whole run both work.  The hand-rolled parser this replaced
  did not care, so that one form regressed.

A malformed cluster command line now exits 2 with a usage message rather
than ltvm's own error (a JSON envelope under `--json`), which is what
every other subcommand already did.

## Testing

```bash
ltvm test co2 sanity-lnet --except 50,109 --json
ltvm test co2 sanity-lnet --only 630,631
```

Runs auster on the cluster's client node and prints one
parsed result object instead of console output to scrape.
Results come from auster's own `results.yml` (written to
the `-D` log dir), never from stdout.

**`--only` / `--except` are the only supported spelling.**
`run_suites()` in `test-framework.sh` begins every suite
with `unset ONLY EXCEPT START_AT STOP_AT`, so the
environment form (`EXCEPT="50 109" ./auster ...`) is
silently discarded and the excluded tests run anyway.

`--json` emits:

```json
{"suite": "...", "cluster": "...", "cfg": "local",
 "duration": 966, "pass": ["test_630"],
 "fail": [{"test": "...", "reason": "..."}],
 "skip": [{"test": "...", "reason": "..."}],
 "benign": [{"test": "...", "reason": "...", "why": "..."}],
 "counts": {"pass": 152, "fail": 0, "skip": 19, "benign": 2},
 "recorded": 173, "total": 172, "coverage_note": "",
 "node": "co2-cli", "log_dir": "/tmp/ltvm-test/..."}
```

Exit is non-zero only for real failures: a non-empty
`fail` list, a preflight refusal, or an infrastructure
error.  Skips and benign failures exit 0.

**Counts are not coverage.**  `FAIL_ON_ERROR` defaults to
true in `cfg/local.sh`, so `test-framework.sh` exits the
whole suite at the first real failure.  The subtests after
it never run and are not in the counts, so a suite cut a
third of the way in still reports `0 FAIL`.  `recorded` and
`total` are the honest pair -- `total` counts the suite
script's own `run_test` lines -- and `coverage_note` spells
out the shortfall when there is one.  A benign failure halts
the suite exactly like any other, so a non-empty `benign`
list is also a reason to check `recorded` against `total`.

Known environment-caused failures live as data in
`BENIGN_FAILURES` in [ltvm_pkg/test_runner.py](ltvm_pkg/test_runner.py),
each with the reason it is environmental.  They report as
`benign`, never as `pass`.  Add one per run with
`--benign SUITE:TEST`, or see them as real failures with
`--no-benign`.

A preflight refuses to start auster when `cfg/<name>.sh`
is missing or differs between nodes, when the deployed
modules do not match the running kernel, when `lnet.conf`
names a network the cluster config does not use, or when
`MGSNID` names a different net than `NETTYPE`.  That last
one is the mismatch a net switch can leave behind, and it
surfaces at mount as a Lustre-looking fault.
`--skip-preflight` bypasses it.

## Target Configuration

Targets live in [targets/targets.yaml](targets/targets.yaml),
which is the source of truth for their keys -- read it
rather than a copy.  Four are not self-explanatory:
`os_family` selects the package-manager family (`rhel`),
`lustre.mode` is the compat-gate mode, `kernels.available`
lists what may be built while `kernels.default` picks one,
and `kernels.config` holds per-target kernel config
overrides.

| Key | Example | Description |
|---|---|---|
| os_family | rhel | Package manager family |
| os_name / os_version | rocky / 9.7 | Distro + version |
| container_image | rockylinux:9 | Build-container base |
| lustre.mode | server_ldiskfs | Compat gate mode |
| zfs.version | 2.4.0 | ZFS release `--zfs` builds (does not enable it) |
| kernels.default | 5.14-rhel9.7 | Default kernel |
| kernels.available | [5.14-rhel9.7, ...] | Buildable kernels |
| kernels.config | {CONFIG_XEN_PVH: y} | Per-target config overrides |
| variants | {mofed-24: {...}} | Optional add-on variants |

**Adding an OS:** add a `targets.yaml` entry, create
`targets/<name>/` with `container.Dockerfile`,
`image.Dockerfile` and `packages-os.txt` (plus
`package-map.txt` if non-RHEL), then `ltvm build all <name>
--lustre-tree <path>`.  For a new kernel minor on an
**existing** OS, just add the short name to
`kernels.available` -- no Dockerfile changes, as long as the
Lustre tree has the `.target` / `.series` / `.config` for it.

**Variants** are overlay Dockerfiles under
`targets/<name>/variants/` that layer on the base
container/image, pinned to a specific kernel.  rocky9's
`mofed-24` is the canonical example: an overlay
container/image pair plus a kernel pin and `params:`
consumed by the Dockerfile.

## Development

### Test suite

`uv run pytest` passes with no failures on two quite different
machines, and keeping both green is the standard:

- a **provisioned host** (`sudo ltvm install` has run): everything runs;
- a **bare checkout** with no `zstd`, `rsync`, `fakeroot`, `mke2fs` and
  no `ltvm` on PATH: the tests that genuinely drive those tools skip,
  naming what is missing, and nothing fails.

Two rules keep that true, and both came from tests that quietly wanted a
configured host:

- A **unit** test must not depend on a host tool.  When the code under
  test sits behind a presence preflight (`image_build._check_mke2fs`,
  `release_package._check_zstd`), neutralize the preflight -- otherwise
  the test fails on the preflight having never reached its subject, and
  what it reports is "fakeroot missing" rather than anything about the
  behaviour it covers.
- An **integration** test that really builds and unpacks assets needs
  the real binaries, since mocking tar and zstd would leave it asserting
  nothing about the tarballs it exists to check.  Those carry
  `@needs_host_tools` (tests/test_package.py) and skip with a reason
  naming the tool and the remedy.

Never let a test shell out to `ltvm` itself for real.  One did, and
passed only because the child failed; on a host where it would have
succeeded the suite was one check away from starting an actual Lustre
build (tests/test_deploy.py::test_legacy_staging_triggers_clear_error).

To assert that something is **absent** -- a hostname, a username, a path
-- substitute a sentinel and look for that, rather than searching for the
machine's real value.  The telemetry leak test did the latter and was
wrong in both directions: it failed on a host named `vm` because "vm" is
inside the key `ltvm_version`, and it would have failed on one named
`ubuntu` or `x86_64` by colliding with a legitimate value, while
narrowing the match enough to dodge that left it blind to a real leak on
the short-named host.  A sentinel collides with nothing, so the whole
payload can be searched and the answer is the same on every machine.
Better still where it fits: assert the output does not *change* when the
identity does (`test_payload_does_not_depend_on_the_machine_name`).

`tests/conftest.py` holds the autouse isolation that keeps the suite out
of the developer's real state: XDG config and state, `LTVM_TELEMETRY=0`,
and `LTVM_COMPLETION_ROOT`.  Add to it rather than patching per test
whenever a new code path writes outside the repo.

### Interactive container shell

```bash
ltvm build shell rocky9
```

### Cross-building Lustre

```bash
ltvm build shell rocky9                                # interactive container
ltvm build lustre rocky9 --lustre-tree ~/lustre-release
ltvm build lustre --for-cluster co2 --lustre-tree ~/lustre-release
```

Builds inside the target's build container against the
target's kernel build tree.  Output goes to the Lustre
tree's `.ltvm-staging/<target>/<arch>/<kernel>[/<variant>]/`.

**Building for a cluster:** `--for-cluster <name>` takes
target, kernel and arch from that cluster's nodes.  A
target's default kernel is often not the one a cluster was
created with, and a plain `build lustre <target>` then
produces modules the cluster cannot load -- a mismatch that
only surfaces at deploy or insmod time.  Explicit `--kernel`
/ `--arch` still win; a conflicting `--target` is an error.

Extra `--configure` args are part of the cached configure
state (stamp: `.ltvm-configure-<target>-<arch>`), so changing
them re-runs autogen+configure automatically.  `--force` is
not needed for that.

## Release Manifest Schema

Each release carries `"schema": "ltvm-release/<N>"` in its
`manifest-*.json`, and fetch refuses any version it does not
explicitly recognize.  Source of truth is `SCHEMA_VERSION`
in [ltvm_pkg/release_package.py](ltvm_pkg/release_package.py),
read by both the writer and the fetch-side check so they
cannot drift.

**Bump when** an older ltvm could not consume the new
release: asset renames, content/compression changes,
manifest shape or per-variant scoping changes,
extraction-path or module-injection changes.  **Do not bump
for** additive changes an old fetcher can ignore.  To bump:
edit `SCHEMA_VERSION`, add a one-line entry to the
bump-history comment above it, and republish every release
that should stay fetchable.

## Code Review Guidance

- **Subprocess command building.** Never interpolate into
  shell strings (`bash -c f"...{x}"`).  Use argument lists.
- **Root-required operations.** On a shared host nothing in the VM
  lifecycle needs root; a new host operation must keep that true or fall
  back to the classic path through `rootless.readiness()`.  On a classic
  host, single-VM lifecycle commands elevate the individual host
  operations that need it, so do not require users to invoke the whole
  command through sudo; cluster create/destroy still require root.
  Read/observe (console-log, deploy, llmount, crash-collect,
  cluster exec/status, list) don't.  Build commands don't.
- **Root in the shared VM directories.** Any root write there must not
  follow a symlink a group member planted.
- **`--force-compat`** silences compat *refusals* but not
  hard errors -- only for known WIP branches.
- **A build option on a non-build command.** Each option has
  exactly one home.  A `--configure` / `--kernel` / `--arch`
  appearing on `deploy` means deploy started building again.

## Issue Tracking

Two trackers, by scope.  **`bd` (beads)** is local,
session-scoped work -- bugs found mid-task, short-lived
TODOs -- and syncs via JSONL committed to git
(`.beads/issues.jsonl`).  **GitHub
Issues** on `lustre-tools/lustre-test-vms` is for
longer-term or external-visible work.  Rule of thumb: under
a week is a bead, a month-plus is a GH issue; migrate beads
that age out.

```bash
bd ready / bd show <id> / bd update <id> --claim / bd close <id>
gh issue list / view <n> / create --title ... --body ...
```

## See Also

- [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) --
  first-time setup walkthrough.
- [docs/RELEASING.md](docs/RELEASING.md) -- rebuilding the
  pre-built QEMU tarballs.
- [docs/NESTED_VIRTUALIZATION.md](docs/NESTED_VIRTUALIZATION.md)
  -- running ltvm under a nested hypervisor.
- [docs/SOFTROCE_SETUP.md](docs/SOFTROCE_SETUP.md) -- LNet
  o2iblnd over SoftRoCE.
- [docs/IPV6.md](docs/IPV6.md) -- the extra NICs' IPv6
  addressing, `deploy --ip-family`, and how to verify a
  node is really on IPv6.
- [docs/SYSTEM_TEST_PLAN.md](docs/SYSTEM_TEST_PLAN.md) --
  end-to-end test matrix.
- [docs/VM_OWNERSHIP.md](docs/VM_OWNERSHIP.md) -- the
  advisory `owner_id` a VM records, and who sets it.
