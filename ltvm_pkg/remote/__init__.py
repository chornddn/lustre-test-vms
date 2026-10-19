"""Remote VM hosts: run ltvm verbs on another machine over SSH.

The controller is the machine that gives the commands.  The host is the
machine that runs the VMs.  docs/REMOTE_HOSTS.md has the design.

Modules:

- protocol: the protocol version, the error codes and the handshake.
- hosts: the host registry on the controller.
- transport: send one request to `serve` and read the reply.
- client: the `--host` dispatch on the controller.
- serve: the `ltvm serve --stdio` entry point on the host.
- allowlist: the one table of verbs that `serve` runs.
- audit: the append-only request log on the host.
- scope: keep a request to the VMs that remote requests made.
- names: the `<host>/<vm>` names on the controller.
"""
