# 🗂️ Flask File Manager

## Install from a clone

Linux and Python 3.8+ are required. Install rsync, the OpenSSH client, and your
distribution's Python venv package. Then, from the repository directory:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp directories.example.json directories.json
cp remote_destinations.example.json remote_destinations.json
```

Edit those local JSON files for your server before starting the app. File-serving
URLs in `directories.json` must point to your existing file server; configure its
access controls separately. Keep source code, credentials, and job storage outside
the managed storage roots. Remote destinations are optional; use `[]` to disable them.

### Important: `root` and `url` must map to the same directory

For every entry in `directories.json`, **`root` and `url` must refer to the same
underlying files**. `root` is the directory path accessible to the fileList
process. `url` is the web address that serves that exact directory to the browser.
They use different syntax, but must represent the same location:

```json
{
  "root": "/srv/shared/comics",
  "url": "https://files.example.com/comics",
  "name": "Comics",
  "group": "comic-readers"
}
```

With this configuration, the web server must serve
`/srv/shared/comics/issue-01.pdf` at
`https://files.example.com/comics/issue-01.pdf`. Subdirectories must map the same
way: `root/series/issue-02.pdf` must be available at
`url/series/issue-02.pdf`.

**fileList does not create this web-server mapping for you.** Configure your HTTP
server's document root or alias to match. If the paths do not correspond, directory
listing and file operations may work while opening files and image previews fail
or show the wrong files. Verify the mapping by opening the URL for a known file
in your browser. The URL must be reachable from the browser, not just the fileList
server. Configure file-server authentication and permissions separately.

Create your own user, with groups matching the configured destinations:

```bash
.venv/bin/python manage_user.py yourname --groups file-managers
./start
```

The launcher starts both Flask and the job worker. Run it as the account with
SSH access. Default port is 8081. See [INSTALL.md](INSTALL.md) for installation,
upgrade, virtual environment, and service configuration details.

## Repository and releases

Only source, documentation, tests, and example configuration are included in Git.
Actual JSON configuration, password hashes, session keys, data, caches, virtual
environments, job records, and `dist/` are excluded by a root-level allowlist in
`.gitignore`. Add new project files to that allowlist deliberately. Never use
`git add -f` for local settings or credentials. Example users contain a placeholder;
use `manage_user.py` to create a real user instead of copying that placeholder.

Build the install/upgrade ZIP on Linux or Windows, without third-party packages:

```bash
python3 tools/build_release.py
```

The result is `dist/fileList-linux-install-upgrade.zip`, with documentation and
example JSON files also placed alongside it. The builder uses an explicit file
list so local configuration and data cannot enter a release through directory
scanning. Attach ZIPs to GitHub Releases instead of committing build artifacts.

Run the regression suite after installing dependencies:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

SSH commands are simulated in these tests; verify real remote connectivity on
each deployment server.

A multi-directory, browser-based file manager built with Flask. Manage your files with a clean UI featuring dark mode, archive handling, file previews, and robust path validation — all from a lightweight Python web server.

## Users and sign-in

The application requires sign-in. Add users to `users.json` as a JSON array. Each user has a username, a password hash in the `password` field, and comma-delimited groups for future authorization rules:

```json
[
  {
    "username": "paul",
    "password": "<Werkzeug password hash>",
    "groups": "admins, file-managers"
  }
]
```

Generate a password hash with:

```powershell
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash(input('Password: ')))"
```

Paste the generated value into `users.json`; never store a plain-text password there.

Each directory in `directories.json` needs a display `name` and a `group`.
Only users whose comma-delimited `groups` list contains that exact directory group
can see or access it. For example:

```json
{
  "root": "/srv/shared/comics",
  "url": "https://files.example.com/comics",
  "name": "Comics",
  "group": "comic-readers"
}
```

---

