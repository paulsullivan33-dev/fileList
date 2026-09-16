import os
import sys
import json
import time
import html
import urllib.parse

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
            entries = sorted(os.listdir(full_path))
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

        for name in entries:
            # Some Unix filesystems expose non-UTF-8 filename bytes as surrogate
            # code points. Encode links from the original filesystem bytes, but
            # use replacement characters anywhere text is rendered as UTF-8.
            encoded = urllib.parse.quote_from_bytes(os.fsencode(name))
            display_name = _display_name(name)
            item_path = os.path.join(full_path, name)
            is_dir = os.path.isdir(item_path)
            ext = os.path.splitext(name.lower())[1]

            if is_dir:
                dir_count += 1
            else:
                file_count += 1

            size_display = ""
            size_raw = 0
            mtime_display = ""
            try:
                st = os.stat(item_path)
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
            if sys.platform.startswith("linux") and not is_dir and ext in {".avi", ".mpg", ".mpeg"}:
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
                f"<td><input type='checkbox' class='sel' "
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
                f"<td class='mtime'>{r['mtime_display']}</td>"
                f"<td class='actions'>{r['actions_html']}</td>"
                "</tr>"
            )

        stats_html = (
            f"<div class='stats'>"
            f"📁 {dir_count} folders · 📄 {file_count} files · "
            f"💾 {format_size(total_size)} total"
            f"</div>"
        )
        external_sites_html = _external_sites_html()

        return (
            CSS_JS
            + "<div class='title-row'>"
            + f"<h1 class='app-title'>{html.escape(APP_TITLE)}</h1>"
            + external_sites_html + "</div>"
            + f"<div class='header'>"
               f"<span style='color:white;'>Select Directory:</span> "
               f"{dir_tabs_html}"
               f"<span class='dark-toggle' onclick='toggleDark()'>🌓 Theme</span>"
               f"<form class='logout-form' method='post' action='/logout'>"
               f"<input type='hidden' name='csrf_token' value='{generate_csrf()}'>"
               f"<button class='logout-link' type='submit'>Log out</button></form>"
               f"</div>"
            + f"<div class='breadcrumbs'>{breadcrumb_html}</div>"
            + f"<input type='hidden' id='currentDir' value='{dir_index}'>"
            + f"<input type='hidden' id='currentPath' value='{html.escape(path, quote=True)}'>"
            + f"<input type='hidden' id='csrfToken' value='{generate_csrf()}'>"
            + stats_html
            + f"""
            <div id='dropZone'>⬆️ Drop files here or click to upload</div>
            <input type='file' id='uploadInput' multiple />

            <div class='toolbar'>
                <button class='toolbar-btn primary' onclick='newFolder()'>📁 New Folder</button>
                <button class='toolbar-btn' onclick='createArchiveDialog()'>🗜️ Archive</button>
                <button class='toolbar-btn danger' onclick='bulkDelete()'>🗑️ Delete Selected</button>
                <button class='toolbar-btn' onclick='bulkMove()'>📦 Move Selected</button>
                <input id='searchInput' placeholder='🔍 Filter...' oninput='filterTable()'>
            </div>

            <table class='table' id='fileTable'>
            <thead>
            <tr>
                <th><input type='checkbox' onclick='toggleAll(this)'></th>
                <th class='sortable' onclick='sortTable(1)'>Name</th>
                <th class='preview-col'>Preview</th>
                <th class='sortable size' onclick='sortTable(3)'>Size</th>
                <th class='sortable mtime' onclick='sortTable(4)'>Modified</th>
                <th>Actions</th>
            </tr>
            </thead>
            <tbody>
            {rows_html}
            </tbody>
            </table>
            <div class='statusbar' id='statusbar'></div>
            <div id='jobsPanel'></div>
            """
            + _inline_scripts()
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
    localStorage.setItem(JOB_KEY, JSON.stringify(jobs));
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
    if (job.error) row.appendChild(document.createTextNode(job.error + ' '));
    if (job.remote_partial_path && ['failed', 'cancelled'].includes(job.status)) {
        row.appendChild(document.createTextNode('Partial remote copy: ' + job.remote_partial_path + ' '));
    }
    if (['pending', 'running'].includes(job.status)) {
        const cancel = document.createElement('button');
        cancel.type = 'button'; cancel.textContent = 'Cancel';
        cancel.onclick = () => cancelJob(job.job_id);
        row.appendChild(cancel);
    }
}

