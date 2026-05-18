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
C_SERVER_BIN  = ROOT / "build"  / "calc_server_c"
C_CLIENT_BIN  = ROOT / "build"  / "calc_client_c"
MODULE_NAME   = "calc_dev"


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
    """Build everything (kernel module + C server + C client) once per session.

    The build script is idempotent; subsequent runs are no-ops when nothing
    has changed. session-scope keeps the make-startup cost out of every
    individual test.
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


def _spawn_server(cmd: list[str], tmp_path: Path) -> tuple[subprocess.Popen, str]:
    """Spawn `cmd`, wait for it to create a Unix socket, return (proc, path).

    Shared by the Python and C server fixtures; they differ only in which
    binary they launch. On failure to bind in 2s, kills the subprocess and
    raises RuntimeError with the server's captured log included.
    """
    sock_path = tmp_path / "calc.sock"
    log_path  = tmp_path / "server.log"

    with open(log_path, "w") as log:
        proc = subprocess.Popen(
            cmd + ["--socket", str(sock_path)],
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if sock_path.exists():
            return proc, str(sock_path)
        if proc.poll() is not None:
            proc.wait()
            raise RuntimeError(
                f"server exited early (rc={proc.returncode})\n"
                f"  cmd: {cmd}\n"
                f"--- server log ---\n{log_path.read_text()}"
            )
        time.sleep(0.02)

    proc.terminate()
    proc.wait(timeout=2)
    raise RuntimeError(
        f"server did not create {sock_path} in 2s\n"
        f"  cmd: {cmd}\n"
        f"--- server log ---\n{log_path.read_text()}"
    )


def _terminate(proc: subprocess.Popen) -> None:
    """SIGTERM then SIGKILL fallback - same pattern as stop_py_server.sh."""
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@pytest.fixture
def running_server(tmp_path, loaded_module):
    """Spawn the Python calc_server with a per-test socket; tear down on exit.

    Depends on `loaded_module` so /dev/calc_dev is guaranteed to exist
    before the server tries to open it. Yields the socket path as a str
    so tests can connect directly.
    """
    proc, sock_path = _spawn_server(
        [sys.executable, str(SERVER_SCRIPT), "-v"], tmp_path,
    )
    yield sock_path
    _terminate(proc)


@pytest.fixture
def running_c_server(tmp_path, loaded_module):
    """Spawn the C calc_server with a per-test socket; tear down on exit.

    Same contract as `running_server` but launches the compiled C binary.
    Hard-fails (not skips) if the binary is missing so a forgotten build
    doesn't get silently papered over.
    """
    if not C_SERVER_BIN.exists():
        pytest.fail(f"{C_SERVER_BIN} not built - run scripts/build_module.sh")
    proc, sock_path = _spawn_server([str(C_SERVER_BIN)], tmp_path)
    yield sock_path
    _terminate(proc)
