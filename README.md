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
On Linux, the same suite also starts real workers with disposable users, folders,
and job queues. It exercises moves between `/tmp` and `/dev/shm`, low-space refusal,
cancellation during copying, a killed worker followed by restart/retry, and worker
lock contention. No installed configuration or real user files are used. A small
timing delay keeps interruption tests deterministic without replacing file I/O.
These tests are skipped on Windows. Cross-filesystem cases require a writable
second filesystem at `/dev/shm`; CI requires them to run rather than skip.

```bash
FILELIST_REQUIRE_LINUX_INTEGRATION=1 .venv/bin/python -m unittest discover -s tests -p test_linux_integration.py -v
```

Validate the installed configuration before starting the service or after a
server change:

```bash
.venv/bin/python tools/check_config.py
```

Add `--ssh` to verify each configured remote destination with the same account
that runs fileList. It checks passwordless SSH, remote-directory write access,
and remote `rsync` availability without copying files.

`start` and `run.sh` now perform this check automatically, including remote
availability, before launching either process. Direct Flask and worker launches
also validate local configuration and required tools. Fix the numbered entries
or named settings reported in the error before restarting. Missing configured
folders are never created automatically: create the intended folder or restore
its mount first. Relative roots resolve against the configuration file's folder.
SSH and rsync are required only when remote destinations are configured.

In **Configuration**, choose **Check category mappings** to compare observed
superFetch labels against `move_downloads.json` destination keys. The check reads
the shared download history and configured feed caches without moving files or
fetching feeds. It identifies missing mappings and reports incomplete coverage
when histories/caches are missing or unreadable. It uses actual category labels
(for example `Books > eBook`), not numeric feed IDs. New categories can be detected
after superFetch refreshes its caches; unseen future labels cannot be predicted.

```bash
.venv/bin/python move_downloads.py --check-mappings
.venv/bin/python tools/check_config.py --categories
```

Set `FILELIST_MOVE_CONFIG` for a non-default mover configuration. Set
`SUPERFETCH_DATA_DIR` (or pass `--superfetch-data` to the mover check) for the
producer's history/XML folder; otherwise the sibling superFetch folder is used,
matching superFetch's default. `SUPERFETCH_SOURCE` locates its feed catalog when
the repository is elsewhere. Missing mover configuration is reported as
**not_configured**, and is never created automatically. Category checks do not
block normal application startup unless explicitly requested with `--categories`.

Download routing is checked against the actual superFetch producer, including
sanitized names, byte truncation, category-specific hashes, and history parsing:

```bash
.venv/bin/python tools/check_superfetch.py --superfetch ../superFetch
```

The regression suite checks the sibling superFetch repository when present; set
`SUPERFETCH_SOURCE` to another `readAndParse.py` path if needed. CI checks out
superFetch's default branch and runs this contract check on every push and PR.
The checker loads only the naming/history functions and never runs downloads.

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

`manage_user.py` now saves a dated backup before replacing existing user settings.
Backups live beside the original file in `.config-backups/<UTC timestamp>-<id>/`.
Use the same helpers before manually editing other JSON configuration files:

```bash
.venv/bin/python tools/config_backup.py backup directories.json
.venv/bin/python tools/config_backup.py list directories.json
.venv/bin/python tools/config_backup.py restore .config-backups/<backup-id>
```

Restore defaults to the original path recorded in the backup; `--target path.json`
restores elsewhere. It checks the saved contents and backs up current settings
before restoring exact previous bytes. Backup failure stops a settings replacement.
Automatic backups cover user-management updates and restores; manual edits in an
external editor require running the backup command yourself. Existing configuration
files are still preserved by the release installer.
After restoring directory settings, use **Reload configuration** or restart the
application so its active folder list reflects the restored file.

The standalone `move_downloads.py` uses the worker's atomic rename, staged copy,
and free-space checks. Existing names (including dangling links) are never replaced.
It requires the same Windows/Linux atomic-rename support as worker file operations.
Same-drive moves rename without reserving space for a second copy; cross-drive
moves publish a complete staged copy before removing the source. Sources should
remain unchanged during copying, as with worker transfers.

Each source folder has a `.filelist-move-state` recovery journal and mover lock.
Only one standalone mover can use that source at a time; its work never enters the
web application's queue. Rerunning the command cleans recognized interrupted
partials and retries unfinished source items. Completed items are already absent
from the source, so they are not repeated. If a destination was published but the
source remains, both are preserved and the output asks for manual review. Completed
journal entries are kept for 30 days. `--dry-run` creates no recovery or destination
files. Interrupted commands can therefore be rerun without overwriting prior data.

