#!/usr/bin/env python3
"""Install or upgrade fileList without replacing server configuration."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def install(target, skip_dependencies=False):
    package = Path(__file__).resolve().parent
    payload = package / "application"
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest.items():
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("Invalid package manifest")
        if hashlib.sha256((payload / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Package checksum failed: {name}")
    target = target.expanduser().resolve()
    if target == package or package in target.parents or target in package.parents:
        raise ValueError("Installation directory must be separate from the extracted package")
    target.mkdir(parents=True, exist_ok=True)
    if not skip_dependencies:
        venv = target / ".venv"
        if not (venv / "bin" / "python").exists():
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        python = str(venv / "bin" / "python")
        pip_check = subprocess.run([python, "-m", "pip", "--version"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if pip_check.returncode:
            print("Repairing pip in the existing virtual environment...", flush=True)
            subprocess.run([python, "-m", "ensurepip", "--upgrade"], check=True)
        subprocess.run([str(venv / "bin" / "python"), "-m", "pip", "install", "-r",
                        str(payload / "requirements.txt")], check=True)
    backup = target / ".upgrade-backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for name in manifest:
        destination = target / name
        if destination.is_symlink():
            raise ValueError(f"Refusing to replace symlink: {destination}")
        if destination.exists():
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, saved)
    for name in manifest:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=destination.parent)
        os.close(handle)
        try:
            shutil.copyfile(payload / name, temporary)
            os.chmod(temporary, 0o755 if name in ("start", "manage_user.py") else 0o644)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    for name in ("directories.json", "users.json", "remote_destinations.json", "external_sites.json"):
        try:
            with (target / name).open("x", encoding="utf-8") as output:
                output.write("[]\n")
            os.chmod(target / name, 0o600)
        except FileExistsError:
            pass
    print(f"Installed: {target}")
    if backup.exists():
        print(f"Previous application files: {backup}")
    print("Existing configuration, users, session key, data and jobs were preserved.")
    print("Configure directories/users if this is a new install, then start ./start as your SSH-enabled account.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True, help="Existing or new application directory")
    parser.add_argument("--skip-dependencies", action="store_true", help="Use an already provisioned Python environment")
    args = parser.parse_args()
    if sys.version_info < (3, 8):
        parser.error("Python 3.8 or newer is required")
    if not sys.platform.startswith("linux"):
        parser.error("Run this installer on Linux")
    install(args.target, args.skip_dependencies)


if __name__ == "__main__":
    main()
