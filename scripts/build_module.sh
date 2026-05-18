#!/usr/bin/env bash
# Build the calc_dev kernel module via its Kbuild Makefile.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_DIR="${HERE}/../server/module"

make -C "$MODULE_DIR"
echo
echo "Built: ${MODULE_DIR}/calc_dev.ko"
