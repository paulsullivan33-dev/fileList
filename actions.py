import os
import html
import json
import shutil
import sys
import zipfile
import tarfile
import stat
import urllib.parse

from flask import request, redirect, abort, jsonify, session
from flask_wtf.csrf import generate_csrf

from styles import CSS_JS
import job_store
import remote_copy
from utils import (
    is_safe_path,
    is_safe_segment,
    safe_join,
    safe_dir_index,
    is_subpath,
    warn_and_redirect,
    user_can_access_directory,
)


def _allowed_directory_indices(directories):
    groups = session.get("groups", [])
    return [
        index for index, directory in enumerate(directories)
        if user_can_access_directory(directory, groups)
    ]


def _enqueue_job(operation, source, destination):
    owner = session.get("username")
    per_user_limit = int(os.environ.get("JOB_MAX_PENDING_PER_USER", "20"))
    if job_store.pending_count(owner) >= per_user_limit:
        raise RuntimeError(f"You already have {per_user_limit} pending jobs.")
    if job_store.has_active_destination(destination):
        raise RuntimeError("A job for that destination is already pending or running.")
    return job_store.create(operation, owner, source, destination)


def _queued_job_response(job, dir_index, path):
    public_job = {
        "job_id": job["job_id"],
        "operation": job["operation"],
        "status": job["status"],
    }
    job_json = json.dumps(public_job).replace("</", "<\\/")
    target_json = json.dumps(
        f"/?dir={dir_index}&path={urllib.parse.quote(path or '')}"
    ).replace("</", "<\\/")
    return (
        CSS_JS
        + "<script>"
          "const key='activeFileJobs';"
          "let jobs=[];try{jobs=JSON.parse(localStorage.getItem(key)||'[]')}catch(e){};"
        + f"const job={job_json};"
          "jobs=Array.isArray(jobs)?jobs.filter(x=>x.job_id!==job.job_id):[];"
          "jobs.push(job);localStorage.setItem(key,JSON.stringify(jobs));"
        + f"window.location={target_json};"
          "</script>"
    )


def _redirect_listing(dir_index, path):
    return redirect(f"/?dir={dir_index}&path={urllib.parse.quote(path or '')}")


def _build_move_here_link(src_index, src_path, src_name, dest_index, dest_path):
    return (
        "<form method='post' action='/move_do'>"
        f"<input type='hidden' name='csrf_token' value='{generate_csrf()}'>"
        f"<input type='hidden' name='src_dir' value='{src_index}'>"
        f"<input type='hidden' name='src_path' value='{html.escape(src_path, quote=True)}'>"
        f"<input type='hidden' name='src_name' value='{html.escape(src_name, quote=True)}'>"
        f"<input type='hidden' name='dest_dir' value='{dest_index}'>"
        f"<input type='hidden' name='dest_path' value='{html.escape(dest_path, quote=True)}'>"
        "<button class='move-here-btn' type='submit'>📥 Move Here</button></form>"
    )


