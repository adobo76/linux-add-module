# linux-add-module

A Linux kernel character device that performs basic signed-integer math
(`+`, `-`, `*`, `/`), with a Python server and client(s) that talk to it
over a Unix domain socket.

```
 Kernel Module                            Server                                  Client
+--------------+                      +---------------+                        +---------------+
|              |                      | calc_server   |                        | calc_client   |
| /dev/calc_dev|  read/write 24B/16B  | (Python or C) |  same 24B/16B structs  | (Python or C) |
|   (kernel)   | <------------------> |               | <--------------------> |               |
|              |                      |               |       over UDS         |               |
+--------------+                      +---------------+                        +---------------+
```

Both server and client come in **Python and C** flavors and any combination
interoperates — same kernel struct layout end-to-end means no translation
layer is needed.

## -------------------------------- Kernel module -----------------------------

The module lives at [module/calc_dev.c](module/calc_dev.c). When
loaded it registers a character device at `/dev/calc_dev` (mode `0666`) that
performs `+`, `-`, `*`, `/` on signed 64-bit integers.

### Wire format

Userspace writes one `struct calc_request` (24 bytes) and then reads one
`struct calc_response` (16 bytes). Both structs are defined in
[module/calc_proto.h](module/calc_proto.h):

```c
struct calc_request  { s32 op;     s32 _pad; s64 a; s64 b; };  // "=iiqq"
struct calc_response { s32 status; s32 _pad; s64 result;    };  // "=iiq"
```

`op` is one of `1=ADD`, `2=SUB`, `3=MUL`, `4=DIV`. `status` is `0=OK`,
`1=BAD_OP`, `2=DIV_ZERO`.

Each `open()` gets its own per-fd state (allocated in `calc_open()` and
stored on `file->private_data`), so two concurrent openers don't share a
pending response. `read()` without a prior `write()` on the same fd returns
`-EAGAIN`.

### Driving it from Python

```python
import os, struct
REQ  = struct.Struct("=iiqq")
RESP = struct.Struct("=iiq")

fd = os.open("/dev/calc_dev", os.O_RDWR)
os.write(fd, REQ.pack(1, 0, 42, 37))           # ADD 42 + 37
status, _, result = RESP.unpack(os.read(fd, RESP.size))
print(status, result)                          # 0 79
os.close(fd)
```

### Prerequisites

```bash
sudo apt-get install build-essential linux-headers-$(uname -r)
```

### Build, load, unload, clean

```bash
./scripts/build_module.sh    # builds the module; artifacts under ./build/
./scripts/load_module.sh     # sudo insmod build/module/calc_dev.ko
./scripts/unload_module.sh   # sudo rmmod calc_dev
./scripts/clean.sh           # rm -rf build/ + Python/pytest caches
```

`build_module.sh` symlinks the kernel-module source files from
`module/` into `build/module/` and runs Kbuild there, so every
product — `.ko`, `.o`, `.mod*`, `.cmd`, `Module.symvers`, `modules.order` —
lands under `./build/` and the source tree stays clean.

`load_module.sh` will automatically run `build_module.sh` first if
`build/module/calc_dev.ko` doesn't exist yet.

### Verifying it's working

```bash
$ ./scripts/load_module.sh
Loaded calc_dev

$ lsmod | grep calc_dev
calc_dev               12288  0

$ sudo dmesg | tail -1
calc_dev: loaded

$ ./scripts/unload_module.sh
Removed calc_dev

$ sudo dmesg | tail -1
calc_dev: unloaded
```
## -------------------------------- Server -----------------------------

The server is a Unix-domain-socket gateway in front of `/dev/calc_dev`.
One thread per client; each thread opens its own `/dev/calc_dev` fd
lazily on first CALC so the kernel's per-open session state is private
to that client.

### Wire protocol on the socket

Type-prefixed binary, defined in [`module/calc_proto.h`](module/calc_proto.h):

```
client -> server   [1-byte type][optional payload]
  0x01 CALC        payload : struct calc_request (24 bytes)
                   response: struct calc_response (16 bytes)
  0x02 LIST_OPS    payload : (none)
                   response: 4 * struct calc_op_info (96 bytes total)
```

