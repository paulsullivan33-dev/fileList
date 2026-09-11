"""Configured SSH destinations and cancellable, staged remote copies."""
import json
import os
import posixpath
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

from utils import is_safe_path, is_safe_segment, safe_dir_index, safe_join, user_can_access_directory


def load_destinations():
    path = Path(os.environ.get("FILELIST_REMOTE_CONFIG", Path(__file__).with_name("remote_destinations.json")))
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as source:
        entries = json.load(source)
    if not isinstance(entries, list):
        raise ValueError("Remote destinations must be a JSON array.")
    seen = set()
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each remote destination must be an object.")
        entry = dict(entry)
        for field in ("id", "name", "host", "path", "group"):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise ValueError(f"Remote destination requires {field}.")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", entry["id"]) or entry["id"] in seen:
            raise ValueError("Remote destination IDs must be unique simple names.")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", entry["host"]):
            raise ValueError("Remote host must be a hostname, IPv4 address, or SSH alias.")
        if entry.get("user") and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", entry["user"]):
            raise ValueError("Invalid SSH user.")
        if "port" in entry and (type(entry["port"]) is not int or not 1 <= entry["port"] <= 65535):
            raise ValueError("SSH port must be between 1 and 65535.")
        if not entry["path"].startswith("/") or any(ord(c) < 32 for c in entry["path"]):
            raise ValueError("Remote path must be an absolute POSIX path without control characters.")
        entry["path"] = posixpath.normpath(entry["path"])
        seen.add(entry["id"])
        result.append(entry)
    return result


def validate_sources(index, path, names, directories, groups):
    index = safe_dir_index(index, directories, default=-1)
    if index is None or not user_can_access_directory(directories[index], groups):
        raise PermissionError("You cannot access the source directory.")
    if not isinstance(path, str) or not is_safe_path(path) or not isinstance(names, list) or not names:
        raise ValueError("Invalid source selection.")
    sources = []
    for name in names:
        if not isinstance(name, str) or not is_safe_segment(name) or "/" in name or name == ".":
            raise ValueError("Invalid source name.")
        source = safe_join(directories[index].root, path, name)
        if source is None or not (os.path.isfile(source) or os.path.isdir(source)):
            raise ValueError(f"Invalid or missing source: {name}")
        sources.append((name, source))
    return index, sources


def destination_for(destination_id, groups):
    for destination in load_destinations():
        if destination["id"] == destination_id and destination["group"] in groups:
            return destination
    raise PermissionError("Remote destination is unavailable or not permitted.")


def selector_html(index, path, names, local_tabs=""):
    import html
    from flask import session
    from flask_wtf.csrf import generate_csrf

    escape = lambda value: html.escape(str(value), quote=True)
    error_html = ""
    try:
        destinations = [d for d in load_destinations() if d["group"] in session.get("groups", [])]
    except (OSError, ValueError) as error:
        destinations = []
        error_html = f"<p>Remote configuration error: {escape(error)}</p>"
    tabs = [local_tabs] if local_tabs else []
    panels = []
    for destination in destinations:
        panel_id = "remote-" + destination["id"]
        tabs.append(
            f"<button type='button' class='destination-tab' aria-expanded='false' "
            f"aria-controls='{escape(panel_id)}' onclick='selectRemoteDestination(this)'>"
            f"{escape(destination['name'])} <small>Remote</small></button>"
        )
        host = (destination.get("user", "") + "@" if destination.get("user") else "") + destination["host"]
        if "port" in destination:
            host += f":{destination['port']}"
        fields = [("csrf_token", generate_csrf()), ("src_dir", index), ("src_path", path),
                  ("remote_id", destination["id"])] + [("name", name) for name in names]
        panels.append(
            f"<div id='{escape(panel_id)}' class='remote-destination-panel' hidden>"
            "<form method='post' action='/remote_copy'>"
            f"<p><strong>{escape(destination['name'])}</strong> · {escape(host)}</p>"
            f"<p class='destination-path'>{escape(destination['path'])}</p>"
            "<p>Copy here and keep the local original.</p>"
            + "".join(f"<input type='hidden' name='{key}' value='{escape(value)}'>" for key, value in fields)
            + "<button class='move-here-btn' type='submit'>Copy here</button></form></div>"
        )
    return (
        "<nav class='destination-tabs' aria-label='Local destinations and Remote servers'>"
        + "<span class='destination-separator' aria-hidden='true'>|</span>".join(tabs)
        + "</nav>" + error_html + "".join(panels)
        + """<script>
function selectRemoteDestination(button) {
    document.querySelectorAll('.remote-destination-panel').forEach(panel => {
        panel.hidden = panel.id !== button.getAttribute('aria-controls');
    });
    document.querySelectorAll('.destination-tab').forEach(tab => {
        tab.setAttribute('aria-expanded', tab === button ? 'true' : 'false');
    });
    document.getElementById('local-destination-panel').hidden = true;
}
</script>"""
    )


