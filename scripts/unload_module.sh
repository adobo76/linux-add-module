#!/usr/bin/env bash
# Remove the calc_dev kernel module.

set -euo pipefail

if ! lsmod | awk '{print $1}' | grep -qx calc_dev; then
    echo "calc_dev is not loaded" >&2
    exit 0
fi

sudo rmmod calc_dev
echo "Removed calc_dev"
