// SPDX-License-Identifier: GPL-2.0
/*
 * calc_server (C) - Unix-domain-socket gateway in front of /dev/calc_dev.
 *
 * Wire-protocol-identical to server/calc_server.py — see module/calc_proto.h
 * for the spec. In short: every client→server message starts with a
 * one-byte type (CALC=0x01 or LIST_OPS=0x02); the response shape is
 * implied by what was asked.
 *
 *   CALC      → forwards the 24-byte request to /dev/calc_dev, sends back
 *               the 16-byte response.
 *   LIST_OPS  → returns the server's static op table (the spec's
 *               "service announcement").
 *
 * One detached pthread per accepted connection; each thread opens its
 * own /dev/calc_dev fd lazily on first CALC so the kernel's per-open
 * session state is never shared across clients.
 */

#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>

#include "calc_proto.h"

#define DEFAULT_SOCKET "/tmp/calc_server.sock"
#define DEFAULT_DEVICE "/dev/calc_dev"
#define BACKLOG 16

/*
 * Cached op table — populated once at startup by ioctl()ing the kernel
 * module (CALC_IOC_LIST_OPS). The kernel module is the canonical source
 * for the supported-ops list; the server just caches the bytes it
 * returns so we don't pay an ioctl per LIST_OPS request.
 */
static struct calc_op_info supported_ops_cache[CALC_NUM_OPS];

/* Set by the signal handler; the accept() loop checks it after EINTR. */
static volatile sig_atomic_t stop_flag = 0;
static int listen_fd = -1;

/*
 * on_signal() - SIGINT/SIGTERM handler.
 *
 * Sets stop_flag and kicks the accept() loop awake by shutting down
 * the listening socket. Runs in signal context, so it only does what
 * is async-signal-safe: write a sig_atomic_t and call shutdown(2)
 * (which is on the POSIX signal-safe list).
 *
 * Inputs:
 *   @sig: signal number that fired (unused).
 *
 * Returns: (void)
 */
static void on_signal(int sig)
{
    (void)sig;
    stop_flag = 1;
    /* Closing the listen socket also kicks accept() out of its blocked
     * state on systems where SA_RESTART defaults to on. */
    if (listen_fd >= 0) {
        shutdown(listen_fd, SHUT_RDWR);
    }
}

/*
 * recv_exact() - read exactly @n bytes from a socket.
 *
 * Loops over recv() so a short read doesn't truncate the result.
 * EINTR is retried transparently; any other recv() error or a clean
 * peer close terminates the call.
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
 * send_exact() - write exactly @n bytes to a socket.
 *
 * Uses MSG_NOSIGNAL so a closed-peer write returns EPIPE rather than
 * raising SIGPIPE (which would otherwise kill the process by default).
 * EINTR is retried transparently.
 *
 * Inputs:
 *   @fd:  open socket.
 *   @buf: source bytes.
 *   @n:   number of bytes to write.
 *
 * Returns:
 *   0 on success;
 *   -1 on error (errno is set; EPIPE if the peer is gone).
 */
static int send_exact(int fd, const void *buf, size_t n)
{
    const char *p = buf;
    while (n > 0) {
        /* MSG_NOSIGNAL: if the peer is gone we want EPIPE, not SIGPIPE. */
        ssize_t r = send(fd, p, n, MSG_NOSIGNAL);
        if (r <= 0) {
            if (r < 0 && errno == EINTR) continue;
            return -1;
        }
        p += r;
        n -= (size_t)r;
    }
    return 0;
}

struct client_ctx {
    int         sock;
    int         cid;
    const char *device_path;
};

/*
 * handle_client() - per-connection dispatch loop.
 *
 * Runs in its own detached pthread. Reads one type byte per request and
 * dispatches:
 *   MSG_CALC      - read 24-byte request, forward to /dev/calc_dev,
 *                   send 16-byte response.
 *   MSG_LIST_OPS  - send the static op table back (service announcement).
 *
 * The /dev/calc_dev fd is opened lazily on the first CALC so a session
 * that only does LIST_OPS never touches the chardev.
 *
 * Inputs:
 *   @arg: heap-allocated struct client_ctx*. Owned by this thread.
 *
 * Returns:
 *   NULL (detached pthread; return value is unused).
 */
