import os
import json
import html
import urllib.parse
import shutil
from contextlib import contextmanager

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
ARCHIVE_EXT = {".zip", ".tar", ".gz", ".tgz", ".7z"}


def configured_bytes(name, default):
    """Read a non-negative byte setting without making a bad setting fatal."""
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def minimum_free_bytes():
    return configured_bytes("FILELIST_MIN_FREE_BYTES", 256 * 1024 * 1024)


def remote_minimum_free_bytes():
    return configured_bytes("FILELIST_REMOTE_MIN_FREE_BYTES", minimum_free_bytes())


def ensure_free_space(directory, required_bytes, reserve_bytes=None):
    """Fail before an operation consumes space needed by the application."""
    required = max(0, int(required_bytes))
    reserve = minimum_free_bytes() if reserve_bytes is None else max(0, int(reserve_bytes))
    free = shutil.disk_usage(directory).free
    if free < required + reserve:
        raise OSError(
            f"Not enough free disk space: need {format_size(required + reserve)}, "
            f"but only {format_size(free)} is available."
        )


# -------------------------------------------------------------------
# Path safety
# -------------------------------------------------------------------
def is_safe_segment(s):
    """A single path segment (no separators, no traversal)."""
    if not isinstance(s, str) or s in {"", ".", ".."}:
        return False
    if s.startswith("/") or s.startswith("\\"):
        return False
    if ":" in s:
        return False
    if "/" in s or "\\" in s or "\x00" in s:
        return False
    if "／" in s or "＼" in s:
        return False
    return True


def is_safe_path(p):
    """A relative POSIX-style path made of safe segments."""
    if p is None or p == "":
        return True
    p_norm = p.replace("\\", "/")
    parts = [seg for seg in p_norm.split("/") if seg != ""]
    return all(is_safe_segment(part) for part in parts)


def safe_join(root, *parts):
    """Join parts to root and verify the result is still inside root.

    Returns the resolved absolute path, or None on escape attempt.
    """
    candidate = os.path.normpath(os.path.join(root, *parts))
    real_root = os.path.realpath(root)
    real_candidate = os.path.realpath(candidate)
    if real_candidate == real_root or real_candidate.startswith(real_root + os.sep):
        return real_candidate
    return None


# -------------------------------------------------------------------
# UI helpers
# -------------------------------------------------------------------
def warn_and_redirect(css_js, msg, dir_index=0, path=""):
    safe_path = urllib.parse.quote(path or "")
    safe_msg = json.dumps(msg).replace("<", "\\u003c")
    return (
        css_js
        + f"<script>alert({safe_msg});"
        f"window.location='/?dir={dir_index}&path={safe_path}';</script>"
    )


@contextmanager
def transfer_item(name):
    """Keep the selected item in worker errors, including terse tool errors."""
    try:
        yield
    except (OSError, RuntimeError, ValueError) as error:
        raise RuntimeError(f"Transfer failed for {name!r}: {error}") from error


def format_size(num):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024.0:
            return f"{num:.1f} {unit}" if unit != "B" else f"{num} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def get_icon(name, is_dir):
    if is_dir:
        return "📁"
    ext = os.path.splitext(name.lower())[1]
    if ext in IMAGE_EXT:
        return "🖼️"
    if ext in ARCHIVE_EXT:
        return "🗜️"
    if ext in {".txt", ".md", ".log"}:
        return "📄"
    return "📎"


def classify_row(name, is_dir):
    if name.startswith("."):
        return "row-hidden"
    if is_dir:
        return "row-dir"
    return ""


def is_subpath(child, parent):
    try:
        rc = os.path.realpath(child)
        rp = os.path.realpath(parent)
        return rc == rp or rc.startswith(rp + os.sep)
    except Exception:
        return False


def safe_dir_index(value, directories, default=0):
    try:
        i = int(value)
    except (TypeError, ValueError):
        i = default
    if i < 0 or i >= len(directories):
        return None
    return i


def user_can_access_directory(directory, groups):
    """A directory is available only to users in its configured group."""
    return bool(directory.group) and directory.group in groups


def current_user_groups(username, users_path=None):
    """Read current grants; missing users or unreadable configuration deny access."""
    users_path = users_path or os.path.join(os.path.dirname(__file__), "users.json")
    try:
        with open(users_path, encoding="utf-8") as source:
            users = json.load(source)
    except (OSError, ValueError):
        return []
    if not isinstance(users, list):
        return []
    for user in users:
        if isinstance(user, dict) and user.get("username") == username:
            groups = user.get("groups", "")
            return [group.strip() for group in groups.split(",") if group.strip()] if isinstance(groups, str) else []
    return []


def resolve_job_directory(selection, directories):
    """Never reinterpret a durable job's old list position as a new folder."""
    identifier = selection.get("directory_id")
    root = selection.get("directory_root")
    if not identifier or not root:
        raise ValueError("This older job has no stable folder identity; submit the operation again.")
    matches = [(index, directory) for index, directory in enumerate(directories)
               if directory.stable_id == identifier]
    if len(matches) != 1:
        raise ValueError("A folder required by this job was removed or its identity changed; submit the operation again.")
    index, directory = matches[0]
    if os.path.normcase(os.path.realpath(directory.root)) != root:
        raise ValueError("A folder root changed since this job was submitted; submit the operation again.")
    return index


def bind_job_selection(selection, directories):
    selection = dict(selection)
    if "directory_id" in selection or "directory_root" in selection:
        index = resolve_job_directory(selection, directories)
    else:
        index = safe_dir_index(selection.get("directory_index"), directories, default=-1)
        if index is None:
            raise ValueError("Invalid job directory.")
    directory = directories[index]
    selection.update(directory_index=index, directory_id=directory.stable_id,
                     directory_root=os.path.normcase(os.path.realpath(directory.root)))
    return selection


def authorize_job(job, directories, groups):
    """Resolve immutable folder references before checking current grants."""
    operation = job.get("operation")
    if operation not in {"copy", "move", "convert", "remote_copy"}:
        raise ValueError("Unknown job operation.")
    selections = [job["source"]]
    if operation in {"copy", "move"}:
        selections.append(job["destination"])
    for selection in selections:
        index = resolve_job_directory(selection, directories)
        if not user_can_access_directory(directories[index], groups):
            raise PermissionError("You cannot access a directory required by this job.")
    if operation == "remote_copy":
        import remote_copy
        remote_copy.destination_for(job["destination"].get("remote_id"), groups)
