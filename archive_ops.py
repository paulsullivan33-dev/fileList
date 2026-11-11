"""Contained archive sources and staged, no-overwrite archive operations."""
import os
import shutil
import stat
import tarfile
import tempfile
import zipfile

from job_worker import _rename_without_replace
from utils import ensure_free_space, format_size, is_safe_segment, safe_join


ARCHIVE_MAX_MEMBERS = 10000
ARCHIVE_MAX_UNCOMPRESSED_BYTES = 10 * 1024 * 1024 * 1024
COPY_CHUNK = 4 * 1024 * 1024
ARCHIVE_FORMATS = {"zip", "tar", "tgz"}


def archive_limits():
    try:
        members = max(1, int(os.environ.get("ARCHIVE_MAX_MEMBERS", ARCHIVE_MAX_MEMBERS)))
    except ValueError:
        members = ARCHIVE_MAX_MEMBERS
    try:
        size = max(0, int(os.environ.get("ARCHIVE_MAX_UNCOMPRESSED_BYTES", ARCHIVE_MAX_UNCOMPRESSED_BYTES)))
    except ValueError:
        size = ARCHIVE_MAX_UNCOMPRESSED_BYTES
    return members, size


def validate_archive_size(entries):
    max_members, max_size = archive_limits()
    if len(entries) > max_members:
        raise ValueError(f"Archive has too many entries (maximum {max_members}).")
    total = sum(size for size in entries if size > 0)
    if total > max_size:
        raise ValueError(f"Archive expands to {format_size(total)}, above the {format_size(max_size)} limit.")
    return total


class _NullReporter:
    """Progress hooks for queued archive jobs; a direct call needs none."""
    def begin(self, total):
        pass

    def add(self, amount):
        pass

    def staging(self, path):
        pass


class _CountingReader:
    """Report bytes as they are read, so progress and cancellation reach
    long single-file reads (tarfile.addfile reads its source itself)."""
    def __init__(self, source, reporter):
        self.source = source
        self.reporter = reporter

    def read(self, size=-1):
        data = self.source.read(size)
        if data:
            self.reporter.add(len(data))
        return data


def _copy_stream(source, output, reporter):
    while True:
        chunk = source.read(COPY_CHUNK)
        if not chunk:
            break
        output.write(chunk)
        reporter.add(len(chunk))


def _source_entries(root, cwd, names):
    """Validate every entry, including directories that a walker might follow."""
    entries = []
    seen = set()

    def visit(path):
        relative = os.path.relpath(path, cwd).replace(os.sep, "/")
        if relative in seen:
            return
        info = os.lstat(path)
        reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(info.st_mode) or reparse:
            raise ValueError(f"Archive links are not allowed: {relative}")
        resolved = safe_join(root, path)
        if resolved is None:
            raise ValueError(f"Archive source escapes the configured root: {relative}")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError(f"Unsupported archive source: {relative}")
        seen.add(relative)
        entries.append((path, relative, info))
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as children:
                for child in sorted(children, key=lambda entry: entry.name):
                    visit(child.path)

    for name in names:
        visit(os.path.join(cwd, name))
    return entries


