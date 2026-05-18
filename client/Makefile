# Build the C calc_client binary.
#
# Override BUILD_DIR=/abs/path to redirect the output. Without an override,
# the build defaults to <project>/build/ (one level above this Makefile),
# which keeps `make -C client` consistent with the top-level build.sh.

BUILD_DIR ?= $(CURDIR)/../build

CFLAGS  ?= -O2 -Wall -Wextra -Wpedantic -std=c11 -D_POSIX_C_SOURCE=200809L

INCLUDES := -I$(CURDIR)/../server/module

SRC := calc_client.c
HDR := ../server/module/calc_proto.h
OUT := $(BUILD_DIR)/calc_client_c

.PHONY: all clean
all: $(OUT)

$(OUT): $(SRC) $(HDR)
	@mkdir -p $(BUILD_DIR)
	$(CC) $(CFLAGS) $(INCLUDES) $(SRC) -o $(OUT)

clean:
	rm -f $(OUT)
