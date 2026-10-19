# Remote VM hosts

## Goal

You run `ltvm` on one machine, the **controller**. The VMs run on a different machine, the **host**. An AI agent or a person on the controller can create VMs and clusters on the host. It can deploy a Lustre tree to them, run suites, and get the results back. The command is the same as for local VMs, with `--host <name>` added.

Often the host is a personal machine, and its owner also uses it for other work. So the design limits what the controller can do on the host, and it keeps the host's owner in control.

Supported hosts are the platforms that ltvm supports now: Linux, macOS on Apple Silicon, and WSL2. Users must follow their own organization's policy for the machines that they connect.

## Current behavior

- ltvm runs only on the machine where you install it. It has no host concept, no transport, and no daemon.
- QEMU starts locally, and ltvm controls it through a local QMP unix socket (`ltvm_pkg/qemu_run.py`).
- VM state is in `/opt/qemu-vms`. Artifacts are in `<repo>/artifacts/`.
- `deploy-lustre` builds the tree in a podman container on the local machine. It bind-mounts `--lustre-tree` and runs `git describe` on it. Then it streams the staging tree into the guest with `tar | ssh` (`ltvm_pkg/deploy.py`).
- Guests are on a private subnet. On macOS the subnet is vmnet `192.168.105.0/24`. On Linux it is the `fcbr0` bridge. Only the local machine can reach the subnet.
- All guests have the root password `initial0`. ltvm turns off SSH host-key checks for guests. This setup is safe only because no other machine can reach the guest subnet.
- On a host with the shared layout, members of the `ltvm` group run the full VM lifecycle with no root. On the classic layout, `cluster create` and `cluster destroy` need sudo (`ltvm_pkg/rootless.py`).
- `create` and `start` already refuse a VM when the RAM of the running VMs plus the new VM is more than the host budget (`qemu_run._memory_shortfall`, `[memory]` in `/etc/ltvm.conf`).
- Claims (`ltvm claim`, `ltvm_pkg/vm_claim.py`) and the advisory `owner_id` already record which session uses a VM.

## Proposed behavior

### Overview

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
                                               check protocol version
                                               check verb against allow-list
                                               check caps and leases
                                               run the verb, write the audit log
  <- stdout, stderr, exit status, JSON errors
```

### Commands

Controller side:

| Command | Action |
|---|---|
| `ltvm host add <name> ssh://ltvm@<addr> --hostkey <fp>` | Make a key for this host, pin the host key, write the entry, and print the `serve authorize` line for the host owner. |
| `ltvm host list` | Show the hosts, their reachability, and their protocol versions. |
| `ltvm host remove <name>` | Delete the entry and the key. Print the line that the host owner must delete. |
| `ltvm --host <name> <verb> ...` | Run a verb on the host. `LTVM_HOST=<name>` does the same. |
| `ltvm --host <name> pull <job\|vm> <what>` | Copy suite results, crash dumps, or debug logs from the host. |

Host side:

| Command | Action |
|---|---|
| `./ltvm install --serve` | Do the normal install, create the `ltvm` user, run the `serve init` wizard, check sshd, and print the steps that are left. |
| `ltvm serve init` | Wizard that writes `serve.toml`. |
| `ltvm serve authorize '<pubkey>' --from <ip>` | Write one restricted line to `authorized_keys`. |
| `ltvm serve status` | Show leases, resource use, and the most recent requests. |
| `ltvm serve disable [--destroy]` | Refuse all requests. With `--destroy`, also destroy all VMs that remote requests made. |
| `ltvm serve enable` | Accept requests again. |
| `ltvm serve --stdio` | Forced command. A person does not run it. |

The name `pull` prevents a collision with `ltvm target fetch`, which already exists.

On the controller, a remote VM has the name `<host>/<vm>`. So a remote VM cannot collide with a local VM.

### Host bootstrap

A person at the host runs `./ltvm install --serve` as an admin user. The command does these steps:

1. The normal `ltvm install` steps: QEMU, network, socket_vmnet or bridge, and the `ltvm` group.
2. It creates the dedicated user `ltvm`.
   - macOS: `sysadminctl`, a hidden user with no password, not in the `admin` group.
   - Linux and WSL2: `useradd`, with the password locked.
   - The shell is `/bin/sh`, because the SSH forced command needs a shell.
   - The user is a member of the `ltvm` group. So it runs the VM lifecycle with no root on the shared layout.
   - The user gets its own artifacts directory, `~ltvm/artifacts` (`LTVM_ARTIFACTS_DIR`), because it cannot write to the admin's checkout.