def _open_source(root, path, expected):
    # Recheck immediately before reading. O_NOFOLLOW rejects a replaced leaf;
    # inode checks also reject ancestor replacements between walk and open.
    if safe_join(root, path) is None:
        raise ValueError(f"Archive source escapes the configured root: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        actual = os.fstat(descriptor)
        if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError(f"Archive source changed while being read: {path}")
        result = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    return result


def create_archive(root, cwd, names, target, kind, reporter=None):
    reporter = reporter or _NullReporter()
    if kind not in ARCHIVE_FORMATS:
        raise ValueError(f"Unsupported archive format: {kind}")
    if os.path.lexists(target):
        raise FileExistsError(f"Destination already exists: {os.path.basename(target)}. Choose a new archive name.")
    entries = _source_entries(root, cwd, names)
    total = sum(info.st_size for _, _, info in entries if stat.S_ISREG(info.st_mode))
    ensure_free_space(cwd, total)
    reporter.begin(total)
    # The ".partial" suffix lets the worker's crash recovery remove a staging
    # folder that a killed worker left behind.
    with tempfile.TemporaryDirectory(prefix=".filelist-archive-", suffix=".partial", dir=cwd) as staging:
        reporter.staging(staging)
        temporary = os.path.join(staging, "archive")
        if kind == "zip":
            with zipfile.ZipFile(temporary, "x", zipfile.ZIP_DEFLATED) as archive:
                for path, relative, info in entries:
                    if stat.S_ISDIR(info.st_mode):
                        archive.writestr(relative + "/", b"")
                    else:
                        with _open_source(root, path, info) as source, archive.open(relative, "w", force_zip64=True) as output:
                            _copy_stream(source, output, reporter)
        else:
            with tarfile.open(temporary, "x:gz" if kind == "tgz" else "x") as archive:
                for path, relative, info in entries:
                    member = tarfile.TarInfo(relative)
                    member.mtime = info.st_mtime
                    member.mode = stat.S_IMODE(info.st_mode) & 0o777
                    if stat.S_ISDIR(info.st_mode):
                        member.type = tarfile.DIRTYPE
                        archive.addfile(member)
                    else:
                        member.size = info.st_size
                        with _open_source(root, path, info) as source:
                            archive.addfile(member, _CountingReader(source, reporter))
        _rename_without_replace(temporary, target)


def _member_target(staging, name, is_directory):
    normalized = name.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if normalized.startswith("/") or any(not is_safe_segment(part) for part in parts):
        raise ValueError(f"Unsafe archive entry: {name}")
    if not parts and not is_directory:
        raise ValueError(f"Invalid archive filename: {name}")
    target = safe_join(staging, *parts)
    if target is None:
        raise ValueError(f"Unsafe archive entry: {name}")
    return target


def _extract_member(staging, name, is_directory, source, reporter=None):
    target = _member_target(staging, name, is_directory)
    if is_directory:
        os.makedirs(target, exist_ok=True)
    else:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        # Also reject duplicate/normalized-colliding archive members.
        with open(target, "xb") as output:
            _copy_stream(source, output, reporter or _NullReporter())


def _flatten_duplicate_wrapper(staging, target):
    """Drop a redundant same-name wrapper folder from an extraction.

    Extracting 'books.zip' publishes a 'books' folder; when the archive
    itself wraps everything in a single 'books' folder, the result would
    be books/books/<files>. Hoist the inner folder's contents up one
    level so the files sit directly in the published folder. Archives
    whose single top folder has a different name keep their structure.
    """
    entries = os.listdir(staging)
    if len(entries) != 1:
        return
    inner = os.path.join(staging, entries[0])
    if entries[0] != os.path.basename(target) or not os.path.isdir(inner):
        return
    for child in os.listdir(inner):
        os.rename(os.path.join(inner, child), os.path.join(staging, child))
    os.rmdir(inner)


def _normalize_permissions(staging):
    """Give the extracted tree standard readable modes (dirs 755, files 644).

    Extraction deliberately ignores the modes stored in the archive, and
    the staging directory is created private (mkdtemp makes it 0700) while
    file modes follow the service umask -- without this the published
    folder could be readable only by the service account, no matter how
    readable the files inside it are.
    """
    for current, _dirs, files in os.walk(staging):
        os.chmod(current, 0o755)
        for name in files:
            os.chmod(os.path.join(current, name), 0o644)


def extract_archive(source, target, validate_size=None, reporter=None):
    validate_size = validate_size or validate_archive_size
    reporter = reporter or _NullReporter()
    if os.path.lexists(target):
        raise FileExistsError(f"Destination already exists: {os.path.basename(target)}. Rename the existing folder before extracting.")
    parent = os.path.dirname(target)
    staging = tempfile.mkdtemp(prefix=".filelist-extract-", suffix=".partial", dir=parent)
    try:
        reporter.staging(staging)
        extension = os.path.splitext(source.lower())[1]
        if extension == ".zip":
            with zipfile.ZipFile(source) as archive:
                members = archive.infolist()
                total = validate_size([member.file_size for member in members])
                ensure_free_space(parent, total)
                reporter.begin(total)
                for member in members:
                    if stat.S_ISLNK(member.external_attr >> 16):
                        raise ValueError(f"Archive links are not allowed: {member.filename}")
                    if member.is_dir():
                        _extract_member(staging, member.filename, True, None)
                    else:
                        with archive.open(member) as content:
                            _extract_member(staging, member.filename, False, content, reporter)
        elif extension in {".tar", ".gz", ".tgz"}:
            with tarfile.open(source, "r:*") as archive:
                members = archive.getmembers()
                total = validate_size([member.size for member in members])
                ensure_free_space(parent, total)
                reporter.begin(total)
                for member in members:
                    if member.isdir():
                        _extract_member(staging, member.name, True, None)
                    elif member.isfile():
                        with archive.extractfile(member) as content:
                            _extract_member(staging, member.name, False, content, reporter)
                    else:
                        raise ValueError(f"Unsafe archive entry type: {member.name}")
        else:
            raise ValueError(f"Unsupported archive format: {extension}")
        _flatten_duplicate_wrapper(staging, target)
        _normalize_permissions(staging)
        _rename_without_replace(staging, target)
    finally:
        if os.path.lexists(staging):
            shutil.rmtree(staging)