def register(app, DIRECTORIES):

    @app.route("/remote_copy", methods=["POST"])
    def remote_copy_items():
        src_path = request.form.get("src_path", "")
        names = request.form.getlist("name")
        try:
            src_index, _ = remote_copy.validate_sources(
                request.form.get("src_dir"), src_path, names, DIRECTORIES,
                session.get("groups", []),
            )
            destination = remote_copy.destination_for(
                request.form.get("remote_id"), session.get("groups", []),
            )
            job = _enqueue_job(
                "remote_copy",
                {"directory_index": src_index, "path": src_path, "names": names},
                {"remote_id": destination["id"], "configuration": destination, "names": names},
            )
        except PermissionError:
            abort(403)
        except (OSError, ValueError, RuntimeError) as error:
            return jsonify(success=False, error=str(error)), 400
        return _queued_job_response(job, src_index, src_path)

    # ---------------------------------------------------------
    # DELETE
    # ---------------------------------------------------------
    @app.route("/delete", methods=["POST"], endpoint="delete_item")
    def delete_item():
        dir_index = safe_dir_index(request.values.get("dir"), DIRECTORIES)
        if dir_index is None:
            abort(400)

        path = request.values.get("path", "")
        name = request.values.get("name", "")

        if not is_safe_path(path) or not is_safe_segment(name):
            return warn_and_redirect(CSS_JS, "Invalid path.", dir_index, "")

        root_dir, _ = DIRECTORIES[dir_index]
        full = safe_join(root_dir, path, name)
        if full is None:
            return warn_and_redirect(CSS_JS, "Path escapes root.", dir_index, "")

        try:
            if os.path.isdir(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
        except FileNotFoundError:
            pass
        except OSError as e:
            return warn_and_redirect(CSS_JS, f"Delete failed: {e}", dir_index, path)

        return _redirect_listing(dir_index, path)

    # ---------------------------------------------------------
    # BULK DELETE
    # ---------------------------------------------------------
    @app.route("/bulk_delete", methods=["POST"], endpoint="bulk_delete")
    def bulk_delete():
        dir_index = safe_dir_index(request.values.get("dir"), DIRECTORIES)
        if dir_index is None:
            return jsonify({"success": False, "error": "Invalid directory."})

        path = request.args.get("path", "") or ""
        names = request.args.get("names", "") or ""
        name_list = [n for n in names.split(",") if n]

        if not is_safe_path(path) or not all(is_safe_segment(n) for n in name_list):
            return jsonify({"success": False, "error": "Invalid name or path."})

        root_dir, _ = DIRECTORIES[dir_index]
        errors = []
        deleted = 0
        for n in name_list:
            target = safe_join(root_dir, path, urllib.parse.unquote(n))
            if target is None:
                errors.append({"name": n, "error": "escapes root"})
                continue
            try:
                if os.path.isdir(target):
                    shutil.rmtree(target)
                else:
                    os.remove(target)
                deleted += 1
            except FileNotFoundError:
                deleted += 1  # already gone, count as success
            except OSError as e:
                errors.append({"name": n, "error": str(e)})

        return jsonify({
            "success": not errors,
            "deleted": deleted,
            "errors": errors,
        })

    # ---------------------------------------------------------
    # MKDIR
    # ---------------------------------------------------------
    @app.route("/mkdir", methods=["POST"], endpoint="mkdir")
    def mkdir():
        dir_index = safe_dir_index(request.form.get("dir"), DIRECTORIES)
        if dir_index is None:
            return jsonify({"success": False, "error": "Invalid directory."})

        path = request.form.get("path", "") or ""
        name = request.form.get("name", "") or ""

        if not is_safe_path(path) or not is_safe_segment(name):
            return jsonify({"success": False, "error": "Invalid name or path."})

        root_dir, _ = DIRECTORIES[dir_index]
        new_dir = safe_join(root_dir, path, name)
        if new_dir is None:
            return jsonify({"success": False, "error": "Path escapes root."})

        try:
            os.makedirs(new_dir, exist_ok=False)
        except FileExistsError:
            return jsonify({"success": False, "error": "Folder already exists."})
        except OSError as e:
            return jsonify({"success": False, "error": str(e)})

        return jsonify({"success": True})

    # ---------------------------------------------------------
    # BULK MOVE
    # ---------------------------------------------------------
    @app.route("/bulk_move", endpoint="bulk_move_ui")
    def bulk_move_ui():
        src_index = safe_dir_index(request.args.get("src_dir"), DIRECTORIES)
        dest_index = safe_dir_index(
            request.args.get("dest_dir", request.args.get("src_dir")),
            DIRECTORIES,
        )
        if src_index is None or dest_index is None:
            abort(400)

        src_path = request.args.get("src_path", "") or ""
        dest_path = request.args.get("dest_path", "") or ""
        names = request.args.getlist("name")
        if (
            not names
            or not all(is_safe_segment(name) for name in names)
            or not is_safe_path(src_path)
            or not is_safe_path(dest_path)
        ):
            return warn_and_redirect(CSS_JS, "Invalid name or path.", src_index, src_path)

        dest_root, _ = DIRECTORIES[dest_index]
        destination_dir = safe_join(dest_root, dest_path)
        if destination_dir is None or not os.path.isdir(destination_dir):
            return warn_and_redirect(CSS_JS, "Destination is not a directory.", src_index, src_path)

        try:
            entries = sorted(
                entry for entry in os.listdir(destination_dir)
                if os.path.isdir(os.path.join(destination_dir, entry))
            )
        except OSError as e:
            return warn_and_redirect(CSS_JS, f"Cannot list: {e}", src_index, src_path)

        import html as _html

        def picker_url(next_path, next_index=dest_index):
            params = [
                ("src_dir", src_index),
                ("src_path", src_path),
                ("dest_dir", next_index),
                ("dest_path", next_path),
            ]
            params.extend(("name", name) for name in names)
            return "/bulk_move?" + urllib.parse.urlencode(params)

        breadcrumb_html = f"<a href='{_html.escape(picker_url(''), quote=True)}'>ROOT</a>"
        cumulative = ""
        for part in dest_path.split("/") if dest_path else []:
            cumulative = part if not cumulative else f"{cumulative}/{part}"
            breadcrumb_html += (
                f" / <a href='{_html.escape(picker_url(cumulative), quote=True)}'>"
                f"{_html.escape(part)}</a>"
            )
        roots_html = "<span class='destination-separator' aria-hidden='true'>|</span>".join(
            f"<a class='dir-btn' href='{_html.escape(picker_url('', index), quote=True)}'>"
            f"{_html.escape(DIRECTORIES[index].name)}</a>"
            for index in _allowed_directory_indices(DIRECTORIES)
        )

        hidden = (
            f"<input type='hidden' name='csrf_token' value='{generate_csrf()}'>"
            f"<input type='hidden' name='src_dir' value='{src_index}'>"
            f"<input type='hidden' name='src_path' value='{_html.escape(src_path, quote=True)}'>"
            f"<input type='hidden' name='dest_dir' value='{dest_index}'>"
            f"<input type='hidden' name='dest_path' value='{_html.escape(dest_path, quote=True)}'>"
            + "".join(
                f"<input type='hidden' name='name' value='{_html.escape(name, quote=True)}'>"
                for name in names
            )
        )
        move_here_html = (
            "<form method='post' action='/bulk_move_do'>"
            + hidden
            + f"<button class='move-here-btn' type='submit'>📥 Move {len(names)} Item(s) Here</button>"
            + "</form>"
        )
        folders_html = "<ul>" + "".join(
            f"<li><a href='{_html.escape(picker_url(f'{dest_path}/{name}'.strip('/')), quote=True)}'>"
            f"{_html.escape(name)}</a></li>"
            for name in entries
        ) + "</ul>"

        return (
            CSS_JS
            + "<div class='header'><span style='color:white;'>Move Selected Items</span>"
              "<span class='dark-toggle' onclick='toggleDark()'>🌓 Theme</span></div>"
            + "<div class='action-box'><div class='action-title'>Select destination folder</div>"
            + remote_copy.selector_html(src_index, src_path, names, roots_html)
            + "<div id='local-destination-panel'>"
            + f"<div class='breadcrumbs'>{breadcrumb_html}</div><br>"
            + f"<div class='move-here-top'>{move_here_html}</div><br>"
            + folders_html
            + "</div></div>"
        )

    @app.route("/bulk_move_do", methods=["POST"], endpoint="bulk_move_do")
    def bulk_move_do():
        src_index = safe_dir_index(request.form.get("src_dir"), DIRECTORIES)
        dest_index = safe_dir_index(request.form.get("dest_dir"), DIRECTORIES)
        if src_index is None or dest_index is None:
            abort(400)

        src_path = request.form.get("src_path", "") or ""
        dest_path = request.form.get("dest_path", "") or ""
        names = request.form.getlist("name")
        if (
            not names
            or not all(is_safe_segment(name) for name in names)
            or not is_safe_path(src_path)
            or not is_safe_path(dest_path)
        ):
            return warn_and_redirect(CSS_JS, "Invalid name or path.", src_index, src_path)

        src_root, _ = DIRECTORIES[src_index]
        dest_root, _ = DIRECTORIES[dest_index]
        destination_dir = safe_join(dest_root, dest_path)
        if destination_dir is None or not os.path.isdir(destination_dir):
            return warn_and_redirect(CSS_JS, "Destination is not a directory.", src_index, src_path)

        for name in names:
            source = safe_join(src_root, src_path, name)
            target = safe_join(destination_dir, name)
            if source is None or target is None or not os.path.lexists(source):
                return warn_and_redirect(CSS_JS, f"Invalid or missing source: {name}", src_index, src_path)
            if os.path.realpath(source) == os.path.realpath(target):
                return warn_and_redirect(CSS_JS, f"Already in that folder: {name}", src_index, src_path)
            if os.path.isdir(source) and is_subpath(destination_dir, source):
                return warn_and_redirect(CSS_JS, f"Cannot move a folder into itself: {name}", src_index, src_path)
            if os.path.lexists(target):
                return warn_and_redirect(CSS_JS, f"Destination already exists: {name}", dest_index, dest_path)
        try:
            job = _enqueue_job(
                "move",
                {"directory_index": src_index, "path": src_path, "names": names},
                {"directory_index": dest_index, "path": dest_path, "names": names},
            )
        except (OSError, RuntimeError) as e:
            return warn_and_redirect(CSS_JS, f"Could not queue move: {e}", src_index, src_path)
        return _queued_job_response(job, dest_index, dest_path)

    # ---------------------------------------------------------
    # COPY
    # ---------------------------------------------------------
    @app.route("/copy", methods=["GET", "POST"], endpoint="copy_item")
    def copy_item():
        values = request.form if request.method == "POST" else request.args
        src_index = safe_dir_index(values.get("src_dir"), DIRECTORIES)
        if src_index is None:
            abort(400)
        dest_index = safe_dir_index(
            values.get("dest_dir", values.get("src_dir")),
            DIRECTORIES
        )
        if dest_index is None:
            abort(400)

        src_path = values.get("src_path", "")
        src_name = values.get("src_name", "")
        dest_path = values.get("dest_path", src_path)

        if not is_safe_path(src_path) or not is_safe_segment(src_name):
            return warn_and_redirect(CSS_JS, "Invalid source path.", src_index, "")
        if not is_safe_path(dest_path):
            return warn_and_redirect(CSS_JS, "Invalid destination path.",
                                     dest_index, "")

        src_root, _ = DIRECTORIES[src_index]
        dest_root, _ = DIRECTORIES[dest_index]

        src_full = safe_join(src_root, src_path, urllib.parse.unquote(src_name))
        dest_full = safe_join(dest_root, dest_path, urllib.parse.unquote(src_name))
        if src_full is None or dest_full is None:
            return warn_and_redirect(CSS_JS, "Path escapes root.", src_index, src_path)

        if request.method == "GET":
            destination_dir = safe_join(dest_root, dest_path)
            if destination_dir is None or not os.path.isdir(destination_dir):
                return warn_and_redirect(CSS_JS, "Destination is not a directory.", src_index, src_path)
            try:
                entries = sorted(
                    entry for entry in os.listdir(destination_dir)
                    if os.path.isdir(os.path.join(destination_dir, entry))
                )
            except OSError as e:
                return warn_and_redirect(CSS_JS, f"Cannot list: {e}", src_index, src_path)

            def picker_url(next_index, next_path):
                return "/copy?" + urllib.parse.urlencode({
                    "src_dir": src_index,
                    "src_path": src_path,
                    "src_name": src_name,
                    "dest_dir": next_index,
                    "dest_path": next_path,
                })

            roots_html = "<span class='destination-separator' aria-hidden='true'>|</span>".join(
                f"<a class='dir-btn' href='{html.escape(picker_url(index, ''), quote=True)}'>"
                f"{html.escape(DIRECTORIES[index].name)}</a>"
                for index in _allowed_directory_indices(DIRECTORIES)
            )
            breadcrumb_html = f"<a href='{html.escape(picker_url(dest_index, ''), quote=True)}'>ROOT</a>"
            cumulative = ""
            for part in dest_path.split("/") if dest_path else []:
                cumulative = part if not cumulative else f"{cumulative}/{part}"
                breadcrumb_html += (
                    f" / <a href='{html.escape(picker_url(dest_index, cumulative), quote=True)}'>"
                    f"{html.escape(part)}</a>"
                )
            folders_html = "<ul>" + "".join(
                f"<li><a href='{html.escape(picker_url(dest_index, f'{dest_path}/{entry}'.strip('/')), quote=True)}'>"
                f"{html.escape(entry)}</a></li>"
                for entry in entries
            ) + "</ul>"
            form_html = (
                "<form method='post' action='/copy'>"
                f"<input type='hidden' name='csrf_token' value='{generate_csrf()}'>"
                f"<input type='hidden' name='src_dir' value='{src_index}'>"
                f"<input type='hidden' name='src_path' value='{html.escape(src_path, quote=True)}'>"
                f"<input type='hidden' name='src_name' value='{html.escape(src_name, quote=True)}'>"
                f"<input type='hidden' name='dest_dir' value='{dest_index}'>"
                f"<input type='hidden' name='dest_path' value='{html.escape(dest_path, quote=True)}'>"
                "<button class='move-here-btn' type='submit'>📋 Copy Here</button></form>"
            )
            return (
                CSS_JS
                + "<div class='header'><span style='color:white;'>Copy Item</span>"
                  "<span class='dark-toggle' onclick='toggleDark()'>🌓 Theme</span></div>"
                + "<div class='action-box'><div class='action-title'>Select destination root and folder</div>"
                + remote_copy.selector_html(src_index, src_path, [src_name], roots_html)
                + "<div id='local-destination-panel'>"
                + f"<div class='breadcrumbs'>{breadcrumb_html}</div><br>"
                + form_html + folders_html + "</div></div>"
            )

        if os.path.isdir(src_full) and is_subpath(dest_full, src_full):
            return warn_and_redirect(
                CSS_JS, "Cannot copy a folder into itself.",
                src_index, src_path
            )

        if not os.path.lexists(src_full):
            return warn_and_redirect(CSS_JS, "Source does not exist.", src_index, src_path)
        if os.path.lexists(dest_full):
            return warn_and_redirect(CSS_JS, "Destination already exists.", dest_index, dest_path)
        try:
            job = _enqueue_job(
                "copy",
                {"directory_index": src_index, "path": src_path,
                 "names": [urllib.parse.unquote(src_name)]},
                {"directory_index": dest_index, "path": dest_path,
                 "names": [urllib.parse.unquote(src_name)]},
            )
        except (OSError, RuntimeError) as e:
            return warn_and_redirect(CSS_JS, f"Could not queue copy: {e}", src_index, src_path)
        return _queued_job_response(job, dest_index, dest_path)

    # ---------------------------------------------------------
    # RENAME
    # ---------------------------------------------------------
    @app.route("/rename", methods=["POST"], endpoint="rename_item")
    def rename_item():
        payload = request.get_json(silent=True) or {}
        dir_index = safe_dir_index(payload.get("dir"), DIRECTORIES)
        if dir_index is None:
            return {"success": False, "error": "Invalid directory."}

        path = payload.get("path", "") or ""
        old = payload.get("old") or ""
        new = payload.get("new") or ""

        if not is_safe_path(path):
            return {"success": False, "error": "Invalid path."}
        if not is_safe_segment(new):
            return {"success": False, "error": "Invalid new name."}

        root_dir, _ = DIRECTORIES[dir_index]
        old_full = safe_join(root_dir, path, urllib.parse.unquote(old))
        new_full = safe_join(root_dir, path, new)
        if old_full is None or new_full is None:
            return {"success": False, "error": "Path escapes root."}

        if not os.path.exists(old_full):
            return {"success": False, "error": "Source does not exist."}
        if os.path.exists(new_full):
            return {"success": False, "error": "Destination already exists."}

        try:
            os.rename(old_full, new_full)
        except OSError as e:
            return {"success": False, "error": str(e)}
        return {"success": True}

    # ---------------------------------------------------------
    # VIDEO CONVERSION (Linux only)
    # ---------------------------------------------------------
    @app.route("/convert", methods=["POST"], endpoint="convert_video")
    def convert_video():
        if not sys.platform.startswith("linux"):
            return {"success": False, "error": "Video conversion is available only on Linux."}, 404

        payload = request.get_json(silent=True) or {}
        dir_index = safe_dir_index(payload.get("dir"), DIRECTORIES)
        path = payload.get("path", "") or ""
        name = payload.get("name", "") or ""
        if dir_index is None or not is_safe_path(path) or not is_safe_segment(name):
            return {"success": False, "error": "Invalid name or path."}, 400

        extension = os.path.splitext(name)[1].lower()
        if extension not in {".avi", ".mpg", ".mpeg"}:
            return {"success": False, "error": "Only AVI and MPG files can be converted."}, 400

        root_dir, _ = DIRECTORIES[dir_index]
        source = safe_join(root_dir, path, name)
        output_name = os.path.splitext(name)[0] + ".mp4"
        output = safe_join(root_dir, path, output_name)
        if source is None or output is None or not os.path.isfile(source):
            return {"success": False, "error": "Source file does not exist."}, 404
        if os.path.exists(output):
            return {"success": False, "error": f"{output_name} already exists."}, 409

        try:
            job = _enqueue_job(
                "convert",
                {"directory_index": dir_index, "path": path, "names": [name]},
                {"directory_index": dir_index, "path": path, "names": [output_name]},
            )
        except (OSError, RuntimeError) as e:
            return {"success": False, "error": f"Could not queue conversion: {e}"}, 503
        return {
            "success": True,
            "job_id": job["job_id"],
            "status": "pending",
            "output": output_name,
        }, 202

    @app.route("/job_status", endpoint="job_status")
    def job_status():
        job_id = request.args.get("job_id", "")
        if not job_id:
            return {"success": False, "error": "Missing job id."}, 400
        job, _ = job_store.find(job_id)
        if job is None or job.get("owner") != session.get("username"):
            return {"success": False, "error": "Job not found."}, 404
        public_job = {
            key: job[key]
            for key in (
                "job_id", "operation", "status", "bytes_total",
                "bytes_completed", "created_at", "started_at", "finished_at", "error",
                "progress_unit", "remote_partial_path",
            )
            if key in job
        }
        return {"success": True, **public_job}

    @app.route("/job_cancel", methods=["POST"], endpoint="job_cancel")
    def job_cancel():
        payload = request.get_json(silent=True) or {}
        job = job_store.request_cancel(payload.get("job_id", ""), session.get("username"))
        if job is None:
            return {"success": False, "error": "Job not found."}, 404
        return {"success": True, "status": job.get("status")}

    # ---------------------------------------------------------
    # MOVE UI
    # ---------------------------------------------------------
    @app.route("/move", endpoint="move_ui")
    def move_ui():
        src_index = safe_dir_index(request.args.get("src_dir"), DIRECTORIES)
        if src_index is None:
            abort(400)
        dest_index = safe_dir_index(
            request.args.get("dest_dir", request.args.get("src_dir")),
            DIRECTORIES
        )
        if dest_index is None:
            abort(400)

        src_path = request.args.get("src_path", "")
        src_name = request.args.get("src_name", "")
        dest_path = request.args.get("dest_path", "")

        if not is_safe_path(src_path) or not is_safe_segment(src_name):
            return warn_and_redirect(CSS_JS, "Invalid source path.", src_index, "")
        if not is_safe_path(dest_path):
            return warn_and_redirect(CSS_JS, "Invalid destination path.",
                                     dest_index, "")

        dest_root, _ = DIRECTORIES[dest_index]
        full_path = safe_join(dest_root, dest_path) or dest_root

        try:
            entries = sorted(
                (e for e in os.listdir(full_path)
                 if os.path.isdir(os.path.join(full_path, e)))
            )
        except OSError as e:
            return warn_and_redirect(CSS_JS, f"Cannot list: {e}",
                                     dest_index, dest_path)

        import html as _html

        def move_picker_url(next_index, next_path):
            return "/move?" + urllib.parse.urlencode({
                "src_dir": src_index,
                "src_path": src_path,
                "src_name": src_name,
                "dest_dir": next_index,
                "dest_path": next_path,
            })

        roots_html = "<span class='destination-separator' aria-hidden='true'>|</span>".join(
            f"<a class='dir-btn' href='{_html.escape(move_picker_url(index, ''), quote=True)}'>"
            f"{_html.escape(DIRECTORIES[index].name)}</a>"
            for index in _allowed_directory_indices(DIRECTORIES)
        )

        breadcrumb_html = (
            f"<a href='{_html.escape(move_picker_url(dest_index, ''), quote=True)}'>ROOT</a>"
        )
        if dest_path:
            cumulative = ""
            for part in dest_path.split("/"):
                cumulative = part if not cumulative else f"{cumulative}/{part}"
                breadcrumb_html += (
                    f" / <a href='{_html.escape(move_picker_url(dest_index, cumulative), quote=True)}'>"
                    f"{_html.escape(part)}</a>"
                )

        move_here_html = _build_move_here_link(
            src_index, src_path, src_name, dest_index, dest_path
        )

        folders_html = "<ul>"
        for name in entries:
            new_dest = f"{dest_path}/{name}".strip("/")
            folders_html += (
                f"<li><a href='{_html.escape(move_picker_url(dest_index, new_dest), quote=True)}'>"
                f"{_html.escape(name)}</a></li>"
            )
        folders_html += "</ul>"

        return (
            CSS_JS
            + "<div class='header'>"
              "<span style='color:white;'>Move Item</span>"
              "<span class='dark-toggle' onclick='toggleDark()'>🌓 Theme</span></div>"
            + "<div class='action-box'>"
              "<div class='action-title'>Select destination folder</div>"
            + remote_copy.selector_html(src_index, src_path, [src_name], roots_html)
            + "<div id='local-destination-panel'>"
            + f"<div class='breadcrumbs'>{breadcrumb_html}</div><br>"
            + f"<div class='move-here-top'>{move_here_html}</div><br>"
            + folders_html
            + "</div></div>"
        )

    # ---------------------------------------------------------
    # MOVE action
    # ---------------------------------------------------------
    @app.route("/move_do", methods=["POST"], endpoint="move_do")
    def move_do():
        src_index = safe_dir_index(request.form.get("src_dir"), DIRECTORIES)
        dest_index = safe_dir_index(request.form.get("dest_dir"), DIRECTORIES)
        if src_index is None or dest_index is None:
            abort(400)

        src_path = request.form.get("src_path", "")
        src_name = request.form.get("src_name", "")
        dest_path = request.form.get("dest_path", "")

        if not is_safe_path(src_path) or not is_safe_segment(src_name):
            return warn_and_redirect(CSS_JS, "Invalid source path.", src_index, "")
        if not is_safe_path(dest_path):
            return warn_and_redirect(CSS_JS, "Invalid destination path.",
                                     dest_index, "")

        src_root, _ = DIRECTORIES[src_index]
        dest_root, _ = DIRECTORIES[dest_index]

        src_full = safe_join(src_root, src_path, urllib.parse.unquote(src_name))
        dest_full = safe_join(dest_root, dest_path, urllib.parse.unquote(src_name))
        if src_full is None or dest_full is None:
            return warn_and_redirect(CSS_JS, "Path escapes root.", src_index, src_path)

        if os.path.isdir(src_full) and is_subpath(dest_full, src_full):
            return warn_and_redirect(
                CSS_JS, "Cannot move a folder into itself.",
                src_index, src_path
            )

        if not os.path.lexists(src_full):
            return warn_and_redirect(CSS_JS, "Source does not exist.", src_index, src_path)
        if os.path.lexists(dest_full):
            return warn_and_redirect(CSS_JS, "Destination already exists.", dest_index, dest_path)
        try:
            job = _enqueue_job(
                "move",
                {"directory_index": src_index, "path": src_path,
                 "names": [urllib.parse.unquote(src_name)]},
                {"directory_index": dest_index, "path": dest_path,
                 "names": [urllib.parse.unquote(src_name)]},
            )
        except (OSError, RuntimeError) as e:
            return warn_and_redirect(CSS_JS, f"Could not queue move: {e}", src_index, src_path)
        return _queued_job_response(job, dest_index, dest_path)

    # ---------------------------------------------------------
    # ARCHIVE (POST, JSON)
    # ---------------------------------------------------------
    @app.route("/archive", methods=["POST"], endpoint="archive_create")
    def archive_create():
        payload = request.get_json(silent=True) or {}
        dir_index = safe_dir_index(payload.get("dir"), DIRECTORIES)
        if dir_index is None:
            return {"success": False, "error": "Invalid directory."}

        path = payload.get("path", "") or ""
        names = payload.get("names") or []
        archive_name = (payload.get("name") or "archive").strip()
        archive_type = (payload.get("type") or "zip").strip().lower()

        if not is_safe_path(path):
            return {"success": False, "error": "Invalid path."}
        if not is_safe_segment(archive_name) or not names:
            return {"success": False, "error": "Invalid name or empty selection."}
        for n in names:
            if not is_safe_segment(n):
                return {"success": False, "error": f"Invalid name: {n}"}

        root_dir, _ = DIRECTORIES[dir_index]
        cwd = safe_join(root_dir, path)
        if cwd is None:
            return {"success": False, "error": "Path escapes root."}

        archive_filename = f"{archive_name}.{archive_type}"
        archive_full = safe_join(root_dir, path, archive_filename)
        if archive_full is None:
            return {"success": False, "error": "Invalid archive name."}

        try:
            if archive_type == "zip":
                with zipfile.ZipFile(archive_full, "w", zipfile.ZIP_DEFLATED) as zf:
                    for n in names:
                        fp = os.path.join(cwd, n)
                        if os.path.isdir(fp):
                            for r, _, files in os.walk(fp):
                                for f in files:
                                    full = os.path.join(r, f)
                                    zf.write(full, os.path.relpath(full, cwd))
                        else:
                            zf.write(fp, n)
            elif archive_type in ("tar", "tgz"):
                mode = "w:gz" if archive_type == "tgz" else "w"
                with tarfile.open(archive_full, mode) as tf:
                    for n in names:
                        tf.add(os.path.join(cwd, n), arcname=n)
            elif archive_type == "7z":
                return {"success": False, "error": "7z not supported."}
            else:
                return {"success": False, "error": f"Unknown archive type: {archive_type}"}
        except OSError as e:
            return {"success": False, "error": str(e)}

        return {"success": True, "archive": archive_filename}

    # ---------------------------------------------------------
    # EXTRACT
    # ---------------------------------------------------------
    @app.route("/extract", methods=["POST"], endpoint="archive_extract")
    def archive_extract():
        dir_index = safe_dir_index(request.values.get("dir"), DIRECTORIES)
        if dir_index is None:
            abort(400)

        path = request.values.get("path", "")
        name = request.values.get("name", "")

        if not is_safe_path(path) or not is_safe_segment(name):
            return warn_and_redirect(CSS_JS, "Invalid path.", dir_index, "")

        root_dir, _ = DIRECTORIES[dir_index]
        archive_full = safe_join(root_dir, path, urllib.parse.unquote(name))
        if archive_full is None or not os.path.isfile(archive_full):
            return warn_and_redirect(CSS_JS, "Archive not found.", dir_index, path)

        extract_dir = safe_join(
            root_dir, path,
            os.path.splitext(urllib.parse.unquote(name))[0]
        )
        if extract_dir is None:
            return warn_and_redirect(CSS_JS, "Invalid extract target.",
                                     dir_index, path)
        os.makedirs(extract_dir, exist_ok=True)

        ext = os.path.splitext(archive_full.lower())[1]
        try:
            if ext == ".zip":
                with zipfile.ZipFile(archive_full) as zf:
                    real_extract = os.path.realpath(extract_dir)
                    for member in zf.infolist():
                        target = os.path.realpath(
                            os.path.join(extract_dir, member.filename)
                        )
                        mode = member.external_attr >> 16
                        if not (target == real_extract or target.startswith(real_extract + os.sep)):
                            raise ValueError(f"Unsafe entry: {member.filename}")
                        if stat.S_ISLNK(mode):
                            raise ValueError(f"Archive links are not allowed: {member.filename}")
                    zf.extractall(extract_dir)
            elif ext in (".tar", ".gz", ".tgz"):
                mode = "r:gz" if ext in (".gz", ".tgz") else "r"
                with tarfile.open(archive_full, mode) as tf:
                    real_extract = os.path.realpath(extract_dir)
                    for member in tf.getmembers():
                        target = os.path.realpath(
                            os.path.join(extract_dir, member.name)
                        )
                        if not (target == real_extract or target.startswith(real_extract + os.sep)):
                            raise ValueError(f"Unsafe entry: {member.name}")
                        if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                            raise ValueError(f"Unsafe entry type: {member.name}")
                    tf.extractall(extract_dir, members=tf.getmembers())
            else:
                return warn_and_redirect(CSS_JS, f"Unsupported format: {ext}",
                                         dir_index, path)
        except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as e:
            return warn_and_redirect(CSS_JS, f"Extract failed: {e}",
                                     dir_index, path)

        return _redirect_listing(dir_index, path)
