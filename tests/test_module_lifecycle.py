"""Verify that the kernel module can be loaded and unloaded cleanly.

The `unloaded_module` fixture owns the module's load state: it unloads any
pre-existing instance before each test and again after, so a failed test
doesn't leave the kernel in a half-loaded state.

Requires sudo (insmod/rmmod). Configure NOPASSWD if running unattended.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
MODULE_NAME = "calc_dev"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a command, raising CalledProcessError (with captured output) on failure."""
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def _is_loaded(name: str = MODULE_NAME) -> bool:
    """Return True iff `name` appears in lsmod's first column."""
    out = subprocess.run(["lsmod"], capture_output=True, text=True, check=True).stdout
    # First line of lsmod is a header ("Module Size Used by"); skip it.
    for line in out.splitlines()[1:]:
        fields = line.split()
        if fields and fields[0] == name:
            return True
    return False


@pytest.fixture
def unloaded_module():
    """Yield with the module guaranteed unloaded; clean up afterwards."""
    if _is_loaded():
        _run([str(SCRIPTS / "unload_module.sh")])
    assert not _is_loaded(), "module should not be loaded at test start"
    yield
    if _is_loaded():
        subprocess.run([str(SCRIPTS / "unload_module.sh")], check=False)


def test_load_then_unload(unloaded_module):
    _run([str(SCRIPTS / "load_module.sh")])
    assert _is_loaded(), f"{MODULE_NAME} should appear in lsmod after load_module.sh"

    _run([str(SCRIPTS / "unload_module.sh")])
    assert not _is_loaded(), f"{MODULE_NAME} should be gone from lsmod after unload_module.sh"
