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

The module currently lives at [server/module/calc_dev.c](server/module/calc_dev.c)
and is a stub — it just logs to dmesg on load and unload while the rest of
the device interface is being built up.

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

Current tests:

- [`test_module_lifecycle.py`](tests/test_module_lifecycle.py) — builds the
  module, loads it, asserts it appears in `lsmod`, unloads it, asserts it's
  gone. The fixture leaves the system in an unloaded state regardless of
  whether the test passes or fails.
