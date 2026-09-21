#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
pid_file=".filelist-run.pid"

if [[ -r "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "fileList is already running (PID $(cat "$pid_file"))."
    exit 1
fi

rm -f "$pid_file"
nohup ./start > run.log 2>&1 &
pid=$!
echo "$pid" > "$pid_file"
disown "$pid"
echo "fileList started (PID $pid). Use ./stop.sh for a clean shutdown."
