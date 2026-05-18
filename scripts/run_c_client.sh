#!/usr/bin/env bash
# Launch the C client. Extra args are forwarded to calc_client_c
# (e.g. --socket /some/other/path).
#
# Uses `exec` so the C process replaces this shell; that means Ctrl-C,
# signals, and the exit code all behave as if the user ran the binary
# directly.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

CLIENT="${ROOT}/build/calc_client_c"

if [[ ! -x "$CLIENT" ]]; then
    echo "$CLIENT not built; run ./scripts/build_module.sh" >&2
    exit 1
fi

exec "$CLIENT" "$@"