Each directory in `directories.json` needs a display `name` and a `group`.
An optional unique `id` (letters, numbers, underscores, and hyphens) gives it an
explicit identity. Without one, fileList derives a stable ID from the absolute
root, so existing configuration files need no changes. Keep explicit IDs stable
when rearranging entries; do not reuse them for different folders.
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

To leave fileList running after you close an SSH session, use:

```bash
./run.sh
```

It starts `start` in the background and writes application and worker output to
`run.log`. Follow that file with `tail -f run.log`. Shut down that background
instance cleanly with `./stop.sh`; it signals `start`, which stops Flask and the
worker, and waits up to ten seconds before forcing termination. For automatic restart after
a crash or reboot, use the systemd service configuration described in
[INSTALL.md](INSTALL.md).

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
FILELIST_MIN_FREE_BYTES=268435456
FILELIST_REMOTE_MIN_FREE_BYTES=268435456
ARCHIVE_MAX_MEMBERS=10000
ARCHIVE_MAX_UNCOMPRESSED_BYTES=10737418240
LOGIN_MAX_ATTEMPTS=5
LOGIN_WINDOW_SECONDS=300
LOGIN_LOCK_SECONDS=300
FILELIST_CONFIG=/home/filelist/fileList/directories.json
```

Only one worker may use a job directory at a time. Copy and move jobs report
byte progress, can be cancelled, survive browser navigation, and remain visible
after Flask restarts. If the worker stops during a job, that job is marked
failed on the next worker start and can be submitted again.

Folders display 100 items per page by default; choose 25, 50, 100, or 250 with
**Items per page**. Search applies to every filename in the current folder, not
just the displayed page. Submit the search with Enter or **Search**. Name, size,
and modified-date sorting also apply across all pages. Folder links retain sort
and page size, while starting a fresh search in the folder you open.

Normal name browsing reads full size/date details only for displayed rows;
ordering by size or date reads those values for every matching entry. Directory
names still need to be enumerated for a consistent page and match count. The
initial size/count summary describes the current page. **Calculate folder totals**
explicitly scans the folder's direct contents, with unreadable entries reported
as partial totals. It does not recursively scan subfolders.

The existing destination picker now previews the exact target paths, selected
item count, recursive file/folder counts, total size, and existing-name conflicts.
Remote copies check names in the selected remote folder without writing there.
The transfer button is enabled after a successful, conflict-free preview; use
**Check again** to refresh it. Changing remote subfolders refreshes the preview.
A changed folder configuration invalidates the preview before queuing. The worker
still checks current paths, conflicts, and space when the job actually runs.

Open **Recent transfers** from the directory header to review your queued,
running, completed, failed, and cancelled jobs. Job history is private to the
signed-in user and is removed according to `JOB_HISTORY_DAYS`.
Open **Operation log** to search recorded filenames, source/destination folders,
failure reasons, and job IDs, with operation and outcome filters. Records show
the user and UTC time, and include transfer queue/start/item results as well as
uploads, deletes, renames, new folders, and archive operations. Results are paged
50 at a time and show only the signed-in user's records.

Logs are stored separately from job history in `JOB_DATA_DIR/operations.sqlite3`.
Set `FILELIST_OPERATION_LOG` to use another database path. The worker prunes
records older than `FILELIST_LOG_RETENTION_DAYS` (default 30) at startup and daily.
A logging failure is reported in the application log without undoing or retrying
a successful file operation. Existing transfers are not backfilled into the log.
Failed and cancelled jobs can be retried from this screen. Active transfers show
their current rate and a rough remaining-time estimate once progress is available.
Active jobs appear before finished history. Pending jobs show their global queue
position and the number of jobs ahead, including the current transfer. This uses
the same creation-time order as the worker. **Cancel pending** removes a waiting
job before it starts; **Request cancellation** asks the worker to stop an active
job. The page shows **Cancellation requested** until it stops. Use **Refresh queue**
for an updated history view; the live transfer panel updates while polling.

The folder page and Recent transfers show a worker-health indicator, refreshed
every ten seconds. It distinguishes ready, working, starting, stopped, missing/
unresponsive heartbeat, and a worker that may be stalled. The worker writes a
heartbeat every two seconds and records main-loop checkpoints separately, so a
background heartbeat cannot mask a blocked transfer loop. A pulse older than
`FILELIST_WORKER_STALE_SECONDS` (default 15) is unresponsive; no main-loop activity
for `FILELIST_WORKER_STALL_SECONDS` (default 120) while working suggests a stall.
Slow storage or an external tool may also explain this state. These are diagnostic
signals: they never restart the worker, cancel transfers, or alter queued work.
Restart both processes after upgrading so the worker begins publishing health.
Both the live panel and Recent transfers list completed, failed, and unstarted
items separately. **Retry remaining** retries failed and unstarted items while
keeping completed items. Terminal results stay visible until you choose to refresh
the file list; the live panel remembers the ten most recent terminal job IDs and
fetches their details with the normal account checks.

Copy, move, and remote-copy jobs record each fully completed item. Retrying creates
a new job containing only the unfinished items, so completed files do not block
the remaining selection. Progress on the new job covers that remaining selection.
Retries recheck current directory and remote-destination permissions; local workers
also recheck permissions before executing a job.

New jobs record stable directory IDs and the original resolved roots. Reordering
directory configuration does not change a queued job's source or destination.
Removing a directory or changing its root stops the job with an explanation.
Older jobs that store only list positions cannot be safely mapped after an
upgrade: submit those operations again from the file list, rather than retrying
their old history entries. Restart both Flask and the worker when upgrading.

Completion records are conservative: old jobs without per-item records, or a crash
between publishing a file and saving its completion record, may still require manual
inspection of a destination conflict. Existing files are never assumed to be a
successful transfer. A move whose source could not be removed also requires manual
inspection. Remote partial files are not resumed byte by byte or automatically deleted.

Uploads show a progress bar and percentage for each file. This measures data sent
to the server; **Uploaded** appears only after the server confirms success. Failed
entries remain visible and offer **Retry upload**. Use **Refresh file list** when
ready to refresh; completing another upload does not clear earlier errors. After
a network error, check whether the file arrived before retrying to avoid duplicates.
Uploads stream to temporary files and publish only after a successful write.
Concurrent uploads with the same name receive distinct numbered filenames; the
upload result shows the actual saved name. Read, write, size-limit, and publication
failures remove the temporary upload without changing existing files.

`/health` is an unauthenticated JSON liveness endpoint for a reverse proxy or
monitor. Signed-in users can open **Configuration** to see only directories and
remote destinations permitted by their current groups. Send a CSRF-protected POST
to `/config_reload` after editing configuration files to reload them without
restarting Flask. Reload validates remote settings and loads the replacement
directories before replacing the active directory list; a failed reload preserves
that list. Remote settings continue to be read from disk when used, and the worker
reads directory configuration when it starts each job.

Archive extraction checks paths, links, entry count, total uncompressed size,
and available space before writing files. Archive creation validates every
source and rejects symbolic links, junctions, and special files. Creation and
extraction use temporary staging and never overwrite an existing destination.
Choose a new archive name or rename the existing extraction folder to proceed.
Failed operations clean up their staging data. Archives require Windows or Linux
with atomic no-replace rename support; unsupported systems fail safely.
Uploads, video conversion, and remote
copy staging also reserve free space. The byte settings above show the defaults;
raise them when the server needs a larger operating-space reserve.
Local copies check capacity for the whole selection before writing, and staged
copies recheck capacity for each item and each chunk. Cross-drive moves use the
same checks before copying or removing the source. Same-drive moves rename files
without requiring space for a second copy. Checks cannot reserve space against
other processes, so a later disk-full error still fails safely and cleans staging.

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
succeeds. Progress reports transferred bytes while rsync runs. Failed or
cancelled copies can leave staging directories;
the job displays the partial path for manual cleanup. A worker crash can also
leave staging directories. Previously completed items remain if a multi-item
job fails or is cancelled. Source files should remain unchanged during copying.

Use **Browse remote folders** in a remote-server tab to select a child directory
under that destination's configured `path`. fileList lists only folders below that
configured root and records the selected path with the queued job. It never permits
browsing or copying above the configured remote root.

The Select All checkbox chooses only rows on the current page, after searching.
Selections do not carry across page navigation.

Run the remote-copy regression checks with:

```bash
python3 -m unittest discover -s tests -v
```
