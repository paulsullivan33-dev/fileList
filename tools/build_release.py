#!/usr/bin/env python3
"""Build a Linux release from an explicit list; never include local config/data."""
import ast
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent.parent
APPLICATION_FILES = (
    "actions.py", "app.py", "auth.py", "browser.py", "config_loader.py",
    "job_store.py", "job_worker.py", "manage_user.py", "move_downloads.py",
    "remote_copy.py", "styles.py", "uploads.py", "utils.py", "requirements.txt",
    "start", "README.md", "directories.example.json", "users.example.json",
    "remote_destinations.example.json", "external_sites.example.json",
    "move_downloads.example.json", "tests/test_remote_copy.py",
)


def build():
    entries = {}
    manifest = {}
    for name in APPLICATION_FILES:
        source = ROOT / name
        content = source.read_text(encoding="utf-8").replace("\r\n", "\n")
        if source.suffix == ".py":
            ast.parse(content, feature_version=(3, 8))
        if source.suffix == ".json":
            json.loads(content)
        data = content.encode("utf-8")
        entries["application/" + name] = data
        manifest[name] = hashlib.sha256(data).hexdigest()
    entries["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    entries["install.py"] = (ROOT / "tools/install.py").read_text(encoding="utf-8").encode("utf-8")
    entries["INSTALL.md"] = (ROOT / "INSTALL.md").read_text(encoding="utf-8").encode("utf-8")
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    output = dist / "fileList-linux-install-upgrade.zip"
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            info = ZipInfo("filelist-release/" + name)
            info.create_system = 3
            executable = Path(name).name in ("start", "install.py", "manage_user.py")
            info.external_attr = (0o100755 if executable else 0o100644) << 16
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, data)
    with ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Release ZIP integrity check failed")
    (dist / "INSTALL.md").write_bytes(entries["INSTALL.md"])
    examples = dist / "examples"
    examples.mkdir(exist_ok=True)
    for name in APPLICATION_FILES:
        if name.endswith(".example.json"):
            (examples / name).write_bytes(entries["application/" + name])
    print(output)


if __name__ == "__main__":
    build()
