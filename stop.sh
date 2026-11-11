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
if [[ ! "$pid" =~ ^[0-9]+$ ]]; then
    echo "The run PID file is invalid; removing it."
    rm -f "$pid_file"
    exit 0
fi

# ./run.sh starts the launcher in its own process group, whose ID is the
# launcher's PID. Signalling the group reaches the launcher, the Flask app
# and the job worker together. An instance started by an older run.sh has
# no group of its own; fall back to signalling the launcher alone.
if kill -0 -- "-$pid" 2>/dev/null; then
    target=(-- "-$pid")
elif kill -0 "$pid" 2>/dev/null; then
    target=("$pid")
    echo "Note: this instance was started by an older run.sh; only its launcher can be signalled."
else
    echo "fileList is not running; removing stale PID file."
    rm -f "$pid_file"
    exit 0
fi

echo "Stopping fileList (PID $pid)..."
# TERM the whole group: the app and worker stop cleanly even if the launcher
# is already gone (the launcher's own trap sends them the same TERM).
kill -TERM "${target[@]}" 2>/dev/null || true
for _ in {1..20}; do
    if ! kill -0 "${target[@]}" 2>/dev/null; then
        rm -f "$pid_file"
        echo "fileList stopped."
        exit 0
    fi
    sleep 0.5
done

echo "fileList did not stop in 10 seconds; sending SIGKILL to the launcher, app and worker."
kill -KILL "${target[@]}" 2>/dev/null || true
for _ in {1..10}; do
    kill -0 "${target[@]}" 2>/dev/null || break
    sleep 0.5
done
if kill -0 "${target[@]}" 2>/dev/null; then
    echo "Some fileList processes are still running; check with: ps -o pid,pgid,cmd -g $pid"
    exit 1
fi
rm -f "$pid_file"
echo "fileList stopped. Interrupted jobs are marked failed when the worker starts again and can be retried."
