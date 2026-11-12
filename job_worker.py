#!/usr/bin/env python3
import argparse
import ctypes
import errno
import json
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
import operation_log
import worker_health
from utils import authorize_job, current_user_groups, ensure_free_space, is_safe_path, is_safe_segment, safe_dir_index, safe_join, transfer_item, resolve_job_directory


CHUNK_SIZE = 4 * 1024 * 1024
try:
    PROGRESS_INTERVAL = float(os.environ.get("JOB_PROGRESS_INTERVAL", "2"))
except ValueError:
    PROGRESS_INTERVAL = 2.0  # Startup validation reports the invalid setting.
AT_FDCWD = -100
RENAME_NOREPLACE = 1


class JobCancelled(Exception):
    pass


# renameat2 reports these when the kernel, libc or filesystem (NFS, many FUSE
# filesystems) cannot do RENAME_NOREPLACE; anything else is a real failure.
_NOREPLACE_UNSUPPORTED = {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}
# os.link reports these when the filesystem has no hard links.
_LINK_UNSUPPORTED = _NOREPLACE_UNSUPPORTED | {errno.EPERM, errno.EMLINK}


def _renameat2_noreplace(source, destination):
    """Atomic no-replace rename. Returns False when it is unsupported here."""
    if not sys.platform.startswith("linux"):
        return False
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        return False
    result = renameat2(
        AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(destination), RENAME_NOREPLACE
    )
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number in _NOREPLACE_UNSUPPORTED:
        return False
    raise OSError(error_number, os.strerror(error_number), destination)


def _link_noreplace(source, destination):
    """Publish a file (or symlink) by hard link, which fails if the name exists.

    Returns False when the filesystem has no hard links. EXDEV and EEXIST
    propagate: moves rely on EXDEV to fall back to copying.
    """
    try:
        os.link(source, destination, follow_symlinks=False)
    except OSError as error:
        if error.errno in _LINK_UNSUPPORTED:
            return False
        raise
    try:
        os.unlink(source)
    except OSError:
        # Keep the operation all-or-nothing: withdraw the new name.
        os.unlink(destination)
        raise
    return True


def _rename_without_replace(source, destination):
    """Rename source to destination without ever replacing an existing entry.

    Prefers Linux renameat2(RENAME_NOREPLACE). Filesystems without it (NFS,
    many FUSE mounts) fall back to a hard link for files, which is equally
    atomic, and finally, for folders or filesystems without hard links, to
    check-then-rename, which leaves a tiny window for a racing writer.
    """
    if os.name != "nt" and _renameat2_noreplace(source, destination):
        return
    if os.name != "nt" and not (os.path.isdir(source) and not os.path.islink(source)):
        if _link_noreplace(source, destination):
            return
    if os.path.lexists(destination):
        raise FileExistsError(errno.EEXIST, "File exists", destination)
    os.rename(source, destination)


def _check_cancel(job_id):
    worker_health.checkpoint()
    if job_store.cancellation_requested(job_id):
        raise JobCancelled("Cancelled by user.")


def _measure(path):
    if os.path.islink(path):
        return 0
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, directories, files in os.walk(path, followlinks=False):
        worker_health.checkpoint()
        directories[:] = [name for name in directories if not os.path.islink(os.path.join(root, name))]
        for name in files:
            worker_health.checkpoint()
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
        self.started = time.monotonic()
        job_store.update(job_path, bytes_total=total, bytes_completed=0,
                         bytes_per_second=0, seconds_remaining=None)

    def add(self, amount, force=False):
        self.completed += amount
        now = time.monotonic()
        if force or now - self.last_update >= PROGRESS_INTERVAL:
            _check_cancel(self.job["job_id"])
            job_store.update(
                self.path,
                bytes_total=self.total,
                bytes_completed=min(self.completed, self.total),
                bytes_per_second=round(self.completed / max(now - self.started, 0.001), 1),
                seconds_remaining=(round((self.total - self.completed) /
                    max(self.completed / max(now - self.started, 0.001), 0.001))
                    if self.completed else None),
            )
            self.last_update = now


def _copy_file(source, destination, progress):
    with open(source, "rb") as input_file, open(destination, "xb") as output_file:
        while True:
            _check_cancel(progress.job["job_id"])
            chunk = input_file.read(CHUNK_SIZE)
            if not chunk:
                break
            ensure_free_space(os.path.dirname(destination), len(chunk))
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
    ensure_free_space(os.path.dirname(destination), _measure(source))
    temporary = os.path.join(
        os.path.dirname(destination),
        f".{os.path.basename(destination)}.{uuid.uuid4().hex}.partial",
    )
    job_store.update(progress.path, temporary_path=temporary)
    try:
        if os.path.islink(source):
            os.symlink(os.readlink(source), temporary, target_is_directory=os.path.isdir(source))
        elif os.path.isdir(source):
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


