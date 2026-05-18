// SPDX-License-Identifier: GPL-2.0
/*
 * calc_client (C) - terminal client for calc_server.
 *
 * Connects to the server's Unix domain socket, queries the server for
 * the list of supported operations (service announcement), presents a
 * menu built from that list, and round-trips one CALC request per
 * user choice.
 *
 * Wire-format: type-prefixed binary protocol, see module/calc_proto.h.
 * The menu is driven by what the server advertises, so adding a new
 * operation in the server alone keeps the C client functional.
 *
 * "Request OKAY..." is a local-success signal printed after send()
 * returns without error - the wire protocol has no separate ACK message.
 */

#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <getopt.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "calc_proto.h"

#define DEFAULT_SOCKET "/tmp/calc_server.sock"

/* Holds one entry of the menu built from the server's service announcement. */
struct menu_entry {
    int  op;                                /* protocol op code */
    char name[CALC_OP_NAME_LEN + 1];        /* +1 for our NUL terminator */
    char symbol[5];
};

/*
 * verb_for() - map a server-advertised op name to a friendly menu verb.
 *
 * Falls back to the raw name itself so the menu stays usable if the
 * server announces a new op the client doesn't have a friendly verb for.
 */
static const char *verb_for(const char *name)
{
    if (!strcmp(name, "ADD")) return "Add";
    if (!strcmp(name, "SUB")) return "Subtract";
    if (!strcmp(name, "MUL")) return "Multiply";
    if (!strcmp(name, "DIV")) return "Divide";
    return name;
}

/*
 * status_text() - map a calc_status code to a human-readable string.
 *
 * Mirrors the Python client's _STATUS_TEXT dict so the two clients
 * print the same friendly error wording.
 *
 * Inputs:
 *   @status: a CALC_STATUS_* value (typically from a calc_response).
 *
 * Returns:
 *   A static, NUL-terminated string. Never NULL.
 */
static const char *status_text(int32_t status)
{
    switch (status) {
    case CALC_STATUS_OK:       return "ok";
    case CALC_STATUS_BAD_OP:   return "unknown operation";
    case CALC_STATUS_DIV_ZERO: return "division by zero";
    default:                   return "error";
    }
}

/*
 * recv_exact() - read exactly @n bytes from a socket.
 *
 * Loops over recv() so a short read doesn't truncate the result.
 * EINTR is retried transparently.
 *
 * Inputs:
 *   @fd:  open socket.
 *   @buf: destination buffer; must hold at least @n bytes.
 *   @n:   number of bytes to read.
 *
 * Returns:
 *   1 on full read;
 *   0 if the peer closed the connection before @n bytes arrived;
 *   -1 on recv() error (errno is set).
 */
