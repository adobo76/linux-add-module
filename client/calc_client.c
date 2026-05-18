// SPDX-License-Identifier: GPL-2.0
/*
 * calc_client (C) - terminal client for calc_server.
 *
 * Connects to the server's Unix domain socket, presents the same numbered
 * menu the Python client uses, and round-trips one calc_request /
 * calc_response per choice. Same wire format on the socket as the Python
 * server and the kernel chardev (24-byte request, 16-byte response), so
 * this C client interoperates with either Python or C server.
 *
 * "Request OKAY..." is a local-success signal printed after send() returns
 * without error - the wire protocol has no separate ACK message.
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

/* Menu entries. Order is independent of the protocol op codes - change
 * the table freely without touching the wire format. */
struct menu_entry {
    const char *label;
    int op;
};

static const struct menu_entry MENU[] = {
    {"Add 2 numbers",      CALC_OP_ADD},
    {"Subtract 2 numbers", CALC_OP_SUB},
    {"Multiply 2 numbers", CALC_OP_MUL},
    {"Divide 2 numbers",   CALC_OP_DIV},
};
#define MENU_SIZE (sizeof(MENU) / sizeof(MENU[0]))

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
static int do_request(int sock, int op, int64_t a, int64_t b)
{
    struct calc_request req;
    memset(&req, 0, sizeof(req));        /* zeros _pad */
    req.op = op;
    req.a  = a;
    req.b  = b;

    printf("Sending request...\n");
    ssize_t sent = send(sock, &req, sizeof(req), MSG_NOSIGNAL);
    if (sent != (ssize_t)sizeof(req)) {
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
 * Items are numbered 1..MENU_SIZE; the last slot (MENU_SIZE + 1) is
 * reserved for Exit.
 *
 * Inputs: (none)
 *
 * Returns: (void)
 */
static void print_menu(void)
{
    printf("\n");
    for (size_t i = 0; i < MENU_SIZE; i++) {
        printf("(%zu) %s\n", i + 1, MENU[i].label);
    }
    printf("(%zu) Exit\n", MENU_SIZE + 1);
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

    char line[64];
    int rc = 0;
    for (;;) {
        print_menu();
        printf("Enter command: ");
        fflush(stdout);

        int r = read_line(line, sizeof(line));
        if (r == 0) { printf("\n"); break; }      /* EOF -> exit cleanly */
        if (r < 0)  { printf("  input too long\n"); continue; }
        if (line[0] == '\0') continue;

        char *end;
        long choice = strtol(line, &end, 10);
        if (*end != '\0') { printf("  not a number\n"); continue; }
        if (choice == (long)(MENU_SIZE + 1)) break;
        if (choice < 1 || choice > (long)MENU_SIZE) {
            printf("  pick 1..%zu\n", MENU_SIZE + 1);
            continue;
        }
        int op = MENU[choice - 1].op;

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
