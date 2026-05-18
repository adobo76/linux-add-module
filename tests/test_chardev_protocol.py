"""Exercise the binary protocol on /dev/calc_dev.

For each operation we write one `struct calc_request` to the device and
read back one `struct calc_response`, asserting both the status code and
the result. Error paths (divide-by-zero, unknown op, read-without-write)
and the per-fd session isolation guarantee are also covered.

Layouts here must match module/calc_proto.h byte-for-byte; if you
change one, change both.
"""

from __future__ import annotations

import errno
import fcntl
import os
import struct

import pytest

# struct calc_request:  s32 op, s32 _pad, s64 a, s64 b   -> 24 bytes
REQUEST  = struct.Struct("=iiqq") # "=iiqq" means:
                                  # - "=" means native byte order
                                  # - "ii" means two 32-bit integers
                                  # - "qq" means two 64-bit integers

# struct calc_response: s32 status, s32 _pad, s64 result -> 16 bytes
RESPONSE = struct.Struct("=iiq") # "=iiq" means:
                                # - "=" means native byte order
                                # - "ii" means two 32-bit integers
                                # - "q" means one 64-bit integer

# struct calc_op_info: s32 op, char[16] name, char[4] symbol -> 24 bytes
OP_INFO = struct.Struct("=i16s4s")

# Mirrors enum calc_op in calc_proto.h.
ADD, SUB, MUL, DIV = 1, 2, 3, 4
CALC_NUM_OPS = 4

# Mirrors enum calc_status in calc_proto.h.
STATUS_OK       = 0
STATUS_BAD_OP   = 1
STATUS_DIV_ZERO = 2

DEV = "/dev/calc_dev"

# CALC_IOC_LIST_OPS encoded the way Linux's _IOR(magic, nr, type) macro
# does it. 2 bits direction (READ=2), 14 bits size, 8 bits magic, 8 bits NR.
_IOC_READ      = 2
_IOC_TYPESHIFT = 8
_IOC_SIZESHIFT = 16
_IOC_DIRSHIFT  = 30
CALC_IOC_LIST_OPS = (
    (_IOC_READ << _IOC_DIRSHIFT)
    | ((OP_INFO.size * CALC_NUM_OPS) << _IOC_SIZESHIFT)
    | (ord("C") << _IOC_TYPESHIFT)
    | 1   # NR
)


def _round_trip(fd: int, op: int, a: int, b: int) -> tuple[int, int]:
    """Send one request, read one response, return (status, result)."""
    os.write(fd, REQUEST.pack(op, 0, a, b))
    status, _pad, result = RESPONSE.unpack(os.read(fd, RESPONSE.size))
    return status, result


@pytest.mark.parametrize("a, b, expected", [
    (42, 37, 79),
    (-5, 10, 5),
    (0, 0, 0),
    (1 << 30, 1 << 30, 1 << 31),   # well within int64
])
def test_add(loaded_module, a, b, expected):
    """ADD with mixed signs, zero, and >32-bit operands.

    The large-operand case (2^30 + 2^30 = 2^31) confirms the kernel
    uses int64 arithmetic; a 32-bit path would overflow here.
    """
    fd = os.open(DEV, os.O_RDWR)
    try:
        assert _round_trip(fd, ADD, a, b) == (STATUS_OK, expected)
    finally:
        os.close(fd)


@pytest.mark.parametrize("a, b, expected", [
    (10, 3, 7),
    (3, 10, -7),
    (0, -5, 5),
])
def test_sub(loaded_module, a, b, expected):
    """SUB including a case (3 - 10) that produces a negative result.

    Confirms the response field is correctly interpreted as signed -
    a buggy unsigned read would surface as a huge positive number.
    """
    fd = os.open(DEV, os.O_RDWR)
    try:
        assert _round_trip(fd, SUB, a, b) == (STATUS_OK, expected)
    finally:
        os.close(fd)


@pytest.mark.parametrize("a, b, expected", [
    (6, 7, 42),
    (-6, 7, -42),
    (-6, -7, 42),
    (0, 12345, 0),
])
def test_mul(loaded_module, a, b, expected):
    """MUL with all four sign combinations plus zero.

    The sign cases catch off-by-one mistakes like using unsigned
    multiplication; the zero case catches accidental special-casing.
    """
    fd = os.open(DEV, os.O_RDWR)
    try:
        assert _round_trip(fd, MUL, a, b) == (STATUS_OK, expected)
    finally:
        os.close(fd)


