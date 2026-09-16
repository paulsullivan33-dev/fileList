import os
import json
import html
import urllib.parse
import shutil

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
    if s is None or s == "":
        return False
    if s.startswith("/") or s.startswith("\\"):
        return False
    if ":" in s:
        return False
    if ".." in s:
        return False
    if "\\" in s or "\x00" in s:
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
    safe_msg = json.dumps(msg)
    return (
        css_js
        + f"<script>alert({safe_msg});"
        f"window.location='/?dir={dir_index}&path={safe_path}';</script>"
    )


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
