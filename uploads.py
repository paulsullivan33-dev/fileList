import os
import re
import shutil
import tempfile
import uuid

from flask import request, jsonify, abort
from werkzeug.utils import secure_filename

from utils import is_safe_path, is_safe_segment, safe_join, safe_dir_index


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
MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE", 500 * 1024 * 1024))
MAX_REQUEST_SIZE = int(os.environ.get("MAX_REQUEST_SIZE", 2 * 1024 * 1024 * 1024))

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

    dest = os.path.join(target_dir, name)
    if os.path.exists(dest):
        dest = _unique_path(target_dir, name)

    written = 0
    with open(dest, "wb") as out:
        while True:
            buf = fs.stream.read(1024 * 1024)
            if not buf:
                break
            written += len(buf)
            if written > max_size:
                out.close()
                os.remove(dest)
                raise ValueError(f"file too large (max {max_size} bytes)")
            out.write(buf)

    return {"name": os.path.basename(dest), "size": written}


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

        files = request.files.getlist("files")
        if not files:
            return jsonify({"success": False, "error": "No files provided."})

        saved, errors = [], []
        for fs in files:
            try:
                result = _save_stream(fs, target_dir, MAX_FILE_SIZE)
                saved.append(result)
            except (ValueError, OSError) as e:
                errors.append({"name": fs.filename, "error": str(e)})

        return jsonify({
            "success": not errors,
            "saved": saved,
            "errors": errors,
        })


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
