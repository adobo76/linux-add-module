# Out-of-tree kernel module build.
# Defers everything to the running kernel's Kbuild via the -C flag.
# Override KDIR=/path/to/kernel/build to target a different kernel.

obj-m := calc_dev.o

KDIR ?= /lib/modules/$(shell uname -r)/build
PWD  := $(shell pwd)

.PHONY: all clean

all:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean
