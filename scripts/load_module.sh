#!/usr/bin/env bash
# Insert the calc_dev kernel module. Builds first if calc_dev.ko is missing.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KO="${HERE}/../server/module/calc_dev.ko"

if [[ ! -f "$KO" ]]; then
    echo "calc_dev.ko not found, building first..." >&2
    "${HERE}/build_module.sh"
fi

if lsmod | awk '{print $1}' | grep -qx calc_dev; then
    echo "calc_dev is already loaded; run ./scripts/unload_module.sh first" >&2
    exit 1
fi

sudo insmod "$KO"
echo "Loaded calc_dev"
