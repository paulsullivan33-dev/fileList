import os
import json
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

    def __iter__(self):
        """Keep existing root, url unpacking compatible."""
        yield self.root
        yield self.url

def load_directories():
    """
    Loads directory configuration from directories.json.
    Each entry must contain:
        {
            "root": "/path/to/folder",
            "url": "/static/url/base",
            "name": "Directory tab label",
            "group": "required-user-group"
        }
    Returns configured Directory records.
    """

    if not os.path.exists(CONFIG_FILE):
        # Default configuration if file missing
        default = [
            {
                "root": "storage",
                "url": "/files",
                "name": "Storage",
                "group": "file-managers"
            }
        ]
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=4)

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    directories = []
    for entry in data:
        root = entry.get("root")
        url = entry.get("url")

        if not root or not url:
            continue

        root_path = Path(root).expanduser()
        if not root_path.is_absolute():
            root_path = CONFIG_FILE.parent / root_path
        root = str(root_path.resolve())

        # Ensure directory exists
        if not os.path.exists(root):
            os.makedirs(root)

        name = entry.get("name") or os.path.basename(root.rstrip("/\\")) or root
        group = entry.get("group") or ""
        directories.append(Directory(root=root, url=url, name=name, group=group))

    return directories