For `CALC` the server forwards the 24-byte payload to `/dev/calc_dev`
verbatim and returns the device's 16-byte response — no translation.
`LIST_OPS` is the spec's **service announcement**: the client queries
it at startup to learn what operations are supported, instead of
hardcoding the list. (The status field in `calc_response` carries the
error code for div-by-zero and unknown-op — see `enum calc_status`.)

Two implementations, picked at run time:

- [`server/calc_server.py`](server/calc_server.py) — Python, ~120 lines.
  Spawned by `./scripts/start_py_server.sh`, stopped by
  `./scripts/stop_py_server.sh`.
- [`server/calc_server.c`](server/calc_server.c) — C (pthreads), built
  to `build/calc_server_c` by `./scripts/build_module.sh`. Same CLI
  flags (`--socket`, `--device`), same protocol — runs interchangeably
  with the Python version. Spawned by `./scripts/start_c_server.sh`,
  stopped by `./scripts/stop_c_server.sh`.

### Start, stop

```bash
./scripts/load_module.sh         # the server needs /dev/calc_dev to exist
./scripts/start_py_server.sh     # or ./scripts/start_c_server.sh
./scripts/stop_py_server.sh      # or ./scripts/stop_c_server.sh
```

Both start scripts use the same PID file (`/tmp/calc_server.pid`), the
same log file (`/tmp/calc_server.log`), and the same socket
(`/tmp/calc_server.sock`) — only one server can run at a time, and the
stop scripts work regardless of which implementation started it
(SIGTERM, wait, SIGKILL fallback after 2s).

### Talking to it from Python

```python
import socket, struct
REQ  = struct.Struct("=iiqq")
RESP = struct.Struct("=iiq")
MSG_CALC = 0x01

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("/tmp/calc_server.sock")
s.sendall(bytes([MSG_CALC]) + REQ.pack(1, 0, 42, 37))   # ADD 42 + 37
status, _, result = RESP.unpack(s.recv(RESP.size))
print(status, result)                                    # 0 79
s.close()
```

### Talking to it from C

```c
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include "calc_proto.h"          // from module/

int main(void) {
    int s = socket(AF_UNIX, SOCK_STREAM, 0);
    struct sockaddr_un a = { .sun_family = AF_UNIX };
    strcpy(a.sun_path, "/tmp/calc_server.sock");
    connect(s, (struct sockaddr *)&a, sizeof(a));

    struct {
        uint8_t             type;
        struct calc_request req;
    } __attribute__((packed)) msg = {
        .type = CALC_MSG_CALC,
        .req  = { .op = CALC_OP_ADD, .a = 42, .b = 37 },
    };
    struct calc_response resp;
    send(s, &msg,  sizeof(msg),  0);
    recv(s, &resp, sizeof(resp), 0);

    printf("%d %lld\n", resp.status, (long long)resp.result);   // 0 79
    close(s);
}
```

Compile with `gcc -I module example.c -o example`.

## -------------------------------- Client -----------------------------

The terminal client connects to the server's UDS, prints a numbered menu
of operations, reads two operands, and prints the result.

Two implementations, both with the same UX:

- [`client/calc_client.py`](client/calc_client.py) — Python.
  Launch via `./scripts/run_py_client.sh`.
- [`client/calc_client.c`](client/calc_client.c) — C, built to
  `build/calc_client_c` by `./scripts/build_module.sh`.
  Launch via `./scripts/run_c_client.sh`.

Both launchers forward extra args (e.g. `--socket /some/path`) and use
`exec` so signals, exit codes, and Ctrl-C behave as if you ran the
underlying binary directly. Both clients query the server's
`LIST_OPS` at startup to build their menu, so the operations shown
match whatever the server advertises.

```bash
./scripts/load_module.sh
./scripts/start_py_server.sh     # or ./scripts/start_c_server.sh
./scripts/run_py_client.sh       # or ./scripts/run_c_client.sh
```

Sample session:

```
Connected to /tmp/calc_server.sock

(1) Add 2 numbers
(2) Subtract 2 numbers
(3) Multiply 2 numbers
(4) Divide 2 numbers
(5) Exit
Enter command: 1
Enter operand 1: 42
Enter operand 2: 37
Sending request...
Request OKAY...
Receiving response...
Result is 79!
```

