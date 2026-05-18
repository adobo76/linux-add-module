# Build the C calc_server binary.
#
# Override BUILD_DIR=/abs/path to redirect the output. Without an override,
# the build defaults to <project>/build/ (one level above this Makefile),
# which keeps `make -C server` consistent with the top-level build.sh.

BUILD_DIR ?= $(CURDIR)/../build

CFLAGS  ?= -O2 -Wall -Wextra -Wpedantic -std=c11 -D_POSIX_C_SOURCE=200809L
LDLIBS  ?= -lpthread

INCLUDES := -I$(CURDIR)/module

SRC := calc_server.c
HDR := module/calc_proto.h
OUT := $(BUILD_DIR)/calc_server_c

.PHONY: all clean
all: $(OUT)

$(OUT): $(SRC) $(HDR)
	@mkdir -p $(BUILD_DIR)
	$(CC) $(CFLAGS) $(INCLUDES) $(SRC) -o $(OUT) $(LDLIBS)

clean:
	rm -f $(OUT)