@pytest.mark.parametrize("a, b, expected", [
    (100, 4, 25),
    (-100, 4, -25),
    (7, 3, 2),       # truncates toward zero (kernel uses div64_s64)
    (-7, 3, -2),     # also truncates toward zero, not floor
])
def test_div(loaded_module, a, b, expected):
    """DIV including 7/3=2 and -7/3=-2 to pin down the rounding mode.

    The kernel uses div64_s64 which truncates toward zero (C semantics).
    Python's `//` would floor (-7 // 3 == -3), so these cases would
    fail if anyone "helpfully" reimplemented the math in pure Python.
    """
    fd = os.open(DEV, os.O_RDWR)
    try:
        assert _round_trip(fd, DIV, a, b) == (STATUS_OK, expected)
    finally:
        os.close(fd)


def test_div_by_zero_returns_status_code(loaded_module):
    """Divide-by-zero must NOT crash the kernel.

    A signed integer divide by zero is undefined in C and on x86 raises
    a fault. The kernel must catch this *before* calling div64_s64 and
    return STATUS_DIV_ZERO instead, with result left at 0.
    """
    fd = os.open(DEV, os.O_RDWR)
    try:
        status, result = _round_trip(fd, DIV, 7, 0)
        assert status == STATUS_DIV_ZERO
        assert result == 0
    finally:
        os.close(fd)


def test_unknown_op_returns_status_code(loaded_module):
    """An op outside the {ADD, SUB, MUL, DIV} enum is rejected cleanly.

    The kernel switch's default arm must return STATUS_BAD_OP. Without
    that branch, a stray op code could silently fall through and
    produce a garbage result.
    """
    fd = os.open(DEV, os.O_RDWR)
    try:
        status, result = _round_trip(fd, 99, 1, 1)
        assert status == STATUS_BAD_OP
        assert result == 0
    finally:
        os.close(fd)


def test_read_without_write_returns_eagain(loaded_module):
    """The kernel should refuse a read on an fd that has no pending response."""
    fd = os.open(DEV, os.O_RDWR)
    try:
        with pytest.raises(OSError) as excinfo:
            os.read(fd, RESPONSE.size)
        assert excinfo.value.errno == errno.EAGAIN
    finally:
        os.close(fd)


def test_per_fd_sessions_are_isolated(loaded_module):
    """A write on one fd must NOT make a result visible on another fd.

    This is the load-bearing test for the per-open session design:
    each open() must allocate its own struct calc_session.
    """
    fd_a = os.open(DEV, os.O_RDWR)
    fd_b = os.open(DEV, os.O_RDWR)
    try:
        # fd_a writes; fd_b should still see no pending response.
        os.write(fd_a, REQUEST.pack(ADD, 0, 1, 2))
        with pytest.raises(OSError) as excinfo:
            os.read(fd_b, RESPONSE.size)
        assert excinfo.value.errno == errno.EAGAIN

        # fd_a still has its own result waiting.
        status, _pad, result = RESPONSE.unpack(os.read(fd_a, RESPONSE.size))
        assert (status, result) == (STATUS_OK, 3)
    finally:
        os.close(fd_a)
        os.close(fd_b)


def test_ioctl_list_ops_returns_canonical_four(loaded_module):
    """The kernel's CALC_IOC_LIST_OPS ioctl exposes the supported-ops table.

    This is the source of truth that both servers query at startup, so
    if it ever drifts the servers will silently advertise the wrong set
    of operations. Verifying it at the chardev level catches that
    independently of the userspace server code.
    """
    fd = os.open(DEV, os.O_RDWR)
    try:
        buf = bytearray(OP_INFO.size * CALC_NUM_OPS)
        fcntl.ioctl(fd, CALC_IOC_LIST_OPS, buf, True)
    finally:
        os.close(fd)

    ops = []
    for i in range(CALC_NUM_OPS):
        op, name_b, sym_b = OP_INFO.unpack_from(buf, i * OP_INFO.size)
        ops.append((
            op,
            name_b.rstrip(b"\0").decode("ascii"),
            sym_b.rstrip(b"\0").decode("ascii"),
        ))

    assert ops == [
        (ADD, "ADD", "+"),
        (SUB, "SUB", "-"),
        (MUL, "MUL", "*"),
        (DIV, "DIV", "/"),
    ]


def test_ioctl_unknown_command_returns_enotty(loaded_module):
    """Any ioctl other than CALC_IOC_LIST_OPS must yield -ENOTTY.

    This is the conventional errno for "this device doesn't speak that
    ioctl" - confirms the dispatcher doesn't accidentally accept random
    numbers.
    """
    bogus = (_IOC_READ << _IOC_DIRSHIFT) | (ord("C") << _IOC_TYPESHIFT) | 99
    fd = os.open(DEV, os.O_RDWR)
    try:
        with pytest.raises(OSError) as excinfo:
            fcntl.ioctl(fd, bogus, bytearray(8), True)
        assert excinfo.value.errno == errno.ENOTTY
    finally:
        os.close(fd)
