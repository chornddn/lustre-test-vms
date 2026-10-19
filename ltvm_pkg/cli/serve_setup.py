"""`ltvm serve init` and the `install --serve` step on a remote host."""

from __future__ import annotations

import argparse
import os
import platform
import sys
from dataclasses import asdict
from pathlib import Path

from ltvm_pkg.cli.util import EXIT_OK, _error, _output
from ltvm_pkg.remote import serve_config, serve_init


def host_facts() -> serve_init.HostFacts:
    """The facts of this host that the wizard uses."""
    from ltvm_pkg import vm_state

    return serve_init.probe(
        [Path.home(), Path(vm_state.VM_DIR)],
        laptop=serve_init.is_laptop(platform.system()),
    )


def cmd_serve_init(args: argparse.Namespace) -> int:
    """Ask for the caps and write serve.toml of this user."""
    facts = host_facts()
    # JSON output has no room for questions.
    cfg = serve_init.wizard(
        facts,
        overrides=serve_init.overrides_from(args),
        yes=args.yes or args.json,
        stdin=sys.stdin,
        stdout=sys.stdout,
    )
    path = serve_config.config_path(os.environ)
    try:
        serve_config.save(os.environ, cfg)
    except OSError as e:
        return _error(f"cannot write {path}: {e}", args.json)
    if args.json:
        _output({"path": str(path), "config": asdict(cfg)}, True)
        return EXIT_OK
    print(f"\nWrote {path}:\n")
    for line in serve_config.dumps(cfg).splitlines():
        print(f"  {line}")
    return EXIT_OK


def install_serve(args: argparse.Namespace) -> int:
    """The --serve steps of `ltvm install` (after the normal install)."""
    from ltvm_pkg import host_setup
    from ltvm_pkg.remote import serve_install

    deps = serve_install.Deps(wsl2=host_setup.is_wsl2())
    try:
        # With --json, stdout holds only the JSON report.
        report = serve_install.install(
            deps,
            serve_init.cap_argv(args),
            stdout=sys.stderr if args.json else sys.stdout,
            quiet=args.json,
        )
    except (serve_install.ServeInstallError, RuntimeError) as e:
        return _error(f"install --serve: {e}", args.json)
    if args.json:
        _output(report, True)
    return EXIT_OK
