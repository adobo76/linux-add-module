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
no translation. LIST_OPS is answered from a cached buffer that the
server fetches *from the kernel* via the CALC_IOC_LIST_OPS ioctl at
startup, so the kernel module is the canonical source of supported
operations (no server-side hardcoding).

One thread per accepted connection. Each thread keeps its own
/dev/calc_dev fd open, which gives it an isolated kernel session
(per-open private_data) and means concurrent clients don't share a
pending response.
"""

from __future__ import annotations

import argparse
import fcntl
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

# Mirror enum / define from calc_proto.h.
CALC_NUM_OPS = 4
OPS_PAYLOAD_SIZE = OP_INFO.size * CALC_NUM_OPS    # 96 bytes

# CALC_IOC_LIST_OPS encoded the way Linux's _IOR(magic, nr, type) macro
# does it (see include/uapi/asm-generic/ioctl.h). 14 bits of size, 8 bits
# each of magic and number, 2 bits of direction.
_IOC_READ      = 2
_IOC_TYPESHIFT = 8
_IOC_SIZESHIFT = 16
_IOC_DIRSHIFT  = 30
CALC_IOC_LIST_OPS = (
    (_IOC_READ << _IOC_DIRSHIFT)
    | (OPS_PAYLOAD_SIZE << _IOC_SIZESHIFT)
    | (ord("C") << _IOC_TYPESHIFT)
    | 1   # NR
)

DEFAULT_SOCKET = "/tmp/calc_server.sock"
DEFAULT_DEVICE = "/dev/calc_dev"

log = logging.getLogger("calc_server")


def query_kernel_ops(device_path: str) -> bytes:
    """Fetch the kernel's supported-ops table via ioctl.

    Returns the raw bytes ready to be sent back on the wire as the
    LIST_OPS response (CALC_NUM_OPS * struct calc_op_info, 96 bytes).
    """
    fd = os.open(device_path, os.O_RDWR)
    try:
        buf = bytearray(OPS_PAYLOAD_SIZE)
        fcntl.ioctl(fd, CALC_IOC_LIST_OPS, buf, True)
        return bytes(buf)
    finally:
        os.close(fd)


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


def handle_client(conn: socket.socket, cid: int, device_path: str,
                  ops_response: bytes) -> None:
    """Drive one client connection's request/response loop.

    Reads one type byte, dispatches:
      - MSG_CALC:     read 24 bytes, forward to /dev/calc_dev, return 16.
      - MSG_LIST_OPS: return the cached op table (96 bytes) that was
                     fetched from the kernel at server startup.

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
                conn.sendall(ops_response)
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


def serve(socket_path: str, device_path: str, ops_response: bytes,
          stop: threading.Event) -> None:
    """Listen on socket_path, dispatch each accept() to its own thread.

    @ops_response is the cached LIST_OPS payload fetched from the kernel
    at startup; threads send it verbatim when a client asks LIST_OPS.
    """
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
                args=(conn, cid, device_path, ops_response),
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

    # Fetch the kernel's op table once at startup; this is what we'll
    # send back when a client asks LIST_OPS.
    try:
        ops_response = query_kernel_ops(args.device)
    except OSError as exc:
        print(f"calc_server: ioctl(CALC_IOC_LIST_OPS) failed: {exc}",
              file=sys.stderr)
        return 2
    log.info("Loaded %d op(s) from %s via ioctl",
             len(ops_response) // OP_INFO.size, args.device)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    serve(args.socket, args.device, ops_response, stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
