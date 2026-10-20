"""`install --serve`: set up the remote guest network (see guest_net).

Linux and WSL2:

- ``ltvm-remote-net.service`` loads an nftables table, then makes the
  bridge fcbr1 with the remote gateway address.  The table drops each
  routed packet that goes into fcbr1 or comes out of it.  A drop in an
  nftables base chain is final, so an ACCEPT rule in another table
  (qemu-bridge.service, Docker, firewalld, ufw) cannot cancel it.  The
  unit loads the table first.  If nft fails, the unit fails and the
  bridge does not come up.  There is no NAT for the remote network.
- ``ltvm-dnsmasq-remote.service`` serves DNS on the remote gateway.
- /etc/qemu/bridge.conf allows fcbr1, for the bridge helper.

macOS:

- A second socket_vmnet in vmnet host mode (no NAT) on its own socket.
- A pf anchor under ``com.apple/``, which the default /etc/pf.conf
  evaluates.  It drops each packet between the remote subnets and any
  other address.  vmnet does not document isolation between two vmnet
  networks, and the host forwards IP packets when vmnet shared mode
  runs.  So the anchor is the guarantee, not vmnet.
- A second dnsmasq on the remote gateway, and its reload job.

install() writes each file with write and runs each command with run.
The caller gives both, so the tests stub all of them.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

from ltvm_pkg import guest_net
from ltvm_pkg.guest_net import GuestNet

WriteFn = Callable[[Path, str, int], None]
RunFn = Callable[..., object]
InUseFn = Callable[[str], bool]

SYSTEMD_DIR = Path("/etc/systemd/system")
ETC_LTVM = Path("/etc/ltvm")
NFT_FILE = ETC_LTVM / "remote-net.nft"
NFT_TABLE = "ltvm_remote"
DNSMASQ_CONF_LINUX = ETC_LTVM / "dnsmasq-remote.conf"
DNSMASQ_PID_LINUX = Path("/run/ltvm-dnsmasq-remote.pid")
HOST_DNSMASQ_DIR = Path("/etc/dnsmasq.d")
HOST_DNSMASQ_EXCEPT = HOST_DNSMASQ_DIR / "ltvm-fcbr1.conf"

PF_ANCHOR = "com.apple/ltvm-remote"
PF_CONF = Path("/usr/local/etc/ltvm-remote-pf.conf")
DNSMASQ_CONF_MACOS = Path("/usr/local/etc/ltvm-dnsmasq-remote.conf")
DNSMASQ_PID_MACOS = Path("/var/run/ltvm-dnsmasq-remote.pid")
SOCKET_GROUP = "ltvm"

# Where to look for a free remote /24 on Linux: the default, then a
# range away from the owner candidates (192.168.200-254).
_LINUX_CANDIDATES = [guest_net.REMOTE_SUBNET_LINUX] + [
    f"192.168.{n}" for n in range(150, 200)
]


# A record in VM_DIR is trusted only when this user owns it.  VM_DIR is
# writable by group ltvm, and the sticky bit stops a member only from
# the replacement of a file that root owns.
TRUSTED_UID = 0
SUBNET_RECORD = "remote-subnet"
EXTRA_SUBNET_RECORD = "remote-extra-subnet"


def _root_record(path: Path) -> str | None:
    """The prefix in a record that root wrote, or None."""
    from ltvm_pkg import vm_state

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != TRUSTED_UID
            or st.st_nlink != 1
        ):
            return None
        try:
            return vm_state.check_prefix24(f.read().strip(), str(path))
        except (OSError, ValueError):
            return None


def _env_or_record(env: str, record: Path) -> str | None:
    from ltvm_pkg import vm_state

    value = os.environ.get(env)
    if value:
        return vm_state.check_prefix24(value, env)
    return _root_record(record)


def _installed_net(subnet: str, extra_subnet: str) -> GuestNet:
    from ltvm_pkg import vm_state

    subnet6 = os.environ.get("LTVM_REMOTE_EXTRA_SUBNET6")
    if subnet6:
        vm_state._validate_subnet6(subnet6, "LTVM_REMOTE_EXTRA_SUBNET6")
    return GuestNet(
        kind="remote",
        subnet=subnet,
        extra_subnet=extra_subnet,
        extra_subnet6=subnet6 or guest_net.REMOTE_EXTRA_SUBNET6,
        bridge=guest_net.REMOTE_BRIDGE,
    )


def _remote_net(
    write: WriteFn, choose: Callable[[str | None], str]
) -> GuestNet:
    """The remote network from the env, root records and defaults.

    The installer never reads a VM_DIR record that root did not write.
    It writes both records as root, so that serve reads the values that
    went into the root config.
    """
    from ltvm_pkg import vm_state

    vm_dir = Path(vm_state.VM_DIR)
    subnet = choose(
        _env_or_record("LTVM_REMOTE_SUBNET", vm_dir / SUBNET_RECORD)
    )
    extra = _env_or_record(
        "LTVM_REMOTE_EXTRA_SUBNET", vm_dir / EXTRA_SUBNET_RECORD
    )
    net = _installed_net(subnet, extra or guest_net.REMOTE_EXTRA_SUBNET)
    write(vm_dir / SUBNET_RECORD, net.subnet + "\n", 0o644)
    write(vm_dir / EXTRA_SUBNET_RECORD, net.extra_subnet + "\n", 0o644)
    return net


def _sbin(name: str) -> str:
    """The absolute path of a root tool, for a unit that runs no shell."""
    found = shutil.which(name, path="/usr/sbin:/sbin:/usr/bin:/bin")
    return found or f"/usr/sbin/{name}"


# ── Linux ────────────────────────────────────────────────


def nft_rules(bridge: str = guest_net.REMOTE_BRIDGE) -> str:
    """The table that stops all forwarding into and out of the bridge.

    Traffic between two remote guests is bridged, not routed.  The
    ``!=`` keeps it when br_netfilter shows bridged packets to this hook.
    """
    return (
        "# Written by ltvm install --serve.  The remote guests must not\n"
        "# reach the owner guests, the LAN or the internet.\n"
        f"table inet {NFT_TABLE}\n"
        f"delete table inet {NFT_TABLE}\n"
        f"table inet {NFT_TABLE} {{\n"
        "\tchain forward {\n"
        "\t\ttype filter hook forward priority -10; policy accept;\n"
        f'\t\tiifname "{bridge}" oifname != "{bridge}" drop\n'
        f'\t\toifname "{bridge}" iifname != "{bridge}" drop\n'
        "\t}\n"
        "}\n"
    )


def net_unit(net: GuestNet) -> str:
    br = net.bridge
    ip, nft = _sbin("ip"), _sbin("nft")
    return (
        "[Unit]\n"
        f"Description=ltvm remote guest bridge {br}, isolated\n"
        f"Before={guest_net.REMOTE_DNSMASQ_UNIT}\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        # The table first: the bridge never comes up without it.  No
        # shell: "-" lets systemd ignore the error when the bridge is
        # there already, or is gone at stop.
        f"ExecStart={nft} -f {NFT_FILE}\n"
        f"ExecStart=-{ip} link add name {br} type bridge\n"
        f"ExecStart={ip} addr replace {net.gateway}/24 dev {br}\n"
        f"ExecStart={ip} link set {br} up\n"
        f"ExecStop=-{ip} link del {br}\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def dnsmasq_conf_linux(net: GuestNet, hosts_dir: Path, user: str | None) -> str:
    text = (
        "# ltvm dnsmasq for the remote guest bridge.  It reads this file\n"
        "# only.  No upstream server: the remote guests have no internet.\n"
        f"interface={net.bridge}\n"
        f"listen-address={net.gateway}\n"
        "bind-dynamic\n"
        "except-interface=lo\n"
        "no-resolv\n"
        "leasefile-ro\n"
        f"hostsdir={hosts_dir}\n"
    )
    if user:
        text += f"user={user}\n"
    return text


def dnsmasq_unit_linux(dnsmasq: str) -> str:
    conf, pid = DNSMASQ_CONF_LINUX, DNSMASQ_PID_LINUX
    return (
        "[Unit]\n"
        "Description=ltvm DNS for the remote guest bridge\n"
        f"After=network.target {guest_net.REMOTE_NET_UNIT}\n"
        f"Requires={guest_net.REMOTE_NET_UNIT}\n"
        "\n"
        "[Service]\n"
        "Type=forking\n"
        f"PIDFile={pid}\n"
        f"ExecStartPre={dnsmasq} --test --conf-file={conf}\n"
        f"ExecStart={dnsmasq} --conf-file={conf} --pid-file={pid}\n"
        "ExecReload=/bin/kill -HUP $MAINPID\n"
        "Restart=on-failure\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def choose_subnet(recorded: str | None, owner: str, in_use: InUseFn) -> str:
    """A free /24 for the remote bridge, not the owner one."""
    for candidate in [c for c in [recorded] if c] + _LINUX_CANDIDATES:
        if candidate == owner or in_use(candidate):
            continue
        return candidate
    raise RuntimeError(
        "no free /24 for the remote guest network in 192.168.101 or "
        "192.168.150-199.  Set it in VM_DIR/remote-subnet."
    )


def _in_use(subnet: str) -> bool:
    from ltvm_pkg import host_setup

    hit = host_setup._subnet_in_use(subnet)
    return hit is not None and hit[0] != guest_net.REMOTE_BRIDGE


def _bridge_acl(write: WriteFn, bridge: str) -> None:
    from ltvm_pkg import rootless

    acl = rootless.BRIDGE_ACL
    if rootless.bridge_allowed(bridge, acl):
        return
    if rootless.bridge_denied(bridge, acl):
        raise RuntimeError(f"{acl} denies {bridge}")
    try:
        text = acl.read_text()
    except OSError:
        text = ""
    if text and not text.endswith("\n"):
        text += "\n"
    write(acl, text + f"allow {bridge}\n", 0o644)


def _install_linux(
    write: WriteFn, run: RunFn, in_use: InUseFn, dnsmasq: str | None
) -> GuestNet:
    from ltvm_pkg import host_firewall, vm_state

    vm_dir = Path(vm_state.VM_DIR)

    def choose(given: str | None) -> str:
        # The env wins, as in guest_net.remote().  Otherwise keep the
        # root record, or use a free /24.
        if os.environ.get("LTVM_REMOTE_SUBNET") and given:
            return given
        return choose_subnet(given, vm_state.SUBNET, in_use)

    net = _remote_net(write, choose)
    dnsmasq = dnsmasq or shutil.which("dnsmasq") or "/usr/sbin/dnsmasq"
    try:
        import pwd

        pwd.getpwnam("dnsmasq")
        user: str | None = "dnsmasq"
    except KeyError:
        user = None
    write(NFT_FILE, nft_rules(net.bridge), 0o644)
    write(SYSTEMD_DIR / guest_net.REMOTE_NET_UNIT, net_unit(net), 0o644)
    write(
        DNSMASQ_CONF_LINUX,
        dnsmasq_conf_linux(net, vm_dir / "hosts.d", user),
        0o644,
    )
    write(
        SYSTEMD_DIR / guest_net.REMOTE_DNSMASQ_UNIT,
        dnsmasq_unit_linux(dnsmasq),
        0o644,
    )
    if HOST_DNSMASQ_DIR.is_dir():
        # Keep the dnsmasq of the host off the remote bridge.
        write(
            HOST_DNSMASQ_EXCEPT,
            "# Written by ltvm: fcbr1 is served by "
            f"{guest_net.REMOTE_DNSMASQ_UNIT}.\n"
            f"except-interface={net.bridge}\n",
            0o644,
        )
    _bridge_acl(write, net.bridge)
    run(["systemctl", "daemon-reload"], check=True)
    run(
        [
            "systemctl",
            "enable",
            "--now",
            guest_net.REMOTE_NET_UNIT,
            guest_net.REMOTE_DNSMASQ_UNIT,
        ],
        check=True,
    )
    # A firewall manager gets host input from the bridge (DNS) only.
    host_firewall.trust_bridge(net.bridge, route=False)
    return net


# ── macOS ────────────────────────────────────────────────


def pf_rules(net: GuestNet) -> str:
    """The anchor that keeps the remote subnets to themselves.

    Host to guest traffic has the source address of the remote gateway,
    which is in the table, so serve still reaches the guests.
    """
    return (
        "# Written by ltvm install --serve.  The remote guests must not\n"
        "# reach the owner guests, the LAN or the internet.\n"
        "table <ltvm_remote> const { "
        f"{net.subnet}.0/24, {net.extra_subnet}.0/24, "
        f"{net.extra_subnet6}::/64 }}\n"
        "block drop quick from <ltvm_remote> to ! <ltvm_remote>\n"
        "block drop quick from ! <ltvm_remote> to <ltvm_remote>\n"
    )


def _plist(job: dict[str, object]) -> str:
    return plistlib.dumps(job).decode()


def macos_jobs(
    net: GuestNet,
    *,
    socket_vmnet: str,
    socket: Path,
    dnsmasq: str,
    hosts_dir: Path,
) -> dict[str, str]:
    """The LaunchDaemon plists, by label, in the order to load them."""
    return {
        guest_net.PF_REMOTE_LABEL: _plist(
            {
                "Label": guest_net.PF_REMOTE_LABEL,
                # -E takes a pf enable reference, as pf.conf asks.
                "ProgramArguments": [
                    "/sbin/pfctl",
                    "-E",
                    "-a",
                    PF_ANCHOR,
                    "-f",
                    str(PF_CONF),
                ],
                "RunAtLoad": True,
                "StandardErrorPath": "/var/log/ltvm-pf-remote.log",
            }
        ),
        guest_net.SOCKET_VMNET_REMOTE_LABEL: _plist(
            {
                "Label": guest_net.SOCKET_VMNET_REMOTE_LABEL,
                "ProgramArguments": [
                    socket_vmnet,
                    "--vmnet-mode=host",
                    f"--vmnet-gateway={net.gateway}",
                    f"--socket-group={SOCKET_GROUP}",
                    str(socket),
                ],
                "RunAtLoad": True,
                "KeepAlive": True,
                "UserName": "root",
                "StandardErrorPath": "/var/log/socket_vmnet-remote/stderr",
                "StandardOutPath": "/var/log/socket_vmnet-remote/stdout",
            }
        ),
        guest_net.DNSMASQ_REMOTE_LABEL: _plist(
            {
                "Label": guest_net.DNSMASQ_REMOTE_LABEL,
                "ProgramArguments": [
                    dnsmasq,
                    "--keep-in-foreground",
                    f"--conf-file={DNSMASQ_CONF_MACOS}",
                    f"--pid-file={DNSMASQ_PID_MACOS}",
                ],
                "RunAtLoad": True,
                "KeepAlive": True,
                "StandardErrorPath": "/var/log/ltvm-dnsmasq-remote/stderr",
                "StandardOutPath": "/var/log/ltvm-dnsmasq-remote/stdout",
            }
        ),
        guest_net.DNSMASQ_REMOTE_RELOAD_LABEL: _plist(
            {
                "Label": guest_net.DNSMASQ_REMOTE_RELOAD_LABEL,
                "ProgramArguments": [
                    "/bin/launchctl",
                    "kill",
                    "SIGHUP",
                    f"system/{guest_net.DNSMASQ_REMOTE_LABEL}",
                ],
                "WatchPaths": [str(hosts_dir)],
            }
        ),
    }


def dnsmasq_conf_macos(net: GuestNet, hosts_dir: Path) -> str:
    return (
        "# ltvm dnsmasq for the remote guest network.  No upstream\n"
        "# server: the remote guests have no internet.\n"
        f"listen-address={net.gateway}\n"
        "bind-interfaces\n"
        "port=53\n"
        "no-resolv\n"
        f"addn-hosts={hosts_dir}\n"
    )


def _install_macos(
    write: WriteFn,
    run: RunFn,
    socket_vmnet: str | None,
    dnsmasq: str | None,
) -> GuestNet:
    from ltvm_pkg import host_setup, vm_state

    net = _remote_net(
        write, lambda given: given or guest_net.REMOTE_SUBNET_MACOS
    )
    if socket_vmnet is None:
        found = host_setup.socket_vmnet_path()
        if found is None:
            raise RuntimeError("socket_vmnet is not installed")
        socket_vmnet = str(found)
    if dnsmasq is None:
        found = host_setup._brew_dnsmasq_bin()
        if found is None:
            raise RuntimeError("dnsmasq is not installed")
        dnsmasq = str(found)
    hosts_dir = Path(vm_state.VM_DIR) / "hosts.d"
    socket = guest_net.remote_vmnet_socket()
    write(PF_CONF, pf_rules(net), 0o644)
    write(DNSMASQ_CONF_MACOS, dnsmasq_conf_macos(net, hosts_dir), 0o644)
    run(
        [
            "mkdir",
            "-p",
            str(socket.parent),
            "/var/log/socket_vmnet-remote",
            "/var/log/ltvm-dnsmasq-remote",
        ],
        check=True,
    )
    jobs = macos_jobs(
        net,
        socket_vmnet=socket_vmnet,
        socket=socket,
        dnsmasq=dnsmasq,
        hosts_dir=hosts_dir,
    )
    for label, text in jobs.items():
        path = guest_net.launchd_plist(label)
        write(path, text, 0o644)
        # bootstrap refuses a job that is loaded.  Remove the old one.
        run(["launchctl", "bootout", f"system/{label}"], check=False)
        run(["launchctl", "bootstrap", "system", str(path)], check=True)
    return net


def install(
    *,
    system: str,
    write: WriteFn,
    run: RunFn,
    in_use: InUseFn | None = None,
    socket_vmnet: str | None = None,
    dnsmasq: str | None = None,
) -> str:
    """Set up the remote guest network.  Return a line for the report."""
    if system == "Darwin":
        net = _install_macos(write, run, socket_vmnet, dnsmasq)
        where = f"socket_vmnet host mode, pf anchor {PF_ANCHOR}"
    elif system == "Linux":
        net = _install_linux(write, run, in_use or _in_use, dnsmasq)
        where = f"bridge {net.bridge}, nft table {NFT_TABLE}"
    else:
        raise ValueError(f"no remote guest network for {system}")
    return f"installed ({net.subnet}.0/24, {where})"
