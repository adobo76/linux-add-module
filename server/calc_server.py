#!/usr/bin/env python3
"""calc_server - Unix-domain-socket gateway in front of /dev/calc_dev.

Clients connect over a Unix domain socket and send one `struct calc_request`
(24 bytes) per call; the server forwards each request to /dev/calc_dev and
returns the `struct calc_response` (16 bytes) back on the same connection.

The wire format on the socket is identical to the wire format on the
chardev — same struct layout, same byte order — so the server has nothing
to translate. It just shuttles bytes between the two file descriptors.

One thread per accepted connection. Each thread keeps its own /dev/calc_dev
fd open, which gives it an isolated kernel session (per-open private_data)
and means concurrent clients don't share any pending response.
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

# Layouts must match server/module/calc_proto.h byte-for-byte.
REQUEST  = struct.Struct("=iiqq")   # op, _pad, a, b           -> 24 bytes
RESPONSE = struct.Struct("=iiq")    # status, _pad, result     -> 16 bytes

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

    Opens its own fd to /dev/calc_dev so the kernel session state is
    isolated from every other client thread.
    """
    log.info("Client %d connected", cid)
    # Sentinel so `finally` knows whether the open() actually succeeded;
    # otherwise an os.open() failure would leave dev_fd unbound and a
    # naked `os.close(dev_fd)` in finally would NameError.
    dev_fd = -1
    try:
        dev_fd = os.open(device_path, os.O_RDWR)
        while True:
            req = _recv_exact(conn, REQUEST.size)
            if req is None:
                break          # peer closed; exit the loop, run cleanup
            os.write(dev_fd, req)
            resp = os.read(dev_fd, RESPONSE.size)
            conn.sendall(resp)
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
