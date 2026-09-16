#!/usr/bin/env python3
import argparse
import ctypes
import errno
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import config_loader
import job_store
from utils import ensure_free_space, is_safe_path, is_safe_segment, safe_dir_index, safe_join


CHUNK_SIZE = 4 * 1024 * 1024
PROGRESS_INTERVAL = float(os.environ.get("JOB_PROGRESS_INTERVAL", "2"))
AT_FDCWD = -100
RENAME_NOREPLACE = 1


class JobCancelled(Exception):
    pass


def _rename_without_replace(source, destination):
    """Use Linux renameat2 so a late destination collision cannot overwrite data."""
    if not sys.platform.startswith("linux"):
        if os.path.lexists(destination):
            raise FileExistsError(destination)
        os.rename(source, destination)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        if os.path.lexists(destination):
            raise FileExistsError(destination)
        os.rename(source, destination)
        return
    result = renameat2(
        AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(destination), RENAME_NOREPLACE
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


def _check_cancel(job_id):
    if job_store.cancellation_requested(job_id):
        raise JobCancelled("Cancelled by user.")


def _measure(path):
    if os.path.islink(path):
        return 0
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, directories, files in os.walk(path, followlinks=False):
        directories[:] = [name for name in directories if not os.path.islink(os.path.join(root, name))]
        for name in files:
            item = os.path.join(root, name)
            if not os.path.islink(item):
                total += os.path.getsize(item)
    return total


class Progress:
    def __init__(self, job, job_path, total):
        self.job = job
        self.path = job_path
        self.total = total
        self.completed = 0
        self.last_update = 0.0
        job_store.update(job_path, bytes_total=total, bytes_completed=0)

    def add(self, amount, force=False):
        self.completed += amount
        now = time.monotonic()
        if force or now - self.last_update >= PROGRESS_INTERVAL:
            _check_cancel(self.job["job_id"])
            job_store.update(
                self.path,
                bytes_total=self.total,
                bytes_completed=min(self.completed, self.total),
            )
            self.last_update = now


def _copy_file(source, destination, progress):
    with open(source, "rb") as input_file, open(destination, "xb") as output_file:
        while True:
            _check_cancel(progress.job["job_id"])
            chunk = input_file.read(CHUNK_SIZE)
            if not chunk:
                break
            output_file.write(chunk)
            progress.add(len(chunk))
        output_file.flush()
        os.fsync(output_file.fileno())
    shutil.copystat(source, destination, follow_symlinks=False)


def _copy_tree(source, destination, progress):
    os.mkdir(destination)
    for root, directories, files in os.walk(source, topdown=True, followlinks=False):
        _check_cancel(progress.job["job_id"])
        relative = os.path.relpath(root, source)
        target_root = destination if relative == "." else os.path.join(destination, relative)
        for name in list(directories):
            source_item = os.path.join(root, name)
            target_item = os.path.join(target_root, name)
            if os.path.islink(source_item):
                os.symlink(os.readlink(source_item), target_item, target_is_directory=True)
                directories.remove(name)
            else:
                os.mkdir(target_item)
        for name in files:
            source_item = os.path.join(root, name)
            target_item = os.path.join(target_root, name)
            if os.path.islink(source_item):
                os.symlink(os.readlink(source_item), target_item)
            else:
                _copy_file(source_item, target_item, progress)
    for root, directories, _ in os.walk(source, topdown=False, followlinks=False):
        relative = os.path.relpath(root, source)
        target_root = destination if relative == "." else os.path.join(destination, relative)
        shutil.copystat(root, target_root, follow_symlinks=False)


def _remove(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def _copy_staged(source, destination, progress):
    if os.path.lexists(destination):
        raise FileExistsError(f"Destination already exists: {os.path.basename(destination)}")
    temporary = os.path.join(
        os.path.dirname(destination),
        f".{os.path.basename(destination)}.{uuid.uuid4().hex}.partial",
    )
    job_store.update(progress.path, temporary_path=temporary)
    try:
        if os.path.isdir(source):
            _copy_tree(source, temporary, progress)
        else:
            _copy_file(source, temporary, progress)
        _check_cancel(progress.job["job_id"])
        if os.path.lexists(destination):
            raise FileExistsError(f"Destination already exists: {os.path.basename(destination)}")
        _rename_without_replace(temporary, destination)
        job_store.update(progress.path, temporary_path=None)
    except Exception:
        if os.path.lexists(temporary):
            _remove(temporary)
        raise


def _validated_items(job, directories):
    source_info = job["source"]
    destination_info = job["destination"]
    source_index = safe_dir_index(source_info.get("directory_index"), directories)
    destination_index = safe_dir_index(destination_info.get("directory_index"), directories)
    source_path = source_info.get("path", "")
    destination_path = destination_info.get("path", "")
    names = source_info.get("names") or []
    if (
        source_index is None
        or destination_index is None
        or not is_safe_path(source_path)
        or not is_safe_path(destination_path)
        or not names
        or not all(is_safe_segment(name) for name in names)
    ):
        raise ValueError("Invalid queued paths.")
    source_root, _ = directories[source_index]
    destination_root, _ = directories[destination_index]
    destination_directory = safe_join(destination_root, destination_path)
    if destination_directory is None or not os.path.isdir(destination_directory):
        raise ValueError("Destination directory no longer exists.")
    items = []
    for name in names:
        source = safe_join(source_root, source_path, name)
        destination = safe_join(destination_root, destination_path, name)
        if source is None or destination is None or not os.path.lexists(source):
            raise ValueError(f"Source no longer exists: {name}")
        if os.path.lexists(destination):
            raise FileExistsError(f"Destination already exists: {name}")
        if os.path.isdir(source):
            real_source = os.path.realpath(source)
            real_destination = os.path.realpath(destination)
            if real_destination == real_source or real_destination.startswith(real_source + os.sep):
                raise ValueError(f"Cannot transfer a folder into itself: {name}")
        items.append((source, destination))
    return items


def _run_transfer(job, path, directories):
    items = _validated_items(job, directories)
    total = sum(_measure(source) for source, _ in items)
    progress = Progress(job, path, total)
    for source, destination in items:
        _check_cancel(job["job_id"])
        if job["operation"] == "move":
            try:
                _rename_without_replace(source, destination)
                progress.add(_measure(destination), force=True)
                continue
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
        _copy_staged(source, destination, progress)
        if job["operation"] == "move":
            try:
                _remove(source)
            except OSError as error:
                raise OSError(
                    "Destination completed, but source removal failed; both copies remain: "
                    f"{error}"
                ) from error
    progress.add(0, force=True)


def _run_conversion(job, path, directories):
    source_info = job["source"]
    index = safe_dir_index(source_info.get("directory_index"), directories)
    relative_path = source_info.get("path", "")
    names = source_info.get("names") or []
    if index is None or not is_safe_path(relative_path) or len(names) != 1 or not is_safe_segment(names[0]):
        raise ValueError("Invalid queued conversion path.")
    root, _ = directories[index]
    source = safe_join(root, relative_path, names[0])
    output_name = os.path.splitext(names[0])[0] + ".mp4"
    output = safe_join(root, relative_path, output_name)
    if source is None or output is None or not os.path.isfile(source):
        raise ValueError("Conversion source no longer exists.")
    if os.path.lexists(output):
        raise FileExistsError(f"Destination already exists: {output_name}")
    ensure_free_space(os.path.dirname(output), os.path.getsize(source))
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed or is not on PATH.")
    temporary = output + f".{job['job_id']}.partial.mp4"
    job_store.update(path, temporary_path=temporary)
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", source, "-c:v", "libx264", "-c:a", "aac",
        "-movflags", "+faststart", temporary,
    ]
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as error_log:
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=error_log, text=True
        )
        try:
            while process.poll() is None:
                if job_store.cancellation_requested(job["job_id"]):
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise JobCancelled("Cancelled by user.")
                time.sleep(1)
            error_log.seek(0)
            error = error_log.read()
            if process.returncode != 0:
                raise RuntimeError((error or "ffmpeg exited with an error.").strip()[-2000:])
            if os.path.lexists(output):
                raise FileExistsError(f"Destination already exists: {output_name}")
            _rename_without_replace(temporary, output)
            job_store.update(path, temporary_path=None)
        finally:
            if os.path.lexists(temporary):
                os.remove(temporary)