def _validated_items(job, directories, job_path=None):
    source_info = job["source"]
    destination_info = job["destination"]
    source_index = resolve_job_directory(source_info, directories)
    destination_index = resolve_job_directory(destination_info, directories)
    source_path = source_info.get("path", "")
    destination_path = destination_info.get("path", "")
    names = source_info.get("names") or []
    if (
        source_index is None
        or destination_index is None
        or not is_safe_path(source_path)
        or not is_safe_path(destination_path)
        or not names
    ):
        raise ValueError("Invalid queued paths.")
    source_root, _ = directories[source_index]
    destination_root, _ = directories[destination_index]
    destination_directory = safe_join(destination_root, destination_path)
    if destination_directory is None or not os.path.isdir(destination_directory):
        raise ValueError("Destination directory no longer exists.")
    items = []
    for name in names:
        if job_path is not None:
            job_store.start_item(job_path, name)
        if not is_safe_segment(name):
            raise ValueError(f"Invalid selected item: {name!r}")
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
    items = _validated_items(job, directories, path)
    total = 0
    for name, (source, _) in zip(job["source"]["names"], items):
        job_store.start_item(path, name)
        with transfer_item(name):
            total += _measure(source)
    job_store.update(path, current_name=None)
    if job["operation"] == "copy":
        ensure_free_space(os.path.dirname(items[0][1]), total)
    progress = Progress(job, path, total)
    for name, (source, destination) in zip(job["source"]["names"], items):
        job_store.start_item(path, name)
        with transfer_item(name):
            _check_cancel(job["job_id"])
            if job["operation"] == "move":
                try:
                    _rename_without_replace(source, destination)
                    job_store.complete_item(path, name)
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
            job_store.complete_item(path, name)
    progress.add(0, force=True)


