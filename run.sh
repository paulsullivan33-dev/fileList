#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
pid_file=".filelist-run.pid"

# A PID is the launcher's process ID and also its process group ID: the
# launcher runs in a group of its own (setsid below), and the Flask app and
# job worker it starts stay in that group. Checking the whole group catches
# an app or worker left running after the launcher itself died.
running_group() {
    [[ -r "$pid_file" ]] || return 1
    local pid
    pid=$(<"$pid_file")
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 -- "-$pid" 2>/dev/null || kill -0 "$pid" 2>/dev/null
}

if running_group; then
    echo "fileList is already running (PID $(cat "$pid_file"))."
    exit 1
fi

rm -f "$pid_file"
./start --check
# setsid gives the launcher its own process group, so ./stop.sh can stop the
# launcher, the app and the worker together, even if the launcher is killed.
FILELIST_PRECHECKED=1 nohup setsid ./start > run.log 2>&1 &
pid=$!
echo "$pid" > "$pid_file"
disown "$pid"
echo "fileList started (PID $pid). Use ./stop.sh for a clean shutdown."
