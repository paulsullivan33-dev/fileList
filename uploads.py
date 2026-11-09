import os
import re
import shutil
import tempfile
import uuid

from flask import request, jsonify, abort, session
import operation_log
from werkzeug.utils import secure_filename
from job_worker import _rename_without_replace

from utils import configured_bytes, ensure_free_space, is_safe_path, is_safe_segment, safe_join, safe_dir_index


# -----------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------
ALLOWED_EXTENSIONS = {
    # Documents
    ".txt", ".md", ".pdf", ".doc", ".docx", ".odt", ".rtf",
    # Spreadsheets
    ".xls", ".xlsx", ".ods", ".csv",
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico",
    # Video
    ".mp4", ".webm", ".mov", ".mkv", ".avi", ".mpg", ".mpeg", ".m4v",
    # Audio
    ".mp3", ".wav", ".ogg", ".flac", ".m4a",
    # Code / data
    ".py", ".js", ".ts", ".html", ".css", ".json", ".xml",
    ".yaml", ".yml", ".toml", ".ini", ".conf", ".sh", ".sql", ".log", ".nzb",
    # Archives
    ".zip", ".tar", ".gz", ".tgz", ".7z",
    # Other
    ".iso", ".img", ".3mf",
}

# 500 MB per file, 2 GB per request
MAX_FILE_SIZE = configured_bytes("MAX_FILE_SIZE", 500 * 1024 * 1024)
MAX_REQUEST_SIZE = configured_bytes("MAX_REQUEST_SIZE", 2 * 1024 * 1024 * 1024)

# Typed or pasted text (the create-text-file route). Generous enough for
# a large pasted NZB, small enough to stay a text file, not an upload.
MAX_TEXT_SIZE = configured_bytes("MAX_TEXT_SIZE", 10 * 1024 * 1024)

RESERVED_NAMES = {".", "..", ".htaccess", ".git", ".gitignore"}


# -----------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------
def _allowed(filename):
    if not filename:
        return False
    ext = os.path.splitext(filename.lower())[1]
    return ext in ALLOWED_EXTENSIONS


def _sanitize(name):
    if not name:
        return None
    # Strip any path the browser may have included
    name = os.path.basename(name.replace("\\", "/"))
    safe = secure_filename(name)
    if not safe or safe in RESERVED_NAMES:
        return None
    return safe


def _unique_path(directory, filename):
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    n = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base} ({n}){ext}")
        n += 1
    return candidate


def _save_stream(fs, target_dir, max_size):
    """Stream a FileStorage to target_dir. Returns saved dict or raises."""
    name = _sanitize(fs.filename or "")
    if not name:
        raise ValueError("invalid filename")
    if not _allowed(name):
        raise ValueError(f"extension not allowed: {name}")

    descriptor, temporary = tempfile.mkstemp(prefix=".filelist-upload-", suffix=".partial", dir=target_dir)
    written = 0
    try:
        with os.fdopen(descriptor, "wb") as out:
            while True:
                buf = fs.stream.read(1024 * 1024)
                if not buf:
                    break
                written += len(buf)
                if written > max_size:
                    raise ValueError(f"file too large (max {max_size} bytes)")
                ensure_free_space(target_dir, len(buf))
                out.write(buf)
            out.flush()
            os.fsync(out.fileno())

        base, extension = os.path.splitext(name)
        number = 0
        while True:
            filename = name if number == 0 else f"{base} ({number}){extension}"
            dest = os.path.join(target_dir, filename)
            try:
                # Atomically publish a complete file; a competing upload or a
                # symlink already at this name is a collision, never a target.
                _rename_without_replace(temporary, dest)
                break
            except FileExistsError:
                number += 1
    finally:
        if os.path.lexists(temporary):
            os.remove(temporary)

    return {"name": os.path.basename(dest), "size": written}