def _probe_stream_codecs(source):
    """Probe a media file's first real video and audio streams with ffprobe.

    Returns ((video_codec, video_index), (audio_codec, audio_index)); either
    entry is None when that stream type is absent, and both are None when
    probing is unavailable or fails.  Attached pictures (cover art) are not
    counted as video.
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None, None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_streams", "-of", "json", source],
            capture_output=True, text=True, timeout=60, check=False,
        )
        streams = json.loads(result.stdout).get("streams", [])
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, None
    video = None
    audio = None
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        codec = stream.get("codec_name")
        index = stream.get("index")
        if not codec or index is None:
            continue
        if stream.get("codec_type") == "video" and video is None:
            disposition = stream.get("disposition") or {}
            if disposition.get("attached_pic"):
                continue
            video = (codec, index)
        elif stream.get("codec_type") == "audio" and audio is None:
            audio = (codec, index)
    return video, audio


def _conversion_arg_sets(source):
    """Ordered ffmpeg argument sets to try for converting source to MP4.

    AVI/MPG/WMV sources keep the historical single full transcode (WMV's
    VC-1/WMA streams are never copied into the MP4).  MKV sources
    are probed: an H.264 video stream is copied rather than re-encoded --
    with the audio copied too when it is already AAC, otherwise transcoded
    to AAC -- and a full transcode is the fallback when the copy attempt is
    not possible or fails.  MKV attempts map only the chosen video and
    audio streams, so embedded subtitles are not carried into the MP4.
    """
    transcode = ["-c:v", "libx264", "-c:a", "aac"]
    if not source.lower().endswith(".mkv"):
        return [transcode]
    video, audio = _probe_stream_codecs(source)
    if video is None:
        return [transcode]
    maps = ["-map", f"0:{video[1]}"]
    if audio is not None:
        maps += ["-map", f"0:{audio[1]}"]
    attempts = []
    if video[0] == "h264":
        if audio is None or audio[0] == "aac":
            attempts.append(maps + ["-c", "copy"])
        else:
            attempts.append(maps + ["-c:v", "copy", "-c:a", "aac"])
    attempts.append(maps + transcode)
    return attempts


def _run_ffmpeg(command, job_id):
    """Run one ffmpeg command to completion.

    Returns (returncode, stderr_text).  Raises JobCancelled when the job's
    cancellation flag is set while ffmpeg runs.
    """
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as error_log:
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=error_log, text=True
        )
        while process.poll() is None:
            worker_health.checkpoint()
            if job_store.cancellation_requested(job_id):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise JobCancelled("Cancelled by user.")
            time.sleep(1)
        error_log.seek(0)
        return process.returncode, error_log.read()


def _run_conversion(job, path, directories):
    source_info = job["source"]
    index = resolve_job_directory(source_info, directories)
    relative_path = source_info.get("path", "")
    names = source_info.get("names") or []
    if index is None or not is_safe_path(relative_path) or len(names) != 1 or not is_safe_segment(names[0]):
        raise ValueError("Invalid queued conversion path.")
    job_store.start_item(path, names[0])
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
    try:
        error = ""
        for arg_set in _conversion_arg_sets(source):
            command = [
                ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-i", source, *arg_set,
                "-movflags", "+faststart", temporary,
            ]
            returncode, error = _run_ffmpeg(command, job["job_id"])
            if returncode == 0:
                break
            if os.path.lexists(temporary):
                os.remove(temporary)
        else:
            raise RuntimeError((error or "ffmpeg exited with an error.").strip()[-2000:])
        if os.path.lexists(output):
            raise FileExistsError(f"Destination already exists: {output_name}")
        _rename_without_replace(temporary, output)
        job_store.update(path, temporary_path=None)
        job_store.complete_item(path, names[0])
    finally:
        if os.path.lexists(temporary):
            os.remove(temporary)


class _ArchiveReporter:
    """Bridge archive_ops' progress hooks to the job record: byte progress,
    cancellation checks (Progress.add calls _check_cancel), and the staging
    path that crash recovery removes."""

    def __init__(self, job, path):
        self.job = job
        self.path = path
        self.progress = None

    def begin(self, total):
        _check_cancel(self.job["job_id"])
        self.progress = Progress(self.job, self.path, total)

    def add(self, amount):
        if self.progress is not None:
            self.progress.add(amount)

    def staging(self, staging_path):
        job_store.update(self.path, temporary_path=staging_path)


def _run_archive_job(job, path, directories):
    """Create an archive, or extract one, as a queued job.

    The web route has already checked the request; this re-checks it against
    the current configuration, because the folder may have changed while the
    job waited in the queue.
    """
    import archive_ops

    source_info, destination_info = job["source"], job["destination"]
    source_index = resolve_job_directory(source_info, directories)
    destination_index = resolve_job_directory(destination_info, directories)
    relative_path = source_info.get("path", "")
    names = source_info.get("names") or []
    outputs = destination_info.get("names") or []
    if (
        source_index != destination_index
        or destination_info.get("path", "") != relative_path
        or not is_safe_path(relative_path)
        or not names
        or not all(is_safe_segment(name) for name in names)
        or len(outputs) != 1
        or not is_safe_segment(outputs[0])
    ):
        raise ValueError("Invalid queued archive paths.")
    root, _ = directories[source_index]
    folder = safe_join(root, relative_path)
    target = safe_join(root, relative_path, outputs[0])
    if folder is None or target is None or not os.path.isdir(folder):
        raise ValueError("The folder for this job no longer exists.")
    reporter = _ArchiveReporter(job, path)
    job_store.start_item(path, names[0])
    if job["operation"] == "archive":
        archive_ops.create_archive(root, folder, names, target,
                                   destination_info.get("archive_type"), reporter=reporter)
    else:
        if len(names) != 1:
            raise ValueError("Invalid queued extraction.")
        archive = safe_join(root, relative_path, names[0])
        if archive is None or not os.path.isfile(archive):
            raise ValueError(f"Archive no longer exists: {names[0]}")
        archive_ops.extract_archive(archive, target, archive_ops.validate_archive_size,
                                    reporter=reporter)
    job_store.update(path, temporary_path=None)
    for name in names:
        job_store.complete_item(path, name)
    if reporter.progress is not None:
        reporter.progress.add(0, force=True)


def run_once():
    worker_health.checkpoint(state="checking_queue", phase="Checking the queue", job_id=None)
    job, path = job_store.claim_next()
    if job is None:
        worker_health.checkpoint(state="idle", phase="Waiting for jobs", job_id=None)
        return False
    worker_health.checkpoint(state="busy", phase="Processing transfer", job_id=job["job_id"])
    try:
        directories = config_loader.load_directories()
        if job["operation"] in {"copy", "move", "convert", "archive", "extract"}:
            authorize_job(job, directories, current_user_groups(job["owner"]))
        if job["operation"] in {"copy", "move"}:
            _run_transfer(job, path, directories)
        elif job["operation"] in {"archive", "extract"}:
            _run_archive_job(job, path, directories)
        elif job["operation"] in {"remote_copy", "remote_move"}:
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
    finally:
        worker_health.checkpoint(state="idle", phase="Waiting for jobs", job_id=None)
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


def _prune_operation_log():
    try:
        operation_log.prune(int(os.environ.get("FILELIST_LOG_RETENTION_DAYS", "30")))
    except (OSError, operation_log.sqlite3.Error) as error:
        import logging
        logging.getLogger(__name__).warning("Could not prune operation logs: %s", error)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Process fileList background jobs.")
    parser.add_argument("--once", action="store_true", help="Process at most one job.")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    from tools.check_config import require_startup
    require_startup()
    lock = _worker_lock()
    monitor = worker_health.Monitor(job_store.job_root()).start()
    try:
        worker_health.checkpoint(state="recovering", phase="Recovering interrupted jobs")
        job_store.recover_interrupted()
        job_store.cleanup_history(int(os.environ.get("JOB_HISTORY_DAYS", "7")))
        _prune_operation_log()
        next_log_cleanup = time.monotonic() + 86400
        while True:
            if time.monotonic() >= next_log_cleanup:
                _prune_operation_log()
                next_log_cleanup = time.monotonic() + 86400
            worked = run_once()
            if args.once:
                return 0
            if not worked:
                time.sleep(max(args.poll_interval, 0.1))
    finally:
        monitor.stop()
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
