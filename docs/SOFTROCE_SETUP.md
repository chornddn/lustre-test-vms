# SoftRoCE (RXE) between ltvm VMs

How to bring up a working RoCEv2 link between two ltvm-managed VMs
using the in-kernel `rdma_rxe` software RoCE driver, and how to run
Lustre's o2iblnd over it. No HCA required -- RXE runs RDMA verbs over
the regular Ethernet NIC.

## What this is good for

- Prototyping and correctness-testing kernel RDMA / o2iblnd code changes
  without needing real Mellanox hardware.
- Exercising the userspace verbs API (`libibverbs`, `librdmacm`) and
  perftest tools end-to-end.
- Running the o2ib-gated parts of `sanity-lnet` (LST between two nodes,
  a Lustre filesystem mounted over o2ib).

## What this is NOT good for

- Measuring anything that depends on PCIe behavior (TLP ordering, root
  complex / IO die effects, RO bit handling, posted-write retirement,
  HCA DMA engines). SoftRoCE never touches the PCIe data path; the
  "RDMA" is implemented entirely in software on top of UDP. The whole
  PCIe Relaxed Ordering story (DDN-6698 etc.) cannot reproduce here.
- Throughput-sensitive benchmarking. Expect ~100-250 MB/s, bounded by
  software packet processing.
- Anything that depends on **NUMA placement**. RXE reports no NUMA node,
  so code that selects a path by NUMA distance takes its degenerate
  branch. Force such paths on explicitly rather than relying on
  auto-detection.
- **`ko2iblnd dev_failover` testing.** Forcing HCA rebuilds under load
  breaks the RXE connection badly enough that an in-flight LST batch
  cannot be stopped; `rmmod lnet_selftest` then blocks forever in
  `sfw_shutdown` (unkillable D state, VM reboot required). This is not
  specific to whatever LND feature you are testing -- it reproduces with
  a stock module -- but it does mean failover tests need real hardware.

## 1. Kernel: InfiniBand core + rdma_rxe

The verbs core and `rdma_rxe` are built as modules via
`targets/common/kernel-config.fragment` and the per-arch fragment.
Vendor HCA drivers are deliberately left off -- a microvm has no HCA to
bind and they dominate the build time.

**aarch64 kernels built before this was enabled have no InfiniBand at
all** (the arch fragment used to carry a blanket `CONFIG_INFINIBAND=n`).
A VM booted on such a kernel fails at boot with:

```
modprobe: FATAL: Module rdma_rxe not found in directory /lib/modules/...
setup-nic-softroce.sh: ERROR: modprobe rdma_rxe failed
```

Rebuild the kernel **and** the image -- the image carries `/lib/modules`,
so a stale image on a fresh kernel produces exactly that error:

```bash
ltvm build kernel rocky9 --arch aarch64 --kernel 5.14-rhel9.5 \
    --lustre-tree ~/lustre-release
ltvm build image  rocky9 --arch aarch64 --kernel 5.14-rhel9.5 \
    --lustre-tree ~/lustre-release
```

`build image` requires `--lustre-tree`; without it the build refuses
because it cannot find the Lustre staging for that kernel.

Verify before creating VMs:

```bash
K=artifacts/rocky9/aarch64/kernels/5.14-rhel9.5-*/
grep -E 'CONFIG_(INFINIBAND|RDMA_RXE)=' $K/build-tree/.config
find $K/modules -name 'rdma_rxe.ko' -o -name 'ib_core.ko'
```

## 2. Lustre: build the in-kernel o2iblnd

`ltvm build lustre` passes `--with-o2ib=no` by default, because the
build container has no external OFED headers. That also means **no
`ko2iblnd.ko` is built**, so nothing can run over RXE. Ask for the
in-kernel LND explicitly:

```bash
ltvm build lustre rocky9 --arch aarch64 --kernel 5.14-rhel9.5 \
    --lustre-tree ~/lustre-release --configure="--with-o2ib=yes"
```

Use the `--configure=<value>` form. With a space, argparse consumes
`--with-o2ib=yes` as a flag of its own and fails with
`argument --configure: expected one argument`.

Expected in the configure output:

```
checking whether to enable OpenIB gen2 support... yes
```

The neighbouring `Auto detection of external O2IB failed. Build of
external o2ib disabled.` warning is normal -- that is the *external*
(MOFED) LND, which we are not building. Confirm the module landed:

```bash
find ~/lustre-release/.ltvm-staging -name 'ko2iblnd.ko'
```

## 3. Create the cluster

```bash
sudo ltvm cluster create co1 --target rocky9 --arch aarch64 \
    --kernel 5.14-rhel9.5 --nic softroce --vcpus 2 --mem 4096 \
    mgs+mds+oss:co1-srv:1 client:co1-cli
ltvm cluster deploy co1 --build ~/lustre-release
```

`--nic softroce` gets `rdma_rxe` loaded at boot and sets `fc_nics=`
so that mgmt (`eth0`) is excluded from LNet. `--kernel` is required
whenever the target's default kernel is not the one you built.

## 4. Move the rxe device onto eth0

**This step is currently required.** `--nic softroce` puts the rxe link
on `eth1`, but ltvm allocates the extra NIC an address in the *same
subnet as mgmt*. With two interfaces on one subnet the kernel routes
peer traffic out `eth0`, which has no rxe device, so `rdma_cm` address
resolution fails and LNet reports:

```
failed to ping <nid>@o2ib: Network is down
```

