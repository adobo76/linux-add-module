#!/usr/bin/env bash
# Build the kernel module. All Kbuild outputs go under ./build/module/
# (relative to the project root), keeping the source tree clean.
#
# We symlink the source files (calc_dev.c, calc_proto.h, Makefile) into
# ./build/module/ and run Kbuild there, so every product Kbuild emits
# (the .ko, .o, .mod.c, .cmd, Module.symvers, modules.order, ...) lands
# in ./build/module/. Symlinks (not copies) keep server/module/ as the
# single source of truth — edit a .c or .h file once and the next build
# picks it up.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

MOD_SRC="${ROOT}/server/module"
MOD_BUILD="${ROOT}/build/module"

mkdir -p "$MOD_BUILD"

# `ln -sf` overwrites any existing symlink, so this stays idempotent even
# if the source list grows later.
ln -sf "${MOD_SRC}/calc_dev.c"    "$MOD_BUILD/"
ln -sf "${MOD_SRC}/calc_proto.h"  "$MOD_BUILD/"
ln -sf "${MOD_SRC}/Makefile"      "$MOD_BUILD/"

make -C "$MOD_BUILD"

echo
echo "Built: ${MOD_BUILD}/calc_dev.ko"
