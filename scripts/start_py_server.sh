#!/usr/bin/env bash
# Start the Python calc_server as a background process.
# Writes its PID to /tmp/calc_server.pid and its log to /tmp/calc_server.log.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

SERVER="${ROOT}/server/calc_server.py"
PID_FILE="/tmp/calc_server.pid"
LOG_FILE="/tmp/calc_server.log"

# Refuse to start if an instance is already running. kill -0 sends signal 0,
# which is a no-op that just succeeds iff the PID is alive and signalable.
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "calc_server already running (PID $(cat "$PID_FILE"))" >&2
    exit 1
fi

# Stale PID file from a crashed previous run: clear it before continuing.
[[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"

# nohup detaches from this shell's terminal session so the server survives
# the script exiting; >file 2>&1 captures stdout+stderr into the log; & puts
# it in the background and $! gives us the new process's PID.
nohup python3 "$SERVER" -v >"$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

echo "calc_server started (PID $(cat "$PID_FILE"))"
echo "  log:    $LOG_FILE"
echo "  socket: /tmp/calc_server.sock"
