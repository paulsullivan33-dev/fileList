"""Standalone download moves using the worker's staged transfer primitives."""
from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import uuid
import time

import job_store
import job_worker
from utils import is_subpath


STATE_DIRECTORY = ".filelist-move-state"


@contextmanager
def source_lock(source):
    root = Path(source) / STATE_DIRECTORY
    root.mkdir(exist_ok=True)
    handle = (root / "mover.lock").open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            if not handle.seek(0, os.SEEK_END):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError("Another download mover is already using this source folder.") from error
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("Another download mover is already using this source folder.") from error
        locked = True
        yield root
    finally:
        if locked and os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def journal_path(root, source, destination):
    key = os.path.normcase(str(source)) + "\0" + os.path.normcase(str(destination))
    return Path(root) / (hashlib.sha256(os.fsencode(key)).hexdigest() + ".json")


def _cleanup_partial(record):
    temporary = record.get("temporary_path")
    if not temporary or not os.path.lexists(temporary):
        return
    temporary = Path(temporary)
    destination = Path(record["destination"])
    if (temporary.parent.resolve() != destination.parent.resolve()
            or not temporary.name.startswith("." + destination.name + ".")
            or not temporary.name.endswith(".partial")):
        raise ValueError("Recovery record does not identify a recognized staging path; inspect it manually.")
    job_worker._remove(str(temporary))


def recover(root):
    """Clean known partials; publication without source removal needs review."""
    blocked = {}
    for path in Path(root).glob("*.json"):
        try:
            record = job_store.read_path(path)
            if (record.get("version") != 1 or Path(record["source"]).parent.resolve() != Path(root).parent.resolve()
                    or journal_path(root, record["source"], record["destination"]).name != path.name):
                raise ValueError("Unrecognized download recovery record; inspect it manually.")
            if record.get("state") == "complete":
                if path.stat().st_mtime < time.time() - 30 * 86400:
                    path.unlink()
                continue
            _cleanup_partial(record)
            if record.get("phase") == "published" and os.path.lexists(record["source"]) and os.path.lexists(record["destination"]):
                raise ValueError("Destination completed but the source remains. Inspect both copies before retrying.")
            if record.get("phase") == "published" and not os.path.lexists(record["source"]) and os.path.lexists(record["destination"]):
                job_store.update(path, state="complete", temporary_path=None)
                continue
            if record.get("state") == "running":
                job_store.update(path, state="failed", temporary_path=None, error="Interrupted download move; retry the unfinished item.")
        except (OSError, ValueError, KeyError, TypeError) as error:
            blocked[path.name] = str(error)
    return blocked


def move(source, destination, state_root):
    source = Path(source)
    destination = Path(destination)
    if os.path.lexists(destination):
        raise FileExistsError(f"Destination already exists: {destination}")
    if not os.path.islink(source) and os.path.isdir(source) and is_subpath(destination, source):
        raise ValueError(f"Cannot move a folder into itself: {source}")
    if is_subpath(destination, state_root):
        raise ValueError("Destination is reserved for download recovery data.")
    path = journal_path(state_root, source, destination)
    record = {"version": 1, "job_id": uuid.uuid4().hex, "source": str(source), "destination": str(destination),
              "state": "running", "phase": "moving", "temporary_path": None, "started_at": job_store.utc_now()}
    job_store._atomic_write(path, record)
    try:
        try:
            job_worker._rename_without_replace(str(source), str(destination))
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
            job_store.update(path, phase="copying")
            progress = job_worker.Progress(record, path, job_worker._measure(str(source)))
            job_worker._copy_staged(str(source), str(destination), progress)
            # Persist publication before attempting source removal. A crash in
            # this window must not trigger an unverified delete on restart.
            job_store.update(path, phase="published")
            try:
                job_worker._remove(str(source))
            except OSError as error:
                raise OSError(f"Destination completed but source removal failed; both copies remain: {error}") from error
        job_store.update(path, state="complete", phase="complete", finished_at=job_store.utc_now(), temporary_path=None)
    except (OSError, RuntimeError, ValueError) as error:
        job_store.update(path, state="failed", error=str(error), finished_at=job_store.utc_now())
        raise
