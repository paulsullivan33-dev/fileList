import json
import os
import secrets
import tempfile
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


def create(operation, owner, source, destination):
    initialize()
    job_id = secrets.token_hex(16)
    record = {
        "version": 1,
        "job_id": job_id,
        "operation": operation,
        "owner": owner,
        "source": source,
        "destination": destination,
        "status": "pending",
        "bytes_total": 0,
        "bytes_completed": 0,
        "created_at": utc_now(),
        "started_at": None,
        "finished_at": None,
        "error": None,
    }
    _atomic_write(_job_path("pending", job_id), record)
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
    """Return an owner's newest durable jobs, including completed history."""
    initialize()
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 50
    records = []
    for state in STATES:
        for path in (job_root() / state).glob("*.json"):
            try:
                job = read_path(path)
            except (OSError, json.JSONDecodeError):
                continue
            if job.get("owner") == owner:
                records.append(job)
    records.sort(
        key=lambda job: job.get("finished_at") or job.get("started_at") or job.get("created_at") or "",
        reverse=True,
    )
    return records[:limit]


def has_active_destination(destination):
    initialize()
    for state in ("pending", "running"):
        for path in (job_root() / state).glob("*.json"):
            try:
                if read_path(path).get("destination") == destination:
                    return True
            except (OSError, json.JSONDecodeError):
                continue
    return False


def claim_next():
    initialize()
    pending = sorted((job_root() / "pending").glob("*.json"), key=lambda p: p.stat().st_mtime)
    for source in pending:
        target = job_root() / "running" / source.name
        try:
            os.replace(source, target)
        except FileNotFoundError:
            continue
        job = read_path(target)
        job.update(status="running", started_at=utc_now())
        _atomic_write(target, job)
        return job, target
    return None, None


def update(path, **changes):
    job = read_path(path)
    job.update(changes)
    _atomic_write(path, job)
    return job


def finish(path, state, error=None):
    if state not in {"complete", "failed", "cancelled"}:
        raise ValueError("Invalid final job state")
    job = read_path(path)
    job.update(status=state, error=error, finished_at=utc_now())
    target = _job_path(state, job["job_id"])
    _atomic_write(target, job)
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    cancel_path(job["job_id"]).unlink(missing_ok=True)
    return job


def cancel_path(job_id):
    return job_root() / "cancel_requests" / job_id


def cancellation_requested(job_id):
    return cancel_path(job_id).exists()


def request_cancel(job_id, owner):
    job, path = find(job_id)
    if job is None or job.get("owner") != owner:
        return None
    if job.get("status") == "pending":
        try:
            target = _job_path("cancelled", job_id)
            os.replace(path, target)
            job.update(status="cancelled", finished_at=utc_now(), error="Cancelled by user.")
            _atomic_write(target, job)
            return job
        except FileNotFoundError:
            job, path = find(job_id)
    if job and job.get("status") == "running":
        cancel_path(job_id).touch(exist_ok=True)
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