def run_once():
    job, path = job_store.claim_next()
    if job is None:
        return False
    directories = config_loader.load_directories()
    try:
        if job["operation"] in {"copy", "move"}:
            _run_transfer(job, path, directories)
        elif job["operation"] == "remote_copy":
            import remote_copy
            remote_copy.run_job(job, path, directories, Progress, _check_cancel)
        elif job["operation"] == "convert":
            _run_conversion(job, path, directories)
        else:
            raise ValueError(f"Unknown operation: {job.get('operation')}")
        job_store.finish(path, "complete")
    except JobCancelled as error:
        job_store.finish(path, "cancelled", str(error))
    except Exception as error:
        job_store.finish(path, "failed", str(error))
    return True


def _worker_lock():
    try:
        import fcntl
    except ImportError as error:
        raise RuntimeError("The job worker requires Linux file locking.") from error
    root = job_store.initialize()
    handle = open(root / "worker.lock", "a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise RuntimeError("Another fileList job worker is already running.") from error
    return handle


def main(argv=None):
    parser = argparse.ArgumentParser(description="Process fileList background jobs.")
    parser.add_argument("--once", action="store_true", help="Process at most one job.")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    lock = _worker_lock()
    try:
        job_store.recover_interrupted()
        job_store.cleanup_history(int(os.environ.get("JOB_HISTORY_DAYS", "7")))
        while True:
            worked = run_once()
            if args.once:
                return 0
            if not worked:
                time.sleep(max(args.poll_interval, 0.1))
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