static int recv_exact(int fd, void *buf, size_t n)
{
    char *p = buf;
    while (n > 0) {
        ssize_t r = recv(fd, p, n, 0);
        if (r == 0) return 0;
        if (r < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        p += r;
        n -= (size_t)r;
    }
    return 1;
}

/*
 * read_line() - read one line of input from stdin into @buf.
 *
 * Strips the trailing newline. If the line is longer than the buffer,
 * drains the rest of the line (up to and including the next '\n') so
 * the next prompt doesn't inherit half a line of garbage.
 *
 * Inputs:
 *   @buf: destination buffer.
 *   @cap: capacity of @buf in bytes; must be > 1.
 *
 * Returns:
 *   1 on success (buf is NUL-terminated, newline stripped);
 *   0 on EOF before any data;
 *  -1 if the line exceeded @cap (excess characters were drained, the
 *     truncated prefix is left in @buf and the caller should reject it).
 */
static int read_line(char *buf, size_t cap)
{
    if (!fgets(buf, (int)cap, stdin)) return 0;
    size_t len = strlen(buf);
    if (len == 0) return 0;
    if (buf[len - 1] == '\n') {
        buf[len - 1] = '\0';
    } else if (len == cap - 1) {
        /* No newline AND we filled the buffer - drain the rest of the
         * line so the next prompt isn't pre-filled with garbage. */
        int ch;
        while ((ch = getchar()) != '\n' && ch != EOF) { }
        return -1;
    }
    return 1;
}

/*
 * parse_int64() - parse a signed 64-bit integer from a NUL-terminated string.
 *
 * Accepts decimal, 0x hex, 0b binary, 0o octal (via strtoll base 0).
 * Rejects strings that have any trailing non-numeric characters, so
 * "42abc" surfaces as an error rather than as 42.
 *
 * Inputs:
 *   @s:   source string (must be NUL-terminated).
 *   @out: where to write the parsed value on success.
 *
 * Returns:
 *   0 on success;
 *   -1 on parse failure or range overflow (errno is set on overflow).
 */
static int parse_int64(const char *s, int64_t *out)
{
    if (*s == '\0') return -1;
    char *end;
    errno = 0;
    long long v = strtoll(s, &end, 0);   /* base 0 -> accepts 0x.., 0b.., 0o.. */
    if (errno != 0 || end == s || *end != '\0') return -1;
    *out = (int64_t)v;
    return 0;
}

/*
 * do_request() - send one calc_request, print the calc_response.
 *
 * Mirrors the Python client's UX exactly: prints "Sending request..."
 * before send(), "Request OKAY..." after send() returns, then
 * "Receiving response..." before recv(), and finally either
 * "Result is N!" or the friendly status text for an error response.
 *
 * "Request OKAY..." is a *local* success signal — the wire protocol
 * carries no separate ACK message.
 *
 * Inputs:
 *   @sock: connected socket to the server.
 *   @op:   one of CALC_OP_* from calc_proto.h.
 *   @a, @b: signed 64-bit operands.
 *
 * Returns:
 *   0 on success (response received and printed, OK or error);
 *   -1 on transport error — the caller should bail out of the REPL.
 */
/*
 * list_ops() - query the server's service announcement.
 *
 * Sends a one-byte LIST_OPS request and reads back CALC_NUM_OPS
 * 24-byte calc_op_info entries. Copies them into the caller's @out
 * array as menu_entry rows with the name/symbol strings NUL-terminated.
 *
 * Inputs:
 *   @sock: connected server socket.
 *   @out:  caller-provided array of CALC_NUM_OPS entries to fill.
 *
 * Returns:
 *   0 on success, -1 on transport error.
 */
static int list_ops(int sock, struct menu_entry out[CALC_NUM_OPS])
{
    uint8_t req = CALC_MSG_LIST_OPS;
    if (send(sock, &req, 1, MSG_NOSIGNAL) != 1) {
        fprintf(stderr, "list_ops: send failed: %s\n", strerror(errno));
        return -1;
    }

    struct calc_op_info wire[CALC_NUM_OPS];
    int rc = recv_exact(sock, wire, sizeof(wire));
    if (rc <= 0) {
        fprintf(stderr, "list_ops: short read (rc=%d)\n", rc);
        return -1;
    }

    for (int i = 0; i < CALC_NUM_OPS; i++) {
        out[i].op = wire[i].op;
        /* Copy with explicit NUL termination since the wire field is
         * NUL-padded but not guaranteed to fit a terminator. */
        memcpy(out[i].name, wire[i].name, CALC_OP_NAME_LEN);
        out[i].name[CALC_OP_NAME_LEN] = '\0';
        memcpy(out[i].symbol, wire[i].symbol, sizeof(wire[i].symbol));
        out[i].symbol[sizeof(wire[i].symbol)] = '\0';
    }
    return 0;
}

/*
 * do_request() - send one CALC request, print the response.
 *
 * Mirrors the Python client's UX exactly: prints "Sending request..."
 * before send(), "Request OKAY..." after send() returns, then
 * "Receiving response..." before recv(), and finally either
 * "Result is N!" or the friendly status text for an error response.
 *
 * "Request OKAY..." is a *local* success signal — the wire protocol
 * carries no separate ACK message.
 *
 * Inputs:
 *   @sock: connected socket to the server.
 *   @op:   one of CALC_OP_* from calc_proto.h.
 *   @a, @b: signed 64-bit operands.
 *
 * Returns:
 *   0 on success (response received and printed, OK or error);
 *   -1 on transport error — the caller should bail out of the REPL.
 */
static int do_request(int sock, int op, int64_t a, int64_t b)
{
    /* On-wire request: 1 byte type + 24 byte calc_request payload. */
    struct {
        uint8_t             type;
        struct calc_request req;
    } __attribute__((packed)) msg;
    msg.type = CALC_MSG_CALC;
    memset(&msg.req, 0, sizeof(msg.req));        /* zeros _pad */
    msg.req.op = op;
    msg.req.a  = a;
    msg.req.b  = b;

    printf("Sending request...\n");
    ssize_t sent = send(sock, &msg, sizeof(msg), MSG_NOSIGNAL);
    if (sent != (ssize_t)sizeof(msg)) {
        fprintf(stderr, "  send failed: %s\n", strerror(errno));
        return -1;
    }
    printf("Request OKAY...\n");

    printf("Receiving response...\n");
    struct calc_response resp;
    int rc = recv_exact(sock, &resp, sizeof(resp));
    if (rc <= 0) {
        fprintf(stderr, "  connection closed mid-response\n");
        return -1;
    }

    if (resp.status == CALC_STATUS_OK) {
        printf("Result is %" PRId64 "!\n", (int64_t)resp.result);
    } else {
        printf("  %s\n", status_text(resp.status));
    }
    return 0;
}

/*
 * print_menu() - render the operation menu to stdout.
 *
 * Items are numbered 1..CALC_NUM_OPS; the last slot (CALC_NUM_OPS + 1)
 * is reserved for Exit. The labels are built at runtime from the
 * server's service-announcement reply via verb_for().
 *
 * Inputs:
 *   @menu: array of CALC_NUM_OPS entries returned by list_ops().
 *
 * Returns: (void)
 */
static void print_menu(const struct menu_entry *menu)
{
    printf("\n");
    for (int i = 0; i < CALC_NUM_OPS; i++) {
        printf("(%d) %s 2 numbers\n", i + 1, verb_for(menu[i].name));
    }
    printf("(%d) Exit\n", CALC_NUM_OPS + 1);
}

/*
 * usage() - print short help to stderr.
 *
 * Inputs:
 *   @prog: argv[0], shown verbatim so the message reflects the user's
 *          invocation name.
 *
 * Returns: (void)
 */
static void usage(const char *prog)
{
    fprintf(stderr,
        "Usage: %s [--socket PATH]\n"
        "  --socket PATH   server socket (default: %s)\n",
        prog, DEFAULT_SOCKET);
}

/*
 * main() - parse args, connect to server, run the REPL loop.
 *
 * Connects to the server's Unix domain socket (default
 * /tmp/calc_server.sock); each iteration prints the menu, reads the
 * user's command and two operands, and calls do_request() to perform
 * one round-trip. Exits cleanly on option (MENU_SIZE + 1), on Ctrl-D,
 * or on transport error.
 *
 * Inputs:
 *   @argc, @argv: standard CLI args; --socket overrides the default
 *                 socket path.
 *
 * Returns:
 *   0 on clean exit;
 *   2 on usage error or unreachable server.
 */
int main(int argc, char *argv[])
{
    const char *socket_path = DEFAULT_SOCKET;

    static struct option opts[] = {
        {"socket", required_argument, NULL, 's'},
        {"help",   no_argument,       NULL, 'h'},
        {NULL, 0, NULL, 0},
    };
    int c;
    while ((c = getopt_long(argc, argv, "s:h", opts, NULL)) != -1) {
        switch (c) {
        case 's': socket_path = optarg; break;
        case 'h': usage(argv[0]); return 0;
        default:  usage(argv[0]); return 2;
        }
    }

    int sock = socket(AF_UNIX, SOCK_STREAM, 0);
    if (sock < 0) {
        perror("socket");
        return 2;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    if (strlen(socket_path) >= sizeof(addr.sun_path)) {
        fprintf(stderr, "calc_client: socket path too long\n");
        close(sock);
        return 2;
    }
    strncpy(addr.sun_path, socket_path, sizeof(addr.sun_path) - 1);

    if (connect(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        fprintf(stderr,
            "calc_client: cannot reach %s: %s\n"
            "  Is the server running? Try ./scripts/start_py_server.sh\n",
            socket_path, strerror(errno));
        close(sock);
        return 2;
    }

    printf("Connected to %s\n", socket_path);

    /* Fetch the op list from the server once at startup. */
    struct menu_entry menu[CALC_NUM_OPS];
    if (list_ops(sock, menu) < 0) {
        fprintf(stderr, "calc_client: failed to fetch op list\n");
        close(sock);
        return 2;
    }

    char line[64];
    int rc = 0;
    for (;;) {
        print_menu(menu);
        printf("Enter command: ");
        fflush(stdout);

        int r = read_line(line, sizeof(line));
        if (r == 0) { printf("\n"); break; }      /* EOF -> exit cleanly */
        if (r < 0)  { printf("  input too long\n"); continue; }
        if (line[0] == '\0') continue;

        char *end;
        long choice = strtol(line, &end, 10);
        if (*end != '\0') { printf("  not a number\n"); continue; }
        if (choice == (long)(CALC_NUM_OPS + 1)) break;
        if (choice < 1 || choice > (long)CALC_NUM_OPS) {
            printf("  pick 1..%d\n", CALC_NUM_OPS + 1);
            continue;
        }
        int op = menu[choice - 1].op;

        printf("Enter operand 1: ");
        fflush(stdout);
        if (read_line(line, sizeof(line)) <= 0) continue;
        if (line[0] == '\0') continue;
        int64_t a;
        if (parse_int64(line, &a) < 0) {
            printf("  '%s' is not an integer\n", line);
            continue;
        }

        printf("Enter operand 2: ");
        fflush(stdout);
        if (read_line(line, sizeof(line)) <= 0) continue;
        if (line[0] == '\0') continue;
        int64_t b;
        if (parse_int64(line, &b) < 0) {
            printf("  '%s' is not an integer\n", line);
            continue;
        }

        if (do_request(sock, op, a, b) < 0) {
            rc = 2;
            break;
        }
    }

    close(sock);
    return rc;
}
