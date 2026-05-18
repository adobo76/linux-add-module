/*
 * calc_proto.h - the wire format between /dev/calc_dev and userspace.
 *
 * Userspace writes one `struct calc_request` to the device and reads back
 * one `struct calc_response`. Same layout, same byte order on both sides;
 * the file is consumed unchanged by Python's struct module via the format
 * strings documented next to each definition.
 */
#ifndef CALC_PROTO_H
#define CALC_PROTO_H

#ifdef __KERNEL__
#include <linux/types.h>
#else
#include <stdint.h>
typedef int32_t __s32;
typedef int64_t __s64;
#endif

/* Operation codes. 1..4, mirrored verbatim in userspace. */
enum calc_op {
    CALC_OP_ADD = 1,
    CALC_OP_SUB = 2,
    CALC_OP_MUL = 3,
    CALC_OP_DIV = 4,
};

/* Status codes carried in struct calc_response.status. */
enum calc_status {
    CALC_STATUS_OK       = 0,
    CALC_STATUS_BAD_OP   = 1,
    CALC_STATUS_DIV_ZERO = 2,
};

/*
 * 24 bytes. Python format: "=iiqq"  (s32 op, s32 pad, s64 a, s64 b)
 * The explicit pad keeps a/b 8-byte aligned without relying on the
 * compiler's struct layout rules.
 */
struct calc_request {
    __s32 op;
    __s32 _pad;
    __s64 a;
    __s64 b;
};

/*
 * 16 bytes. Python format: "=iiq"  (s32 status, s32 pad, s64 result)
 */
struct calc_response {
    __s32 status;
    __s32 _pad;
    __s64 result;
};

#endif /* CALC_PROTO_H */
