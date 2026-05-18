"""Shared pytest configuration for all tests under tests/.

pytest auto-loads conftest.py and makes any fixtures defined here available
to every test file in this directory (and below) without an explicit import.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
MODULE_NAME = "calc_dev"


def _is_loaded(name: str = MODULE_NAME) -> bool:
    """Return True iff `name` appears in lsmod's first column."""
    out = subprocess.run(["lsmod"], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines()[1:]:        # skip header
        fields = line.split()
        if fields and fields[0] == name:
            return True
    return False


@pytest.fixture(scope="session", autouse=True)
def _build_once():
    """Build calc_dev.ko once per pytest session.

    `make` is incremental, so this is effectively a no-op when nothing has
    changed since the last build. session-scope guarantees we don't even
    pay the make-startup cost more than once per pytest invocation, no
    matter how many test files end up running.
    """
    subprocess.run(
        [str(SCRIPTS / "build_module.sh")],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture
def loaded_module():
    """Ensure /dev/calc_dev exists for the test, idempotent.

    Loads the module if (and only if) it isn't already loaded. Leaves it
    loaded after the test so subsequent tests don't pay the load cost;
    test_module_lifecycle.py's own `unloaded_module` fixture will unload
    it before that test runs if needed.
    """
    if not _is_loaded():
        subprocess.run(
            [str(SCRIPTS / "load_module.sh")],
            check=True, capture_output=True, text=True,
        )
    assert Path("/dev/calc_dev").exists(), "/dev/calc_dev missing after load"
    yield
