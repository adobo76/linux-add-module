"""Shared pytest configuration for all tests under tests/.

pytest auto-loads conftest.py and makes any fixtures defined here available
to every test file in this directory (and below) without an explicit import.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
SERVER_SCRIPT = ROOT / "server" / "calc_server.py"
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


@pytest.fixture
def running_server(tmp_path, loaded_module):
    """Spawn calc_server.py with a per-test socket; tear down on exit.

    Depends on `loaded_module` so /dev/calc_dev is guaranteed to exist
    before the server tries to open it. Yields the socket path as a str
    so tests can connect to it directly.
    """
    sock_path = tmp_path / "calc.sock"
    log_path  = tmp_path / "server.log"

    with open(log_path, "w") as log:
        proc = subprocess.Popen(
            [sys.executable, str(SERVER_SCRIPT), "--socket", str(sock_path), "-v"],
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    # Wait up to ~2s for the server to bind. If the subprocess exits in
    # that window, surface its log so the failure is debuggable.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if sock_path.exists():
            break
        if proc.poll() is not None:
            proc.wait()
            raise RuntimeError(
                f"calc_server exited early (rc={proc.returncode})\n"
                f"--- server log ---\n{log_path.read_text()}"
            )
        time.sleep(0.02)
    else:
        proc.terminate()
        proc.wait(timeout=2)
        raise RuntimeError(
            f"calc_server did not create {sock_path} in 2s\n"
            f"--- server log ---\n{log_path.read_text()}"
        )

    yield str(sock_path)

    # Teardown: SIGTERM, wait briefly, SIGKILL if still alive.
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