def ssh_command(destination):
    command = ["ssh", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes", "-oConnectTimeout=15",
               "-oServerAliveInterval=15", "-oServerAliveCountMax=3"]
    if "port" in destination:
        command += ["-p", str(destination["port"])]
    if destination.get("user"):
        command += ["-l", destination["user"]]
    return command


def run_process(command, check_cancel):
    """Drain output to disk so verbose errors cannot deadlock a transfer."""
    with tempfile.TemporaryFile(mode="w+b") as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        try:
            while process.poll() is None:
                check_cancel()
                time.sleep(0.2)
            if process.returncode:
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - 3000))
                raise RuntimeError(log.read().decode("utf-8", "replace").strip() or f"Transfer command failed ({process.returncode}).")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def run_job(job, job_path, directories, progress_class, check_cancel):
    import job_store
    # Re-read authorization at execution time; queued jobs do not carry privileges.
    with Path(__file__).with_name("users.json").open(encoding="utf-8") as source:
        users = json.load(source)
    user = next((u for u in users if u.get("username") == job["owner"]), None)
    groups = [g.strip() for g in user.get("groups", "").split(",") if g.strip()] if user else []
    info = job["source"]
    _, sources = validate_sources(info.get("directory_index"), info.get("path", ""), info.get("names"), directories, groups)
    destination = destination_for(job["destination"].get("remote_id"), groups)
    if destination != job["destination"].get("configuration"):
        raise ValueError("Remote configuration changed since submission; submit the copy again.")
    # Item-level progress avoids implying that queued bytes are already transferred.
    progress = progress_class(job, job_path, len(sources))
    job_store.update(job_path, progress_unit="items")
    ssh = ssh_command(destination)
    check = lambda: check_cancel(job["job_id"])
    quote = shlex.quote
    for ordinal, (name, source) in enumerate(sources):
        check()
        target = posixpath.join(destination["path"], name)
        stage = posixpath.join(destination["path"], f".filelist-{job['job_id']}-{ordinal}.partial")
        # The staging directory is unique and created exclusively. No remote deletion
        # is attempted on failure: interrupted work remains recognizable for cleanup.
        prepare = (
            f"test -d {quote(destination['path'])} && "
            f"if test -e {quote(target)} || test -L {quote(target)}; then "
            "echo 'Destination already exists' >&2; exit 1; fi && "
            f"mkdir -- {quote(stage)}"
        )
        run_process(ssh + [destination["host"], prepare], check)
        job_store.update(job_path, remote_partial_path=stage)
        # --protect-args preserves spaces/shell characters; -l preserves symlinks
        # inside directories instead of following them outside the selected tree.
        run_process(["rsync", "-rlpt", "--protect-args", "-e", shlex.join(ssh), "--",
                     source, f"{destination['host']}:{stage}/"], check)
        check()
        staged_item = posixpath.join(stage, os.path.basename(source))
        publish = (
            f"mv -T -n -- {quote(staged_item)} {quote(target)} && "
            f"if test -e {quote(staged_item)} || test -L {quote(staged_item)}; then "
            "echo 'Destination already exists; staged copy retained' >&2; exit 1; fi && "
            f"rmdir -- {quote(stage)}"
        )
        run_process(ssh + [destination["host"], publish], check)
        job_store.update(job_path, remote_partial_path=None)
        progress.add(1, force=True)