async function cancelJob(jobId) {
    const response = await csrfFetch('/job_cancel', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({job_id: jobId})
    });
    const data = await response.json().catch(() => ({}));
    if (!data.success) alert('Cancellation failed: ' + (data.error || response.status));
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
            forgetJob(jobId);
            flash(`${data.operation} complete`);
            if (trigger) trigger.textContent = 'Converted';
            setTimeout(() => location.reload(), 800);
            return;
        }
        if (data.status === 'failed' || data.status === 'cancelled') {
            forgetJob(jobId);
            if (trigger) trigger.textContent = 'Convert to MP4';
            alert(`${data.operation} ${data.status}: ` + (data.error || 'No details'));
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
    const tbody = document.querySelector('#fileTable tbody');
    const rows = Array.from(tbody.querySelectorAll('tr'));
    const dir = (state.sortCol === col && state.sortDir === 'asc') ? 'desc' : 'asc';
    state.sortCol = col;
    state.sortDir = dir;
    saveState();

    const isDir = r => r.dataset.isdir === '1';
    const cell = (r, i) => r.cells[i] || {dataset: {sort: ''}, textContent: ''};
    const get = r => (cell(r, col).dataset.sort || cell(r, col).textContent || '').toLowerCase();
    const isNum = !isNaN(parseFloat(get(rows[0]))) && isFinite(get(rows[0]));

    rows.sort((a, b) => {
        if (a.dataset.name === '..') return -1;
        if (b.dataset.name === '..') return  1;
        if (isDir(a) !== isDir(b)) return isDir(a) ? -1 : 1;
        const av = get(a), bv = get(b);
        if (isNum) {
            return dir === 'asc' ? parseFloat(av) - parseFloat(bv)
                                  : parseFloat(bv) - parseFloat(av);
        }
        if (av < bv) return dir === 'asc' ? -1 : 1;
        if (av > bv) return dir === 'asc' ?  1 : -1;
        return 0;
    });

    rows.forEach(r => tbody.appendChild(r));
}

// -----------------------------------------------------------------
// Filtering
// -----------------------------------------------------------------
function filterTable() {
    const q = document.getElementById('searchInput').value.toLowerCase();
    let shown = 0;
    document.querySelectorAll('#fileTable tbody tr').forEach(r => {
        const name = (r.dataset.name || '').toLowerCase();
        const match = name.includes(q);
        r.style.display = match ? '' : 'none';
        if (match && r.dataset.name !== '..') shown++;
    });
    const sb = document.getElementById('statusbar');
    if (sb) sb.textContent = q ? `Showing ${shown} match(es) for "${q}"` : '';
}

// -----------------------------------------------------------------
// Selection
// -----------------------------------------------------------------
function toggleAll(cb) {
    document.querySelectorAll('.sel').forEach(x => x.checked = cb.checked);
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
    const fd = new FormData();
    fd.append('dir', dir);
    fd.append('path', path);
    files.forEach(f => fd.append('files', f));

    try {
        const r = await csrfFetch('/upload', { method: 'POST', body: fd });
        const data = await r.json().catch(() => ({}));
        if (data.success) {
            flash('Upload complete');
            setTimeout(() => location.reload(), 600);
        } else {
            alert('Upload failed: ' +
                  (data.error || JSON.stringify(data.errors) || `Server returned ${r.status}`));
        }
    } catch (e) {
        alert('Upload error: ' + e);
    }
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
    if (state.sortCol !== undefined) sortTable(state.sortCol);
    const q = document.getElementById('searchInput');
    if (q && state.search) { q.value = state.search; filterTable(); }
    q?.addEventListener('input', () => {
        state.search = q.value;
        saveState();
        filterTable();
    });
});
</script>
"""
