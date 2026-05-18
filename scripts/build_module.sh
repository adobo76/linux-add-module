#!/usr/bin/env bash
# Build everything: kernel module, C server, C client.
# All artifacts land under ./build/ at the project root.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

BUILD="${ROOT}/build"
MOD_SRC="${ROOT}/server/module"
MOD_BUILD="${BUILD}/module"

# ----- Kernel module -----
# Symlink the source files into the build dir so Kbuild deposits every
# product it emits (the .ko, .o, .mod.c, .cmd, Module.symvers, ...) under
# ./build/module/ — server/module/ stays clean.
mkdir -p "$MOD_BUILD"
ln -sf "${MOD_SRC}/calc_dev.c"    "$MOD_BUILD/"
ln -sf "${MOD_SRC}/calc_proto.h"  "$MOD_BUILD/"
ln -sf "${MOD_SRC}/Makefile"      "$MOD_BUILD/"
make -C "$MOD_BUILD"

# ----- C server -----
# server/Makefile honors BUILD_DIR; pointing it at ./build/ gives us
# ./build/calc_server_c alongside ./build/module/.
make -C "${ROOT}/server" BUILD_DIR="$BUILD"

# ----- C client -----
make -C "${ROOT}/client" BUILD_DIR="$BUILD"

echo
echo "Built:"
echo "  ${MOD_BUILD}/calc_dev.ko"
echo "  ${BUILD}/calc_server_c"
echo "  ${BUILD}/calc_client_c"
