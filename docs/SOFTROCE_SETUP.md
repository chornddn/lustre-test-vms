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
ltvm deploy co1 --lustre-tree ~/lustre-release --net o2ib
```

`--nic softroce` gets `rdma_rxe` loaded at boot and sets `fc_nics=`
so that mgmt (`eth0`) is excluded from LNet. It is the default NIC
type, so a plain `cluster create` gives you one too. `--kernel` is
required whenever the target's default kernel is not the one you
built.

`--net o2ib` is what makes the cluster *run* o2ib: it writes
`cfg/local.sh` and `/etc/modprobe.d/lnet.conf` on every node from one
resolved net, with the extra NIC's address as the MGS NID. A softroce
cluster runs tcp just as well -- `ltvm deploy co1 --net tcp` moves it
back, and `llmount` reformats, so no `writeconf` is involved.

## 4. Extra-NIC addressing

**No manual step is needed here any more.** Earlier revisions of this
document told you to tear the rxe link off `eth1` and rebuild it on
`eth0` after every boot. That workaround existed because ltvm gave every
extra NIC an address in the *same subnet as mgmt*, so the kernel had two
`scope link` routes for one prefix and sent peer traffic out `eth0`,
which has no rxe device.

Extra NICs now get their own network (`172.16.100.0/24` by default,
`$LTVM_EXTRA_SUBNET` to change it), and all of them share it, so several
NICs are rails of one LNet network rather than separate one-rail
networks. Because they share a subnet, `rc.local` gives each one a
routing table of its own selected by source address:

```
32765:  from 172.16.100.33 lookup 101
32764:  from 172.16.100.34 lookup 102
```

so a socket bound to a NIC's address egresses on that NIC. It also sets
`arp_ignore=1` / `arp_announce=2`, because every NIC of every VM shares
one L2 broadcast domain (the `fcbr0` bridge on Linux, one `socket_vmnet`
hub on macOS) and the default `arp_ignore=0` would let the wrong
interface answer ARP for its neighbour's address.

An earlier note here claimed `socket_vmnet` "only forwards its own
subnet". **That is wrong.** It is a hub: it floods every frame to every
other client with no MAC learning and no IP inspection. A second subnet
crosses it fine. The failure that claim was based on was ARP flux, which
the sysctls above address.

Verified on macOS with `--nic tcp --nic tcp`: each rail resolves its
peer to that peer's *own* interface MAC, and traffic sourced from each
address leaves on its matching interface.

> **Not yet re-verified for o2ib.** The check above used tcp NICs.
> `rdma_cm` performs its own address resolution rather than inheriting a
> bound socket's route, so whether it honours these source-based rules is
> unconfirmed. If `lnetctl ping` fails with `Network is down`, that is the
> first thing to investigate -- not the addressing, which is now known
> good.

## 5. Verify

First confirm the rxe device sits on the extra NIC and that the routing
is per-rail -- if either is wrong, nothing below will work:

```bash
for n in co1-srv co1-cli; do ssh $n '
    hostname; rdma link show; ip rule; ip -4 -br addr show eth1'
done
```

Expect `rxe0 ... netdev eth1`, a `from 172.16.100.x lookup 101` rule,
and `eth1` on `172.16.100.0/24`. An `eth0` netdev means the VM booted an
image older than the addressing fix -- rebuild it with
`ltvm build image`, since `rc.local` lives inside the image.

```bash
for n in co1-srv co1-cli; do ssh $n '
    modprobe ko2iblnd
    lnetctl lnet configure
    lnetctl net add --net o2ib0 --if eth1
    hostname; lctl list_nids'
done
ssh co1-cli 'lnetctl ping <server-extra-nic-ip>@o2ib'
```

Use the peer's **extra-NIC** address (`172.16.100.x`), not its mgmt
address. `lnetctl net add` may report `EEXIST` when `lnet.conf` already
applied at modprobe time; that is fine, check `lctl list_nids`.

A healthy bring-up logs:

```
LNet: Using FastReg for registration
LNet: Added LNI 172.16.100.134@o2ib [8/256/0/180]
```

For a pure-verbs smoke test, `perftest` is preinstalled (server
backgrounded, since ssh is synchronous; `-F` skips the CPU-frequency
check):

```bash
ssh co1-srv 'true; nohup ib_write_bw -d rxe0 -F -D 5 >/tmp/ib.log 2>&1 & echo ok'
ssh co1-cli 'ib_write_bw -d rxe0 -F -D 5 <server-ip>'
```

## 6. Running sanity-lnet over o2ib

Nothing to hand-write: `ltvm deploy co1 --net o2ib` already put
`NETTYPE=o2ib` and the o2ib `MGSNID` into `cfg/local.sh` on every node,
and the matching `lnet.conf` next to it.  Mount and run:

```bash
ltvm llmount co1-srv
ltvm test co1 sanity-lnet
```

Editing `NETTYPE` or `MGSNID` by hand is how the two files drift apart.
`ltvm test` refuses to start when they disagree.

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
