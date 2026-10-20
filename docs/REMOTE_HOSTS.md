# Remote VM hosts

You run `ltvm` on one machine, the **controller**. The VMs run on a different machine, the **host**. An AI agent or a person on the controller can create VMs and clusters on the host. It can deploy a Lustre tree to them, run suites, and copy the results back. The command is the same as for local VMs, with `--host <name>` added:

```bash
ltvm --host lab1 cluster create co2 mgs+mds:co2-mds:1 oss:co2-oss:2 client:co2-c1
ltvm --host lab1 cluster deploy co2 --build ~/lustre-release --mount
ltvm --host lab1 suite run co2 sanity --only '0a 1' --wait
ltvm --host lab1 pull co2 suite
```

Often the host is a personal machine, and its owner also uses it for other work. So `serve`, the program that runs each request on the host, limits what the controller can do. The host owner keeps control.

Supported hosts are Linux, macOS on Apple Silicon, and WSL2. Follow the policy of your organization for the machines that you connect.

## How it works

The design follows the Docker-context model (`DOCKER_HOST=ssh://`). There is no daemon and no new open port. SSH gives authentication and encryption.

```
controller                                   host
----------                                   ----
ltvm --host lab1 cluster create ...
  -> ssh -i ~/.config/ltvm/keys/lab1 ltvm@lab1
                                             sshd
                                               authorized_keys:
                                               restrict,command="ltvm serve --stdio",from="<ip>"
                                             ltvm serve --stdio   (user: ltvm, not admin)
                                               check: host enabled
                                               check: protocol version
                                               check: shared layout, not root
                                               check: verb on the allow-list
                                               check: names, paths, podman
                                               check: AC power and caps
                                               run the verb, write the audit line
  <- reply line, verb output, exit status
```

- Each request is one SSH session. The controller sends the argument vector as one string. sshd gives it to `serve` in `SSH_ORIGINAL_COMMAND`. `serve` splits it with `shlex` and gives it to no shell.
- The first line in each direction is a JSON handshake with the protocol version and the ltvm version. The protocol version is 1. It is separate from the ltvm version.
- The controller parses the command first. So a bad option fails on the controller, before the network.
- `serve` never runs `sudo`. It refuses to run as root. It refuses a host that does not have the shared layout (see "Root" in the README).
- `serve` turns off telemetry and the update check for each request and for each child ltvm process.

## Set up the host

### 1. Install

Put the ltvm checkout where the dedicated user can read it, for example `/opt/ltvm`. The forced command runs the checkout and its Python as that user. A checkout in your home directory usually fails this check.

As an admin user, run the install with `--serve`. On Linux and WSL2, use `sudo`. On macOS, do not use `sudo`.

```bash
sudo ./ltvm install --serve            # Linux, WSL2
./ltvm install --serve                 # macOS
./ltvm install --serve --yes           # accept the default caps, ask nothing
```

`install --serve` does the normal install. Then it does these steps:

1. It makes the dedicated user `ltvm`, if the user does not exist.
   - macOS: a hidden user (UID 401-499), with no password, not in `admin`. The primary group is `staff`, for socket_vmnet. The home is `/Users/ltvm`.
   - Linux and WSL2: `useradd`, home `/home/ltvm`, primary group `ltvm`, also in `kvm` when that group exists. `usermod --lock` locks the password.
   - The shell is `/bin/sh`, because sshd runs the forced command through the shell.
   - The user is a member of the `ltvm` group. So it runs the VM lifecycle with no root on the shared layout.
   - The user gets its own artifacts directory, `~ltvm/artifacts`. `serve` sets `LTVM_ARTIFACTS_DIR` to it.
   - Linux and WSL2: the user gets 65536 subordinate UIDs and GIDs in `/etc/subuid` and `/etc/subgid`, for rootless podman. The range starts after the highest range in each file, and at 100000 or more. A user that has a range keeps it.
