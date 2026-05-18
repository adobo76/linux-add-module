#!/usr/bin/env python3
"""calc_server - Unix-domain-socket gateway in front of /dev/calc_dev.

Clients speak a tiny type-prefixed binary protocol; see module/calc_proto.h
for the canonical definition. Briefly:

  client -> server   [1-byte type][optional payload]
    0x01 CALC        payload: struct calc_request (24 bytes)
                     response: struct calc_response (16 bytes)
    0x02 LIST_OPS    payload: (none)
                     response: CALC_NUM_OPS * struct calc_op_info (96 bytes)

CALC requests are forwarded verbatim to /dev/calc_dev — the server does
no translation. LIST_OPS is served from a server-side table so the client
can discover the available operations without hardcoding them.

One thread per accepted connection. Each thread keeps its own
/dev/calc_dev fd open, which gives it an isolated kernel session
(per-open private_data) and means concurrent clients don't share a
pending response.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import struct
import sys
import threading

# Layouts must match module/calc_proto.h byte-for-byte.
REQUEST  = struct.Struct("=iiqq")     # op, _pad, a, b          -> 24 bytes
RESPONSE = struct.Struct("=iiq")      # status, _pad, result    -> 16 bytes
OP_INFO  = struct.Struct("=i16s4s")   # op, name[16], symbol[4] -> 24 bytes

# Wire-protocol message types; client->server requests only.
MSG_CALC      = 0x01
MSG_LIST_OPS  = 0x02

# Server-side op table. This is the canonical source for the
# service-announcement response. Keep `op` values in sync with
# enum calc_op in module/calc_proto.h.
SUPPORTED_OPS: list[tuple[int, str, str]] = [
    (1, "ADD", "+"),
    (2, "SUB", "-"),
    (3, "MUL", "*"),
    (4, "DIV", "/"),
]

# Pre-encode the LIST_OPS response since it never changes at runtime.
OPS_RESPONSE: bytes = b"".join(
    OP_INFO.pack(op, name.encode(), sym.encode())
    for op, name, sym in SUPPORTED_OPS
)

DEFAULT_SOCKET = "/tmp/calc_server.sock"
DEFAULT_DEVICE = "/dev/calc_dev"

log = logging.getLogger("calc_server")


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    """Read exactly n bytes from sock, or return None on clean disconnect.

    socket.recv() can return short reads; this loops until we have a full
    request or the peer closes the connection.
    """
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def handle_client(conn: socket.socket, cid: int, device_path: str) -> None:
    """Drive one client connection's request/response loop.

    Reads one type byte, dispatches:
      - MSG_CALC:     read 24 bytes, forward to /dev/calc_dev, return 16.
      - MSG_LIST_OPS: return the pre-encoded op table (96 bytes).

    Opens its own fd to /dev/calc_dev so the kernel session state is
    isolated from every other client thread. Lazy: only opens the device
    if the client actually issues a CALC; LIST_OPS-only sessions never
    touch the chardev.
    """
    log.info("Client %d connected", cid)
    # Sentinel so `finally` knows whether the open() actually succeeded.
    dev_fd = -1
    try:
        while True:
            header = _recv_exact(conn, 1)
            if header is None:
                break                              # peer closed cleanly

            msg_type = header[0]
            if msg_type == MSG_CALC:
                req = _recv_exact(conn, REQUEST.size)
                if req is None:
                    log.warning("Client %d: short CALC payload", cid)
                    break
                if dev_fd < 0:
                    dev_fd = os.open(device_path, os.O_RDWR)
                os.write(dev_fd, req)
                resp = os.read(dev_fd, RESPONSE.size)
                conn.sendall(resp)
            elif msg_type == MSG_LIST_OPS:
                conn.sendall(OPS_RESPONSE)
            else:
                log.warning("Client %d: unknown msg type 0x%02x; dropping",
                            cid, msg_type)
                break
    except OSError as exc:
        log.warning("Client %d: %s", cid, exc)
    finally:
        if dev_fd >= 0:
            os.close(dev_fd)
        conn.close()
        log.info("Client %d disconnected", cid)


def serve(socket_path: str, device_path: str, stop: threading.Event) -> None:
    """Listen on socket_path, dispatch each accept() to its own thread."""
    # Remove any stale socket file from a previous crash.
    if os.path.exists(socket_path):
        os.unlink(socket_path)

    listen = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listen.bind(socket_path)
    os.chmod(socket_path, 0o666)   # let normal users connect
    listen.listen(16)
    listen.settimeout(0.5)         # so the accept loop notices `stop` promptly
    log.info("Listening on %s", socket_path)

    cid = 0
    try:
        while not stop.is_set():
            try:
                conn, _ = listen.accept()
            except socket.timeout:
                continue
            cid += 1
            t = threading.Thread(
                target=handle_client,
                args=(conn, cid, device_path),
                name=f"client-{cid}",
                daemon=True,
            )
            t.start()
    finally:
        listen.close()
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="calc_dev IPC gateway")
    parser.add_argument("--socket", default=DEFAULT_SOCKET,
                        help=f"Unix socket path (default: {DEFAULT_SOCKET})")
    parser.add_argument("--device", default=DEFAULT_DEVICE,
                        help=f"calc chardev path (default: {DEFAULT_DEVICE})")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="-v for INFO, -vv for DEBUG")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=max(logging.WARNING - 10 * args.verbose, logging.DEBUG),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    if not os.path.exists(args.device):
        print(
            f"calc_server: {args.device} not found - "
            f"load the kernel module first (./scripts/load_module.sh)",
            file=sys.stderr,
        )
        return 2

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    serve(args.socket, args.device, stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
