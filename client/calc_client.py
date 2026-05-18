#!/usr/bin/env python3
"""calc_client - terminal client for calc_server.

Connects to the calc_server Unix domain socket, queries the server for
the list of supported operations (service announcement), presents a
numbered menu built from that list, and round-trips one CALC request
per user choice.

Wire-format: type-prefixed binary protocol — see module/calc_proto.h.
The menu is driven by what the server advertises, so adding a new op
in the server alone is enough; the client doesn't need a code change.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys

# Layouts must match module/calc_proto.h byte-for-byte.
REQUEST  = struct.Struct("=iiqq")     # op, _pad, a, b           -> 24 bytes
RESPONSE = struct.Struct("=iiq")      # status, _pad, result     -> 16 bytes
OP_INFO  = struct.Struct("=i16s4s")   # op, name[16], symbol[4]  -> 24 bytes

# Wire-protocol message types (see module/calc_proto.h).
MSG_CALC      = 0x01
MSG_LIST_OPS  = 0x02

# Hardcoded: server is guaranteed to send exactly this many entries.
CALC_NUM_OPS = 4

DEFAULT_SOCKET = "/tmp/calc_server.sock"

# Mirrors enum calc_status in calc_proto.h.
STATUS_OK       = 0
STATUS_BAD_OP   = 1
STATUS_DIV_ZERO = 2

_STATUS_TEXT = {
    STATUS_BAD_OP:   "unknown operation",
    STATUS_DIV_ZERO: "division by zero",
}

# Map short op names (sent by the server) to friendly menu verbs. If the
# server announces a name we don't recognize, we fall back to the raw
# name itself — so adding an op server-side keeps the menu functional.
_VERB_FOR_NAME = {
    "ADD": "Add",
    "SUB": "Subtract",
    "MUL": "Multiply",
    "DIV": "Divide",
}


# --------------------------------------------------------------------------
# Wire helpers
# --------------------------------------------------------------------------

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from sock or raise ConnectionError."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("server closed connection mid-response")
        buf.extend(chunk)
    return bytes(buf)


def _list_ops(sock: socket.socket) -> list[tuple[int, str, str]]:
    """Query the server for its supported operations (service announcement).

    Sends MSG_LIST_OPS (1 byte), expects back N × calc_op_info (24 bytes
    each, N = CALC_NUM_OPS). Returns a list of (op, name, symbol).
    """
    sock.sendall(bytes([MSG_LIST_OPS]))
    buf = _recv_exact(sock, OP_INFO.size * CALC_NUM_OPS)
    ops: list[tuple[int, str, str]] = []
    for i in range(CALC_NUM_OPS):
        op, name_b, sym_b = OP_INFO.unpack_from(buf, i * OP_INFO.size)
        ops.append((
            op,
            name_b.rstrip(b"\0").decode("ascii"),
            sym_b.rstrip(b"\0").decode("ascii"),
        ))
    return ops


def _do_request(sock: socket.socket, op: int, a: int, b: int) -> None:
    """One CALC request/response round-trip with status-aware printing.

    Prints the same UX lines the spec's sample shows. "Request OKAY..."
    is a local-success signal (sendall() returned without error); the
    wire protocol has no separate ACK message.
    """
    print("Sending request...")
    sock.sendall(bytes([MSG_CALC]) + REQUEST.pack(op, 0, a, b))
    print("Request OKAY...")

    print("Receiving response...")
    status, _pad, result = RESPONSE.unpack(_recv_exact(sock, RESPONSE.size))

    if status == STATUS_OK:
        print(f"Result is {result}!")
    else:
        print(f"  {_STATUS_TEXT.get(status, f'error status {status}')}")


# --------------------------------------------------------------------------
# Terminal UI
# --------------------------------------------------------------------------

def _read_int(prompt: str) -> int | None:
    """Read a signed integer from stdin. None on EOF or invalid input.

    Accepts decimal, hex (0x...), octal (0o...), and binary (0b...) via
    int(x, 0). Range-checks against signed 64-bit so a too-large value
    surfaces as a friendly error rather than a struct.error from pack().
    """
    try:
        raw = input(prompt).strip()
    except EOFError:
        print()
        return None
    if not raw:
        return None
    try:
        value = int(raw, 0)
    except ValueError:
        print(f"  '{raw}' is not an integer")
        return None
    if not (-(1 << 63) <= value <= (1 << 63) - 1):
        print(f"  {value} is out of signed 64-bit range")
        return None
    return value


def _label_for(name: str) -> str:
    """Render the menu label for an op named @name (as advertised by the server).

    Falls back to the raw name for unknown ops, so a server advertising a
    new operation still produces a usable menu without a client change.
    """
    return f"{_VERB_FOR_NAME.get(name, name)} 2 numbers"


def _print_menu(ops: list[tuple[int, str, str]]) -> None:
    print()
    for i, (_op, name, _sym) in enumerate(ops, start=1):
        print(f"({i}) {_label_for(name)}")
    print(f"({len(ops) + 1}) Exit")


def repl(sock: socket.socket, ops: list[tuple[int, str, str]]) -> int:
    """Main interactive loop. Returns the process exit code.

    @ops is the server's service-announcement reply — used to drive the
    menu and to map menu choices back to op codes.
    """
    while True:
        _print_menu(ops)
        try:
            raw = input("Enter command: ").strip()
        except EOFError:
            print()
            return 0
        if not raw:
            continue
        try:
            choice = int(raw)
        except ValueError:
            print("  not a number")
            continue

        if choice == len(ops) + 1:
            return 0
        if not 1 <= choice <= len(ops):
            print(f"  pick 1..{len(ops) + 1}")
            continue

        op = ops[choice - 1][0]
        a = _read_int("Enter operand 1: ")
        if a is None:
            continue
        b = _read_int("Enter operand 2: ")
        if b is None:
            continue

        try:
            _do_request(sock, op, a, b)
        except (ConnectionError, OSError, struct.error) as exc:
            print(f"  connection error: {exc}")
            return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="calc_dev terminal client")
    parser.add_argument(
        "--socket", default=DEFAULT_SOCKET,
        help=f"server socket path (default: {DEFAULT_SOCKET})",
    )
    args = parser.parse_args(argv)

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(args.socket)
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        print(
            f"calc_client: cannot reach {args.socket}: {exc}\n"
            f"  Is the server running? Try ./scripts/start_py_server.sh",
            file=sys.stderr,
        )
        return 2

    print(f"Connected to {args.socket}")
    try:
        ops = _list_ops(sock)
    except (ConnectionError, OSError, struct.error) as exc:
        print(f"calc_client: failed to fetch op list: {exc}", file=sys.stderr)
        sock.close()
        return 2

    try:
        return repl(sock, ops)
    except KeyboardInterrupt:
        print()
        return 0
    finally:
        sock.close()


if __name__ == "__main__":
    sys.exit(main())