2. It checks that the user can read the checkout and its Python. If a directory blocks the user, it prints the directories and skips the wizard.
3. It runs the `serve init` wizard as that user. See [Caps](#caps).
4. It installs the reap job. See [Leases](#leases-and-reap).
5. Linux (not WSL2): it installs a polkit rule for the power guard. See [Sleep](#sleep-and-power).
6. It sets up the remote guest network. See [Remote guest network](#remote-guest-network).
7. It checks sshd for the user: running, `AllowUsers`, `DenyUsers`, `AllowGroups`, `ExposeAuthInfo`, and on macOS the `PATH` and Remote Login.
8. It prints the steps that you must do, and the `ltvm host add` command for the controller.

`install --serve` never changes the sshd configuration, Remote Login or a VPN. It prints these steps for you. It changes a firewall only for the remote guest bridge (see below).

### Remote guest network

Remote VMs have a guest network of their own. The VMs of the host owner stay on the owner network, as before. A VM is remote when its owner ID starts with `remote:`, which `serve` sets at create time. The owner ID selects the network, the IP pool, the gateway, and the bridge or socket for every NIC.

| | Owner network | Remote network |
|---|---|---|
| Mgmt subnet | 192.168.100.0/24 (macOS 192.168.105.0/24) | 192.168.101.0/24 (macOS 192.168.106.0/24) |
| Extra NIC subnet | 172.16.100.0/24, fd17:2016:1000:f100::/64 | 172.16.101.0/24, fd17:2016:1000:f101::/64 |
| Linux attachment | bridge `fcbr0`, NAT | bridge `fcbr1`, no NAT |
| macOS attachment | socket_vmnet, vmnet shared mode | second socket_vmnet, vmnet host mode, socket `socket_vmnet.ltvm-remote` |
| DNS | `ltvm-dnsmasq` | `ltvm-dnsmasq-remote`, local names only |

`LTVM_REMOTE_SUBNET` and `VM_DIR/remote-subnet` move the remote mgmt subnet. `LTVM_REMOTE_EXTRA_SUBNET` and `VM_DIR/remote-extra-subnet` move the remote extra NIC subnet. On Linux, `install --serve` picks a free /24 (192.168.101, then 192.168.150-199). Each value must be the prefix of a private /24, such as `192.168.101`. ltvm refuses any other value.

Group `ltvm` can write in `VM_DIR`, so `install --serve` does not trust a record that root did not write. It uses the env, a record that root owns, or the default. Then it writes both records again as root. The sticky bit of `VM_DIR` stops a group member from the replacement of a file that root owns.

On Linux and WSL2, `ltvm-remote-net.service` loads the nftables table `inet ltvm_remote` and then makes `fcbr1`. The table drops each routed packet that goes into `fcbr1` or comes out of it. `ltvm-dnsmasq-remote.service` serves DNS on the remote gateway. `/etc/qemu/bridge.conf` gets `allow fcbr1`. A running ufw gets `allow in on fcbr1` with no `route allow` rule. firewalld puts `fcbr1` in the trusted zone.

On macOS, these LaunchDaemons run the remote network:

- `io.github.ltvm.pf-remote` runs `pfctl -E -a com.apple/ltvm-remote -f /usr/local/etc/ltvm-remote-pf.conf` at boot. The default `/etc/pf.conf` evaluates the anchors under `com.apple/`.
- `io.github.ltvm.socket_vmnet-remote` runs socket_vmnet with `--vmnet-mode=host` and `--socket-group=ltvm`.
- `io.github.ltvm.dnsmasq-remote` and `io.github.ltvm.dnsmasq-remote-reload` serve DNS on the remote gateway.

`serve` refuses a remote `create` or `cluster create` with `layout_unsupported` when a part of the remote network is missing. `ltvm doctor` reports the remote network. It also reports a remote VM with an address in the owner network. Such a VM was made before the remote network existed. It does not start: destroy it and create it again.

If the wizard did not run, run it as the dedicated user:

```bash
sudo -u ltvm -H /usr/local/bin/ltvm serve init
```

On macOS, every local user is in the `staff` group. So `ltvm` can read the files in your home that `staff` or everyone can read. Check with `ls -la ~`. Use `chmod 0700` where you need to.

### 2. Configure sshd

Add this block to `/etc/ssh/sshd_config.d/ltvm.conf`, or to the end of `/etc/ssh/sshd_config`. `install --serve` prints the block when your sshd needs it.

```
Match User ltvm
    ExposeAuthInfo yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AuthenticationMethods publickey
    PermitTTY no
    AllowTcpForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no
    PermitUserRC no
    SetEnv PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
```

- `ExposeAuthInfo yes` is necessary. Without it, sshd does not set `SSH_USER_AUTH`. Then the audit log and the VM owner ID cannot name the controller key, and they show `-`.
- `SetEnv PATH=...` is necessary on macOS only. On macOS, the sshd PATH for a forced command has no `/usr/local/bin`. So sshd cannot find `ltvm`. On Apple Silicon, Homebrew puts `sshpass` and `podman` in `/opt/homebrew/bin`, so the PATH must have that directory too. The reap LaunchDaemon gets the same PATH. `SetEnv` needs OpenSSH 8.7 or later. macOS 13 and later have it.
- The other lines repeat the `restrict` option of the key line. They are a second layer.
- A `Match` block must be at the end of `sshd_config`. A drop-in file in `sshd_config.d/` works on RHEL 9, Ubuntu 22.04 and later, and macOS 13 and later.

Check the result, then reload sshd:

```bash
sudo sshd -t
sudo sshd -T -C user=ltvm,host=x,addr=127.0.0.1 | grep -E 'exposeauthinfo|setenv'
sudo systemctl reload sshd                          # Debian and Ubuntu: ssh
sudo launchctl kickstart -k system/com.openssh.sshd # macOS
```

On macOS, turn on Remote Login: System Settings > General > Sharing > Remote Login. Under "Allow access for", select "Only these users" and add `ltvm`. Keep your own account in the list only if you use ssh to this Mac. If you add only `ltvm` to `com.apple.access_ssh`, you lock out your own ssh access.

### 3. Restrict the network

ltvm does not install or configure a VPN. We recommend Tailscale with an access control list (ACL) that lets only the controller reach port 22 on the host. Tag the controller `tag:ltvm-controller` and the host `tag:ltvm-host`. Then put this rule in the tailnet policy file:

```json
{
  "tagOwners": {
    "tag:ltvm-controller": ["autogroup:admin"],
    "tag:ltvm-host":       ["autogroup:admin"]
  },
  "acls": [
    {"action": "accept", "src": ["tag:ltvm-controller"], "dst": ["tag:ltvm-host:22"]}
  ]
}
```

- Remove the default "allow all" rule. No other rule may give a different source access to `tag:ltvm-host:22`.
- Give the controller Tailscale address (`100.x.y.z`) to `--from` when you pair. Then the key works only from that address.
- On WSL2, run Tailscale inside the distro. Then the Windows NAT and the port proxy are not in the path.

### 4. WSL2

A WSL2 host needs more steps. `install --serve` prints them when sshd does not run in the distro.

1. Install `openssh-server` in the distro (apt or dnf).
2. Put `[boot]` and `systemd=true` in `/etc/wsl.conf`. Run `wsl --shutdown` in Windows, then start the distro again. The reap timer and sshd need systemd.
3. Run `sudo systemctl enable --now ssh` (or `sshd`).
4. Make port 22 of the distro reachable. The best way is Tailscale inside the distro. Without Tailscale, put `[wsl2]` and `networkingMode=mirrored` in `%UserProfile%\.wslconfig`, or forward port 22 with `netsh interface portproxy`. Then let port 22 in through the Windows firewall, from your private network only.
5. Set the Windows power plan so that the PC does not sleep while a VM runs. ltvm cannot stop Windows from going to sleep.

Behind the WSL2 NAT, the host sees the vEthernet gateway as the controller address, not the real address. Give the address that the host sees to `--from`.

## Pair the controller and the host

Each side pins the other. No person edits `authorized_keys` by hand.

1. `install --serve` prints a line such as this one. The fingerprint is the SHA256 fingerprint of `/etc/ssh/ssh_host_ed25519_key.pub`.

   ```bash
   ltvm host add lab1 ssh://ltvm@100.64.0.7 --hostkey SHA256:abc...
   ```

   Run it on the controller. Use the address that the controller reaches (the VPN address, if you use a VPN). `host add` takes only an ed25519 host key.

2. `host add` does these steps:
   - It makes an ed25519 key at `~/.config/ltvm/keys/lab1`, with no passphrase. Agents use the key with no person present.
   - It runs `ssh-keyscan` and keeps the host key only if its fingerprint is the pinned one. A different key gives `host_key_mismatch`, and `host add` adds nothing. If the host is not reachable, `host add` adds the host. The first request then pins the key against the same fingerprint.
   - It writes the entry to `$XDG_CONFIG_HOME/ltvm/hosts.json`.
   - It prints the command for the host owner:

   ```bash
   sudo -u ltvm -H ltvm serve authorize 'ssh-ed25519 AAAA... ltvm:<controller>' --from 100.64.0.9
   ```

   `--from` is the address that the host sees for the controller. By default, `host add` uses the local address of the route to the host. Change it if a NAT or a VPN is in the path.

   `--from` takes one IP address or one CIDR range, such as `100.64.0.9` or `100.64.0.0/24`. It refuses a wildcard (`*`, `?`), a host name, a `!` pattern and a list. It also refuses a CIDR range with host bits set, such as `100.64.0.9/24`.

3. Run that command on the host. `serve authorize` adds one line to `~ltvm/.ssh/authorized_keys` (directory 0700, file 0600). The restrict options are always there:

   ```
   restrict,command="ltvm serve --stdio",from="100.64.0.9" ssh-ed25519 AAAA... ltvm:<controller>
   ```

   `serve authorize` refuses a key that already has a line.

4. On the controller, check the host:

   ```bash
   ltvm host list            # status: ok, or an error code
   ltvm host list --json     # name, url, status, protocol, ltvm
   ```

To remove a controller, delete its line from `~ltvm/.ssh/authorized_keys`. On the controller, `ltvm host remove lab1` deletes the entry and the key files, and prints the line that the host owner must delete.

The controller ssh command ignores your `~/.ssh/config` (`-F /dev/null`). So a `ProxyJump` in that file does not apply. The command uses `BatchMode`, `StrictHostKeyChecking=yes`, the per-host known_hosts file, no agent and no forwards.

We do not use FIDO or Secure Enclave keys. These keys need a touch for each use, and a touch blocks agents.

## Use a remote host

### Select the host

- `ltvm --host lab1 <verb> ...` sends the verb to the host. `--host` must come before the verb. `ltvm list --host lab1` is an error.
- `LTVM_HOST=lab1` in the environment does the same for every verb, except `serve`, `host`, `completion`, `install`, `update` and `doctor`. These verbs act on the controller. So `eval "$(ltvm completion)"` in a shell rc file does not reach the host.
- A name `lab1/<vm>` selects host `lab1` when `lab1` is a registered host. `--host` wins over the name prefix, and the prefix wins over `LTVM_HOST`.

On the controller, a remote VM has the name `<host>/<vm>`. `list`, `cluster list`, `cluster status` and `cluster exec` show the prefix. You can give a name with or without the prefix. Exception: the values of `--node-target VM=...` and `--node-kernel VM=...` take the bare node name.

### Allowed verbs

| Area | Allowed |
|---|---|
| Lifecycle | `create`, `destroy`, `start`, `stop`, `list` |
| VM actions | `vm console-log`, `vm crash-collect`, `vm nmi`, `vm snapshot`, `vm restore`, `vm exec`, `vm dk-collect` |
| Clusters | `cluster create`, `destroy`, `start`, `stop`, `status`, `list`, `exec`, `deploy`, `llmount`, `llumount` |
| Artifacts | `target list`, `target show`, `target fetch`, `build all`, `build container`, `build kernel`, `build image`, `build zfs`, `build mofed-kmods`, `build status` |
| Lustre | `deploy-lustre`, `llmount`, `llumount`, `suite run`, `suite status`, `suite collect` |
| Remote only | `pull`, `lease renew` (and `receive-tree`, which the controller sends for a deploy) |

`serve` refuses every other verb with `verb_refused`. Examples: `install`, `update`, `doctor`, `clean`, `skills`, `telemetry`, `claim`, `release`, `vm set`, `cluster ssh`, `cluster krb5`, `build lustre`, `build shell`, `target build`, `target publish`, `target delete`, and the `make-*` verbs. A new ltvm verb stays refused until a person adds it to `ltvm_pkg/remote/allowlist.py`. The controller sends an alias under `vm` (for example `vm create`) as its top-level verb (`create`). `serve` refuses the `vm` spelling.

### Scope rules

`serve` lets the controller act only on what remote requests made. A VM that a remote request makes has the owner ID `remote:<key fingerprint>`.

- `list` and `cluster list` show only remote VMs and clusters. They do not show the VMs of the host owner.
- A verb on a VM or cluster that the owner made gives `vm_not_found` or `cluster_not_found`. The message is the same as for a VM that does not exist.
- `create` with the name of an owner VM gives `name_in_use`. `create` with the name of a remote VM starts it, as on a local host.
- `create --owner` gives `bad_request`.
- A free-form name option gives `bad_request` when it can change a host path. These options are `--arch`, `--kernel`, `--mofed-version`, `--zfs-version`, `--variant`, `--target` and the target positional, and the value part of `--node-kernel` and `--node-target`. A name must have only letters, digits and `. _ + -`, must not start with `.`, and must not have `..`.
- A `passthrough` NIC gives `needs_root`.
- Path options give `path_refused`: `create --image`, `vm crash-collect --outdir` and `--mod-dir`, `suite run --results-dir`, `suite collect --dest` and `--results-dir`, `--zfs-source`, `--url`, and a `--lustre-tree` that is not a job tree.
- `build all`, `build kernel` and `build image` with no `--lustre-tree` give `path_refused`. Locally these verbs build the current directory. On the host, that directory is the home of `ltvm`. These builds use no tree, so they pass: `build image --no-lustre`, and a kernel build of a Debian or kernel.org target.
- `vm console-log --follow` gives `verb_refused`, because `serve` does not stream that output.
- `vm crash-collect` always writes to `VM_DIR/crashes/<vm>/` on the host. `vm dk-collect` always writes to `VM_DIR/dk/<vm>/`.
- A build or a deploy gives `podman_unsafe` when podman on a Linux or WSL2 host is not rootless. On macOS, podman runs containers in the podman machine VM, so `serve` accepts it.

The controller reaches guests only through `serve`: `vm exec`, `cluster exec`, `suite`, `llmount`, and `pull`. There is no ProxyJump and no port forward. All guests have the root password `initial0`, so only the host must reach the guest subnets. Remote guests are on their own network and cannot reach the guests of the owner. See [Security boundary](#security-boundary).

### Deploy a Lustre tree

```bash
ltvm --host lab1 create co1-single --vcpus 2 --mem 4096 --lease 4h
ltvm --host lab1 deploy-lustre co1-single --lustre-tree ~/lustre-release --mount
ltvm --host lab1 vm exec co1-single lctl dl
```

The build runs on the host, in the build container of the host. The controller and the host can have different architectures.

1. The controller makes a gzip tar stream of the tree. The stream holds the files from `git ls-files --cached --others --exclude-standard`, without deleted files and submodule directories. It also holds a `LUSTRE-VERSION-FILE` that the controller makes from `git describe`. It does not hold `.git`.
2. `serve` unpacks the stream into a new job directory, `~ltvm/jobs/j-YYYYMMDD-HHMMSS-<hex>/tree/`. It refuses the stream with `path_refused` for an absolute name, a `..` component, a hard link, a device or a `.git` entry (in any case, such as `.GIT`). It also refuses a symbolic link whose real path is outside the tree. A link can go through other links of the stream. So `serve` resolves each link after the last member.
3. The controller replaces `--lustre-tree` (or `--build`) with the job tree, and `serve` runs the verb.

The controller sends the tree for `deploy-lustre`, `cluster deploy`, and `build all|kernel|image` with `--lustre-tree`. With no tree option, `deploy-lustre` and `cluster deploy` send the current directory.

- The host runs no code from the tree outside the build container. It runs no `git` on a job tree and does not run `LUSTRE-VERSION-GEN`. The version comes from the `LUSTRE-VERSION-FILE` in the stream.
- The build container can write links into the job tree. So the host opens its stamp, lock and version files in the tree with `O_NOFOLLOW`. A staging path with a link in it, or a staging link that resolves out of the staging, stops the build.
- The tree must be a git checkout. A tree that is not a git checkout gives `bad_request` on the controller. So a remote deploy of a release tarball or of the Lustre that `target fetch` bundles is not possible.
- Each deploy sends the full tree and makes a new job. Each job has its own ccache. So each deploy is a full build on the host. See fork F2 in [Design decisions](#design-decisions).
- The controller prints the output of a verb only when the verb ends. A deploy shows nothing while it builds.
- A request can take at most `LTVM_REMOTE_TIMEOUT` seconds (default 8 hours, `0` for no limit). Then it fails with `timed_out`. SSH keepalives stop a request in about 2 minutes when the host or the network is dead.

### Copy results back

```bash
ltvm --host lab1 pull co2 suite                  # to ./ltvm-pull/lab1/suite/
ltvm pull lab1/co1-single crash --dest /tmp/out  # the name selects the host
ltvm --host lab1 pull j-20261019-101500-1a2b3c4d dk
```

`pull <vm|cluster|job> <what>` copies one type of result into `--dest`. The default is `./ltvm-pull/<host>/<what>/`. `--dest` and `--json` stay on the controller. `pull` with no host is an error.

| `<what>` | Read on the host from | Made by |
|---|---|---|
| `suite` | `VM_DIR/suites/ltvm-suite/<vm or cluster>/` | `suite run --wait`, `suite collect` |
| `crash` | `VM_DIR/crashes/<vm>/` | `vm crash-collect` |
| `dk` | `VM_DIR/dk/<vm>/` | `vm dk-collect` |
| `console` | `VM_DIR/sockets/<vm>.log` | QEMU |

- A cluster name selects all of its nodes for `crash`, `dk` and `console`. For `suite` it selects the results directory of the cluster.
- A guest of a remote request writes the suite results. So under `serve`, `suite collect` copies them with `rsync -rpt`, not `-a`. The copy has no links and no device files. The suite summary does not follow a link to a `results.yml` or to a directory.
- A job name selects the VMs and clusters that the job deployed to.
- The request has no path. `serve` refuses any other shape with `bad_request`.
- `serve` does not follow a symbolic link. A link in the path gives `path_refused`. `serve` skips a link inside a result directory, and prints a message on stderr.
- `pull` replaces a file in the destination. `pull` does not write through a symbolic link in the destination.
- When there are no results, `pull` prints "no <what> results" and exits 0.

`vm dk-collect <vm> [--outdir DIR] [--timeout S]` runs `lctl dk` in the guest and writes `dk-<vm>-<timestamp>.log`. Locally, the default directory is `~/ltvm-dk`. Through `serve`, the directory is always `VM_DIR/dk/<vm>/`, and `pull ... dk` copies it back. `lctl dk` empties the debug buffer of the guest.

## Leases and reap

Each VM or cluster that a remote `create` or `cluster create` makes gets a lease. When the lease expires, the host destroys the VM or the cluster. So a controller that crashes or forgets does not leave VMs on the host.

- The default lease is `lease_hours` in `serve.toml` (8 hours by default). `--lease TIME` on `create` or `cluster create` sets a different length (`30m`, `4h`, `2d`). `--lease` without `--host` is an error.
- `ltvm --host lab1 lease renew <vm|cluster> [--for TIME]` makes the lease end TIME from now. The name of a cluster node renews the lease of its cluster.
- A `--lease` or `--for` value above `max_lease_hours` in `serve.toml` (24 hours by default) gives `bad_request`. The message names the max. The default lease is never longer than the max.
- `serve` writes the lease before the verb runs. So a create that fails still has a lease. Reap drops a lease when its VM does not exist.
- A cluster has one lease. Its nodes have no lease of their own.
- A `create` on a remote VM that exists resets the lease length. `--dry-run` gives no lease.
- A remote `destroy` does not drop the lease. Reap drops it later.
- Through `--host`, `list --json` shows `lease_expires` on each VM and cluster. The text shows `lease=YYYY-MM-DDTHH:MM`.

A reap job runs `ltvm serve reap` as the dedicated user every 5 minutes:

- macOS: `/Library/LaunchDaemons/io.github.ltvm.serve-reap.plist` (`StartInterval` 300).
- Linux and WSL2: `/etc/systemd/system/ltvm-serve-reap.timer` and `.service` (`OnBootSec=60s`, `OnUnitActiveSec=300s`). WSL2 needs `systemd=true`. The service has `KillMode=process`, so the sleep lock holder that reap starts continues after reap exits.

For each expired lease, reap runs the scope checks of `serve`, then `destroy` or `cluster destroy`, and writes one audit line. A failed destroy keeps the lease, and the next reap tries again.

Reap also destroys a remote VM or cluster with no lease when it is older than `lease_hours`. The age comes from the create time of the VM, or of the oldest node of a cluster. A VM with no create time stays until `serve disable --destroy`. Reap never destroys a cluster node by itself.

Reap also deletes old jobs. A job goes when it is older than `lease_hours` and no remote VM or cluster that exists uses it. A job delete writes no audit line.

Leases are separate from claims (`ltvm claim`). See fork F1 in [Design decisions](#design-decisions).

## Caps

`serve init` finds the CPUs, RAM and free disk of the host, and suggests a value for each cap. Press Enter to accept a value. Type `none` to remove a cap.

| Cap | Flag | Default |
|---|---|---|
| vCPUs of all remote VMs | `--max-vcpus N` | 80 % of the logical CPUs |
| RAM of all remote VMs | `--max-ram-gb GB` | 80 % of the physical RAM |
| Disk | `--max-disk-gb GB` | the smaller of 100 GB and 25 % of the free disk |
| Default lease | `--lease-hours H` | 8 |
| Longest lease that a request can ask for | `--max-lease-hours H` | 24 |
| New VMs only on AC power | `--ac-only`, `--no-ac-only` | on for a laptop, off for a desktop |

`--yes` accepts all defaults. `--json` asks nothing. `install --serve` gives the same flags to the wizard. The file is `~ltvm/.config/ltvm/serve.toml`:

```toml
enabled = true
max_vcpus = 12
max_ram_gb = 25.6
max_disk_gb = 100.0
lease_hours = 8.0
max_lease_hours = 24.0
ac_only = true
```

`install --serve` and `serve init` always write this file. When the file is missing, `serve` refuses every request with `bad_config`. `serve disable` and `serve enable` still work, and they write a new file.

Admission control checks `create` and `cluster create` against all caps:

- Requested: `--vcpus` and `--mem` for each new VM. With no `--mem`, the default memory of the target. Disk: 1 GB for each new VM, because the disk files are sparse. A remote VM that exists asks for nothing.
- Used: all remote VMs, both running and stopped. Disk used is the allocated blocks of the remote VM disks. It also includes `~ltvm/jobs`, the artifacts directory, and `VM_DIR/crashes`, `VM_DIR/dk` and `VM_DIR/suites`. `serve` counts the disk only when the owner sets a disk cap.
- A resource is over its cap when used plus requested is more than the cap.

The disk cap also applies to the verbs that write to the disk but make no VM:

- `build all|container|kernel|image|zfs|mofed-kmods`, `target fetch`, `vm snapshot`, `vm crash-collect` and `vm dk-collect` give `over_cap` when the used disk is at the cap.
- `receive-tree` gives `over_cap` when the used disk is at the cap. The unpack stops before a file goes above the cap. Then `serve` removes the job.

There is no cap on the number of VMs. For example, with a vCPU cap of 80, the controller can make 80 VMs with 1 vCPU each, or 10 VMs with 8 vCPUs each.

`serve` refuses a request at once. It does not put the request in a queue. With `--json`, the controller writes this envelope to stderr. The error code is in `code`, and the message is in `error`:

```json
{
  "error": "the request goes above the host cap for vcpus",
  "code": "over_cap",
  "hint": "Ask for less, or use local VMs.",
  "requested": {"vcpus": 16, "ram_gb": 32.0, "disk_gb": 4.0},
  "used":      {"vcpus": 72, "ram_gb": 96.0, "disk_gb": 60.3},
  "cap":       {"vcpus": 80, "ram_gb": 102.4, "disk_gb": 100.0}
}
```

Without `--json`, the text has the same data as `requested: vcpus=16 ram_gb=32.0 disk_gb=4.0` lines. A value of `null` (text: `-`) is a resource with no cap. GB values are GiB.

The memory admission check of a local host (`[memory]` in `/etc/ltvm.conf`) also applies when the VM starts.

## Sleep and power

While a lease is active, `serve` keeps a detached process that stops idle sleep:

- macOS: `caffeinate -s`. It works only on AC power. It needs no privilege.
- Linux: `systemd-inhibit --what=sleep --mode=block sleep infinity`. The ltvm user runs from ssh or from the reap timer, and has no local session. So `install --serve` installs a polkit rule, `/etc/polkit-1/rules.d/50-ltvm-serve.rules`. The rule lets the user `ltvm` take `org.freedesktop.login1.inhibit-block-sleep`.
- WSL2: no action. Set the Windows power plan.

`serve` starts the guard before a create, and checks it after each verb. Reap stops it when no lease is active. A problem with the guard prints one warning and never fails a request. The guard log is `~ltvm/.local/state/ltvm/power-guard.log`.

Ubuntu 22.04 has polkit 0.105. This version reads `.pkla` files and not `rules.d`. So the rule does nothing there, and the guard cannot hold the lock. Add an equivalent `.pkla` file by hand on that release.

**Lid close.** A closed lid puts the host to sleep. Neither guard stops lid sleep. `caffeinate -s` does not stop it. By default, logind ignores sleep locks for the lid switch (`LidSwitchIgnoreInhibited=yes`). The VMs stop with the host and continue when the host wakes. The leases stay, but lease time continues while the host sleeps. The next reap destroys a VM whose lease expired during the sleep.

**AC only.** With `ac_only = true`, `create` and `cluster create` fail with `on_battery` while the host runs on battery. `serve` does not check `start`. The probe is `pmset -g batt` on macOS and `/sys/class/power_supply` on Linux. A host with no battery, or a probe that fails, counts as AC power. WSL2 usually shows no battery, so `ac_only` does nothing there.

## Owner controls

Run the owner commands as the dedicated user. The settings, the leases, the audit log and the jobs are in the home directory of that user. Only that user destroys remote VMs with no sudo.

```bash
sudo -u ltvm -H ltvm serve status            # leases, use against caps, last 10 requests
sudo -u ltvm -H ltvm serve status --recent 50 --json
sudo -u ltvm -H ltvm serve disable           # refuse all requests
sudo -u ltvm -H ltvm serve disable --destroy # also destroy every remote VM and cluster, and delete the jobs
sudo -u ltvm -H ltvm serve enable            # accept requests again
sudo -u ltvm -H ltvm serve init              # change the caps
```

- The text of `serve status` shows each control character in a name or a request argument as `\xNN`. So a request cannot fake a line or send an escape code to your terminal. The controller does the same for the error, hint and detail text of a host reply. `--json` and the output of the verb do not change.
- `serve disable` writes `enabled = false` and keeps the caps. `serve` then refuses every request with `disabled`, the probe of `host list` too.
- `serve disable --destroy` destroys each VM and cluster with a lease, and each remote VM with no lease. Then it deletes all jobs. A request that started before `disable` can still finish. Run `disable --destroy` again to remove what it made.
- A `serve.toml` that `serve` cannot read, or that is missing, refuses every request with `bad_config`.
- To remove a controller, delete its line in `~ltvm/.ssh/authorized_keys`.

`serve` writes one audit line for each request to `~ltvm/.local/state/ltvm/serve.log`. Each line has the time, the key fingerprint, the verb, the arguments and the result. `serve` opens the file in append mode, with mode 0600. A verb that kills `serve` leaves no line.

| Item | Path on the host |
|---|---|
| Settings | `~ltvm/.config/ltvm/serve.toml` |
| Leases | `~ltvm/.local/state/ltvm/leases.json` |
| Audit log | `~ltvm/.local/state/ltvm/serve.log` |
| Jobs (trees, builds) | `~ltvm/jobs/` |
| Artifacts | `~ltvm/artifacts/` |
| Remote VMs | `VM_DIR` (`/opt/qemu-vms`), with owner ID `remote:<key>` |

## Errors

Each refusal has a stable code. With `--json`, the code is in the `code` field and the message is in the `error` field. A refusal exits 1.

| Code | Meaning | What to do |
|---|---|---|
| `disabled` | The host owner disabled remote use of the host. | Use local VMs. |
| `version_mismatch` | The controller and the host use different protocol versions. | If the host is older, the owner runs `ltvm update` on the host. If the host is newer, update the controller. |
| `verb_refused` | The verb is not on the allow-list. | Run it locally. |
| `bad_request` | `serve` cannot read the request. | Fix the command. |
| `bad_reply` | The controller cannot read the reply. For example, the host has an ltvm with no `serve`. | Update the host. |
| `unknown_host` | The controller has no host with that name. | Run `ltvm host list`. |
| `layout_unsupported` | The host does not have the shared layout, or `serve` runs as root. A `create` or `cluster create` also gives it when the remote guest network is missing. | The owner runs `ltvm install` on the host, and `ltvm doctor` to check it. |
| `needs_root` | The request needs root on the host. | Use local VMs. |
| `path_refused` | A path argument is not allowed on a remote host. | Remove the option. Use `pull` for results. |
| `vm_not_found` | No VM that a remote request made has that name. | Check `ltvm --host <h> list`. |
| `cluster_not_found` | No cluster that a remote request made has that name. | Check `ltvm --host <h> cluster list`. |
| `name_in_use` | A VM or cluster that the host owner made has that name. | Use a different name. |
| `unreachable` | The controller cannot connect to the host. | Use local VMs. The ssh message is in the hint. |
| `timed_out` | The host did not finish the request in time. | Check `LTVM_REMOTE_TIMEOUT`. |
| `host_key_mismatch` | The host key is not the key that the controller pinned. | Stop. Ask the host owner. |
| `over_cap` | The request goes above a cap that the host owner set. | Ask for less, or use local VMs. |
| `bad_config` | `serve` cannot read `serve.toml`, or the file is missing. | The owner fixes the file, or runs `ltvm serve init`. |
| `on_battery` | The host runs on battery, and the owner allows new VMs only on AC power. | Use local VMs, or wait. |
| `podman_unsafe` | podman on the host is not rootless and not in a VM. | The owner fixes podman. |

The list of codes is `ERROR_CODES` in `ltvm_pkg/remote/protocol.py`.

## Security boundary

**A deploy runs code from the controller on the host.** A Lustre tree holds Makefiles and configure scripts. The allow-list limits which ltvm verbs run. It is not a sandbox. Two layers give the isolation:

- The build container. It is rootless podman on Linux and WSL2, or the podman machine VM on macOS. For a build, the host refuses podman that is not rootless and not in a VM (`podman_unsafe`). A native Lustre build under `serve` runs with `--network=none`.
- The dedicated user `ltvm`. It is not an admin, it has no password, and it cannot use sudo. It must not be able to read the files of the owner.

A container escape or a local privilege escalation on the host defeats this design. Connect only controllers that you trust to run code as a normal user on the host.

Other limits:

- **A key with no passphrase.** A person who steals the controller key can use the host. The allow-list, the scope rules and the caps still apply. The key works only from the `from=` address.
- **Root daemons stay.** socket_vmnet and dnsmasq run as root on macOS. The `ltvm` user does not change that.
- **No automatic update.** The controller never updates the host, because an update would let the controller replace the code that does the checks. A protocol version change stops all remote use until the owner updates the host.
- **The test switch.** For the exec:// test hosts, `LTVM_SERVE_TEST_LAYOUT=ready` turns off the layout check and the power guard. The `serve` command refuses a request that has the switch under sshd (`SSH_CONNECTION` is set). Do not add `LTVM_*` to `AcceptEnv` in `sshd_config`.
- **Guests share one root password.** Only the host reaches the guest subnets. Do not route a guest subnet to other machines.

**The remote guest network is isolated.** A remote guest cannot reach a guest of the owner. A guest of the owner cannot reach a remote guest. A remote guest also cannot reach the LAN or the internet. Remote guests reach each other and the host. Two parts give the isolation:

- Each network has its own L2 segment: its own bridge on Linux, its own vmnet interface on macOS. A guest cannot send a frame to the other network.
- The host does not route between the networks. On Linux, the nftables table `inet ltvm_remote` drops each routed packet into or out of `fcbr1`. A drop in an nftables base chain is final, so the ACCEPT rules of other tables (qemu-bridge.service, Docker, firewalld, ufw) cannot cancel it. If nft fails, `ltvm-remote-net.service` fails and `fcbr1` does not come up. On macOS, the pf anchor `com.apple/ltvm-remote` drops each packet between the remote subnets and any other address.

On macOS, vmnet does not give this guarantee. Apple documents no isolation between two vmnet networks. vmnet shared mode turns on IP forwarding (`net.inet.ip.forwarding=1`), and the host has an address on both networks. So expect the host to route between them without the pf anchor. The isolation on macOS thus depends on pf: on the anchor, and on pf staying enabled. A `pfctl -d` or a pf.conf with no `com.apple/*` anchor removes it.

Limits of the isolation:

- A remote guest can reach the services of the host on the remote gateway address. A guest of the owner can do the same on its gateway.
- Both dnsmasq instances read the same `hosts.d`. So a remote guest can resolve the names and addresses of owner VMs. It cannot reach them.

## Design decisions

**F1. Leases are separate from claims.** An expired claim means "another session can use the VM". An expired lease means "destroy the VM". `release --force` by any local user, and a renewed claim, would also clear a lease. So leases are in their own file, `leases.json`, and local claims do not change.

**F2. Full tree each deploy.** Each deploy sends the full tree (about 100 MB for lustre-release) and builds it in a new job. There is no git object cache on the host and no ccache between jobs. Add a cache only if the end-to-end test shows that the time to send or build is a problem.

## Known limits

These items are not proven on a real host yet. The end-to-end test on a macOS host and a WSL2 host will prove them.

- No request went over a real sshd. The tier-2 ssh tests (`tests/test_remote_ssh_tier2.py`) need a second local user and did not run.
- `install --serve` did not make a real user. No real launchd job or systemd timer ran.
- No real Lustre build, deploy, `pull` or `lctl dk` ran through `serve`. The tests replace the verb handlers.
- The power guard did not run on a real laptop. The lid-close behavior and the polkit rule are from documentation only.
- The controller builds the tree stream in memory, with no size limit.
- There is no isolation between controllers. Each paired controller has its own owner ID, but the scope check accepts any `remote:` owner. So any paired controller can see, use and destroy each remote VM and cluster on the host.
- A disk-writing verb that starts below the disk cap can go above it. `serve` checks the cap only before the verb runs.
- The build container can leave a link in a job tree that points outside the tree. The host reads some files of the tree after a build, such as `configure`, `config.h` and the kernel patch series. Those reads can follow such a link. The content goes to the build, not to the controller.
- Two parallel creates can both pass the cap check. `serve` takes no lock.
- A cross Lustre build has a network. It installs its toolchain and sysroot with dnf, zypper or apt, and then runs configure and make from the tree. Code in the tree can thus reach each address that the host reaches, also the guests of the owner. Those guests have the root password `initial0`. A native Lustre build runs with `--network=none`. A ZFS build also has a network for dnf or apt, but it builds an OpenZFS release, not code from the controller. `serve` refuses `--zfs-source`.
- A cluster cannot span hosts. The guest subnet is private to each host.
- The isolation of the remote guest network is not proven on real hosts. No real nft table, bridge `fcbr1`, second socket_vmnet or pf anchor was loaded. On macOS, these three facts are from documentation only:
  - pf drops the routed packets between the two vmnet bridges.
  - The pf enable reference stays after `pfctl -E` exits.
  - socket_vmnet host mode takes `--vmnet-gateway`.
- The controller does not stream verb output. A long deploy prints only at the end.
