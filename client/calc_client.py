#!/usr/bin/env python3
"""calc_client - terminal client for calc_server.

Connects to the calc_server Unix domain socket, presents a numbered menu
of operations, and round-trips one calc_request/calc_response per choice.

Wire format: same 24-byte request / 16-byte response that calc_server
speaks (and that the kernel chardev speaks). The client does no
translation; it just packs the user's choices into a struct and unpacks
the response.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys

# Layouts must match server/module/calc_proto.h byte-for-byte.
REQUEST  = struct.Struct("=iiqq")    # op, _pad, a, b           -> 24 bytes
RESPONSE = struct.Struct("=iiq")     # status, _pad, result     -> 16 bytes

DEFAULT_SOCKET = "/tmp/calc_server.sock"

# Mirrors enum calc_op in calc_proto.h.
ADD, SUB, MUL, DIV = 1, 2, 3, 4

# Menu entries: (display label, op code). Order is independent of the op
# numbering — change it freely without touching the protocol.
MENU = [
    ("Add 2 numbers",      ADD),
    ("Subtract 2 numbers", SUB),
    ("Multiply 2 numbers", MUL),
    ("Divide 2 numbers",   DIV),
]

# Mirrors enum calc_status in calc_proto.h.
STATUS_OK       = 0
STATUS_BAD_OP   = 1
STATUS_DIV_ZERO = 2

_STATUS_TEXT = {
    STATUS_BAD_OP:   "unknown operation",
    STATUS_DIV_ZERO: "division by zero",
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


def _do_request(sock: socket.socket, op: int, a: int, b: int) -> None:
    """One request/response round-trip with status-aware printing.

    Prints the same UX lines the spec's sample shows. "Request OKAY..."
    is a local-success signal (sendall() returned without error); the
    wire protocol has no separate ACK message and doesn't need one for
    this purpose.
    """
    print("Sending request...")
    sock.sendall(REQUEST.pack(op, 0, a, b))
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


def _print_menu() -> None:
    print()
    for i, (label, _op) in enumerate(MENU, start=1):
        print(f"({i}) {label}")
    print(f"({len(MENU) + 1}) Exit")


def repl(sock: socket.socket) -> int:
    """Main interactive loop. Returns the process exit code."""
    while True:
        _print_menu()
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

        if choice == len(MENU) + 1:
            return 0
        if not 1 <= choice <= len(MENU):
            print(f"  pick 1..{len(MENU) + 1}")
            continue

        op = MENU[choice - 1][1]
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
        return repl(sock)
    except KeyboardInterrupt:
        print()
        return 0
    finally:
        sock.close()


if __name__ == "__main__":
    sys.exit(main())