def _create_text_file(target_dir, name, data):
    """Write data as a NEW file named name in target_dir.

    The caller validates the name. Publication follows the same
    contract as _save_stream: the content is written to a temp file in
    the same directory, fsynced, then published with
    _rename_without_replace, so an existing file, directory, or
    symlink at that name is a collision (FileExistsError), never an
    overwrite. Returns {"name": name, "size": len(data)}.
    """
    descriptor, temporary = tempfile.mkstemp(prefix=".filelist-create-", suffix=".partial", dir=target_dir)
    try:
        with os.fdopen(descriptor, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        dest = os.path.join(target_dir, name)
        _rename_without_replace(temporary, dest)
    finally:
        if os.path.lexists(temporary):
            os.remove(temporary)
    return {"name": name, "size": len(data)}


# -----------------------------------------------------------------
# Routes
# -----------------------------------------------------------------
def register(app, DIRECTORIES):

    @app.errorhandler(413)
    def too_large(_):
        return jsonify({
            "success": False,
            "error": f"Upload too large (max {MAX_REQUEST_SIZE} bytes)."
        }), 413

    @app.route("/upload", methods=["POST"], endpoint="upload_files")
    def upload_files():
        dir_index = safe_dir_index(request.form.get("dir"), DIRECTORIES)
        if dir_index is None:
            return jsonify({"success": False, "error": "Invalid directory."})

        path = request.form.get("path", "") or ""
        if not is_safe_path(path):
            return jsonify({"success": False, "error": "Invalid path."})

        root_dir, _ = DIRECTORIES[dir_index]
        target_dir = safe_join(root_dir, path)
        if target_dir is None or not os.path.isdir(target_dir):
            return jsonify({"success": False,
                            "error": "Destination is not a directory."})

        try:
            ensure_free_space(target_dir, request.content_length or 0)
        except OSError as error:
            return jsonify({"success": False, "error": str(error)}), 507

        files = request.files.getlist("files")
        if not files:
            return jsonify({"success": False, "error": "No files provided."})

        saved, errors = [], []
        for fs in files:
            try:
                result = _save_stream(fs, target_dir, MAX_FILE_SIZE)
                saved.append(result)
                operation_log.record(session.get("username"), "upload", "complete", source=fs.filename,
                                     destination=os.path.join(target_dir, result["name"]), item=result["name"])
            except (ValueError, OSError) as e:
                errors.append({"name": fs.filename, "error": str(e)})
                operation_log.record(session.get("username"), "upload", "failed", source=fs.filename,
                                     destination=target_dir, item=fs.filename, detail=str(e))

        return jsonify({
            "success": not errors,
            "saved": saved,
            "errors": errors,
        })

    @app.route("/create_text", methods=["POST"], endpoint="create_text_file")
    def create_text_file():
        dir_index = safe_dir_index(request.form.get("dir"), DIRECTORIES)
        if dir_index is None:
            return jsonify({"success": False, "error": "Invalid directory."})

        path = request.form.get("path", "") or ""
        if not is_safe_path(path):
            return jsonify({"success": False, "error": "Invalid path."})

        root_dir, _ = DIRECTORIES[dir_index]
        target_dir = safe_join(root_dir, path)
        if target_dir is None or not os.path.isdir(target_dir):
            return jsonify({"success": False,
                            "error": "Destination is not a directory."})

        # The name is used exactly as typed (unlike upload names, which
        # are sanitized): anything that is not one safe segment is
        # refused outright, so the file created is the file asked for.
        name = request.form.get("name", "") or ""
        if not is_safe_segment(name) or name in RESERVED_NAMES:
            return jsonify({"success": False, "error": "Invalid filename."})
        if not _allowed(name):
            return jsonify({"success": False,
                            "error": f"File type not allowed: {name}"})

        data = (request.form.get("content", "") or "").encode("utf-8")
        if len(data) > MAX_TEXT_SIZE:
            return jsonify({"success": False,
                            "error": f"Text too large (max {MAX_TEXT_SIZE} bytes)."})

        destination = os.path.join(target_dir, name)
        try:
            ensure_free_space(target_dir, len(data))
            result = _create_text_file(target_dir, name, data)
        except FileExistsError:
            operation_log.record(session.get("username"), "create_text", "failed",
                                 destination=destination, item=name,
                                 detail="File already exists")
            return jsonify({"success": False,
                            "error": "A file with that name already exists."})
        except OSError as e:
            operation_log.record(session.get("username"), "create_text", "failed",
                                 destination=destination, item=name, detail=str(e))
            return jsonify({"success": False, "error": str(e)})

        operation_log.record(session.get("username"), "create_text", "complete",
                             destination=destination, item=result["name"])
        return jsonify({"success": True,
                        "name": result["name"], "size": result["size"]})


# -----------------------------------------------------------------
# Optional: chunked upload for very large files
# -----------------------------------------------------------------
# Uncomment the block below and add `uploads_chunked` to create_app
# if you need to upload files larger than your MAX_REQUEST_SIZE.

"""
import json

@app.route('/upload_chunk', methods=['POST'], endpoint='upload_chunk')
def upload_chunk():
    dir_index = safe_dir_index(request.args.get('dir'), DIRECTORIES)
    if dir_index is None:
        return jsonify({'success': False, 'error': 'Invalid directory.'})

    path = request.args.get('path', '') or ''
    if not is_safe_path(path):
        return jsonify({'success': False, 'error': 'Invalid path.'})

    upload_id = request.args.get('upload_id', '')
    if not re.fullmatch(r'[a-f0-9]{16,64}', upload_id):
        return jsonify({'success': False, 'error': 'Invalid upload id.'})

    try:
        index = int(request.args.get('index', '0'))
        total = int(request.args.get('total', '1'))
    except ValueError:
        return jsonify({'success': False, 'error': 'Bad chunk index.'})
    if index < 0 or total < 1 or index >= total:
        return jsonify({'success': False, 'error': 'Bad chunk range.'})

    root_dir, _ = DIRECTORIES[dir_index]
    target_dir = safe_join(root_dir, path)
    if target_dir is None or not os.path.isdir(target_dir):
        return jsonify({'success': False, 'error': 'Destination invalid.'})

    chunk_dir = os.path.join(tempfile.gettempdir(), 'uploads', upload_id)
    os.makedirs(chunk_dir, exist_ok=True)
    chunk = request.files.get('chunk')
    if not chunk:
        return jsonify({'success': False, 'error': 'No chunk data.'})
    chunk.save(os.path.join(chunk_dir, f'{index:06d}.part'))

    if index == total - 1:
        filename = _sanitize(request.args.get('filename', ''))
        if not filename:
            shutil.rmtree(chunk_dir, ignore_errors=True)
            return jsonify({'success': False, 'error': 'Invalid filename.'})
        if not _allowed(filename):
            shutil.rmtree(chunk_dir, ignore_errors=True)
            return jsonify({'success': False, 'error': 'Extension not allowed.'})

        final = os.path.join(target_dir, filename)
        if os.path.exists(final):
            final = _unique_path(target_dir, filename)

        try:
            with open(final, 'wb') as out:
                for i in range(total):
                    part = os.path.join(chunk_dir, f'{i:06d}.part')
                    with open(part, 'rb') as f:
                        shutil.copyfileobj(f, out)
            size = os.path.getsize(final)
        except OSError as e:
            shutil.rmtree(chunk_dir, ignore_errors=True)
            return jsonify({'success': False, 'error': str(e)})
        finally:
            shutil.rmtree(chunk_dir, ignore_errors=True)

        return jsonify({
            'success': True,
            'saved': [{'name': os.path.basename(final), 'size': size}],
            'complete': True,
        })

    return jsonify({'success': True, 'complete': False})
"""
