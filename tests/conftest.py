"""Shared pytest configuration for all tests under tests/.

pytest auto-loads conftest.py and makes any fixtures defined here available
to every test file in this directory (and below) without an explicit import.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


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
