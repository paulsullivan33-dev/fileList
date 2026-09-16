#!/usr/bin/env python3
"""Validate fileList configuration before deployment or troubleshooting."""
import argparse
import json
import os
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


def load_json(path):
    try:
        with path.open(encoding="utf-8") as source:
            return json.load(source), None
    except FileNotFoundError:
        return None, f"Missing: {path}"
    except (OSError, json.JSONDecodeError) as error:
        return None, f"Cannot read {path}: {error}"


def check_directories(path):
    data, error = load_json(path)
    if error:
        return [], [error]
    if not isinstance(data, list) or not data:
        return [], ["directories.json must be a non-empty JSON array."]
    messages, errors = [], []
    for number, entry in enumerate(data, 1):
        if not isinstance(entry, dict):
            errors.append(f"Directory {number} must be an object.")
            continue
        root, url, group = entry.get("root"), entry.get("url"), entry.get("group")
        if not all(isinstance(value, str) and value.strip() for value in (root, url, group)):
            errors.append(f"Directory {number} requires non-empty root, url, and group values.")
            continue
        folder = Path(root).expanduser()
        if not folder.is_absolute() or not folder.is_dir():
            errors.append(f"Directory {number} root is not an existing absolute directory: {root}")
            continue
        if not os.access(folder, os.R_OK | os.W_OK | os.X_OK):
            errors.append(f"Directory {number} root is not readable and writable: {root}")
            continue
        messages.append(f"Directory {number}: {root} ({entry.get('name') or folder.name})")
    return messages, errors


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


def check_runtime():
    messages, errors = [], []
    for command in ("ssh", "rsync"):
        executable = shutil.which(command)
        if executable:
            messages.append(f"{command}: {executable}")
        else:
            errors.append(f"Required command is unavailable: {command}")
    root = job_store.job_root()
    parent = root.parent
    if parent.exists() and os.access(parent, os.W_OK | os.X_OK):
        messages.append(f"Job directory: {root}")
    else:
        errors.append(f"Job directory parent is not writable: {parent}")
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh", action="store_true", help="Verify passwordless SSH, remote write access, and rsync")
    args = parser.parse_args(argv)
    config = Path(os.environ.get("FILELIST_CONFIG", ROOT / "directories.json")).expanduser()
    users = ROOT / "users.json"
    checks = [check_directories(config), check_users(users), check_runtime(), check_remotes(args.ssh)]
    errors = []
    for messages, failures in checks:
        for message in messages:
            print("OK:", message)
        for failure in failures:
            print("ERROR:", failure)
        errors.extend(failures)
    if errors:
        print(f"Configuration check failed with {len(errors)} error(s).")
        return 1
    print("Configuration check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
