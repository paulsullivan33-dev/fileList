"""Read-only transfer plans for the existing destination picker forms."""
import hashlib
import json
import os
import posixpath
import stat

import remote_copy
from utils import bind_job_selection, format_size, is_safe_path, is_safe_segment, is_subpath, safe_dir_index, safe_join, user_can_access_directory


def signature(operation, source, destination):
    # List positions are presentation details, not folder identities.
    selections = [{key: value for key, value in selection.items() if key != "directory_index"}
                  for selection in (source, destination)]
    encoded = json.dumps([operation, *selections], sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _measure(path):
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or (getattr(info, "st_file_attributes", 0) & 0x400):
        return {"bytes": 0, "files": 0, "folders": 0, "links": 1}
    if stat.S_ISREG(info.st_mode):
        return {"bytes": info.st_size, "files": 1, "folders": 0, "links": 0}
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"Unsupported file type: {path}")
    total = {"bytes": 0, "files": 0, "folders": 1, "links": 0}
    with os.scandir(path) as children:
        for child in children:
            measured = _measure(child.path)
            for key in total:
                total[key] += measured[key]
    return total


def plan(operation, source_index, source_path, names, directories, groups,
         destination_index=None, destination_path="", remote_id="", remote_subpath=""):
    if operation not in {"copy", "move"}:
        raise ValueError("Choose copy or move.")
    if not isinstance(names, list) or not names or not all(is_safe_segment(name) for name in names):
        raise ValueError("Select valid filenames to preview.")
    if not isinstance(source_path, str) or not is_safe_path(source_path):
        raise ValueError("Invalid source folder.")
    index, sources = remote_copy.validate_sources(source_index, source_path, names, directories, groups)
    source = bind_job_selection({"directory_index": index, "path": source_path, "names": names}, directories)
    is_remote = bool(remote_id)
    if is_remote:
        configured = remote_copy.destination_for(remote_id, groups)
        selected = remote_copy.with_subpath(configured, remote_subpath)
        conflicts = remote_copy.preview_destination(selected, names)
        destination = {"remote_id": selected["id"], "configuration": selected, "names": names}
        folder = f"{selected['host']}:{selected['path']}"
        operation = "copy"
    else:
        target_index = safe_dir_index(destination_index, directories, default=-1)
        if target_index is None or not user_can_access_directory(directories[target_index], groups):
            raise PermissionError("You cannot access the destination folder.")
        if not isinstance(destination_path, str) or not is_safe_path(destination_path):
            raise ValueError("Invalid destination folder.")
        folder = safe_join(directories[target_index].root, destination_path)
        if folder is None or not os.path.isdir(folder):
            raise ValueError("Destination folder is missing.")
        destination = bind_job_selection({"directory_index": target_index, "path": destination_path, "names": names}, directories)
    totals = {"bytes": 0, "files": 0, "folders": 0, "links": 0}
    items = []
    for name, path in sources:
        try:
            measured = _measure(path)
        except (OSError, ValueError) as error:
            raise ValueError(f"Cannot measure {name!r}: {error}") from error
        for key in totals:
            totals[key] += measured[key]
        conflict = ""
        if is_remote:
            target = f"{selected['host']}:{posixpath.join(selected['path'], name)}"
            if name in conflicts:
                conflict = "Name already exists at the destination"
        else:
            target = os.path.join(folder, name)
            if os.path.realpath(path) == os.path.realpath(target):
                conflict = "Already in this folder"
            elif os.path.isdir(path) and is_subpath(folder, path):
                conflict = "Cannot put a folder inside itself"
            elif os.path.lexists(target):
                conflict = "Name already exists at the destination"
        items.append({"name": name, "source": path, "target": target,
                      "bytes": measured["bytes"], "size": format_size(measured["bytes"]), "conflict": conflict})
    return {"operation": operation, "destination": folder, "selected_count": len(items),
            "total_bytes": totals["bytes"], "total_size": format_size(totals["bytes"]),
            "file_count": totals["files"], "folder_count": totals["folders"], "link_count": totals["links"],
            "items": items, "can_transfer": not any(item["conflict"] for item in items),
            "preview_signature": signature("remote_copy" if is_remote else operation, source, destination)}


def scripts():
    return r"""<script>
function invalidateTransferPreview(form) {
    form.previewVersion = (form.previewVersion || 0) + 1;
    form.previewReady = false;
    form.querySelector('button[type=submit]').disabled = true;
    const box = form.querySelector('.transfer-preview');
    box.textContent = 'Checking destination and selection size…';
    return form.previewVersion;
}
async function refreshTransferPreview(form) {
    const version = invalidateTransferPreview(form);
    const box = form.querySelector('.transfer-preview');
    try {
        const fields = new FormData(form);
        fields.set('operation', form.dataset.previewOperation || 'copy');
        const response = await fetch('/transfer_preview', {method: 'POST', body: fields});
        const data = await response.json().catch(() => ({}));
        if (version !== form.previewVersion) return;
        if (!response.ok || !data.success) throw new Error(data.error || 'Could not preview this transfer.');
        box.replaceChildren();
        const destination = document.createElement('p');
        destination.textContent = 'Destination: ' + data.destination; box.appendChild(destination);
        const summary = document.createElement('p');
        summary.textContent = `${data.selected_count} selected item(s) · ${data.file_count} file(s) · ${data.folder_count} folder(s) · Total size: ${data.total_size}` +
            (data.link_count ? ` · ${data.link_count} link(s)` : '');
        box.appendChild(summary);
        const list = document.createElement('ul');
        for (const item of data.items) {
            const row = document.createElement('li');
            row.textContent = `${item.name} → ${item.target} (${item.size})` + (item.conflict ? ' — ' + item.conflict : ' — Ready');
            if (item.conflict) row.className = 'transfer-conflict';
            list.appendChild(row);
        }
        box.appendChild(list);
        const status = document.createElement('p');
        status.textContent = data.can_transfer ? 'Ready. Choose the transfer button to start.' : 'Resolve the listed conflicts or choose another destination.';
        box.appendChild(status);
        let signature = form.querySelector('[name=preview_signature]');
        if (!signature) { signature = document.createElement('input'); signature.type = 'hidden'; signature.name = 'preview_signature'; form.appendChild(signature); }
        signature.value = data.preview_signature;
        form.previewReady = !!data.can_transfer;
        form.querySelector('button[type=submit]').disabled = !form.previewReady;
    } catch (error) {
        if (version !== form.previewVersion) return;
        box.textContent = error.message || 'Could not preview this transfer. Check again.';
    }
}
function initializeTransferPreviews() {
    document.querySelectorAll('form[data-preview-operation]').forEach(form => {
        const preview = document.createElement('section');
        preview.className = 'transfer-preview'; preview.setAttribute('aria-live', 'polite');
        form.insertBefore(preview, form.querySelector('button[type=submit]'));
        const recheck = document.createElement('button');
        recheck.type = 'button'; recheck.textContent = 'Check again';
        recheck.onclick = () => refreshTransferPreview(form); form.appendChild(recheck);
        form.addEventListener('submit', event => {
            if (!form.previewReady) { event.preventDefault(); refreshTransferPreview(form); }
        });
        if (!form.closest('[hidden]')) refreshTransferPreview(form);
    });
}
document.addEventListener('DOMContentLoaded', initializeTransferPreviews);
</script>"""
