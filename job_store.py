import json
import os
import secrets
import tempfile
import operation_log
from datetime import datetime, timezone
from pathlib import Path


STATES = ("pending", "running", "complete", "failed", "cancelled")
APP_DIR = Path(__file__).resolve().parent
DEFAULT_JOB_DIR = Path.home() / ".local" / "state" / "filelist" / "jobs"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def job_root():
    return Path(os.environ.get("JOB_DATA_DIR", DEFAULT_JOB_DIR)).expanduser().resolve()


def initialize():
    root = job_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    for name in (*STATES, "cancel_requests"):
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
    return root


def _job_path(state, job_id):
    return job_root() / state / f"{job_id}.json"


def _atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(data, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def read_path(path):
    with open(path, "r", encoding="utf-8") as source:
        return json.load(source)


def find(job_id):
    if not isinstance(job_id, str) or len(job_id) != 32:
        return None, None
    for state in STATES:
        path = _job_path(state, job_id)
        try:
            return read_path(path), path
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError):
            return None, None
    return None, None


def create(operation, owner, source, destination, directories=None):
    if directories is not None:
        from utils import bind_job_selection
        source = bind_job_selection(source, directories)
        if operation in {"copy", "move", "convert"}:
            destination = bind_job_selection(destination, directories)
    initialize()
    job_id = secrets.token_hex(16)
    record = {
        "version": 2 if "directory_id" in source else 1,
        "job_id": job_id,
        "operation": operation,
        "owner": owner,
        "source": source,
        "destination": destination,
        "status": "pending",
        "bytes_total": 0,
        "bytes_completed": 0,
        "completed_names": [],
        "failed_names": [],
        "current_name": None,
        "created_at": utc_now(),
        "started_at": None,
        "finished_at": None,
        "error": None,
    }
    _atomic_write(_job_path("pending", job_id), record)
    operation_log.record_job(record, "queued")
    return record


def pending_count(owner=None):
    initialize()
    count = 0
    for path in (job_root() / "pending").glob("*.json"):
        if owner is None:
            count += 1
            continue
        try:
            if read_path(path).get("owner") == owner:
                count += 1
        except (OSError, json.JSONDecodeError):
            continue
    return count


def history(owner, limit=50):
    """Keep active jobs visible before an owner's recent terminal history."""
    initialize()
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 50
    records = {}
    for state in STATES:
        for path in (job_root() / state).glob("*.json"):
            try:
                job = read_path(path)
            except (OSError, json.JSONDecodeError):
                continue
            if job.get("owner") == owner:
                job["status"] = state
                records[job.get("job_id", path.stem)] = job
    records = list(records.values())
    active = [job for job in records if job.get("status") in {"pending", "running"}]
    finished = [job for job in records if job.get("status") not in {"pending", "running"}]
    active.sort(key=lambda job: (job.get("status") != "running", job.get("created_at", ""), job.get("job_id", "")))
    finished.sort(key=lambda job: job.get("finished_at") or job.get("created_at") or "", reverse=True)
    return (active + finished)[:limit]


def _pending_entries():
    initialize()
    entries = []
    for path in (job_root() / "pending").glob("*.json"):
        try:
            job = read_path(path)
            if isinstance(job, dict):
                entries.append((job, path))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(entries, key=lambda entry: (entry[0].get("created_at", ""), entry[1].name))


def queue_snapshot(owner=None):
    """Positions use exactly the worker's FIFO order, across all owners."""
    pending = _pending_entries()
    running = 0
    for path in (job_root() / "running").glob("*.json"):
        try:
            read_path(path)
            running += 1
        except (OSError, json.JSONDecodeError):
            continue
    return {job["job_id"]: {"queue_position": index + 1, "jobs_ahead": index + running,
                             "waiting_for_running_job": bool(running)}
            for index, (job, _) in enumerate(pending) if owner is None or job.get("owner") == owner}


def has_active_destination(destination):
    initialize()
    for state in ("pending", "running"):
        for path in (job_root() / state).glob("*.json"):
            try:
                stored = dict(read_path(path).get("destination") or {})
                candidate = dict(destination)
                if "directory_id" in stored and "directory_id" in candidate:
                    stored.pop("directory_index", None)
                    candidate.pop("directory_index", None)
                if stored == candidate:
                    return True
            except (OSError, json.JSONDecodeError):
                continue
    return False


def claim_next():
    for _, source in _pending_entries():
        target = job_root() / "running" / source.name
        try:
            os.replace(source, target)
        except FileNotFoundError:
            continue
        job = read_path(target)
        job.update(status="running", started_at=utc_now())
        _atomic_write(target, job)
        operation_log.record_job(job, "running")
        return job, target
    return None, None


