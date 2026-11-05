import os
import sys
import json
import time
import html
import urllib.parse
import folder_listing
import worker_health

from flask import abort, request, session
from flask_wtf.csrf import generate_csrf

from styles import APP_TITLE, CSS_JS
from utils import (
    is_safe_path,
    safe_join,
    safe_dir_index,
    format_size,
    get_icon,
    classify_row,
    IMAGE_EXT,
    ARCHIVE_EXT,
    warn_and_redirect,
    user_can_access_directory,
)


def _display_name(name):
    """Return filesystem text that can always be emitted in a UTF-8 response."""
    return name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _external_sites_html():
    """Load and render configured HTTP(S) links."""
    config_path = os.path.join(os.path.dirname(__file__), "external_sites.json")
    try:
        with open(config_path, "r", encoding="utf-8") as config_file:
            sites = json.load(config_file)
    except (OSError, json.JSONDecodeError):
        return ""

    links = []
    for site in sites if isinstance(sites, list) else []:
        if not isinstance(site, dict):
            continue
        description = site.get("description")
        link = site.get("link")
        parsed = urllib.parse.urlparse(link) if isinstance(link, str) else None
        if (
            not isinstance(description, str)
            or not description.strip()
            or parsed is None
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
        ):
            continue
        links.append(
            f"<a href='{html.escape(link, quote=True)}' target='_blank' "
            f"rel='noopener noreferrer'>{html.escape(description.strip())}</a>"
        )

    if not links:
        return ""
    return "<div class='external-sites'>" + " <span>|</span> ".join(links) + "</div>"