static void *handle_client(void *arg)
{
    struct client_ctx *ctx = arg;
    int sock = ctx->sock;
    int cid  = ctx->cid;
    const char *device_path = ctx->device_path;
    free(ctx);

    fprintf(stderr, "Client %d connected\n", cid);

    int dev_fd = -1;
    struct calc_request  req;
    struct calc_response resp;
    uint8_t msg_type;

    for (;;) {
        int rc = recv_exact(sock, &msg_type, sizeof(msg_type));
        if (rc == 0) break;                 /* clean peer close */
        if (rc < 0) {
            fprintf(stderr, "Client %d: recv: %s\n", cid, strerror(errno));
            break;
        }

        if (msg_type == CALC_MSG_CALC) {
            rc = recv_exact(sock, &req, sizeof(req));
            if (rc <= 0) {
                fprintf(stderr, "Client %d: short CALC payload\n", cid);
                break;
            }
            if (dev_fd < 0) {
                dev_fd = open(device_path, O_RDWR);
                if (dev_fd < 0) {
                    fprintf(stderr, "Client %d: open %s: %s\n",
                            cid, device_path, strerror(errno));
                    break;
                }
            }
            if (write(dev_fd, &req, sizeof(req)) != (ssize_t)sizeof(req)) {
                fprintf(stderr, "Client %d: write %s: %s\n",
                        cid, device_path, strerror(errno));
                break;
            }
            if (read(dev_fd, &resp, sizeof(resp)) != (ssize_t)sizeof(resp)) {
                fprintf(stderr, "Client %d: read %s: %s\n",
                        cid, device_path, strerror(errno));
                break;
            }
            if (send_exact(sock, &resp, sizeof(resp)) < 0) {
                fprintf(stderr, "Client %d: send: %s\n", cid, strerror(errno));
                break;
            }
        } else if (msg_type == CALC_MSG_LIST_OPS) {
            if (send_exact(sock, supported_ops_cache,
                           sizeof(supported_ops_cache)) < 0) {
                fprintf(stderr, "Client %d: send ops: %s\n",
                        cid, strerror(errno));
                break;
            }
        } else {
            fprintf(stderr, "Client %d: unknown msg type 0x%02x; dropping\n",
                    cid, msg_type);
            break;
        }
    }

    if (dev_fd >= 0) close(dev_fd);
    close(sock);
    fprintf(stderr, "Client %d disconnected\n", cid);
    return NULL;
}

/*
 * load_supported_ops() - fetch the kernel's op table into the cache.
 *
 * Opens @device_path, issues CALC_IOC_LIST_OPS, and stashes the result
 * in `supported_ops_cache`. Called once from main() at startup so
 * subsequent LIST_OPS requests are answered without ioctl overhead.
 *
 * Inputs:
 *   @device_path: path to the calc chardev (e.g. /dev/calc_dev).
 *
 * Returns: 0 on success, -1 on error (errno set).
 */
static int load_supported_ops(const char *device_path)
{
    int fd = open(device_path, O_RDWR);
    if (fd < 0)
        return -1;
    int rc = ioctl(fd, CALC_IOC_LIST_OPS, supported_ops_cache);
    int saved_errno = errno;
    close(fd);
    errno = saved_errno;
    return rc;
}

/*
 * usage() - print short help to stderr.
 *
 * Inputs:
 *   @prog: argv[0], shown verbatim so the message reflects whatever
 *          name the user invoked us as (handy for symlinks).
 *
 * Returns: (void)
 */
static void usage(const char *prog)
{
    fprintf(stderr,
        "Usage: %s [--socket PATH] [--device PATH]\n"
        "  --socket PATH   Unix socket path (default: %s)\n"
        "  --device PATH   calc chardev path (default: %s)\n",
        prog, DEFAULT_SOCKET, DEFAULT_DEVICE);
}