3. It runs `ltvm serve init` as the `ltvm` user. See [Caps](#caps-and-admission-control).
4. It checks that sshd runs and that it accepts the `ltvm` user.
   - macOS: it tells the person how to turn on Remote Login for the `ltvm` user only.
   - WSL2: it checks sshd inside the distro and prints the WSL2 steps.
5. It prints the `ltvm host add ... --hostkey <fp>` line for the person to paste on the controller.

ltvm does not install or configure a VPN. The docs recommend Tailscale with an ACL: only `tag:ltvm-controller` can reach `tag:ltvm-host` on port 22. On WSL2, install Tailscale inside the distro. Then the Windows NAT and port proxy have no part in the path.

### Pairing

Each side pins the other. No person edits `authorized_keys` by hand.

1. The host prints `ltvm host add <name> ssh://ltvm@<addr> --hostkey <fp>`. The person pastes it on the controller. The controller trusts only that host key.
2. `host add` makes an ed25519 key at `~/.config/ltvm/keys/<name>`. The key has no passphrase, because agents use it with no person present. `host add` prints `ltvm serve authorize '<pubkey>' --from <controller ip>`.
3. The person pastes that line on the host. `serve authorize` writes this line, and the restrict options are always present:

   ```
   restrict,command="ltvm serve --stdio",from="<controller ip>" ssh-ed25519 AAAA... ltvm:<controller>
   ```

To revoke the controller, delete its line. `ltvm host remove` prints the line to delete.

We do not use FIDO or Secure Enclave keys. These keys need a touch for each use, and a touch blocks autonomous agents.

### `serve` and the allow-list

`ltvm serve --stdio` reads `SSH_ORIGINAL_COMMAND`. It does not pass the command to a shell. It parses the command into an argument vector and checks it.

`serve` refuses to start if the host does not have the shared layout. It never calls `sudo`. A verb that needs root, such as a VM with a `passthrough` NIC, is refused.

1. The first message is a version exchange. If the protocol versions are different, `serve` stops. The controller prints the update command for the host owner. The controller never updates the host, because an update would let the controller replace the code that does the checks.
2. The verb must be on the allow-list.
3. Arguments that are paths must resolve inside the job directory or the VM state for this request.
4. Resource requests go through admission control.
5. `serve` writes one line to `~/.local/state/ltvm/serve.log` for each request: time, key fingerprint, verb, arguments, and result.

Allowed verbs:

- `create`, `destroy`, `start`, `stop`, `list`
- `vm` subcommands that inspect, snapshot, or collect a crash
- `cluster`
- `target fetch`, `target list`, `target show`, `build`, into the `ltvm` user's artifacts directory
- `deploy-lustre`, but only with a tree from `receive-tree`
- `llmount`, `llumount`
- `suite`
- `ssh <vm> <cmd>`, which runs a command in a guest
- `receive-tree`, `pull`, `lease`, `status`

Refused verbs:

- `install`, `update`, `skills`, `doctor --fix`, `clean`. These verbs need sudo or change the host's ltvm.
- `make-install`, `make-uninstall`, `make-reinstall`. These verbs act on the host itself, not on a guest.
- `target delete --remote`, `target publish`.
- `telemetry`. `serve` forces telemetry off for all remote requests.
- Any verb that is not on the allow-list. A new verb is refused until someone adds it to the list.

### Remote build and deploy

The build runs on the host. The controller and the host can have different architectures. For example, an aarch64 Mac cannot build natively for an x86_64 guest on a WSL2 host.

1. On the controller, `ltvm --host X deploy-lustre --lustre-tree <path>` makes a tar stream. The stream holds:
   - the files that `git ls-files` lists
   - untracked files that git does not ignore
   - the git metadata that `git describe` needs
2. `serve receive-tree <job>` unpacks the stream into `~ltvm/jobs/<job>/tree`.
3. `serve` runs the normal `deploy-lustre` on that directory.

**Security boundary.** A Lustre tree holds Makefiles and configure scripts. A deploy runs code that the controller sends. The forced command limits which ltvm verbs run, but it is not a sandbox. Two layers give the isolation:

- the build container: the podman machine VM on macOS, or rootless podman on Linux and WSL2
- the `ltvm` user, which is not an admin and cannot read the owner's files

`serve` refuses a remote build if podman is not rootless and not VM-backed.

### Guest access

The controller reaches guests only through `serve`: `ssh <vm> <cmd>`, `suite`, `llmount`, and `pull`. The design does not allow SSH ProxyJump or port forwards. The guests share the root password `initial0`, so only `serve` must be able to reach them.

### Results

The command `ltvm --host X pull <job|vm> <what>` copies results to the controller.

The `serve` command accepts 4 values of `<what>`: `suite`, `crash`, `dk`, and `console`. It reads only from the job directory or the VM state for that request. The `pull` verb does not accept a free path.

### Caps and admission control

`serve init` finds the host's CPUs, RAM, and free disk. It suggests a value for each cap. The person presses Enter to accept a value.

| Cap | Default |
|---|---|
| vCPUs for VMs | 80 % of logical CPUs |
| RAM for VMs | 80 % of physical RAM |
| Disk for jobs and images | the smaller of 100 GB and 25 % of free space |
| Default lease | 8 h |
| Run only on AC power | yes on a laptop. The wizard does not ask on a desktop. |

`--yes` accepts all defaults. Flags such as `--max-vcpus N` set one value. These options let a person script the setup. The caps are in `~ltvm/.config/ltvm/serve.toml`.

The RAM cap uses the memory admission check that exists now. `serve` adds the same type of check for vCPUs and disk.

There is no cap on the number of VMs. For example, the vCPU cap is 80. Then the controller can make 80 VMs with 1 vCPU each. Or it can make 10 VMs with 8 vCPUs each. RAM and disk must also be sufficient.

`serve` refuses a request at once if the request goes above a cap. It does not put the request in a queue. The error has the requested, used, and free value for each resource, as text and as JSON:

```json
{"error": "over_cap",
 "requested": {"vcpus": 16, "ram_gb": 32, "disk_gb": 40},
 "used":      {"vcpus": 72, "ram_gb": 96, "disk_gb": 60},
 "cap":       {"vcpus": 80, "ram_gb": 102, "disk_gb": 100}}
```

An agent can then ask for a smaller cluster, or use local VMs.

### Leases

Each VM or cluster that a remote request makes gets a lease. The default is 8 h. `ltvm --host X lease renew <vm|cluster> [--for 4h]` extends a lease. A launchd job or systemd timer runs `serve reap`. It destroys VMs whose lease has expired. So a controller that crashes does not leave VMs on the host.

### Sleep

While a lease is active, `serve` stops the host from going to sleep.

- macOS: `caffeinate -s`, which works only on AC power.
- Linux: `systemd-inhibit`.
- WSL2: no action. The docs tell the person to set the Windows power plan.

If the lid closes, or if the host goes to battery and the AC-only cap is set, the host goes to sleep. The leases stay. The VMs continue after the host wakes.

### Owner controls

The host owner always has control:

- `ltvm serve disable --destroy` stops all remote use at once.
- `ltvm serve status` shows who uses the host and how much.
- `serve.log` is append-only. It records each request.
- To revoke a controller, delete its `authorized_keys` line.

## Design forks

**F1. Leases and claims.** ltvm has claims now (`ltvm claim`). Options:
- (a) Make a lease a claim with an expiry time.
- (b) Keep leases separate from claims.

Recommendation: (a), if the claim code can hold an expiry time without a change to local behavior. Read `ltvm_pkg/cli/claim.py` before step 5.

**F2. Tree transfer size.** A full send of a lustre-release tree for each deploy is about 100 MB or more. Options:
- (a) Send the full tree each time.
- (b) Keep a git object cache on the host, and send only the missing objects with `git bundle`.

Recommendation: (a) first, and measure it. Add (b) only if the time to send is a problem on the end-to-end test.

## Risks and limits

- **A deploy runs code from the controller.** The build container and the `ltvm` user are the isolation. A container escape or a local privilege escalation on the host defeats this design.
- **Root daemons stay on the host.** socket_vmnet and dnsmasq run as root on macOS. The `ltvm` user does not remove them.
- **A key with no passphrase.** A person who steals the controller key can use the host. The allow-list and the caps still apply. The key works only from the `from=` address.
- **One cluster on one host.** The guest subnet is private to each host. A cluster cannot span hosts. Clusters across hosts are a follow-up.
- **No automatic update.** The protocol version check stops all remote use until a person updates the host.

## Test plan

1. **Automated, on any Linux CI host.** A second local user acts as the remote host on `localhost`, with its own `authorized_keys`. Tests run the full controller-to-host path.
2. **Adversarial tests of `serve`.** Each case must fail:
   - a verb that is not on the allow-list
   - shell metacharacters in arguments
   - a `pull` path that tries to leave the job directory with `..` or with a symlink in the tree
   - a different protocol version
   - a request above a cap
   - a request while `serve` is disabled
3. **Manual, end-to-end.**
   - a macOS host on Apple Silicon (aarch64 guests)
   - a WSL2 host (x86_64 guests)
   - Each run creates a cluster, deploys a tree, mounts Lustre, runs a suite, pulls the results, and lets a lease expire.

## Delivery

The work has 6 changes, in this order:

1. `serve` and the allow-list
2. `host add` and the `--host` transport
3. `receive-tree` and remote build and deploy
4. `pull`
5. leases, caps, and sleep control
6. docs and an update to the ltvm skill

We open no pull request until the full feature passes the end-to-end tests on both hosts.

## Status

UNBUILT. This document is the design only.
