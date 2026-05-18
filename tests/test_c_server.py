"""End-to-end tests for the C calc_server binary.

Same protocol-level expectations as test_python_server.py (the C and
Python servers speak an identical wire format), exercised against the
compiled C binary via the `running_c_server` fixture in conftest.py.

If anything in this file fails but the Python equivalent passes, the bug
is C-specific (a forgotten endian conversion, a botched signal handler,
a thread-safety issue, etc.) rather than in the shared kernel chardev
or the protocol design.
"""

from __future__ import annotations

import socket
import struct
import threading

import pytest

REQUEST  = struct.Struct("=iiqq")    # op, _pad, a, b           -> 24 bytes
RESPONSE = struct.Struct("=iiq")     # status, _pad, result     -> 16 bytes

ADD, SUB, MUL, DIV = 1, 2, 3, 4

STATUS_OK       = 0
STATUS_BAD_OP   = 1
STATUS_DIV_ZERO = 2


def _connect(socket_path: str) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(socket_path)
    return s


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("server closed connection mid-response")
        buf.extend(chunk)
    return bytes(buf)


def _round_trip(sock: socket.socket, op: int, a: int, b: int) -> tuple[int, int]:
    sock.sendall(REQUEST.pack(op, 0, a, b))
    status, _pad, result = RESPONSE.unpack(_recv_exact(sock, RESPONSE.size))
    return status, result


@pytest.mark.parametrize("op, a, b, expected", [
    (ADD, 42, 37, 79),
    (SUB, 10,  3,  7),
    (MUL, -6,  7, -42),
    (DIV, 100, 4, 25),
])
def test_each_op_round_trips_through_c_server(running_c_server, op, a, b, expected):
    """One representative case per op end-to-end through the C server."""
    with _connect(running_c_server) as sock:
        assert _round_trip(sock, op, a, b) == (STATUS_OK, expected)


def test_div_by_zero_status_propagates(running_c_server):
    """Kernel-level STATUS_DIV_ZERO must surface unchanged at the client.

    The C server is a pass-through, so a non-OK status field has to
    traverse the socket boundary without the server intercepting it.
    """
    with _connect(running_c_server) as sock:
        status, result = _round_trip(sock, DIV, 7, 0)
        assert status == STATUS_DIV_ZERO
        assert result == 0


def test_unknown_op_status_propagates(running_c_server):
    """Kernel-level STATUS_BAD_OP must surface unchanged at the client."""
    with _connect(running_c_server) as sock:
        status, result = _round_trip(sock, 99, 1, 1)
        assert status == STATUS_BAD_OP
        assert result == 0


def test_multiple_requests_on_one_connection(running_c_server):
    """One client connection can pipeline many request/response pairs."""
    with _connect(running_c_server) as sock:
        assert _round_trip(sock, ADD,  1,  2) == (STATUS_OK, 3)
        assert _round_trip(sock, MUL,  4,  5) == (STATUS_OK, 20)
        assert _round_trip(sock, SUB, 10,  3) == (STATUS_OK, 7)
        assert _round_trip(sock, DIV, 20,  4) == (STATUS_OK, 5)


def test_concurrent_clients_dont_see_each_others_results(running_c_server):
    """Two simultaneous clients each get only their own answers.

    The C server uses pthread_create + pthread_detach per accept(). Each
    thread keeps its own /dev/calc_dev fd, so per-fd kernel session state
    means clients can never see each other's pending responses. This test
    pounds on that invariant from two threads * 50 iterations.
    """
    errors: list[str] = []

    def worker(op: int, a: int, b: int, expected: int) -> None:
        try:
            with _connect(running_c_server) as sock:
                for _ in range(50):
                    assert _round_trip(sock, op, a, b) == (STATUS_OK, expected)
        except AssertionError as exc:
            errors.append(f"({op}, {a}, {b}): {exc}")

    t1 = threading.Thread(target=worker, args=(ADD,  100,   1,  101))
    t2 = threading.Thread(target=worker, args=(MUL, 1000, 1000, 1000000))
    t1.start(); t2.start()
    t1.join();  t2.join()

    assert not errors, f"concurrent workers saw wrong results: {errors}"


def test_python_client_against_c_server(running_c_server):
    """The Python client speaks the same protocol as the C client.

    Uses the same `running_c_server` fixture but drives it with the
    Python client subprocess instead of a raw socket. Proves the wire
    format is truly language-agnostic — a regression in either side's
    struct layout would surface here.
    """
    import subprocess
    import sys
    from pathlib import Path

    client = Path(__file__).resolve().parent.parent / "client" / "calc_client.py"
    result = subprocess.run(
        [sys.executable, str(client), "--socket", running_c_server],
        input="1\n42\n37\n5\n",
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert "Result is 79!" in result.stdout