`Request OKAY...` is a local-success signal printed after the client's
`sendall()` returns — the wire protocol itself doesn't carry a separate
ACK. The status field in the 16-byte response handles error reporting
(division by zero → "division by zero", unknown op → "unknown operation").

Override the socket with `--socket /some/path` if the server is listening
elsewhere.

## -------------------------------- Tests -----------------------------

Tests are written in [pytest](https://docs.pytest.org/). Install once:

```bash
sudo apt-get install python3-pytest
```

Run the whole suite, a single file, or a subset by name:

```bash
pytest tests/                                # all tests
pytest tests/test_module_lifecycle.py        # one file
pytest tests/ -k load                        # only tests whose name matches "load"
pytest tests/ -v                             # verbose output
```

Tests that touch the kernel module call `sudo` internally, so unless you've
set up `NOPASSWD` you may be prompted for your password.

Shared fixtures live in [`tests/conftest.py`](tests/conftest.py):

- `_build_once` — session-scoped, `autouse`. Builds `calc_dev.ko` once per
  pytest invocation so individual tests never need to think about it.
- `loaded_module` — function-scoped. Idempotently ensures `/dev/calc_dev`
  exists for the duration of the test; only calls `load_module.sh` if the
  module isn't already loaded.
- `running_server` — function-scoped. Spawns `calc_server.py` on a
  per-test socket under `tmp_path` and tears it down after the test.
  Depends on `loaded_module` so the chardev is guaranteed present.

Current tests:

- [`test_module_lifecycle.py`](tests/test_module_lifecycle.py) — loads the
  module, asserts it appears in `lsmod`, unloads it, asserts it's gone.
  Owns the load/unload cycle directly (uses its own `unloaded_module`
  fixture) so it always starts from a known clean state.
- [`test_chardev_protocol.py`](tests/test_chardev_protocol.py) — exercises
  the binary protocol on `/dev/calc_dev`: every operation with multiple
  operand combinations, divide-by-zero and unknown-op error codes,
  `EAGAIN` on read-before-write, and per-fd session isolation (a write on
  one fd must not satisfy a read on another). Uses the `loaded_module`
  fixture.
- [`test_python_server.py`](tests/test_python_server.py) — end-to-end
  tests for `server/calc_server.py`. Each test spawns its own server
  subprocess on a per-test socket path (so it never conflicts with a
  real server you've started). Verifies round-trip for each op, error
  propagation (status codes flow through the server unchanged), multi-
  request pipelining on one connection, per-thread isolation when two
  clients hit the server concurrently, and the **LIST_OPS service
  announcement** (canonical op list + interleaved LIST_OPS/CALC on one
  connection).
- [`test_python_client.py`](tests/test_python_client.py) — end-to-end
  tests for `client/calc_client.py`. Drives the client as a subprocess,
  feeds it stdin, and asserts on captured stdout/stderr. Covers menu
  display, every op (parametrized), the exact `Sending request... /
  Request OKAY... / Receiving response... / Result is N!` UX lines from
  the spec sample, error paths (divide-by-zero, out-of-range command,
  non-numeric input), graceful EOF handling, and the "server not
  reachable" exit code.
- [`test_c_server.py`](tests/test_c_server.py) — same shape as
  `test_python_server.py` but run against the compiled `calc_server_c`
  binary (via the `running_c_server` fixture). Includes a
  `test_python_client_against_c_server` cross-interop case so a
  language-specific regression in either side's struct layout would
  light up.
- [`test_c_client.py`](tests/test_c_client.py) — same shape as
  `test_python_client.py` but run against `build/calc_client_c`.
  Includes a `test_c_client_against_c_server` cross-interop case that
  exercises the **pure-C path** end-to-end (no Python anywhere in
  client, server, or wire format).

## -------------------------------- License -----------------------------

GPL-2.0 — see [LICENSE](LICENSE). The kernel module declares
`MODULE_LICENSE("GPL")`, so the project is GPL-2.0 throughout for
consistency.