def register(app, DIRECTORIES):

    @app.route("/folder_stats")
    def folder_stats():
        index = safe_dir_index(request.args.get("dir"), DIRECTORIES, default=-1)
        if index is None or not user_can_access_directory(DIRECTORIES[index], session.get("groups", [])):
            abort(403)
        path = request.args.get("path", "")
        if not is_safe_path(path):
            return {"success": False, "error": "Invalid folder path."}, 400
        folder = safe_join(DIRECTORIES[index].root, path)
        if folder is None:
            return {"success": False, "error": "Invalid folder path."}, 400
        try:
            result = folder_listing.totals(folder)
        except OSError as error:
            return {"success": False, "error": f"Cannot calculate folder totals: {error}"}, 400
        return {"success": True, **result, "size": format_size(result["bytes"])}

    @app.route("/")
    def index():
        user_groups = session.get("groups", [])
        available_indices = [
            index for index, directory in enumerate(DIRECTORIES)
            if user_can_access_directory(directory, user_groups)
        ]
        if not available_indices:
            abort(403, "Your account is not assigned to any directories.")
        dir_index = safe_dir_index(
            request.args.get("dir"), DIRECTORIES, default=available_indices[0]
        )
        if dir_index is None or dir_index not in available_indices:
            abort(403)

        path = request.args.get("path", "")
        if not is_safe_path(path):
            return warn_and_redirect(CSS_JS, "Invalid path. Dot-walk is not allowed.",
                                     dir_index, "")

        root_dir, base_url = DIRECTORIES[dir_index]
        full_path = safe_join(root_dir, path)
        if full_path is None:
            return warn_and_redirect(CSS_JS, "Path escapes the allowed root.",
                                     dir_index, "")

        try:
            listing = folder_listing.page(full_path, request.args.get("page", 1), request.args.get("per_page", 100),
                                          request.args.get("q", ""), request.args.get("sort", 1), request.args.get("order", "asc"))
            entries = listing["entries"]
        except (FileNotFoundError, NotADirectoryError) as e:
            # The requested child no longer exists (or was replaced by a file).
            # Redirecting to the same path would repeat this error forever, so
            # recover at the selected directory's root instead.
            return warn_and_redirect(CSS_JS, f"Cannot list directory: {e}",
                                     dir_index, "")
        except PermissionError as e:
            return warn_and_redirect(CSS_JS, f"Cannot list directory: {e}",
                                     dir_index, path)

        # Build rows + compute stats
        rows = []
        total_size = 0
        dir_count = 0
        file_count = 0

        for entry in entries:
            name = entry["name"]
            # Some Unix filesystems expose non-UTF-8 filename bytes as surrogate
            # code points. Encode links from the original filesystem bytes, but
            # use replacement characters anywhere text is rendered as UTF-8.
            encoded = urllib.parse.quote_from_bytes(os.fsencode(name))
            display_name = _display_name(name)
            item_path = os.path.join(full_path, name)
            is_dir = entry["is_dir"]
            ext = os.path.splitext(name.lower())[1]

            if is_dir:
                dir_count += 1
            else:
                file_count += 1

            size_display = ""
            size_raw = 0
            mtime_display = ""
            try:
                st = entry["stat"]
                if st is None:
                    raise OSError("Metadata unavailable")
                size_raw = st.st_size
                if not is_dir:
                    total_size += size_raw
                    size_display = format_size(size_raw)
                mtime_display = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
            except OSError:
                size_display = "?"
                mtime_display = "?"

            preview_html = ""
            if not is_dir and ext in IMAGE_EXT:
                if path:
                    img_url = f"{base_url}/{urllib.parse.quote(path)}/{encoded}"
                else:
                    img_url = f"{base_url}/{encoded}"
                preview_html = f"<img class='preview-img' src='{img_url}' loading='lazy'>"

            if is_dir:
                new_path = f"{path}/{name}".strip("/")
                encoded_path = urllib.parse.quote_from_bytes(os.fsencode(new_path))
                open_link = f"/?dir={dir_index}&path={encoded_path}"
                rel = ""
            else:
                if path:
                    open_link = f"{base_url}/{urllib.parse.quote(path)}/{encoded}"
                else:
                    open_link = f"{base_url}/{encoded}"
                rel = " rel='noopener noreferrer'"

            move_link = (
                f"/move?src_dir={dir_index}"
                f"&src_path={urllib.parse.quote(path)}"
                f"&src_name={encoded}"
            )
            copy_link = (
                f"/copy?src_dir={dir_index}"
                f"&src_path={urllib.parse.quote(path)}"
                f"&src_name={encoded}"
            )
            delete_link = (
                f"/delete?dir={dir_index}"
                f"&path={urllib.parse.quote(path)}"
                f"&name={encoded}"
            )
            is_archive = ext in ARCHIVE_EXT
            extract_link = ""
            if is_archive:
                extract_params = (
                    f"dir={dir_index}&path={urllib.parse.quote(path)}&name={encoded}"
                )
                extract_link = (
                    " / <a href='#' "
                    f"onclick=\"postAction('/extract','{extract_params}'); return false\">"
                    "Extract</a>"
                )

            convert_link = ""
            if sys.platform.startswith("linux") and not is_dir and ext in {".avi", ".mpg", ".mpeg", ".mkv", ".wmv"}:
                convert_link = (
                    f" / <a href='#' class='convert-link' "
                    f"data-name='{html.escape(display_name, quote=True)}' "
                    f"onclick='convertVideo(this); return false'>Convert to MP4</a>"
                )

            actions_html = (
                f"<a href='{move_link}'>Move</a> / "
                f"<a href='{copy_link}'>Copy</a> / "
                f"<a href='#' onclick=\"if(confirm('Delete this item?')) postAction('/delete','{delete_link.split('?', 1)[1]}'); return false\">Delete</a> / "
                f"<a href='#' class='rename-link' "
                f"data-name='{html.escape(display_name, quote=True)}' "
                f"data-encoded='{encoded}' "
                f"onclick='startRename(this); return false'>Rename</a>"
                f"{extract_link}"
                f"{convert_link}"
            )

            row_class = classify_row(name, is_dir)
            row_attr = f" class='{row_class}'" if row_class else ""
            icon = get_icon(name, is_dir)

            rows.append({
                "encoded": encoded,
                "name": name,
                "display_name": display_name,
                "is_dir": is_dir,
                "size_display": size_display,
                "size_raw": size_raw,
                "mtime_display": mtime_display,
                "preview_html": preview_html,
                "open_link": open_link,
                "rel": rel,
                "actions_html": actions_html,
                "row_attr": row_attr,
                "icon": icon,
            })

        # Add a ".." parent row when not at the root
        if path:
            parent = "/".join(path.split("/")[:-1])
            rows.insert(0, {
                "encoded": "..",
                "name": "..",
                "display_name": "..",
                "is_dir": True,
                "size_display": "",
                "size_raw": 0,
                "mtime_display": "",
                "preview_html": "",
                "open_link": f"/?dir={dir_index}&path={urllib.parse.quote(parent)}",
                "rel": "",
                "actions_html": "",
                "row_attr": " class='row-parent'",
                "icon": "⬆️",
            })

        # Breadcrumbs
        root_label = os.path.basename(root_dir.rstrip("/")) or root_dir
        breadcrumb_html = (
            f"<a href='/?dir={dir_index}'>"
            f"{html.escape(root_label)}</a>"
        )
        if path:
            parts = path.split("/")
            cumulative_parts = []
            collapse = len(parts) > 4
            for i, part in enumerate(parts):
                cumulative_parts.append(part)
                if collapse and 0 < i < len(parts) - 2:
                    continue
                href = (f"/?dir={dir_index}"
                        f"&path={urllib.parse.quote('/'.join(cumulative_parts))}")
                breadcrumb_html += (
                    f" / <a href='{href}'>{html.escape(part)}</a>"
                )
            if collapse:
                full_display = "/".join(parts)
                breadcrumb_html = (
                    f"<a href='/?dir={dir_index}'>"
                    f"{html.escape(root_label)}</a>"
                    f" / <span title='{html.escape(full_display)}'>…</span>"
                    + breadcrumb_html.split("</a>", 1)[1]
                )

        # Directory tabs
        dir_tabs_html = ""
        for i, directory in enumerate(DIRECTORIES):
            if i not in available_indices:
                continue
            short = directory.name
            cls = "active" if i == dir_index else ""
            if dir_tabs_html:
                dir_tabs_html += "<span class='destination-separator' aria-hidden='true'>|</span>"
            dir_tabs_html += (
                f"<a class='{cls}' href='/?dir={i}'>"
                f"{html.escape(short)}</a>"
            )

        # Rows
        rows_html = ""
        for r in rows:
            is_dir_flag = "1" if r["is_dir"] else "0"
            rows_html += (
                f"<tr{r['row_attr']} data-name='{html.escape(r['display_name'])}' "
                f"data-isdir='{is_dir_flag}'>"
                f"<td><input type='checkbox' class='sel' {'disabled' if r['display_name'] == '..' else ''} "
                f"value='{html.escape(r['display_name'])}' "
                f"data-encoded='{r['encoded']}'></td>"
                f"<td class='filename' data-sort='{html.escape(r['display_name'].lower())}'>"
                f"<span class='icon'>{r['icon']}</span> "
                f"<a href='{r['open_link']}' {r['rel']} "
                f"title='{html.escape(r['display_name'], quote=True)}' "
                f"id='name_display_{r['encoded']}'>"
                f"{html.escape(r['display_name'])}</a>"
                f"<input id='name_input_{r['encoded']}' "
                f"data-encoded='{r['encoded']}' "
                f"style='display:none;width:200px;' "
                f"onkeydown='renameKey(event, this)'>"
                f"</td>"
                f"<td class='preview-col'>{r['preview_html']}</td>"
                f"<td class='size' data-sort='{r['size_raw']}'>{r['size_display']}</td>"
                f"<td class='mtime' data-sort='{html.escape(r['mtime_display'])}'>{r['mtime_display']}</td>"
                f"<td class='actions'>{r['actions_html']}</td>"
                "</tr>"
            )

        def page_url(number, query=None):
            return "/?" + urllib.parse.urlencode({"dir": dir_index, "path": path, "page": number,
                "per_page": listing["page_size"], "q": listing["query"] if query is None else query,
                "sort": listing["sort"], "order": listing["order"]})
        navigation = "<nav class='listing-navigation' aria-label='Folder pages'>"
        if listing["page"] > 1:
            navigation += f"<a href='{html.escape(page_url(listing['page'] - 1), quote=True)}'>Previous</a> "
        navigation += (f"<span>Page {listing['page']} of {listing['pages']} · "
                       f"Showing {listing['start']}–{listing['end']} of {listing['total']}"
                       + (" matches" if listing["query"] else " items") + "</span> ")
        if listing["page"] < listing["pages"]:
            navigation += f"<a href='{html.escape(page_url(listing['page'] + 1), quote=True)}'>Next</a> "
        if listing["query"]:
            navigation += f"<a href='{html.escape(page_url(1, ''), quote=True)}'>Clear search</a> "
        navigation += (f"<span>· {dir_count} folders · {file_count} files · "
                       f"{format_size(total_size)} in displayed files</span> "
                       "<button type='button' onclick='loadFolderTotals(this)'>Calculate folder totals</button> "
                       "<span id='folderTotals' aria-live='polite'></span> ")
        fields = {"dir": dir_index, "path": path, "q": listing["query"],
                  "sort": listing["sort"], "order": listing["order"]}
        hidden_fields = "".join(f"<input type='hidden' name='{key}' value='{html.escape(str(value), quote=True)}'>"
                                for key, value in fields.items())
        navigation += ("<form method='get'>" + hidden_fields + "<label>Items per page <select name='per_page' onchange='this.form.submit()'>"
                       + "".join(f"<option value='{size}' {'selected' if size == listing['page_size'] else ''}>{size}</option>"
                                 for size in folder_listing.PAGE_SIZES) + "</select></label></form></nav>")
        search_fields = "".join(f"<input type='hidden' name='{key}' value='{html.escape(str(value), quote=True)}'>"
                               for key, value in {**fields, "per_page": listing["page_size"]}.items() if key != "q")
        external_sites_html = _external_sites_html()

        return (
            CSS_JS
            + "<div class='title-row'>"
            + f"<h1 class='app-title'>{html.escape(APP_TITLE)}</h1>"
            + external_sites_html + "</div>"
            + f"<div class='header'>"
               f"<span style='color:white;'>Select Directory:</span> "
               f"{dir_tabs_html}"
               f"<a class='header-link' href='/jobs'>Recent transfers</a>"
               f"<a class='header-link' href='/operations'>Operation log</a>"
               f"<a class='header-link' href='/configuration'>Configuration</a>"
               f"<span class='dark-toggle' onclick='toggleDark()'>🌓 Theme</span>"
               f"<form class='logout-form' method='post' action='/logout'>"
               f"<input type='hidden' name='csrf_token' value='{generate_csrf()}'>"
               f"<button class='logout-link' type='submit'>Log out</button></form>"
               f"</div>"
            + f"<div class='breadcrumbs'>{breadcrumb_html}</div>"
            + f"<input type='hidden' id='currentDir' value='{dir_index}'>"
            + f"<input type='hidden' id='currentPath' value='{html.escape(path, quote=True)}'>"
            + f"<input type='hidden' id='csrfToken' value='{generate_csrf()}'>"
            + worker_health.banner() + navigation
            + f"""
            <div id='uploadQueue' aria-live='polite'></div>

            <div class='toolbar'>
                <button class='toolbar-btn primary' onclick='newFolder()'>📁 New Folder</button>
                <button class='toolbar-btn' onclick='createArchiveDialog()'>🗜️ Archive</button>
                <button class='toolbar-btn danger' onclick='bulkDelete()'>🗑️ Delete Selected</button>
                <button class='toolbar-btn' onclick='bulkMove()'>📦 Move Selected</button>
                <div id='dropZone' role='button' tabindex='0' aria-controls='uploadInput'>⬆️ Drop files or click to upload</div>
                <input type='file' id='uploadInput' multiple />
                <form id='listingSearch' method='get' onsubmit='filterTable(event)'>
                    {search_fields}
                    <input id='searchInput' name='q' type='search' value='{html.escape(listing["query"], quote=True)}'
                           placeholder='Search this folder...' aria-label='Search all filenames in this folder'>
                    <button type='submit'>Search</button>
                </form>
            </div>

            <table class='table' id='fileTable'>
            <thead>
            <tr>
                <th><input type='checkbox' title='Select all items on this page' onclick='toggleAll(this)'></th>
                <th class='sortable' data-sort-col='1' onclick='sortTable(1)'>Name <span class='sort-indicator'></span></th>
                <th class='preview-col'>Preview</th>
                <th class='sortable size' data-sort-col='3' onclick='sortTable(3)'>Size <span class='sort-indicator'></span></th>
                <th class='sortable mtime' data-sort-col='4' onclick='sortTable(4)'>Modified <span class='sort-indicator'></span></th>
                <th>Actions</th>
            </tr>
            </thead>
            <tbody>
            {rows_html}
            </tbody>
            </table>
            <div class='statusbar' id='statusbar'>Selection is limited to this page.</div>
            <div id='jobsPanel'></div>
            """
            + navigation + _inline_scripts()
        )


