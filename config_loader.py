import os
import json
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get("FILELIST_CONFIG", APP_DIR / "directories.json")).expanduser().resolve()


@dataclass(frozen=True)
class Directory:
    root: str
    url: str
    name: str
    group: str
    id: str = ""

    @property
    def stable_id(self):
        # Existing configurations need no rewrite. Root-derived identities stay
        # stable across reordering and change when a root is replaced.
        root = os.path.normcase(os.path.abspath(self.root))
        return self.id or "root-" + hashlib.sha256(os.fsencode(root)).hexdigest()

    def __iter__(self):
        """Keep existing root, url unpacking compatible."""
        yield self.root
        yield self.url

def read_directories(config_path):
    """Validate the whole configuration without creating folders or files."""
    config_path = Path(config_path).expanduser().resolve()
    try:
        with config_path.open(encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read directory configuration {config_path}: {error}") from error
    if not isinstance(data, list) or not data:
        raise ValueError(f"{config_path}: expected a non-empty JSON array of directories.")
    directories, errors, identifiers = [], [], set()
    for number, entry in enumerate(data, 1):
        label = f"Directory {number}"
        if not isinstance(entry, dict):
            errors.append(f"{label}: expected an object.")
            continue
        root, url, group = entry.get("root"), entry.get("url"), entry.get("group")
        if not all(isinstance(value, str) and value.strip() for value in (root, url, group)):
            errors.append(f"{label}: root, url, and group must be non-empty strings.")
            continue
        identifier = entry.get("id", "")
        if not isinstance(identifier, str) or (identifier and not re.fullmatch(r"[A-Za-z0-9_-]+", identifier)):
            errors.append(f"{label}: invalid ID; use letters, numbers, underscores, or hyphens.")
            continue
        name = entry.get("name")
        if name is not None and not isinstance(name, str):
            errors.append(f"{label}: name must be a string.")
            continue
        root_path = Path(root).expanduser()
        if not root_path.is_absolute():
            root_path = config_path.parent / root_path
        try:
            root_path = root_path.resolve()
            if not root_path.is_dir():
                errors.append(f"{label}: folder is missing or not a directory: {root_path}")
                continue
            if not os.access(root_path, os.R_OK | os.W_OK | os.X_OK):
                errors.append(f"{label}: folder is not readable and writable: {root_path}")
                continue
        except (OSError, RuntimeError, ValueError) as error:
            errors.append(f"{label}: cannot inspect {root_path}: {error}")
            continue
        directory = Directory(str(root_path), url, name or root_path.name or str(root_path), group, identifier)
        if directory.stable_id in identifiers:
            errors.append(f"Duplicate directory ID: {directory.stable_id} ({label})")
            continue
        identifiers.add(directory.stable_id)
        directories.append(directory)
    if errors:
        raise ValueError("\n".join(errors))
    return directories


def load_directories():
    return read_directories(CONFIG_FILE)
