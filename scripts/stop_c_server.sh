#!/usr/bin/env bash
# Stop the calc_server started by start_c_server.sh (or start_py_server.sh —
# the PID file is shared, so the same kill logic works either way).
# Sends SIGTERM, waits briefly for a graceful exit, then SIGKILLs.

set -euo pipefail

PID_FILE="/tmp/calc_server.pid"

if [[ ! -f "$PID_FILE" ]]; then
    echo "calc_server is not running (no $PID_FILE)" >&2
    exit 0
fi

PID="$(cat "$PID_FILE")"

if ! kill -0 "$PID" 2>/dev/null; then
    echo "calc_server PID $PID is stale; cleaning up PID file" >&2
    rm -f "$PID_FILE"
    exit 0
fi

# SIGTERM (default for `kill`) — the C server's signal handler flips
# stop_flag and shuts down the listen socket, breaking the accept loop.
kill "$PID"

# Wait up to ~2s for it to exit before giving up and SIGKILL-ing.
for _ in $(seq 1 10); do
    if ! kill -0 "$PID" 2>/dev/null; then
        break
    fi
    sleep 0.2
done

if kill -0 "$PID" 2>/dev/null; then
    echo "calc_server (PID $PID) didn't exit on SIGTERM; sending SIGKILL" >&2
    kill -KILL "$PID" 2>/dev/null || true
fi

rm -f "$PID_FILE"
echo "calc_server stopped"