Renumbering `eth1` onto its own subnet does not help on macOS: every
NIC attaches to the same `socket_vmnet` socket, which only forwards its
own subnet, so ARP for any other subnet never reaches the peer (visible
as `ip neigh` entries stuck in `FAILED`, and the peer's `tcpdump`
showing the request arriving on `eth0`).

The reliable arrangement is to run RXE on `eth0`, the interface that
definitely has L2 connectivity to the other VMs:

```bash
for n in co1-srv co1-cli; do ssh $n '
    lnetctl lnet unconfigure 2>/dev/null
    rdma link delete rxe0 2>/dev/null
    ip link set eth1 down 2>/dev/null
    rdma link add rxe0 type rxe netdev eth0
    echo '\''options lnet networks="o2ib0(eth0)"'\'' \
        > /etc/modprobe.d/lnet.conf'
done
```

This is **runtime state and does not survive a VM reboot** -- `rc.local`
recreates the link on `eth1` from the `fc_nics=` cmdline. The
`/etc/modprobe.d` edits do persist (they live in the VM's overlay).
Re-run the `rdma link` half after any reboot.

## 5. Verify

```bash
for n in co1-srv co1-cli; do ssh $n '
    modprobe ko2iblnd
    lnetctl lnet configure
    lnetctl net add --net o2ib0 --if eth0
    hostname; lctl list_nids'
done
ssh co1-cli 'lnetctl ping <server-ip>@o2ib'
```

A healthy bring-up logs:

```
LNet: Using FastReg for registration
LNet: Added LNI 192.168.105.134@o2ib [8/256/0/180]
```

For a pure-verbs smoke test, `perftest` is preinstalled (server
backgrounded, since ssh is synchronous; `-F` skips the CPU-frequency
check):

```bash
ssh co1-srv 'true; nohup ib_write_bw -d rxe0 -F -D 5 >/tmp/ib.log 2>&1 & echo ok'
ssh co1-cli 'ib_write_bw -d rxe0 -F -D 5 <server-ip>'
```

## 6. Running sanity-lnet over o2ib

Point the test config at the o2ib net. `cluster deploy` installs the
stock `cfg/local.sh`, which defaults to `tcp`; append overrides rather
than replacing the file, or you will drop the `${VAR:-default}`
definitions that `init_test_env` derives `DIR`/`MOUNT1` from (the
symptom is `DIR= not in /mnt/lustre. Aborting.` and exit 99):

```bash
cat >> /usr/lib64/lustre/tests/cfg/local.sh <<'EOF'
mds_HOST=co1-srv
mgs_HOST=co1-srv
ost_HOST=co1-srv
CLIENTS=co1-cli
MDSCOUNT=1
MDSDEV1=/dev/vdb
OSTCOUNT=1
OSTDEV1=/dev/vdc
NETTYPE=o2ib
MGSNID=192.168.105.134@o2ib
LOAD_MODULES_REMOTE=true
PDSH="pdsh -S -Rssh -w"
EOF
```

Write the same file to every node, then run from the client:

```bash
ssh co1-cli 'cd /usr/lib64/lustre/tests && ONLY=313 bash ./sanity-lnet.sh'
```

### Tests that set ko2iblnd module options will silently do nothing

Any test driving `MODOPTS_KO2IBLND` needs one more step on an
**installed** (RPM / `make install`) tree. `load_module()` only uses
`insmod` when it finds the module under `$LUSTRE`; otherwise it falls
back to `modprobe`. Lustre's own `/etc/modprobe.d/ko2iblnd.conf` ships

```
install ko2iblnd /usr/sbin/ko2iblnd-probe
```

and that script re-execs `modprobe --ignore-install ...`, dropping the
command-line options on the floor:

```bash
modprobe ko2iblnd <opt>=2                  # option lost, reads back 0
modprobe --ignore-install ko2iblnd <opt>=2 # applied
```

The test then runs against a default-configured module and fails in a
way that looks like a code bug. Comment the rule out on every node
before running such tests:

```bash
for n in co1-srv co1-cli; do
    ssh $n "sed -i 's|^install ko2iblnd |#install ko2iblnd |' \
        /etc/modprobe.d/ko2iblnd.conf"
done
```

This does not arise from a build tree, where `insmod` is used and
`modprobe.conf` is bypassed entirely.

## Gotchas

- **`ssh` propagates the last command's exit code.** A trailing
  `pkill -f foo` that finds no match returns 1 and the whole ssh
  fails. Prefix with `true;` or append `|| true`.
- **Background jobs need `nohup ... &`**. Plain `&` under `ssh`
  may get reaped when the session closes.
- **One RXE device per VM is enough.** Don't add multiple rxe links
  over the same netdev -- they'll conflict.
- **Link layer is Ethernet, not InfiniBand**, even though `ibv_devinfo`
  prints `transport: InfiniBand`. That's the verbs transport class;
  what's on the wire is RoCEv2 over UDP.
- **Teardown warns.** `rmmod`-ing an LND logs
  `WARNING ... __rxe_cleanup ... cleanup failed, err = -22` from
  `rdma_destroy_qp`. That is an `rdma_rxe` QP-teardown quirk, not a
  fault in the caller.
- **Don't expect RC behavior to match real HCAs at the edges.** SoftRoCE
  implements the verbs spec but performance characteristics, error
  paths, completion timing, and concurrency limits all differ from
  Mellanox/Broadcom silicon. Code that works against rxe may still
  break against real HCAs and vice versa.
