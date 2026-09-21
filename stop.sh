#!/usr/bin/env bash
# Stop a fileList instance started by ./run.sh.
set -euo pipefail

cd "$(dirname "$0")"
pid_file=".filelist-run.pid"

if [[ ! -r "$pid_file" ]]; then
    echo "No run PID file found. If fileList was started with ./start, press Ctrl+C in that terminal."
    exit 1
fi

pid=$(<"$pid_file")
if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
    echo "fileList is not running; removing stale PID file."
    rm -f "$pid_file"
    exit 0
fi

echo "Stopping fileList (PID $pid)..."
kill -TERM "$pid"
for _ in {1..20}; do
    if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$pid_file"
        echo "fileList stopped."
        exit 0
    fi
    sleep 0.5
done

echo "fileList did not stop in 10 seconds; sending SIGKILL."
kill -KILL "$pid" 2>/dev/null || true
rm -f "$pid_file"
echo "fileList stopped. Interrupted jobs are marked failed when the worker starts again and can be retried."
