"""Remote hosts: clusters, artifacts, and the guards of child ltvm processes.

The child host (exec://) runs `serve` as a separate process, so these
tests use cluster dry runs and the read-only artifact verbs there.  The
in-process host (`inproc`, from test_remote_lifecycle) runs the verbs
that change VM state, with the conftest isolation in place.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

# The real function: conftest replaces the module attribute in each test.
from ltvm_pkg.update_check import maybe_check_for_updates as _real_update_check
from tests.test_ltvm_cli import _load_ltvm


@pytest.fixture(scope="module")
def ltvm() -> Any:
    return _load_ltvm()


# ── guards that child ltvm processes inherit ─────────────


class TestChildGuards:
    def test_no_sudo_env_forbids_sudo_in_run(
        self, ltvm: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        from ltvm_pkg import priv

        monkeypatch.setattr(priv, "_sudo_forbidden", False)
        monkeypatch.setenv("LTVM_NO_SUDO", "1")
        ltvm.run(["list", "--json"])
        assert priv._sudo_forbidden is True
        with pytest.raises(priv.SudoForbidden):
            priv.sudo_run(["true"])

    def test_without_the_env_run_leaves_sudo_allowed(
        self, ltvm: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        from ltvm_pkg import priv

        monkeypatch.setattr(priv, "_sudo_forbidden", False)
        ltvm.run(["list", "--json"])
        assert priv._sudo_forbidden is False

    def test_forbid_sudo_sets_the_env_for_children(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg import priv

        monkeypatch.setattr(priv, "_sudo_forbidden", False)
        priv.forbid_sudo()
        assert os.environ.get("LTVM_NO_SUDO") == "1"

    def test_update_check_env_stops_the_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg import update_check

        def sentinel() -> None:
            raise AssertionError("the update check read its config")

        monkeypatch.setattr(update_check, "_load_config", sentinel)
        monkeypatch.setenv("LTVM_UPDATE_CHECK", "0")
        _real_update_check(force=True)

    def test_child_ltvm_is_this_script_when_serve_set_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg import paths

        monkeypatch.setenv("LTVM_SELF", "/opt/ltvm/ltvm")
        assert paths.ltvm_command() == [sys.executable, "/opt/ltvm/ltvm"]


# ── the exec:// transport ────────────────────────────────


def test_exec_transport_stops_a_hung_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ltvm_pkg.remote import hosts, transport

    hung = tmp_path / "hung-serve"
    hung.write_text("import time\ntime.sleep(60)\n")
    hosts.add(hosts.Host(name="h", url=f"exec://{hung}"))
    monkeypatch.setattr(transport.ExecTransport, "timeout", 0.5)
    resp = hosts.transport_for("h").request(["list"])
    assert resp.error is not None
    assert resp.error.code == "timed_out"
