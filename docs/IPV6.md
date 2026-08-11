# IPv6 on ltvm clusters

Every cluster created by a current ltvm gets an IPv6 address on each of
its Lustre-network NICs, alongside the IPv4 one. Which family LNet uses
is a deploy flag. This document says what is addressed, how the
addresses are derived, how to run in IPv6 mode, and how to tell whether
a node really is on IPv6.

## What is addressed, and what is not

- **The extra (`--nic`) NICs are dual-stack.** `eth1..ethN` each hold a
  `172.16.100.x/24` address and a static ULA. `rc.local` assigns both at
  boot from the kernel command line, and gives each one a source-based
  routing table so a NI bound to one rail egresses on it.
- **The management NIC (`eth0`) is IPv4 only**, deliberately.
  `rc.local` sets `accept_ra=0` and `autoconf=0` on it so an
  IPv6-capable host bridge cannot hand it a global address.

The reason mgmt is excluded is not tidiness. `h2name_or_ip()` in
`test-framework.sh` resolves a node name to a NID by running
`hostname -I` on that node and splitting the output into an IPv4 list
and a large-NID list. A stray global IPv6 address on mgmt would become
a NID candidate and the framework could pick it.

There is no `fe80::` link-local on the extras either. They run with
`addr_gen_mode=none`, so a missing link-local is expected and is not a
fault.

## The prefix and the derivation rule

The default prefix is `fd17:2016:1000:f100::/64`. The interface ID
spells out the four IPv4 octets, each as `f` followed by the octet
zero-padded to three decimal digits:

```
172.16.100.203  ->  fd17:2016:1000:f100:f172:f016:f100:f203
172.16.100.23   ->  fd17:2016:1000:f100:f172:f016:f100:f023
```

The encoding is injective in the whole IPv4 address, so uniqueness comes
free from the IPv4 allocator and a reader can match `lctl list_nids`
against `ip -4 a` at a glance.

Override the prefix with `$LTVM_EXTRA_SUBNET6` or `VM_DIR/extra-subnet6`,
mirroring `$LTVM_EXTRA_SUBNET`.

### Why every hextet is four digits

**The invariant is that every hextet must be `>= 0x1000`**, and the
override is validated against it: four hextets, each `0x1000` or more.

A hextet at or above `0x1000` always prints four digits and is never
zero, so `inet_ntop` can never strip a leading zero and can never
introduce a `::`. Hold that for all eight and the address always renders
at maximum width: 39 characters, seven colons, no compression.

That width is the whole point. IPv6 defects cluster around string sizes
and truncation. A full-width address gives a 43-character NID against an
`LNET_NIDSTR_SIZE` of 64, where the longest IPv4 NID is 19. An address
like `fd00:fc:100::203` is 16 characters — shorter than the IPv4 address
it replaces, so it exercises nothing. An override that breaks the
`>= 0x1000` rule silently removes that coverage, which is why the
validation refuses it rather than warning.

## Running in IPv6 mode

```bash
ltvm deploy co1 --lustre-tree ~/lustre-dev/lustre-release --net tcp --ip-family ipv6 --as my-task
```

`--ip-family` defaults to `ipv4` and is recorded on the cluster the same
way `--net` is, so a bare redeploy keeps it. `ltvm cluster status`
reports it.

`--ip-family ipv6` writes `FORCE_LARGE_NID=true` and an IPv6 `MGSNID`
into `cfg/local.sh`.

## Why IPv6 needs `--net tcp`

`test-framework.sh` refuses any other net:

```
if [[ $NETTYPE != tcp* ]]; then
        error "FORCE_LARGE_NID only supported by tcp"
fi
```

o2iblnd does carry `AF_INET6` code, but the suites will not run it, so
`--ip-family ipv6 --net o2ib` is refused at resolve time, before any node
is touched. The addresses are still assigned, so o2ib over IPv6 can be
poked at by hand with `lnetctl`.

## Why `lnet.conf` does not change

`/etc/modprobe.d/lnet.conf` is byte-identical for both families:

```
options lnet networks="tcp0(eth1,eth2)"
```

The family is chosen at module-configure time, not in the modprobe
config. `lnet_inet_enumerate()` takes a `v6_first` flag; when it is set,
each device's IPv6 address is entered in the interface list before its
IPv4 one, and `lnet_inet_select()` takes the first entry whose name
matches. The flag is set only by `lnetctl lnet configure --large`,
which is what `FORCE_LARGE_NID=true` makes the test framework run.