## 📋 Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Project Structure](#project-structure)
- [Installation](#installation)
- [Usage](#usage)
- [Security Notes](#security-notes)
- [Future Enhancements](#future-enhancements)
- [License](#license)

---

## Overview

Flask File Manager is a self-hosted web application that provides a familiar file-explorer experience directly in the browser. It supports multiple root directories, allowing you to navigate, preview, organize, and manipulate files without ever leaving the web interface. Designed for developers and power users who want a lightweight alternative to heavyweight GUI file managers or cloud storage dashboards.

---

## Features

- 📁 **Multi-directory support** — Configure multiple root directories and switch between them seamlessly
- 👁️ **File previews** — Inline previews for images, text files, PDFs, and more
- 🌙 **Dark mode** — Toggle between light and dark themes; preference is persisted
- 🗜️ **Archive & extract** — Create `.zip` archives and extract them directly in the browser
- ✏️ **Rename & move** — Rename files and move them across directories via a simple UI
- 🔒 **Path validation** — Server-side enforcement prevents directory traversal and unauthorized access
- 🎨 **Consistent styling** — Centralized styles for a cohesive, responsive layout

---

## Project Structure

The application consists of the Flask web process (`app.py`) and a durable
filesystem job worker (`job_worker.py`). The worker processes copy, move, and
video-conversion jobs stored as atomic JSON files by `job_store.py`.

## Background jobs

Start both processes with:

```bash
chmod +x start
./start
```

By default, job records are stored under
`~/.local/state/filelist/jobs`. Override that location with `JOB_DATA_DIR`.
The directory must be local, writable only by the service account, and must not
be one of the roots exposed by the file manager.

Supported settings:

```text
JOB_DATA_DIR=/home/filelist/.local/state/filelist/jobs
JOB_MAX_PENDING_PER_USER=20
JOB_HISTORY_DAYS=7
JOB_PROGRESS_INTERVAL=2
FILELIST_CONFIG=/home/filelist/fileList/directories.json
```

Only one worker may use a job directory at a time. Copy and move jobs report
byte progress, can be cancelled, survive browser navigation, and remain visible
after Flask restarts. If the worker stops during a job, that job is marked
failed on the next worker start and can be submitted again.

## Copy to remote servers (Linux)

Remote servers appear in a separate **Remote servers** section on the Copy,
Move, and Move Selected Items screens. Each entry shows its display name,
SSH host, and destination directory. **Copy here** submits a background copy
and keeps the local original, even when selected from a Move screen.

Edit `remote_destinations.json` (initially an empty array). Use
`remote_destinations.example.json` as a starting point:

```json
[
  {
    "id": "media-server",
    "name": "Media server",
    "host": "media-server",
    "user": "paul",
    "port": 22,
    "path": "/srv/media/incoming",
    "group": "file-managers"
  }
]
```

- `id`: unique stable identifier; `name`: label displayed on the screen.
- `host`: hostname, IPv4 address, or alias from the worker account's SSH config.
- `user` and `port`: optional; omitted values use SSH configuration/defaults.
- `path`: existing absolute directory on the remote Linux server.
- `group`: required user group, using the same groups as `users.json`.

Set `FILELIST_REMOTE_CONFIG` to an absolute JSON file path to store this
configuration elsewhere. Set the same value for Flask and the worker.
Changes are read without restarting; a job fails if its remote configuration
changes before execution. Both source and remote permissions are checked.

Install `rsync` and the OpenSSH client on the file manager server, and `rsync`
and GNU coreutils on each remote Linux server. The worker must run as the
Linux account with passwordless SSH access and known host keys. Verify access
as that account, for example `ssh -oBatchMode=yes paul@media-server true`.
The application never prompts for passwords or accepts unknown host keys.
SSH aliases and identity settings in that account's `~/.ssh/config` are honored.

A selected directory is copied as a directory underneath the configured path;
its contents are not flattened. Spaces and shell characters are supported.
Nested symlinks are preserved, not followed. Existing destination names are
never merged or overwritten. Copies are first staged in a hidden
`.filelist-<job-id>-<item-number>.partial` directory and published after rsync
succeeds. Progress counts completed items; a single large item remains at 0/1
while transferring. Failed or cancelled copies can leave staging directories;
the job displays the partial path for manual cleanup. A worker crash can also
leave staging directories. Previously completed items remain if a multi-item
job fails or is cancelled. Source files should remain unchanged during copying.

Run the remote-copy regression checks with:

```bash
python3 -m unittest discover -s tests -v
```
