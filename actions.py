import os
import html
import json
import shutil
import sys
import zipfile
import tarfile
import stat
import urllib.parse
import sqlite3
import operation_log

from flask import request, redirect, abort, jsonify, session
from flask_wtf.csrf import generate_csrf

from styles import CSS_JS
import job_store
import remote_copy
import config_loader
import archive_ops
import transfer_preview
import worker_health
from utils import (
    authorize_job,
    bind_job_selection,
    current_user_groups,
    is_safe_path,
    is_safe_segment,
    ensure_free_space,
    format_size,
    safe_join,
    safe_dir_index,
    is_subpath,
    warn_and_redirect,
    user_can_access_directory,
)


ARCHIVE_MAX_MEMBERS = 10000
ARCHIVE_MAX_UNCOMPRESSED_BYTES = 10 * 1024 * 1024 * 1024


def _archive_limits():
    try:
        members = max(1, int(os.environ.get("ARCHIVE_MAX_MEMBERS", ARCHIVE_MAX_MEMBERS)))
    except ValueError:
        members = ARCHIVE_MAX_MEMBERS
    try:
        size = max(0, int(os.environ.get("ARCHIVE_MAX_UNCOMPRESSED_BYTES", ARCHIVE_MAX_UNCOMPRESSED_BYTES)))
    except ValueError:
        size = ARCHIVE_MAX_UNCOMPRESSED_BYTES
    return members, size


def _validate_archive_size(entries):
    max_members, max_size = _archive_limits()
    if len(entries) > max_members:
        raise ValueError(f"Archive has too many entries (maximum {max_members}).")
    total = sum(size for size in entries if size > 0)
    if total > max_size:
        raise ValueError(f"Archive expands to {format_size(total)}, above the {format_size(max_size)} limit.")
    return total


def _allowed_directory_indices(directories):
    groups = session.get("groups", [])
    return [
        index for index, directory in enumerate(directories)
        if user_can_access_directory(directory, groups)
    ]


def _enqueue_job(operation, source, destination, directories):
    owner = session.get("username")
    try:
        source = bind_job_selection(source, directories)
        if operation in {"copy", "move", "convert"}:
            destination = bind_job_selection(destination, directories)
        expected_preview = request.form.get("preview_signature")
        if expected_preview and expected_preview != transfer_preview.signature(operation, source, destination):
            raise ValueError("Folder configuration changed since the preview. Reopen the destination and check again.")
        per_user_limit = int(os.environ.get("JOB_MAX_PENDING_PER_USER", "20"))
        if job_store.pending_count(owner) >= per_user_limit:
            raise RuntimeError(f"You already have {per_user_limit} pending jobs.")
        if job_store.has_active_destination(destination):
            raise RuntimeError("A job for that destination is already pending or running.")
        return job_store.create(operation, owner, source, destination)
    except (OSError, RuntimeError, ValueError) as error:
        operation_log.record_job({"owner": owner, "operation": operation, "source": source, "destination": destination},
                                 "failed", detail=f"Could not queue operation: {error}")
        raise


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
        "<form method='post' action='/move_do' data-preview-operation='move'>"
        f"<input type='hidden' name='csrf_token' value='{generate_csrf()}'>"
        f"<input type='hidden' name='src_dir' value='{src_index}'>"
        f"<input type='hidden' name='src_path' value='{html.escape(src_path, quote=True)}'>"
        f"<input type='hidden' name='src_name' value='{html.escape(src_name, quote=True)}'>"
        f"<input type='hidden' name='dest_dir' value='{dest_index}'>"
        f"<input type='hidden' name='dest_path' value='{html.escape(dest_path, quote=True)}'>"
        "<button class='move-here-btn' type='submit' disabled>📥 Move Here</button></form>"
    )