So a dual-stack interface yields an IPv6 NI or an IPv4 NI depending on
how LNet is configured, from one unchanged config file.

## `sanity-lnet` skips more subtests in IPv6 mode

With `FORCE_LARGE_NID=true`, `sanity-lnet.sh` calls `always_except`
against a list of known LU tickets (101, 103, 199, 208, 213, 220, 228,
230, 231, 255, 257, 270, 302 at the time of writing). Those report as
skips, not failures. The list moves with the Lustre tree, so re-derive
it from the tree under test rather than treating a changed skip count as
a regression.

The suite auto-detects the family from `INTERFACES[0]`: IPv6 only forces
IPv6 mode, IPv4 only forces IPv4 mode, and a dual-stack interface defers
to `FORCE_LARGE_NID`. Dual-stack is what lets one cluster run either
mode without being recreated.

## Verifying a node really is on IPv6

This is the only check that separates "ltvm wrote the right file" from
"LNet did the right thing". Rerun it after any change to `lnet_net.py`
or `rc.local`.

```bash
ssh co1-mds 'modprobe lnet; lnetctl lnet configure --all --large; lctl list_nids'
ssh co1-mds 'lctl ping fd17:2016:1000:f100:f172:f016:f100:f023@tcp'
```

`lctl list_nids` must print a 43-character NID and must match `MGSNID`
in `cfg/local.sh` **character for character**. ltvm writes `MGSNID` as a
string and Lustre matches it as a string, so a formatting difference
between the two is a mount failure that reads as a Lustre fault.
Compare them programmatically, not by eye.

An IPv4 NID here means LNet did not take the IPv6 address. A
*compressed* IPv6 NID means the address broke the width invariant. Both
are faults; capture `dmesg | grep -i lnet` before going further.

If the interfaces carry no IPv6 address at all, the VM booted a base
image older than the addressing change — `rc.local` lives inside the
image. Rebuild it with `ltvm build image` and recreate the cluster, in
that order: the running overlays back onto `base.ext4`.

## Known limit: a filesystem will not mount over an IPv6 NID

**LNet works over IPv6; a Lustre filesystem does not mount over it yet.**
This is LU-18041, open upstream.

`UUID_MAX` is 40, so an `obd_uuid` holds a NID of at most 39 characters.
A full-width IPv6 NID is 43. `client_obd_setup()` refuses the MGC's UUID
and the mount fails with `-EINVAL`:

```
LustreError: (ldlm_lib.c:369:client_obd_setup()) target UUID must be 40 characters or less
LustreError: (obd_config.c:845:class_setup()) setup MGCfd17:2016:1000:f100:f172:f016:f100:f203@tcp failed (-22)
LustreError: (super25.c:177:lustre_fill_super()) llite: Unable to mount <unknown>: rc = -22
```

The failure lands at MDT mount on the server. Related client-side
symptoms reported upstream are `cannot find UUID by nid`, `no valid NIDs
for new import connection`, and `mds_connect ... rc = -11`.

No prefix avoids this. Any IPv6 address longer than 35 characters
overflows once `@tcp` is appended, so no full-width address can ever
fit. Gerrit 65491, "canonicalize IPv6 NID-derived UUIDs", is the change
that shortens the string; it has not landed.

Two consequences for testing:

- `ltvm llmount` and every filesystem suite fail on an IPv6 deploy.
  Nothing works around this; the mount itself is what fails.
- An LNet suite fails only because auster formats and mounts in its own
  setup before the suite script runs. `--no-setup` (auster `-N`) skips
  that setup, and the suite then runs:

  ```bash
  ltvm test co1 sanity-lnet --no-setup --except 50,109 --as my-task
  ```

  This has been run. `sanity-lnet` starts in IPv6 mode and its subtests
  execute against IPv6 NIDs. The suite does not complete, but it halts
  on two failures that also halt the IPv4 run on the same cluster, so
  neither is caused by IPv6:

  - `test_50`, a benign page-allocation shortage on 2 GB VMs; exclude it
    together with `test_109`.
  - `test_241`, at `test_241` in both families. `check_parameter()`
    compares a line count against `${#INTERFACES[@]}` while the test
    configures only `INTERFACES[0]`, so a cluster with two extra NICs
    fails it. A cluster created with one `--nic` should not.

Until 65491 lands, filesystem coverage on ltvm needs IPv4. IPv6 also
supports LNet-level checks run by hand: `lctl list_nids`, `lctl ping`,
and `lnetctl` work against the configured NIs.
