"""End-to-end tests for server/calc_server.py.

Each test spawns its own calc_server subprocess listening on a per-test
Unix domain socket, then connects as a client and exercises the
type-prefixed wire protocol from module/calc_proto.h:

  - CALC (0x01) → 24-byte calc_request payload, 16-byte calc_response back
  - LIST_OPS (0x02) → no payload, CALC_NUM_OPS × 24-byte calc_op_info back
"""

from __future__ import annotations

import socket
import struct
import threading

import pytest

REQUEST  = struct.Struct("=iiqq")     # op, _pad, a, b           -> 24 bytes
RESPONSE = struct.Struct("=iiq")      # status, _pad, result     -> 16 bytes
OP_INFO  = struct.Struct("=i16s4s")   # op, name[16], symbol[4]  -> 24 bytes

# Wire-protocol message types.
MSG_CALC      = 0x01
MSG_LIST_OPS  = 0x02
CALC_NUM_OPS  = 4

# Mirrors enum calc_op in calc_proto.h.
ADD, SUB, MUL, DIV = 1, 2, 3, 4

# Mirrors enum calc_status in calc_proto.h.
STATUS_OK       = 0
STATUS_BAD_OP   = 1
STATUS_DIV_ZERO = 2


# --------------------------------------------------------------------------
# Small helpers (running_server fixture lives in conftest.py)
# --------------------------------------------------------------------------

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
    """Send one CALC, return (status, result).

    Wire-protocol: 1 type byte (MSG_CALC) followed by the 24-byte
    calc_request payload, then a 16-byte calc_response back.
    """
    sock.sendall(bytes([MSG_CALC]) + REQUEST.pack(op, 0, a, b))
    status, _pad, result = RESPONSE.unpack(_recv_exact(sock, RESPONSE.size))
    return status, result


def _list_ops(sock: socket.socket) -> list[tuple[int, str, str]]:
    """Send LIST_OPS, parse the response into (op, name, symbol) tuples."""
    sock.sendall(bytes([MSG_LIST_OPS]))
    buf = _recv_exact(sock, OP_INFO.size * CALC_NUM_OPS)
    ops = []
    for i in range(CALC_NUM_OPS):
        op, name_b, sym_b = OP_INFO.unpack_from(buf, i * OP_INFO.size)
        ops.append((
            op,
            name_b.rstrip(b"\0").decode("ascii"),
            sym_b.rstrip(b"\0").decode("ascii"),
        ))
    return ops


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


def test_list_ops_returns_canonical_four(running_server):
    """LIST_OPS announces exactly the four ops defined in calc_proto.h.

    Verifies the spec's "service announcement" requirement: the client
    can discover available operations from the server without
    hardcoding them.
    """
    with _connect(running_server) as sock:
        ops = _list_ops(sock)
    assert [(op, name) for (op, name, _sym) in ops] == [
        (ADD, "ADD"),
        (SUB, "SUB"),
        (MUL, "MUL"),
        (DIV, "DIV"),
    ]
    symbols = {name: sym for (_op, name, sym) in ops}
    assert symbols == {"ADD": "+", "SUB": "-", "MUL": "*", "DIV": "/"}


def test_list_ops_then_calc_on_same_connection(running_server):
    """A session can interleave LIST_OPS and CALC on one connection.

    Catches state-machine bugs where the server's loop expects a
    particular sequence of messages.
    """
    with _connect(running_server) as sock:
        ops = _list_ops(sock)
        assert len(ops) == 4
        assert _round_trip(sock, ADD, 42, 37) == (STATUS_OK, 79)
        # And again, in the other order: CALC then LIST_OPS.
        assert _round_trip(sock, MUL, 6, 7) == (STATUS_OK, 42)
        ops2 = _list_ops(sock)
        assert ops == ops2