def update(path, **changes):
    job = read_path(path)
    job.update(changes)
    _atomic_write(path, job)
    return job


def start_item(path, name):
    update(path, current_name=name)


def complete_item(path, name):
    """Persist only items whose transfer has fully succeeded."""
    job = read_path(path)
    completed = job.setdefault("completed_names", [])
    newly_completed = name not in completed
    if name not in completed:
        completed.append(name)
    if job.get("current_name") == name:
        job["current_name"] = None
    _atomic_write(path, job)
    if newly_completed:
        operation_log.record_job(job, "complete", item=name)


def item_summary(job):
    """Separate failed items from unstarted items without guessing old errors."""
    names = list(dict.fromkeys(job.get("source", {}).get("names", [])))
    completed = [name for name in names
                 if job.get("status") == "complete" or name in job.get("completed_names", [])]
    failed = [name for name in names if name not in completed and name in job.get("failed_names", [])]
    current = job.get("current_name") if job.get("status") == "running" else None
    if current not in names or current in completed:
        current = None
    remaining = [name for name in names if name not in completed and name not in failed and name != current]
    retry = [name for name in names if name not in completed]
    selections = [job.get("source", {})]
    if job.get("operation") in {"copy", "move"}:
        selections.append(job.get("destination", {}))
    stable = all(selection.get("directory_id") and selection.get("directory_root") for selection in selections)
    return {"completed_names": completed, "failed_names": failed,
            "remaining_names": remaining, "current_name": current,
            "total_count": len(names), "retry_count": len(retry),
            "can_retry": job.get("status") in {"failed", "cancelled"} and bool(retry) and bool(stable),
            "requires_resubmit": bool(retry) and not stable}


def retry_selection(job):
    """Keep completed items out of a retry without trusting destination existence."""
    source, destination = dict(job["source"]), dict(job["destination"])
    if job["operation"] in {"copy", "move", "remote_copy", "remote_move", "convert"}:
        completed = set(job.get("completed_names", []))
        remaining = [name for name in source.get("names", []) if name not in completed]
        if not remaining:
            raise ValueError("All selected items already completed; there is nothing to retry.")
        source["names"] = remaining
        if job["operation"] != "convert":
            destination["names"] = remaining
    return source, destination


def finish(path, state, error=None):
    if state not in {"complete", "failed", "cancelled"}:
        raise ValueError("Invalid final job state")
    job = read_path(path)
    current = job.get("current_name")
    if state == "failed" and current and current not in job.get("completed_names", []):
        job["failed_names"] = [current]
    job["current_name"] = None
    job.update(status=state, error=error, finished_at=utc_now())
    target = _job_path(state, job["job_id"])
    _atomic_write(target, job)
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    cancel_path(job["job_id"]).unlink(missing_ok=True)
    operation_log.record_job(job, state, item=current if current not in job.get("completed_names", []) else "", detail=error)
    return job


def cancel_path(job_id):
    return job_root() / "cancel_requests" / job_id


def cancellation_requested(job_id):
    return cancel_path(job_id).exists()


def request_cancel(job_id, owner):
    job, path = find(job_id)
    if job is None or job.get("owner") != owner:
        return None
    # The state directory is authoritative during atomic claim/cancel moves;
    # the JSON's status may briefly still say 'pending' after a worker claims it.
    if path.parent.name == "pending":
        try:
            target = _job_path("cancelled", job_id)
            os.replace(path, target)
            job.update(status="cancelled", finished_at=utc_now(), error="Cancelled by user.")
            _atomic_write(target, job)
            operation_log.record_job(job, "cancelled", detail="Cancelled by user.")
            return job
        except FileNotFoundError:
            job, path = find(job_id)
    if job and path.parent.name == "running":
        cancel_path(job_id).touch(exist_ok=True)
        latest, latest_path = find(job_id)
        if latest is not None:
            job, path = latest, latest_path
            if path.parent.name not in {"pending", "running"}:
                cancel_path(job_id).unlink(missing_ok=True)
    if job:
        job["status"] = path.parent.name
    return job


def recover_interrupted():
    initialize()
    recovered = 0
    for path in list((job_root() / "running").glob("*.json")):
        try:
            job = read_path(path)
            temporary = job.get("temporary_path")
            if temporary and ".partial" in os.path.basename(temporary) and os.path.lexists(temporary):
                if os.path.isdir(temporary) and not os.path.islink(temporary):
                    import shutil
                    shutil.rmtree(temporary)
                else:
                    os.remove(temporary)
            finish(path, "failed", "Worker stopped before this job completed; retry the operation.")
            recovered += 1
        except (OSError, json.JSONDecodeError):
            continue
    return recovered


def cleanup_history(days):
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    for state in ("complete", "failed", "cancelled"):
        for path in (job_root() / state).glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue
