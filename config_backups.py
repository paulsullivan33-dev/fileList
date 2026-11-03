"""Dated JSON configuration backups and atomic, reversible replacements."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import uuid


def backup(path):
    path = Path(path).expanduser().resolve()
    payload = path.read_bytes()
    mode = stat.S_IMODE(path.stat().st_mode)
    identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
    directory = path.parent / ".config-backups" / identifier
    directory.mkdir(parents=True)
    try:
        with (directory / "config.json").open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(directory / "config.json", mode)
        manifest = {"version": 1, "source": str(path), "created_at": datetime.now(timezone.utc).isoformat(),
                    "sha256": hashlib.sha256(payload).hexdigest(), "mode": mode}
        with (directory / "manifest.json").open("x", encoding="utf-8") as output:
            json.dump(manifest, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        # An incomplete backup is never offered by list_backups or restore.
        (directory / "manifest.json").unlink(missing_ok=True)
        raise
    return directory


def replace_bytes(path, payload, mode=None):
    path = Path(path).expanduser().resolve()
    # Backup failure stops the replacement, preserving the current settings.
    previous = backup(path) if path.exists() else None
    existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else mode or 0o600
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, existing_mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return previous


def write_json(path, data):
    return replace_bytes(path, (json.dumps(data, indent=2) + "\n").encode("utf-8"))


def list_backups(path):
    path = Path(path).expanduser().resolve()
    result = []
    for manifest_path in (path.parent / ".config-backups").glob("*/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (manifest.get("version") == 1 and Path(manifest["source"]) == path
                    and (manifest_path.parent / "config.json").is_file()):
                result.append({**manifest, "backup": str(manifest_path.parent)})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(result, key=lambda entry: entry["created_at"], reverse=True)


def restore(directory, target=None):
    directory = Path(directory).expanduser().resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != 1:
        raise ValueError("Unsupported configuration backup format.")
    payload = (directory / "config.json").read_bytes()
    if hashlib.sha256(payload).hexdigest() != manifest.get("sha256"):
        raise ValueError("Configuration backup checksum does not match; nothing was restored.")
    if not isinstance(json.loads(payload), (dict, list)):
        raise ValueError("Configuration backup must contain a JSON object or array.")
    target = Path(target or manifest["source"]).expanduser().resolve()
    previous = replace_bytes(target, payload, manifest.get("mode", 0o600))
    return target, previous
