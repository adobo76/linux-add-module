# linux-add-module

A Linux kernel character device that performs basic signed-integer math
(`+`, `-`, `*`, `/`), with a Python server and client(s) that talk to it
over a Unix domain socket.

```
 Kernel Module                        Server                                Client
+--------------+  ioctl/read/write  +-----------+  length-prefixed JSON  +----------+
|              | <----------------> |           | <--------------------> |          |
| /dev/calc_dev|                    |           |   over UDS             |          |
|   (kernel)   |                    |     ?     | <--------------------> |     ?    |
|              |                    |           |                        |          |
+--------------+                    +-----------+                        +----------+
```

## -------------------------------- Kernel module -----------------------------

The module lives at [server/module/calc_dev.c](server/module/calc_dev.c). When
loaded it registers a character device at `/dev/calc_dev` (mode `0666`) that
performs `+`, `-`, `*`, `/` on signed 64-bit integers.

### Wire format

Userspace writes one `struct calc_request` (24 bytes) and then reads one
`struct calc_response` (16 bytes). Both structs are defined in
[server/module/calc_proto.h](server/module/calc_proto.h):

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
./scripts/build_module.sh    # make -C server/module
./scripts/load_module.sh     # sudo insmod server/module/calc_dev.ko
./scripts/unload_module.sh   # sudo rmmod calc_dev
./scripts/clean.sh           # make -C server/module clean
```

`load_module.sh` will automatically run `build_module.sh` first if
`server/module/calc_dev.ko` doesn't exist yet.

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
