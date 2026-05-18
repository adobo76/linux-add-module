#!/usr/bin/env bash
# Launch the Python client. Extra args are forwarded to calc_client.py
# (e.g. --socket /some/other/path).
#
# Uses `exec` so the Python process replaces this shell; that means
# Ctrl-C, signals, and the exit code all behave as if the user ran the
# client directly.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

exec python3 "${ROOT}/client/calc_client.py" "$@"