def _inline_scripts():
    return """
<script>
// -----------------------------------------------------------------
// State
// -----------------------------------------------------------------
const STATE_KEY = 'fileBrowserState';
const JOB_KEY = 'activeFileJobs';
const csrfToken = document.getElementById('csrfToken').value;

function csrfFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set('X-CSRFToken', csrfToken);
    return fetch(url, {...options, headers});
}

function postAction(url, queryString) {
    const form = document.createElement('form');
    form.method = 'POST';
    form.action = url;
    const values = new URLSearchParams(queryString);
    values.set('csrf_token', csrfToken);
    for (const [name, value] of values) {
        const input = document.createElement('input');
        input.type = 'hidden'; input.name = name; input.value = value;
        form.appendChild(input);
    }
    document.body.appendChild(form);
    form.submit();
}
// A corrupt saved preference must not prevent the rest of the page (including
// New Folder) from initializing.
let state = {};
try {
    const savedState = JSON.parse(localStorage.getItem(STATE_KEY) || '{}');
    if (savedState && typeof savedState === 'object') state = savedState;
} catch (e) {}
if (state.darkMode) document.documentElement.classList.add('dark-mode');

function saveState() {
    localStorage.setItem(STATE_KEY, JSON.stringify(state));
}

function activeJobs() {
    try {
        const jobs = JSON.parse(localStorage.getItem(JOB_KEY) || '[]');
        return Array.isArray(jobs) ? jobs : [];
    } catch (e) { return []; }
}

function saveJobs(jobs) {
    // Persist identifiers only; names and errors are fetched with owner checks.
    const active = jobs.filter(job => !['complete', 'failed', 'cancelled'].includes(job.status));
    const recent = jobs.filter(job => ['complete', 'failed', 'cancelled'].includes(job.status)).slice(-10);
    localStorage.setItem(JOB_KEY, JSON.stringify([...active, ...recent].map(
        ({job_id, operation, status}) => ({job_id, operation, status})
    )));
}

function rememberJob(job) {
    const jobs = activeJobs().filter(item => item.job_id !== job.job_id);
    jobs.push(job);
    saveJobs(jobs);
}

function forgetJob(jobId) {
    saveJobs(activeJobs().filter(item => item.job_id !== jobId));
    document.getElementById('job_' + jobId)?.remove();
}

function showJob(job) {
    const panel = document.getElementById('jobsPanel');
    if (!panel) return;
    let row = document.getElementById('job_' + job.job_id);
    if (!row) {
        row = document.createElement('div');
        row.id = 'job_' + job.job_id;
        row.className = 'job-status';
        panel.appendChild(row);
    }
    const total = Number(job.bytes_total || 0);
    const completed = Number(job.bytes_completed || 0);
    const percent = total > 0 ? Math.min(100, Math.round(completed * 100 / total)) : null;
    row.textContent = `${(job.operation || 'job').replaceAll('_', ' ')}: ${job.status}` +
        (job.progress_unit === 'items' ? ` (${completed}/${total} items)` :
            (percent === null ? '' : ` (${percent}%)`)) + ' ';
    if (job.status === 'pending' && job.queue_position != null) {
        row.appendChild(document.createTextNode(`Queued #${job.queue_position} · ${job.jobs_ahead} job(s) ahead. `));
    }
    if (job.cancellation_requested && job.status === 'running') {
        row.appendChild(document.createTextNode('Cancellation requested. '));
    }
    if (job.error) row.appendChild(document.createTextNode(job.error + ' '));
    if (job.bytes_per_second) {
        const eta = job.seconds_remaining == null ? '' : ` · about ${Math.ceil(job.seconds_remaining)}s left`;
        row.appendChild(document.createTextNode(`${formatBytes(job.bytes_per_second)}/s${eta} `));
    }
    if (job.remote_partial_path && ['failed', 'cancelled'].includes(job.status)) {
        row.appendChild(document.createTextNode('Partial remote copy: ' + job.remote_partial_path + ' '));
    }
    const itemTotal = ['completed_names', 'failed_names', 'remaining_names']
        .reduce((sum, key) => sum + (Array.isArray(job[key]) ? job[key].length : 0), 0);
    for (const [label, key] of [['Completed', 'completed_names'], ['Failed', 'failed_names'], ['Remaining', 'remaining_names']]) {
        // Skip empty sections, and skip the lists entirely for a one-item
        // job (e.g. a single-file conversion): the status line and the
        // in-progress name already say everything there is to say.
        if (!Array.isArray(job[key]) || job[key].length === 0 || itemTotal <= 1) continue;
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = `${label} (${job[key].length})`;
        details.appendChild(summary);
        const list = document.createElement('ul');
        for (const name of job[key]) {
            const item = document.createElement('li'); item.textContent = name;
            list.appendChild(item);
        }
        details.appendChild(list); row.appendChild(details);
    }
    if (job.current_name) row.appendChild(document.createTextNode('In progress: ' + job.current_name + ' '));
    if (job.requires_resubmit) {
        row.appendChild(document.createTextNode('Select the remaining files and start a new transfer; this older transfer cannot be retried safely. '));
    }
    if (['pending', 'running'].includes(job.status)) {
        const cancel = document.createElement('button');
        cancel.type = 'button'; cancel.textContent = job.cancellation_requested ? 'Cancellation requested' : 'Cancel';
        cancel.disabled = !!job.cancellation_requested;
        cancel.onclick = () => cancelJob(job.job_id, cancel);
        row.appendChild(cancel);
    }
    if (['failed', 'cancelled'].includes(job.status) && job.can_retry !== false) {
        const retry = document.createElement('button');
        retry.type = 'button'; retry.textContent = 'Retry remaining' + (job.retry_count == null ? '' : ` (${job.retry_count})`);
        retry.title = 'Retry failed and unstarted items; keep completed items';
        retry.onclick = () => retryJob(job.job_id, retry);
        row.appendChild(retry);
    }
    if (['complete', 'failed', 'cancelled'].includes(job.status)) {
        const dismiss = document.createElement('button');
        dismiss.type = 'button'; dismiss.textContent = '×';
        dismiss.title = 'Dismiss';
        dismiss.onclick = () => forgetJob(job.job_id);
        row.appendChild(dismiss);
        const refresh = document.createElement('button');
        refresh.type = 'button'; refresh.textContent = 'Refresh file list';
        refresh.onclick = () => location.reload(); row.appendChild(refresh);
    }
}

function formatBytes(value) {
    const units = ['B', 'KB', 'MB', 'GB', 'TB']; let n = Number(value || 0), i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

async function retryJob(jobId, button = null) {
    if (button) button.disabled = true;
    const fd = new FormData(); fd.append('job_id', jobId);
    try {
        const response = await csrfFetch('/job_retry', {
            method: 'POST', headers: {'Accept': 'application/json'}, body: fd
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.success || !data.job_id) {
            alert(data.error || 'Retry failed');
            return;
        }
        rememberJob(data);
        forgetJob(jobId);
        showJob(data);
        pollJob(data.job_id);
    } catch (error) {
        alert('Retry could not be confirmed. Check Recent transfers before trying again.');
    } finally {
        if (button) button.disabled = false;
    }
}

async function cancelJob(jobId, button = null) {
    const original = button?.textContent || 'Cancel';
    if (button) { button.disabled = true; button.textContent = 'Requesting cancellation…'; }
    try {
        const response = await csrfFetch('/job_cancel', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({job_id: jobId})
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.success) throw new Error(data.error || 'Cancellation failed');
        if (button) button.textContent = data.status === 'cancelled' ? 'Cancelled' : data.cancellation_requested ? 'Cancellation requested' : 'Transfer finished';
    } catch (error) {
        if (button) { button.disabled = false; button.textContent = original; }
        alert('Cancellation could not be confirmed. Refresh the queue before trying again. ' + error.message);
    }
}

async function pollJob(jobId, trigger = null) {
    try {
        const response = await fetch('/job_status?job_id=' + encodeURIComponent(jobId));
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.success) {
            forgetJob(jobId);
            if (trigger) trigger.textContent = 'Convert to MP4';
            return;
        }
        showJob(data);
        if (data.status === 'complete') {
            rememberJob(data);
            flash(`${data.operation} complete`);
            if (trigger) trigger.textContent = 'Converted';
            // A finished popup should not hold screen space: dismiss it
            // shortly after completion (the × button dismisses it sooner,
            // and the job stays in Recent transfers either way).
            setTimeout(() => forgetJob(jobId), 5000);
            return;
        }
        if (data.status === 'failed' || data.status === 'cancelled') {
            rememberJob(data);
            if (trigger) trigger.textContent = 'Convert to MP4';
            return;
        }
        if (trigger) trigger.textContent = data.status === 'pending' ? 'Queued…' : 'Converting…';
        setTimeout(() => pollJob(jobId, trigger), 2000);
    } catch (e) {
        setTimeout(() => pollJob(jobId, trigger), 5000);
    }
}

function resumeJobs() {
    activeJobs().forEach(job => { showJob(job); pollJob(job.job_id); });
}

// -----------------------------------------------------------------
// Toast notifications
// -----------------------------------------------------------------
function flash(msg) {
    const t = document.createElement('div');
    t.className = 'toast';
    t.textContent = msg;
    document.body.appendChild(t);
    requestAnimationFrame(() => t.classList.add('show'));
    setTimeout(() => {
        t.classList.remove('show');
        setTimeout(() => t.remove(), 300);
    }, 1500);
}

// -----------------------------------------------------------------
// Theme
// -----------------------------------------------------------------
function toggleDark() {
    state.darkMode = !state.darkMode;
    saveState();
    document.documentElement.classList.toggle('dark-mode', state.darkMode);
}

// -----------------------------------------------------------------
// Sorting
// -----------------------------------------------------------------
function sortTable(col) {
    const nextDir = (state.sortCol === col && state.sortDir === 'asc') ? 'desc' : 'asc';
    const url = new URL(window.location.href);
    url.searchParams.set('sort', col); url.searchParams.set('order', nextDir);
    url.searchParams.set('page', '1'); window.location.assign(url);
}

function applySort(col, dir) {
    state.sortCol = col; state.sortDir = dir; saveState();
    carrySortInLinks(col, dir);
    document.querySelectorAll('[data-sort-col]').forEach(header => {
        const active = Number(header.dataset.sortCol) === col;
        header.classList.toggle('sorted', active);
        const indicator = header.querySelector('.sort-indicator');
        if (indicator) indicator.textContent = active ? (dir === 'asc' ? ' ▲' : ' ▼') : '';
        header.setAttribute('aria-sort', active ? (dir === 'asc' ? 'ascending' : 'descending') : 'none');
    });
}

function carrySortInLinks(col, dir) {
    const current = new URL(window.location.href);
    document.querySelectorAll('a[href^="/?"]').forEach(link => {
        const url = new URL(link.getAttribute('href'), window.location.origin);
        url.searchParams.set('sort', col); url.searchParams.set('order', dir);
        if (current.searchParams.has('per_page')) url.searchParams.set('per_page', current.searchParams.get('per_page'));
        link.setAttribute('href', url.pathname + url.search);
    });
}

function filterTable(event) {
    if (event) event.preventDefault();
    const url = new URL(window.location.href);
    const query = document.getElementById('searchInput').value.trim();
    if (query) url.searchParams.set('q', query); else url.searchParams.delete('q');
    url.searchParams.set('page', '1'); window.location.assign(url);
}

async function loadFolderTotals(button) {
    const output = document.getElementById('folderTotals');
    button.disabled = true; output.textContent = 'Calculating…';
    try {
        const query = new URLSearchParams({dir: document.getElementById('currentDir').value,
                                         path: document.getElementById('currentPath').value});
        const response = await fetch('/folder_stats?' + query);
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.success) throw new Error(data.error || 'Could not calculate totals');
        output.textContent = `Folder totals (direct contents): ${data.folders} folders · ${data.files} files · ${data.size}` +
            (data.unavailable ? ` · ${data.unavailable} unavailable entries; totals are partial` : '');
        button.textContent = 'Recalculate totals';
    } catch (error) { output.textContent = error.message; }
    finally { button.disabled = false; }
}

// -----------------------------------------------------------------
// Selection
// -----------------------------------------------------------------
function toggleAll(cb) {
    document.querySelectorAll('#fileTable tbody tr').forEach(row => {
        if (row.style.display !== 'none' && row.dataset.name !== '..') {
            const checkbox = row.querySelector('.sel');
            if (checkbox) checkbox.checked = cb.checked;
        }
    });
}

function getSelected() {
    return Array.from(document.querySelectorAll('.sel'))
        .filter(x => x.checked && x.value !== '..')
        .map(x => ({name: x.value, encoded: x.dataset.encoded}));
}

// -----------------------------------------------------------------
// Bulk ops
// -----------------------------------------------------------------
function bulkDelete() {
    const sel = getSelected();
    if (!sel.length) { alert('Nothing selected.'); return; }
    if (!confirm(`Delete ${sel.length} item(s)? This cannot be undone.`)) return;
    const dir  = document.getElementById('currentDir').value;
    const path = document.getElementById('currentPath').value;
    const qs = new URLSearchParams({
        dir, path,
        names: sel.map(s => s.encoded).join(',')
    });
    csrfFetch('/bulk_delete?' + qs, {method: 'POST'})
        .then(r => r.json())
        .then(d => {
            if (d.success) location.reload();
            else alert('Failed: ' + (d.error || JSON.stringify(d.errors)));
        });
}

function bulkMove() {
    const sel = getSelected();
    if (!sel.length) { alert('Nothing selected.'); return; }
    const dir  = document.getElementById('currentDir').value;
    const path = document.getElementById('currentPath').value;
    const qs = new URLSearchParams({src_dir: dir, src_path: path});
    sel.forEach(item => qs.append('name', item.name));
    window.location = '/bulk_move?' + qs;
}

function newFolder() {
    const name = prompt('Folder name:');
    if (!name) return;
    const dir  = document.getElementById('currentDir').value;
    const path = document.getElementById('currentPath').value;
    const fd = new FormData();
    fd.append('dir', dir);
    fd.append('path', path);
    fd.append('name', name);
    csrfFetch('/mkdir', {method: 'POST', body: fd})
        .then(r => r.json())
        .then(d => {
            if (d.success) location.reload();
            else alert('Failed: ' + d.error);
        });
}

// -----------------------------------------------------------------
// Archive
// -----------------------------------------------------------------
function createArchiveDialog() {
    const sel = getSelected();
    if (!sel.length) { alert("No files selected."); return; }
    const name = prompt("Archive name:", "archive");
    if (!name) return;
    const type = prompt("Type (zip/tar/tgz):", "zip");
    if (!type) return;
    const dir  = document.getElementById("currentDir").value;
    const path = document.getElementById("currentPath").value;
    csrfFetch("/archive", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({dir: parseInt(dir), path,
                              names: sel.map(s => s.name), type, name})
    })
    .then(r => r.json())
    .then(d => {
        if (d.success) { flash('Created: ' + d.archive); setTimeout(() => location.reload(), 800); }
        else alert("Error: " + d.error);
    });
}

// -----------------------------------------------------------------
// Rename
// -----------------------------------------------------------------
function startRename(trigger) {
    const name = trigger.dataset.name;
    const encoded = trigger.dataset.encoded;
    const display = document.getElementById("name_display_" + encoded);
    const input   = document.getElementById("name_input_" + encoded);
    input.value = name;
    display.style.display = "none";
    input.style.display = "inline-block";
    input.focus();
    input.select();
}

function renameKey(e, input) {
    const encoded = input.dataset.encoded;
    if (e.key === "Escape") { cancelRename(encoded); return; }
    if (e.key === "Enter") {
        const dir = parseInt(document.getElementById("currentDir").value);
        const path = document.getElementById("currentPath").value;
        csrfFetch("/rename", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({dir, path, old: encoded, new: input.value})
        })
        .then(r => r.json())
        .then(d => {
            if (d.success) location.reload();
            else alert("Rename failed: " + d.error);
        });
    }
}

function convertVideo(trigger) {
    const name = trigger.dataset.name;
    if (!confirm(`Convert "${name}" to MP4? The original file will be kept.`)) return;
    const dir = parseInt(document.getElementById("currentDir").value);
    const path = document.getElementById("currentPath").value;
    trigger.textContent = 'Converting…';
    csrfFetch('/convert', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({dir, path, name})
    })
        .then(r => r.json())
        .then(d => {
            if (d.success) {
                rememberJob({job_id: d.job_id, operation: 'convert', status: d.status});
                trigger.textContent = d.status === 'queued' ? 'Queued…' : 'Converting…';
                pollJob(d.job_id, trigger);
            } else {
                trigger.textContent = 'Convert to MP4';
                alert('Conversion failed: ' + d.error);
            }
        })
        .catch(e => {
            trigger.textContent = 'Convert to MP4';
            alert('Conversion failed: ' + e);
    });
}

resumeJobs();

function cancelRename(encoded) {
    document.getElementById("name_input_" + encoded).style.display = "none";
    document.getElementById("name_display_" + encoded).style.display = "inline-block";
}

// -----------------------------------------------------------------
// Keyboard nav
// -----------------------------------------------------------------
let currentRow = -1;
document.addEventListener('keydown', e => {
    if (['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) return;
    const rows = Array.from(document.querySelectorAll('#fileTable tbody tr'))
                      .filter(r => r.style.display !== 'none');
    if (!rows.length) return;

    if (e.key === 'ArrowDown')      { e.preventDefault(); moveSel(rows,  1); }
    else if (e.key === 'ArrowUp')   { e.preventDefault(); moveSel(rows, -1); }
    else if (e.key === 'Home')      { e.preventDefault(); currentRow = -1; moveSel(rows, 1); }
    else if (e.key === 'End')       { e.preventDefault(); currentRow = rows.length; moveSel(rows, -1); }
    else if (e.key === ' ')         {
        e.preventDefault();
        const cb = rows[currentRow]?.querySelector('.sel');
        if (cb) cb.checked = !cb.checked;
    }
    else if (e.key === 'Enter' && currentRow >= 0) {
        const link = rows[currentRow].querySelector('.filename a');
        if (link) link.click();
    }
    else if (e.key === 'Delete' && currentRow >= 0) {
        const cb = rows[currentRow].querySelector('.sel');
        if (cb) { cb.checked = true; bulkDelete(); }
    }
    else if (e.key === 'F2' && currentRow >= 0) {
        const renameLink = rows[currentRow].querySelector('.rename-link');
        if (renameLink) startRename(renameLink);
    }
    else if (e.key.toLowerCase() === 'a' && (e.ctrlKey || e.metaKey)) {
        // allow native select-all
    }
});

function moveSel(rows, delta) {
    rows.forEach(r => r.classList.remove('focused'));
    if (currentRow < 0 && delta > 0) currentRow = -1;
    currentRow = Math.max(0, Math.min(rows.length - 1, currentRow + delta));
    rows[currentRow].classList.add('focused');
    rows[currentRow].scrollIntoView({block: 'nearest'});
}

// -----------------------------------------------------------------
// Image preview on click
// -----------------------------------------------------------------
document.addEventListener('click', e => {
    if (e.target.classList && e.target.classList.contains('preview-img')) {
        e.preventDefault();
        e.stopPropagation();
        showPreview(e.target.src);
    }
});

// Middle-click opens in new tab
document.addEventListener('auxclick', e => {
    if (e.button !== 1) return;
    const link = e.target.closest('a');
    if (!link || !link.href) return;
    e.preventDefault();
    window.open(link.href, '_blank');
});

// -----------------------------------------------------------------
// Drag & Drop Upload
// -----------------------------------------------------------------
const dropZone = document.getElementById('dropZone');
const fileInput = document.getElementById('uploadInput');

// The drop zone is also the visible file-picker control. Without this,
// clicking it does nothing because the actual input is intentionally hidden.
dropZone.addEventListener('click', () => fileInput.click());

['dragenter', 'dragover'].forEach(evt =>
    dropZone.addEventListener(evt, e => {
        e.preventDefault(); e.stopPropagation();
        dropZone.classList.add('dragover');
    })
);
['dragleave', 'drop'].forEach(evt =>
    dropZone.addEventListener(evt, e => {
        e.preventDefault(); e.stopPropagation();
        dropZone.classList.remove('dragover');
    })
);

dropZone.addEventListener('drop', e => {
    const files = Array.from(e.dataTransfer.files || []);
    if (files.length) uploadFiles(files);
});

fileInput.addEventListener('change', e => {
    uploadFiles(Array.from(e.target.files));
    e.target.value = '';
});

async function uploadFiles(files) {
    if (!files.length) return;
    const dir  = document.getElementById('currentDir').value;
    const path = document.getElementById('currentPath').value;
    const queue = document.getElementById('uploadQueue');
    const entries = files.map(file => {
        const row = document.createElement('div');
        row.className = 'upload-item';
        row.innerHTML = `<span class="upload-name"></span><progress max="100" value="0"></progress><span class="upload-percent">0%</span><span class="upload-result"></span>`;
        row.querySelector('.upload-name').textContent = file.name;
        const retry = document.createElement('button');
        retry.type = 'button'; retry.textContent = 'Retry upload'; retry.hidden = true;
        row.appendChild(retry);
        queue.appendChild(row);
        const run = async () => {
            if (retry.disabled) return false;
            retry.disabled = true;
            const success = await uploadOne(file, row, dir, path);
            retry.hidden = success;
            retry.disabled = false;
            return success;
        };
        retry.onclick = run;
        return {run};
    });
    let completed = 0;
    for (const entry of entries) {
        if (await entry.run()) completed++;
    }
    const failed = entries.length - completed;
    flash(`${completed} uploaded${failed ? `, ${failed} failed — see details below` : ''}`);
    // Keep results and failed files available, even if another batch succeeds.
    if (!queue.querySelector('.upload-refresh')) {
        const refresh = document.createElement('button');
        refresh.type = 'button'; refresh.className = 'upload-refresh';
        refresh.textContent = 'Refresh file list';
        refresh.onclick = () => location.reload();
        queue.appendChild(refresh);
    }
}

function uploadOne(file, row, dir, path) {
    return new Promise(resolve => {
        const progress = row.querySelector('progress');
        const percent = row.querySelector('.upload-percent');
        const result = row.querySelector('.upload-result');
        progress.value = 0;
        percent.textContent = '0%';
        result.textContent = 'Uploading…';
        result.className = 'upload-result';
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/upload');
        xhr.setRequestHeader('X-CSRFToken', csrfToken);
        xhr.upload.addEventListener('progress', event => {
            if (!event.lengthComputable) return;
            const value = Math.round(event.loaded * 100 / event.total);
            progress.value = value;
            percent.textContent = `${value}%`;
        });
        xhr.addEventListener('load', () => {
            let data = {};
            try { data = JSON.parse(xhr.responseText); } catch (e) {}
            if (xhr.status >= 200 && xhr.status < 300 && data.success) {
                progress.value = 100;
                percent.textContent = '100%';
                const savedName = data.saved?.[0]?.name;
                result.textContent = savedName && savedName !== file.name ? `Uploaded as ${savedName}` : 'Uploaded';
                result.className = 'upload-result success';
                resolve(true);
            } else {
                result.textContent = data.error || data.errors?.[0]?.error || `Upload failed (${xhr.status})`;
                result.className = 'upload-result error';
                resolve(false);
            }
        });
        const fail = message => {
            result.textContent = message;
            result.className = 'upload-result error';
            resolve(false);
        };
        xhr.addEventListener('error', () => fail('Network error. Check the file list before retrying.'));
        xhr.addEventListener('abort', () => fail('Upload cancelled.'));
        xhr.addEventListener('timeout', () => fail('Upload timed out. Check the file list before retrying.'));
        const fd = new FormData();
        fd.append('dir', dir); fd.append('path', path); fd.append('files', file);
        try { xhr.send(fd); } catch (error) { fail('Could not start upload.'); }
    });
}

// -----------------------------------------------------------------
// Image preview modal
// -----------------------------------------------------------------
let previewModal = null, previewImg = null;
let isDragging = false, dragStartX = 0, dragStartY = 0, imgStartX = 0, imgStartY = 0;

function showPreview(url) {
    if (!previewModal) {
        previewModal = document.createElement("div");
        previewModal.style.cssText =
            "position:fixed;top:0;left:0;width:100vw;height:100vh;" +
            "background:rgba(0,0,0,0.85);display:flex;align-items:center;" +
            "justify-content:center;z-index:99999;cursor:zoom-out;";
        previewImg = document.createElement("img");
        previewImg.style.cssText =
            "max-width:90%;max-height:90%;border-radius:6px;" +
            "transition:transform 0.1s ease-out;cursor:grab;";
        previewModal.appendChild(previewImg);
        document.body.appendChild(previewModal);
        previewModal.addEventListener("click", hidePreview);
        previewModal.addEventListener("wheel", function(e) {
            e.preventDefault();
            let scale = previewImg.scale || 1;
            scale += e.deltaY * -0.0015;
            scale = Math.min(Math.max(0.2, scale), 5);
            previewImg.scale = scale;
            previewImg.style.transform = `scale(${scale})`;
        });
        previewImg.addEventListener("mousedown", function(e) {
            isDragging = true;
            dragStartX = e.clientX; dragStartY = e.clientY;
            imgStartX = previewImg.offsetLeft; imgStartY = previewImg.offsetTop;
            previewImg.style.cursor = "grabbing";
        });
        document.addEventListener("mousemove", function(e) {
            if (!isDragging) return;
            previewImg.style.position = "relative";
            previewImg.style.left = (imgStartX + e.clientX - dragStartX) + "px";
            previewImg.style.top  = (imgStartY + e.clientY - dragStartY) + "px";
        });
        document.addEventListener("mouseup", function() {
            isDragging = false;
            if (previewImg) previewImg.style.cursor = "grab";
        });
    }
    previewImg.src = url;
    previewImg.scale = 1;
    previewImg.style.transform = "scale(1)";
    previewImg.style.left = "0px";
    previewImg.style.top = "0px";
    previewModal.style.display = "flex";
}
function hidePreview() { if (previewModal) previewModal.style.display = "none"; }

// -----------------------------------------------------------------
// Init
// -----------------------------------------------------------------
window.addEventListener('DOMContentLoaded', () => {
    const q = document.getElementById('searchInput');
    const params = new URLSearchParams(window.location.search);
    // Sort and filter come only from this page's URL. Restoring them from
    // localStorage leaked the previous folder's state: the sort toggle
    // alternated direction and an old filter hid files in the new folder.
    delete state.search;
    const query = params.get('q') || '';
    if (q && query) q.value = query;
    const sortCol = Number(params.get('sort'));
    const sortDir = params.get('order');
    if ([1, 3, 4].includes(sortCol) && ['asc', 'desc'].includes(sortDir)) {
        applySort(sortCol, sortDir);
    } else {
        applySort(1, 'asc');
    }
    dropZone.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
    });
});
</script>
"""
