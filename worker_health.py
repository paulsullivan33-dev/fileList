"""Best-effort worker heartbeat plus main-loop activity diagnostics."""
import html
import logging
import math
import os
from pathlib import Path
import threading
import time


HEARTBEAT_INTERVAL = 2.0
_active = None


class Monitor:
    def __init__(self, root):
        self.path = Path(root) / "worker_health.json"
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.event = threading.Event()
        self.pulse = threading.Event()
        self.data = {"pid": os.getpid(), "state": "starting", "phase": "Starting worker",
                     "job_id": None, "last_activity": time.time()}
        self.thread = threading.Thread(target=self._run, name="filelist-heartbeat", daemon=True)
        self.last_warning = float("-inf")

    def start(self):
        global _active
        _active = self
        self.write()
        self.thread.start()
        return self

    def activity(self, **changes):
        with self.lock:
            changed = any(self.data.get(key) != value for key, value in changes.items())
            self.data.update(changes, last_activity=time.time())
        if changed:
            self.pulse.set()

    def write(self):
        import job_store
        with self.write_lock:
            with self.lock:
                data = {**self.data, "heartbeat": time.time()}
            try:
                for attempt in range(4):
                    try:
                        job_store._atomic_write(self.path, data)
                        break
                    except PermissionError:
                        if attempt == 3:
                            raise
                        time.sleep(0.01)
            except OSError as error:
                if time.monotonic() - self.last_warning >= 30:
                    logging.getLogger(__name__).warning("Cannot write worker heartbeat: %s", error)
                    self.last_warning = time.monotonic()

    def _run(self):
        while not self.event.is_set():
            self.pulse.wait(HEARTBEAT_INTERVAL)
            self.pulse.clear()
            if not self.event.is_set():
                self.write()

    def stop(self):
        global _active
        self.event.set()
        self.pulse.set()
        self.thread.join(timeout=5)
        self.activity(state="stopped", phase="Worker stopped", job_id=None)
        self.write()
        if _active is self:
            _active = None


def checkpoint(**changes):
    if _active is not None:
        _active.activity(**changes)


def _read_record():
    import job_store
    for attempt in range(4):
        try:
            return job_store.read_path(job_store.job_root() / "worker_health.json")
        except PermissionError:
            # Windows can briefly deny access while the atomic replacement is
            # in flight. A short retry avoids falsely reporting a missing worker.
            if attempt == 3:
                raise
            time.sleep(0.01)


def status(now=None):
    import job_store
    now = time.time() if now is None else now
    try:
        data = _read_record()
        seen = max(0, now - float(data["heartbeat"]))
        activity = max(0, now - float(data["last_activity"]))
        stale = float(os.environ.get("FILELIST_WORKER_STALE_SECONDS", "15"))
        stalled = float(os.environ.get("FILELIST_WORKER_STALL_SECONDS", "120"))
        if (data.get("state") not in {"starting", "recovering", "checking_queue", "busy", "idle", "stopped"}
                or not all(math.isfinite(float(value)) for value in (data["heartbeat"], data["last_activity"], stale, stalled))):
            raise ValueError("Invalid worker heartbeat")
    except (OSError, ValueError, KeyError, TypeError):
        return {"state": "unknown", "label": "Worker status unavailable",
                "message": "No readable worker heartbeat. Start or restart the worker to process queued transfers."}
    if data.get("state") == "stopped":
        state, label, message = "stopped", "Worker stopped", "Queued transfers will wait until the worker starts."
    elif seen > stale:
        state, label, message = "unresponsive", "Worker not responding", "The heartbeat is old. The worker may have stopped or lost access to job storage; check its log."
    elif data.get("state") != "idle" and activity > stalled:
        state, label, message = "stalled", "Worker may be stalled", "The heartbeat is current, but work has not reached a checkpoint recently. It may be waiting on storage or an external tool; check its log."
    elif data.get("state") == "idle":
        state, label, message = "idle", "Worker ready", "The worker is waiting for queued transfers."
    elif data.get("state") in {"starting", "recovering"}:
        state, label, message = "starting", "Worker starting", "The worker is checking interrupted jobs before processing the queue."
    else:
        state, label, message = "busy", "Worker working", "The worker is responsive. Pending transfers wait for the current work to finish."
    return {"state": state, "label": label, "message": message,
            "last_seen_seconds": round(seen), "last_activity_seconds": round(activity)}


PROBLEM_STATES = {"stopped", "unresponsive", "stalled", "unknown"}


def banner():
    health = status()
    # The box exists to flag a problem. While the worker is healthy (ready,
    # working, starting) it stays hidden and takes no screen space; it
    # appears on its own as soon as a poll reports a problem state.
    hidden = "" if health['state'] in PROBLEM_STATES else " hidden"
    return (f"<section id='workerHealth' class='worker-health' data-state='{health['state']}'{hidden} aria-live='polite'>"
            f"<strong>{html.escape(health['label'])}</strong><span> · {html.escape(health['message'])}</span></section>"
            + r"""<script>
(function () {
    const box = document.getElementById('workerHealth');
    const problemStates = ['stopped', 'unresponsive', 'stalled', 'unknown'];
    async function update() {
        try {
            const response = await fetch('/worker_status');
            const data = await response.json().catch(() => ({}));
            if (!response.ok || !data.success) throw new Error();
            box.dataset.state = data.state;
            box.hidden = !problemStates.includes(data.state);
            box.querySelector('strong').textContent = data.label;
            box.querySelector('span').textContent = ' · ' + data.message;
        } catch (error) {
            box.dataset.state = 'unknown';
            box.hidden = false;
            box.querySelector('strong').textContent = 'Worker status unavailable';
            box.querySelector('span').textContent = ' · Could not refresh worker status. Check the connection or refresh the page.';
        }
        setTimeout(update, 10000);
    }
    setTimeout(update, 10000);
})();
</script>""")
