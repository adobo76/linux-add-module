"""End-to-end tests for server/calc_server.py.

Each test spawns its own calc_server subprocess listening on a per-test
Unix domain socket, then connects as a client and verifies the binary
pass-through behavior: client sends a 24-byte calc_request, server
forwards it to /dev/calc_dev, client receives the 16-byte calc_response.

Layouts here mirror server/module/calc_proto.h byte-for-byte - this is
exactly what the chardev protocol test uses, just exchanged over a
socket instead of over the device fd.
"""

from __future__ import annotations

import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = ROOT / "server" / "calc_server.py"

REQUEST  = struct.Struct("=iiqq")    # op, _pad, a, b           -> 24 bytes
RESPONSE = struct.Struct("=iiq")     # status, _pad, result     -> 16 bytes

# Mirrors enum calc_op in calc_proto.h.
ADD, SUB, MUL, DIV = 1, 2, 3, 4

# Mirrors enum calc_status in calc_proto.h.
STATUS_OK       = 0
STATUS_BAD_OP   = 1
STATUS_DIV_ZERO = 2


# --------------------------------------------------------------------------
# Fixture and small helpers
# --------------------------------------------------------------------------

@pytest.fixture
def running_server(tmp_path, loaded_module):
    """Spawn calc_server.py with a per-test socket; tear down on exit.

    Depends on `loaded_module` (defined in conftest.py) so /dev/calc_dev
    is guaranteed to exist before the server tries to open it.
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


def _connect(socket_path: str) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(socket_path)
    return s


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes or raise ConnectionError on short read."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("server closed connection mid-response")
        buf.extend(chunk)
    return bytes(buf)


def _round_trip(sock: socket.socket, op: int, a: int, b: int) -> tuple[int, int]:
    """Send one request, return (status, result)."""
    sock.sendall(REQUEST.pack(op, 0, a, b))
    status, _pad, result = RESPONSE.unpack(_recv_exact(sock, RESPONSE.size))
    return status, result


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

@pytest.mark.parametrize("op, a, b, expected", [
    (ADD, 42, 37, 79),
    (SUB, 10,  3,  7),
    (MUL, -6,  7, -42),
    (DIV, 100, 4, 25),
])
def test_each_op_round_trips_through_server(running_server, op, a, b, expected):
    """One representative case per op end-to-end through the server.

    The chardev test already covers operand corner cases; here we're
    only verifying the server doesn't mangle or drop bytes on either
    direction of the wire.
    """
    with _connect(running_server) as sock:
        assert _round_trip(sock, op, a, b) == (STATUS_OK, expected)


def test_div_by_zero_status_propagates(running_server):
    """Kernel-level STATUS_DIV_ZERO must surface unchanged at the client.

    The server is a pass-through, so a non-OK status field has to traverse
    the socket boundary without the server "helpfully" intercepting it.
    """
    with _connect(running_server) as sock:
        status, result = _round_trip(sock, DIV, 7, 0)
        assert status == STATUS_DIV_ZERO
        assert result == 0


def test_unknown_op_status_propagates(running_server):
    """Kernel-level STATUS_BAD_OP must surface unchanged at the client."""
    with _connect(running_server) as sock:
        status, result = _round_trip(sock, 99, 1, 1)
        assert status == STATUS_BAD_OP
        assert result == 0


def test_multiple_requests_on_one_connection(running_server):
    """A single connection can pipeline many request/response pairs.

    Catches state-leak bugs where the server's per-thread loop leaves
    stale bytes in either the socket buffer or the chardev pending state.
    """
    with _connect(running_server) as sock:
        assert _round_trip(sock, ADD,  1,  2) == (STATUS_OK, 3)
        assert _round_trip(sock, MUL,  4,  5) == (STATUS_OK, 20)
        assert _round_trip(sock, SUB, 10,  3) == (STATUS_OK, 7)
        assert _round_trip(sock, DIV, 20,  4) == (STATUS_OK, 5)


def test_concurrent_clients_dont_see_each_others_results(running_server):
    """Two simultaneous clients each get only their own answers.

    This is the load-bearing test for the per-client-thread + per-fd-session
    design: if anything were shared between threads (a single device fd,
    a server-side response buffer), the workers would occasionally see
    each other's results. Many iterations to give a race a chance to show.
    """
    errors: list[str] = []

    def worker(op: int, a: int, b: int, expected: int) -> None:
        try:
            with _connect(running_server) as sock:
                for _ in range(50):
                    assert _round_trip(sock, op, a, b) == (STATUS_OK, expected)
        except AssertionError as exc:
            errors.append(f"({op}, {a}, {b}): {exc}")

    t1 = threading.Thread(target=worker, args=(ADD,  100,   1,  101))
    t2 = threading.Thread(target=worker, args=(MUL, 1000, 1000, 1000000))
    t1.start(); t2.start()
    t1.join();  t2.join()

    assert not errors, f"concurrent workers saw wrong results: {errors}"
