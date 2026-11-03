#!/usr/bin/env python3
"""Validate fileList configuration before deployment or troubleshooting."""
import argparse
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import job_store
import remote_copy
from config_loader import read_directories


def load_json(path):
    try:
        with path.open(encoding="utf-8") as source:
            return json.load(source), None
    except FileNotFoundError:
        return None, f"Missing: {path}"
    except (OSError, ValueError) as error:
        return None, f"Cannot read {path}: {error}"


def check_directories(path):
    try:
        directories = read_directories(path)
    except ValueError as error:
        return [], str(error).splitlines()
    return [f"Directory {number}: {directory.root} ({directory.name})"
            for number, directory in enumerate(directories, 1)], []


def check_users(path):
    data, error = load_json(path)
    if error:
        return [], [error]
    if not isinstance(data, list) or not data:
        return [], ["users.json must contain at least one user."]
    names, errors = set(), []
    for number, entry in enumerate(data, 1):
        if not isinstance(entry, dict) or not all(isinstance(entry.get(key), str) and entry[key].strip()
                                                   for key in ("username", "password", "groups")):
            errors.append(f"User {number} requires username, password hash, and groups.")
            continue
        if entry["username"] in names:
            errors.append(f"Duplicate username: {entry['username']}")
        names.add(entry["username"])
    return [f"Users: {len(names)} configured"], errors


def check_writable_location(path, label):
    path = Path(path)
    if path.exists() and not path.is_dir():
        return [], [f"{label} is not a directory: {path}"]
    ancestor = path
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
        return [], [f"{label} cannot be created or written: {path}"]
    return [f"{label}: {path}"], []


def check_runtime(require_remote_tools=False):
    messages, errors = check_writable_location(job_store.job_root(), "Job directory")
    import operation_log
    log = operation_log.database_path()
    log_messages, log_errors = check_writable_location(log.parent, "Operation log directory")
    messages.extend(log_messages)
    errors.extend(log_errors)
    if log.exists() and (not log.is_file() or not os.access(log, os.W_OK)):
        errors.append(f"Operation log is not a writable file: {log}")
    if require_remote_tools:
        for command in ("ssh", "rsync"):
            executable = shutil.which(command)
            if executable:
                messages.append(f"{command}: {executable}")
            else:
                errors.append(f"Remote copying requires an unavailable command: {command}")
    settings = {
        "PORT": (1, 65535), "MAX_FILE_SIZE": (1, None), "MAX_REQUEST_SIZE": (1, None),
        "FILELIST_MIN_FREE_BYTES": (0, None), "FILELIST_REMOTE_MIN_FREE_BYTES": (0, None),
        "JOB_MAX_PENDING_PER_USER": (1, None), "JOB_HISTORY_DAYS": (0, None),
        "FILELIST_LOG_RETENTION_DAYS": (1, None), "LOGIN_MAX_ATTEMPTS": (1, None),
        "FILELIST_WORKER_STALE_SECONDS": (5, None), "FILELIST_WORKER_STALL_SECONDS": (10, None),
        "LOGIN_WINDOW_SECONDS": (1, None), "LOGIN_LOCK_SECONDS": (1, None),
        "ARCHIVE_MAX_MEMBERS": (1, None), "ARCHIVE_MAX_UNCOMPRESSED_BYTES": (0, None),
    }
    for name, (minimum, maximum) in settings.items():
        if name not in os.environ:
            continue
        try:
            value = int(os.environ[name])
            if value < minimum or (maximum is not None and value > maximum):
                raise ValueError()
        except ValueError:
            errors.append(f"{name} must be an integer >= {minimum}" + (f" and <= {maximum}." if maximum else "."))
    if "JOB_PROGRESS_INTERVAL" in os.environ:
        try:
            import math
            value = float(os.environ["JOB_PROGRESS_INTERVAL"])
            if not math.isfinite(value) or value <= 0:
                raise ValueError()
        except ValueError:
            errors.append("JOB_PROGRESS_INTERVAL must be a positive finite number.")
    return messages, errors


def check_remotes(verify_ssh=False):
    try:
        destinations = remote_copy.load_destinations()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [], [f"Remote destinations: {error}"]
    if not destinations:
        return ["Remote destinations: none configured"], []
    messages, errors = [], []
    for destination in destinations:
        label = f"{destination['name']} ({destination['host']}:{destination['path']})"
        if not verify_ssh:
            messages.append("Remote destination: " + label)
            continue
        command = remote_copy.ssh_command(destination) + [destination["host"], (
            f"test -d {shlex.quote(destination['path'])} && "
            f"test -w {shlex.quote(destination['path'])} && rsync --version >/dev/null"
        )]
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=20, check=False)
        except OSError as error:
            errors.append(f"Remote destination failed to start SSH: {label}: {error}")
            continue
        except subprocess.TimeoutExpired:
            errors.append(f"Remote destination timed out: {label}")
            continue
        if result.returncode:
            errors.append(f"Remote destination unavailable: {label}: {result.stdout.strip()[-500:]}")
        else:
            messages.append("Remote destination verified: " + label)
    return messages, errors


def validate_startup(config=None, users=None, verify_ssh=False):
    config = Path(config or os.environ.get("FILELIST_CONFIG", ROOT / "directories.json")).expanduser()
    users = Path(users or ROOT / "users.json")
    directory_check = check_directories(config)
    user_check = check_users(users)
    remote_check = check_remotes(verify_ssh)
    try:
        require_remote_tools = bool(remote_copy.load_destinations())
    except (OSError, ValueError):
        require_remote_tools = False
    runtime_check = check_runtime(require_remote_tools)
    return ([message for check in (directory_check, user_check, remote_check, runtime_check) for message in check[0]],
            [error for check in (directory_check, user_check, remote_check, runtime_check) for error in check[1]])


def require_startup(config=None, users=None):
    _, errors = validate_startup(config, users)
    if errors:
        raise RuntimeError("fileList cannot start:\n" + "\n".join(errors))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh", action="store_true", help="Verify passwordless SSH, remote write access, and rsync")
    args = parser.parse_args(argv)
    messages, errors = validate_startup(verify_ssh=args.ssh)
    for message in messages:
        print("OK:", message)
    for error in errors:
        print("ERROR:", error)
    if errors:
        print(f"Configuration check failed with {len(errors)} error(s).")
        return 1
    print("Configuration check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