def register(app, DIRECTORIES):

    def enqueue_job(operation, source, destination):
        return _enqueue_job(operation, source, destination, DIRECTORIES)

    def log_operation(operation, outcome, source="", destination="", item="", detail=""):
        operation_log.record(session.get("username"), operation, outcome, source, destination, item, detail=detail)

    @app.route("/worker_status")
    def worker_status():
        return {"success": True, **worker_health.status()}

    @app.route("/transfer_preview", methods=["POST"])
    def preview_transfer():
        values = request.get_json(silent=True) if request.is_json else request.form
        values = values or {}
        names = values.get("names") if request.is_json else request.form.getlist("name")
        if not names and values.get("src_name"):
            names = [values.get("src_name")]
        try:
            preview = transfer_preview.plan(
                values.get("operation", "copy"), values.get("src_dir"), values.get("src_path", ""), names,
                DIRECTORIES, session.get("groups", []), values.get("dest_dir"), values.get("dest_path", ""),
                values.get("remote_id", ""), values.get("remote_subpath", ""),
            )
        except PermissionError as error:
            return {"success": False, "error": str(error)}, 403
        except (OSError, RuntimeError, ValueError) as error:
            return {"success": False, "error": str(error)}, 400
        return {"success": True, **preview}

    @app.route("/operations")
    def operation_history():
        query = request.args.get("q", "").strip()
        operation = request.args.get("operation", "").strip()
        outcome = request.args.get("outcome", "").strip()
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1
        try:
            records = operation_log.search(session.get("username"), query, operation, outcome, limit=51, offset=(page - 1) * 50)
        except (OSError, sqlite3.Error) as error:
            app.logger.warning("Operation log unavailable: %s", error)
            return CSS_JS + "<h1>Operation log</h1><p>The operation log is unavailable. Check the application log for details.</p>", 503
        def page_link(number, label):
            url = "/operations?" + urllib.parse.urlencode({"q": query, "operation": operation, "outcome": outcome, "page": number})
            return f"<a href='{html.escape(url, quote=True)}'>{label}</a> "
        navigation = page_link(page - 1, "Previous") if page > 1 else ""
        if len(records) > 50:
            navigation += page_link(page + 1, "Next")
        rows = "".join("<tr>" + "".join(f"<td>{html.escape(str(record[key]))}</td>"
                      for key in ("timestamp", "username", "operation", "outcome", "item", "source", "destination", "detail", "job_id")) + "</tr>"
                      for record in records[:50]) or "<tr><td colspan='9'>No matching operations.</td></tr>"
        fields = "".join(f"<label>{label} <input name='{key}' value='{html.escape(value, quote=True)}'></label> "
                         for key, label, value in (("q", "Search files, folders, errors or job IDs", query),
                                                  ("operation", "Operation", operation), ("outcome", "Outcome", outcome)))
        return (CSS_JS + "<h1>Operation log</h1><p><a href='/'>Back to files</a> · <a href='/jobs'>Recent transfers</a></p>"
                + "<form method='get'>" + fields + "<button>Search</button></form>"
                + "<p>Outcomes: queued, running, complete, failed, cancelled. Times are UTC.</p>"
                + navigation + "<table class='table'><thead><tr>"
                + "".join(f"<th>{label}</th>" for label in ("When", "User", "Operation", "Outcome", "Item", "Source", "Destination", "Details", "Job ID"))
                + "</tr></thead><tbody>" + rows + "</tbody></table>" + navigation)

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
            destination = remote_copy.with_subpath(destination, request.form.get("remote_subpath", ""))
            job = enqueue_job(
                "remote_copy",
                {"directory_index": src_index, "path": src_path, "names": names},
                {"remote_id": destination["id"], "configuration": destination, "names": names},
            )
        except PermissionError:
            abort(403)
        except (OSError, ValueError, RuntimeError) as error:
            return jsonify(success=False, error=str(error)), 400
        return _queued_job_response(job, src_index, src_path)

    @app.route("/remote_directories")
    def remote_directories():
        try:
            destination = remote_copy.destination_for(request.args.get("remote_id"), session.get("groups", []))
            return {"success": True, **remote_copy.list_directories(destination, request.args.get("path", ""))}
        except PermissionError:
            abort(403)
        except (OSError, ValueError, RuntimeError) as error:
            return {"success": False, "error": str(error)}, 502

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
            log_operation("delete", "failed", source=full, item=name, detail=str(e))
            return warn_and_redirect(CSS_JS, f"Delete failed: {e}", dir_index, path)

        log_operation("delete", "complete", source=full, item=name)
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
                log_operation("delete", "complete", source=target, item=n)
            except FileNotFoundError:
                deleted += 1  # already gone, count as success
                log_operation("delete", "complete", source=target, item=n, detail="Already absent")
            except OSError as e:
                errors.append({"name": n, "error": str(e)})
                log_operation("delete", "failed", source=target, item=n, detail=str(e))

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
            log_operation("mkdir", "failed", destination=new_dir, item=name, detail="Folder already exists")
            return jsonify({"success": False, "error": "Folder already exists."})
        except OSError as e:
            log_operation("mkdir", "failed", destination=new_dir, item=name, detail=str(e))
            return jsonify({"success": False, "error": str(e)})

        log_operation("mkdir", "complete", destination=new_dir, item=name)
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
        if not names:
            return warn_and_redirect(CSS_JS, "No items were selected.", src_index, src_path)
        bad_names = [name for name in names if not is_safe_segment(name) or "/" in name]
        if bad_names:
            return warn_and_redirect(CSS_JS, f"Invalid selected item: {bad_names[0]!r}.", src_index, src_path)
        if not is_safe_path(src_path):
            return warn_and_redirect(CSS_JS, f"Invalid source path: {src_path!r}.", src_index, "")
        if not is_safe_path(dest_path):
            return warn_and_redirect(CSS_JS, f"Invalid destination path: {dest_path!r}.", src_index, src_path)

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
            "<form method='post' action='/bulk_move_do' data-preview-operation='move'>"
            + hidden
            + f"<button class='move-here-btn' type='submit' disabled>📥 Move {len(names)} Item(s) Here</button>"
            + "</form>"
        )
        folders_html = "<ul>" + "".join(
            f"<li><a href='{_html.escape(picker_url(f'{dest_path}/{name}'.strip('/')), quote=True)}'>"
            f"📁 {_html.escape(name)}</a></li>"
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
            + "<div>Folders in this destination:</div>"
            + folders_html
            + f"<div class='move-here-top'>{move_here_html}</div>"
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
        if not names:
            return warn_and_redirect(CSS_JS, "No items were selected.", src_index, src_path)
        bad_names = [name for name in names if not is_safe_segment(name) or "/" in name]
        if bad_names:
            return warn_and_redirect(CSS_JS, f"Invalid selected item: {bad_names[0]!r}.", src_index, src_path)
        if not is_safe_path(src_path):
            return warn_and_redirect(CSS_JS, f"Invalid source path: {src_path!r}.", src_index, "")
        if not is_safe_path(dest_path):
            return warn_and_redirect(CSS_JS, f"Invalid destination path: {dest_path!r}.", src_index, src_path)

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
            job = enqueue_job(
                "move",
                {"directory_index": src_index, "path": src_path, "names": names},
                {"directory_index": dest_index, "path": dest_path, "names": names},
            )
        except (OSError, RuntimeError, ValueError) as e:
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
            return warn_and_redirect(CSS_JS, f"Invalid source path or filename: {src_path!r} / {src_name!r}.", src_index, "")
        if not is_safe_path(dest_path):
            return warn_and_redirect(CSS_JS, "Invalid destination path.",
                                     dest_index, "")

        src_root, _ = DIRECTORIES[src_index]
        dest_root, _ = DIRECTORIES[dest_index]

        src_full = safe_join(src_root, src_path, src_name)
        dest_full = safe_join(dest_root, dest_path, src_name)
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
                "<form method='post' action='/copy' data-preview-operation='copy'>"
                f"<input type='hidden' name='csrf_token' value='{generate_csrf()}'>"
                f"<input type='hidden' name='src_dir' value='{src_index}'>"
                f"<input type='hidden' name='src_path' value='{html.escape(src_path, quote=True)}'>"
                f"<input type='hidden' name='src_name' value='{html.escape(src_name, quote=True)}'>"
                f"<input type='hidden' name='dest_dir' value='{dest_index}'>"
                f"<input type='hidden' name='dest_path' value='{html.escape(dest_path, quote=True)}'>"
                "<button class='move-here-btn' type='submit' disabled>📋 Copy Here</button></form>"
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
            log_operation("copy", "failed", src_full, dest_full, src_name, "Source does not exist")
            return warn_and_redirect(CSS_JS, f"Source does not exist: {src_name!r}.", src_index, src_path)
        if os.path.lexists(dest_full):
            log_operation("copy", "failed", src_full, dest_full, src_name, "Destination already exists")
            return warn_and_redirect(CSS_JS, f"Destination already exists: {src_name!r}.", dest_index, dest_path)
        try:
            job = enqueue_job(
                "copy",
                {"directory_index": src_index, "path": src_path,
                 "names": [src_name]},
                {"directory_index": dest_index, "path": dest_path,
                 "names": [src_name]},
            )
        except (OSError, RuntimeError, ValueError) as e:
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
            log_operation("rename", "failed", old_full, new_full, old, "Source does not exist")
            return {"success": False, "error": "Source does not exist."}
        if os.path.exists(new_full):
            log_operation("rename", "failed", old_full, new_full, old, "Destination already exists")
            return {"success": False, "error": "Destination already exists."}

        try:
            os.rename(old_full, new_full)
        except OSError as e:
            log_operation("rename", "failed", old_full, new_full, old, str(e))
            return {"success": False, "error": str(e)}
        log_operation("rename", "complete", old_full, new_full, old)
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
        if extension not in {".avi", ".mpg", ".mpeg", ".mkv"}:
            return {"success": False, "error": "Only AVI, MPG and MKV files can be converted."}, 400

        root_dir, _ = DIRECTORIES[dir_index]
        source = safe_join(root_dir, path, name)
        output_name = os.path.splitext(name)[0] + ".mp4"
        output = safe_join(root_dir, path, output_name)
        if source is None or output is None or not os.path.isfile(source):
            return {"success": False, "error": "Source file does not exist."}, 404
        if os.path.exists(output):
            return {"success": False, "error": f"{output_name} already exists."}, 409

        try:
            ensure_free_space(os.path.dirname(output), os.path.getsize(source))
        except OSError as error:
            return {"success": False, "error": str(error)}, 507
        try:
            job = enqueue_job(
                "convert",
                {"directory_index": dir_index, "path": path, "names": [name]},
                {"directory_index": dir_index, "path": path, "names": [output_name]},
            )
        except (OSError, RuntimeError, ValueError) as e:
            return {"success": False, "error": f"Could not queue conversion: {e}"}, 503
        return {
            "success": True,
            "job_id": job["job_id"],
            "status": "pending",
            "output": output_name,
        }, 202

    @app.route("/jobs", endpoint="job_history")
    def job_history():
        jobs = job_store.history(session.get("username"))
        queue = job_store.queue_snapshot(session.get("username"))
        rows = []
        for job in jobs:
            summary = job_store.item_summary(job)
            position = queue.get(job["job_id"], {})
            status = str(job.get("status", ""))
            if status == "pending" and position:
                status = f"Queued #{position['queue_position']} · {position['jobs_ahead']} job(s) ahead"
            cancel_requested = job_store.cancellation_requested(job["job_id"])
            if cancel_requested and job.get("status") == "running":
                status += " · Cancellation requested"
            source = job.get("source", {})
            names = source.get("names", []) if isinstance(source, dict) else []
            item_label = ", ".join(str(name) for name in names[:3])
            if len(names) > 3:
                item_label += f" and {len(names) - 3} more"
            item_details = ""
            for label, key in (("Completed", "completed_names"), ("Failed", "failed_names"), ("Remaining", "remaining_names")):
                items = summary[key]
                item_details += (
                    f"<details><summary>{label} ({len(items)})</summary><ul>"
                    + "".join(f"<li>{html.escape(str(name))}</li>" for name in items) + "</ul></details>"
                )
            if summary["current_name"]:
                item_details += f"<p>In progress: {html.escape(str(summary['current_name']))}</p>"
            total = job.get("bytes_total", 0)
            completed = job.get("bytes_completed", 0)
            transfer = f"{format_size(completed)} / {format_size(total)}" if total else "—"
            speed = job.get("bytes_per_second", 0)
            eta = job.get("seconds_remaining")
            if speed:
                transfer += f" · {format_size(speed)}/s"
            if eta is not None:
                transfer += f" · about {int(eta)}s left"
            when = job.get("finished_at") or job.get("started_at") or job.get("created_at") or ""
            detail = job.get("error") or job.get("remote_partial_path") or ""
            if summary["requires_resubmit"]:
                detail += " Select the remaining files and start a new transfer; this older transfer cannot be retried safely."
            rows.append(
                "<tr>"
                f"<td>{html.escape(str(job.get('operation', '')))}</td>"
                f"<td>{html.escape(status)}</td>"
                f"<td>{html.escape(item_label)}{item_details}</td>"
                f"<td>{html.escape(transfer)}</td>"
                f"<td>{html.escape(str(when).replace('T', ' ').replace('+00:00', ' UTC'))}</td>"
                f"<td>{html.escape(str(detail))}"
                + (f" <form class='inline-form' method='post' action='/job_cancel'><input type='hidden' name='csrf_token' value='{generate_csrf()}'>"
                   f"<input type='hidden' name='job_id' value='{html.escape(job['job_id'], quote=True)}'>"
                   f"<button {'disabled' if cancel_requested else ''}>{'Cancellation requested' if cancel_requested else 'Cancel pending' if job.get('status') == 'pending' else 'Request cancellation'}</button></form>"
                   if job.get('status') in {'pending', 'running'} else "")
                + (f" <form class='inline-form' method='post' action='/job_retry'><input type='hidden' name='csrf_token' value='{generate_csrf()}'>"
                   f"<input type='hidden' name='job_id' value='{html.escape(job['job_id'], quote=True)}'><button>Retry remaining ({summary['retry_count']})</button></form>"
                   if summary['can_retry'] else "")
                + "</td>"
                "</tr>"
            )
        table = "".join(rows) or "<tr><td colspan='6'>No recent transfers.</td></tr>"
        return (
            CSS_JS
            + "<div class='title-row'><h1 class='app-title'>Recent transfers</h1></div>"
            + worker_health.banner()
            + "<p class='breadcrumbs'><a href='/'>← Back to files</a></p>"
            + "<p><a href='/jobs'>Refresh queue</a> · Pending jobs run in queue order after the current transfer.</p>"
            + "<p>Retry remaining retries failed and unstarted items. Completed items are kept.</p>"
            + "<table class='table'><thead><tr><th>Operation</th><th>Status</th>"
              "<th>Items</th><th>Progress</th><th>When</th><th>Details</th>"
              "</tr></thead><tbody>" + table + "</tbody></table>"
        )

    @app.route("/job_status", endpoint="job_status")
    def job_status():
        job_id = request.args.get("job_id", "")
        if not job_id:
            return {"success": False, "error": "Missing job id."}, 400
        job, stored_path = job_store.find(job_id)
        if job is None or job.get("owner") != session.get("username"):
            return {"success": False, "error": "Job not found."}, 404
        job["status"] = stored_path.parent.name
        public_job = {
            key: job[key]
            for key in (
                "job_id", "operation", "status", "bytes_total",
                "bytes_completed", "created_at", "started_at", "finished_at", "error",
                "progress_unit", "remote_partial_path", "bytes_per_second", "seconds_remaining",
            )
            if key in job
        }
        position = job_store.queue_snapshot(session.get("username")).get(job_id, {}) if job["status"] == "pending" else {}
        return {"success": True, **public_job, **job_store.item_summary(job), **position,
                "cancellation_requested": job_store.cancellation_requested(job_id)}

    @app.route("/job_cancel", methods=["POST"], endpoint="job_cancel")
    def job_cancel():
        payload = request.get_json(silent=True) or request.form
        job = job_store.request_cancel(payload.get("job_id", ""), session.get("username"))
        if job is None:
            return {"success": False, "error": "Job not found."}, 404
        if not request.is_json and request.accept_mimetypes.best != "application/json":
            return redirect("/jobs")
        return {"success": True, "status": job.get("status"),
                "cancellation_requested": job_store.cancellation_requested(job["job_id"])}

    @app.route("/job_retry", methods=["POST"], endpoint="job_retry")
    def job_retry():
        old, _ = job_store.find(request.values.get("job_id", ""))
        if old is None or old.get("owner") != session.get("username"):
            return {"success": False, "error": "Job not found."}, 404
        if old.get("status") not in {"failed", "cancelled"}:
            return {"success": False, "error": "Only failed or cancelled jobs can be retried."}, 400
        try:
            authorize_job(old, DIRECTORIES, session.get("groups", []))
            authorize_job(old, DIRECTORIES, current_user_groups(
                old["owner"], os.path.join(app.root_path, "users.json")))
            source, destination = job_store.retry_selection(old)
            job = enqueue_job(old["operation"], source, destination)
        except PermissionError as error:
            return {"success": False, "error": str(error)}, 403
        except (KeyError, OSError, RuntimeError, ValueError) as error:
            return {"success": False, "error": str(error)}, 400
        if request.accept_mimetypes.best == "application/json":
            return {"success": True, "job_id": job["job_id"],
                    "operation": job["operation"], "status": job["status"]}, 202
        return _queued_job_response(job, job["source"]["directory_index"], job["source"].get("path", ""))

    def configuration_groups():
        current_groups = current_user_groups(session.get("username"), os.path.join(app.root_path, "users.json"))
        return set(session.get("groups", [])) & set(current_groups)

    def visible_configuration():
        groups = configuration_groups()
        result = {"directories": [{"name": d.name, "root": d.root, "group": d.group}
                                   for d in DIRECTORIES if user_can_access_directory(d, groups)]}
        try:
            result["remote_destinations"] = [{"id": d["id"], "name": d["name"], "path": d["path"]}
                                             for d in remote_copy.load_destinations() if d["group"] in groups]
            result["remote_error"] = None
        except (OSError, ValueError):
            result["remote_destinations"], result["remote_error"] = [], "Remote configuration is unavailable."
        return result

    @app.route("/config_status", endpoint="config_status")
    def config_status():
        return {"success": True, **visible_configuration()}

    @app.route("/configuration", endpoint="configuration")
    def configuration():
        visible = visible_configuration()
        directories = "".join(
            f"<li><strong>{html.escape(d['name'])}</strong>: {html.escape(d['root'])} ({html.escape(d['group'])})</li>"
            for d in visible["directories"]
        ) or "<li>No directories available to your account.</li>"
        remote_rows = "".join(f"<li><strong>{html.escape(d['name'])}</strong>: {html.escape(d['path'])}</li>"
                              for d in visible["remote_destinations"]) or "<li>No remote destinations available to your account.</li>"
        remote_error = f"<p>{html.escape(visible['remote_error'])}</p>" if visible["remote_error"] else ""
        return (CSS_JS + "<div class='title-row'><h1 class='app-title'>Configuration</h1></div>"
                "<p class='breadcrumbs'><a href='/'>← Back to files</a></p><h2>Directories</h2><ul>" + directories + "</ul>"
                + "<h2>Remote destinations</h2>" + remote_error + "<ul>" + remote_rows + "</ul>"
                + f"<form id='reload-configuration'><input type='hidden' name='csrf_token' value='{generate_csrf()}'><button>Reload configuration</button></form>"
                  "<script>document.getElementById('reload-configuration').onsubmit=async e=>{e.preventDefault();const r=await fetch('/config_reload',{method:'POST',body:new FormData(e.target)});const d=await r.json();if(d.success)location.reload();else alert(d.error||'Reload failed');};</script>")

    @app.route("/config_reload", methods=["POST"], endpoint="config_reload")
    def config_reload():
        try:
            remote_copy.load_destinations()
            directories = config_loader.load_directories()
        except (OSError, ValueError, json.JSONDecodeError):
            return {"success": False, "error": "Configuration reload failed."}, 400
        DIRECTORIES[:] = directories
        groups = configuration_groups()
        return {"success": True, "directories": sum(user_can_access_directory(d, groups) for d in DIRECTORIES)}

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
            return warn_and_redirect(CSS_JS, f"Invalid source path or filename: {src_path!r} / {src_name!r}.", src_index, "")
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
                f"📁 {_html.escape(name)}</a></li>"
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
            + "<div>Folders in this destination:</div>"
            + folders_html
            + f"<div class='move-here-top'>{move_here_html}</div>"
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
            return warn_and_redirect(CSS_JS, f"Invalid source path or filename: {src_path!r} / {src_name!r}.", src_index, "")
        if not is_safe_path(dest_path):
            return warn_and_redirect(CSS_JS, "Invalid destination path.",
                                     dest_index, "")

        src_root, _ = DIRECTORIES[src_index]
        dest_root, _ = DIRECTORIES[dest_index]

        src_full = safe_join(src_root, src_path, src_name)
        dest_full = safe_join(dest_root, dest_path, src_name)
        if src_full is None or dest_full is None:
            return warn_and_redirect(CSS_JS, "Path escapes root.", src_index, src_path)

        if os.path.isdir(src_full) and is_subpath(dest_full, src_full):
            return warn_and_redirect(
                CSS_JS, "Cannot move a folder into itself.",
                src_index, src_path
            )

        if not os.path.lexists(src_full):
            log_operation("move", "failed", src_full, dest_full, src_name, "Source does not exist")
            return warn_and_redirect(CSS_JS, f"Source does not exist: {src_name!r}.", src_index, src_path)
        if os.path.lexists(dest_full):
            log_operation("move", "failed", src_full, dest_full, src_name, "Destination already exists")
            return warn_and_redirect(CSS_JS, f"Destination already exists: {src_name!r}.", dest_index, dest_path)
        try:
            job = enqueue_job(
                "move",
                {"directory_index": src_index, "path": src_path,
                 "names": [src_name]},
                {"directory_index": dest_index, "path": dest_path,
                 "names": [src_name]},
            )
        except (OSError, RuntimeError, ValueError) as e:
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
            archive_ops.create_archive(root_dir, cwd, names, archive_full, archive_type)
        except FileExistsError as error:
            log_operation("archive", "failed", cwd, archive_full, ", ".join(names), str(error))
            return {"success": False, "error": str(error)}, 409
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, tarfile.TarError) as error:
            log_operation("archive", "failed", cwd, archive_full, ", ".join(names), str(error))
            return {"success": False, "error": str(error)}, 400

        log_operation("archive", "complete", cwd, archive_full, ", ".join(names))
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

        try:
            archive_ops.extract_archive(archive_full, extract_dir, _validate_archive_size)
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, tarfile.TarError) as error:
            log_operation("extract", "failed", archive_full, extract_dir, name, str(error))
            return warn_and_redirect(CSS_JS, f"Extract failed: {error}", dir_index, path)

        log_operation("extract", "complete", archive_full, extract_dir, name)
        return _redirect_listing(dir_index, path)