/*
 * main() - parse args, install signal handlers, listen, dispatch.
 *
 * Walks the standard listener boilerplate (socket → bind → listen)
 * then accepts in a loop, spawning one detached pthread per accepted
 * connection. SIGINT/SIGTERM cleanly break the loop via the on_signal()
 * handler.
 *
 * Inputs:
 *   @argc, @argv: standard CLI args; --socket and --device override
 *                 the compiled-in defaults.
 *
 * Returns:
 *   0 on clean shutdown;
 *   1 on setup failure (socket/bind/listen errors);
 *   2 on usage error or missing device.
 */
int main(int argc, char *argv[])
{
    const char *socket_path = DEFAULT_SOCKET;
    const char *device_path = DEFAULT_DEVICE;

    static struct option opts[] = {
        {"socket", required_argument, NULL, 's'},
        {"device", required_argument, NULL, 'd'},
        {"help",   no_argument,       NULL, 'h'},
        {NULL, 0, NULL, 0},
    };
    int c;
    while ((c = getopt_long(argc, argv, "s:d:h", opts, NULL)) != -1) {
        switch (c) {
        case 's': socket_path = optarg; break;
        case 'd': device_path = optarg; break;
        case 'h': usage(argv[0]); return 0;
        default:  usage(argv[0]); return 2;
        }
    }

    if (access(device_path, F_OK) < 0) {
        fprintf(stderr,
            "calc_server: %s: %s - load the kernel module first\n",
            device_path, strerror(errno));
        return 2;
    }

    /* Fetch the kernel's op table once at startup. Cached for every
     * subsequent LIST_OPS request. */
    if (load_supported_ops(device_path) < 0) {
        fprintf(stderr,
            "calc_server: ioctl(CALC_IOC_LIST_OPS) on %s: %s\n",
            device_path, strerror(errno));
        return 2;
    }
    fprintf(stderr, "Loaded %d op(s) from %s via ioctl\n",
            CALC_NUM_OPS, device_path);

    /* Install signal handlers BEFORE we open any resources we'd want to
     * clean up. sa_flags=0 means no SA_RESTART, so accept() will return
     * EINTR on signal and we can break out of the loop cleanly. */
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_signal;
    sigaction(SIGINT,  &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    signal(SIGPIPE, SIG_IGN);

    /* Remove any stale socket from a previous crash. */
    unlink(socket_path);

    listen_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (listen_fd < 0) {
        perror("socket");
        return 1;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    if (strlen(socket_path) >= sizeof(addr.sun_path)) {
        fprintf(stderr, "calc_server: socket path too long\n");
        return 1;
    }
    strncpy(addr.sun_path, socket_path, sizeof(addr.sun_path) - 1);

    if (bind(listen_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind");
        return 1;
    }
    chmod(socket_path, 0666);    /* let normal users connect */

    if (listen(listen_fd, BACKLOG) < 0) {
        perror("listen");
        return 1;
    }

    fprintf(stderr, "Listening on %s\n", socket_path);

    int cid = 0;
    while (!stop_flag) {
        int conn = accept(listen_fd, NULL, NULL);
        if (conn < 0) {
            if (errno == EINTR) {
                if (stop_flag) break;
                continue;
            }
            if (errno == EBADF || errno == EINVAL) break;
            perror("accept");
            break;
        }

        struct client_ctx *ctx = malloc(sizeof(*ctx));
        if (!ctx) {
            close(conn);
            continue;
        }
        ctx->sock        = conn;
        ctx->cid         = ++cid;
        ctx->device_path = device_path;

        pthread_t tid;
        if (pthread_create(&tid, NULL, handle_client, ctx) != 0) {
            perror("pthread_create");
            close(conn);
            free(ctx);
            continue;
        }
        /* Detached: we never join, the OS reclaims the thread when it
         * returns from handle_client. */
        pthread_detach(tid);
    }

    if (listen_fd >= 0) close(listen_fd);
    unlink(socket_path);
    return 0;
}
