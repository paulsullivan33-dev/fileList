# fileList — Linux install and upgrade

This package contains the full application, an installer, configuration examples,
and remote-copy tests. It excludes personal users/passwords, session secrets,
server configuration, uploaded files, and Python caches.

## Requirements

Linux, Python 3.8+, Python venv/pip support, Bash, OpenSSH client and rsync.
Python 3.8.10 is accepted. Pip selects dependency releases compatible with the
interpreter; newer Python installations can use newer dependency releases.
The installer downloads Python requirements from PyPI, so internet access is
required unless dependencies are already provisioned. Remote servers need rsync
and GNU coreutils. Video conversion additionally requires ffmpeg.

On Debian/Ubuntu, an administrator can install prerequisites with:

```bash
sudo apt-get update
sudo apt-get install python3 python3-venv python3-pip rsync openssh-client unzip
```

Use the Linux account that owns your existing application and has passwordless
SSH access. Avoid running the app or installer as root. Each server retains its
own SSH configuration and known-host keys.

## Upgrade an existing server

1. Stop both the Flask application and the job worker using your existing service
   manager or Ctrl+C in the terminal running `start`. Finish active transfers first.
2. Extract this ZIP somewhere outside the installed application directory.
3. Run the installer against the existing application directory:

```bash
unzip fileList-linux-install-upgrade.zip -d /tmp/filelist-update
python3 /tmp/filelist-update/filelist-release/install.py --target "$HOME/fileList"
```

Replace `$HOME/fileList` with your actual installation path. The installer checks
package hashes, installs Python dependencies into `.venv`, backs up replaced
application files under `.upgrade-backups/<timestamp>`, and replaces only files
listed in the manifest. Existing `directories.json`, `remote_destinations.json`,
`external_sites.json`, `users.json`, `.flask_session_key`, data directories and
job history are preserved. Missing configuration files receive empty arrays.
Environment overrides such as `FILELIST_CONFIG` and `JOB_DATA_DIR` are unchanged.
The launcher `start` is replaced; preserve any custom launcher changes beforehand.
The installer does not stop/start services or change system service definitions.

Restart with your existing service manager, or:

```bash
cd "$HOME/fileList"
./start
```

If your service launches Python directly, point it at this installation's
`.venv/bin/python` so it uses the installed dependencies. Run only one worker per
job directory. An interrupted upgrade can be rerun; the installer does not
automatically roll back dependency changes. To restore code, stop the processes
and copy the desired backup's contents over the installation, then restart.

## New installation

Use the same installer command with a new target directory. Then configure:

Five example JSON files are included under `application/` in the ZIP and are
installed alongside the application: `directories.example.json`,
`remote_destinations.example.json`, `users.example.json`,
`external_sites.example.json`, and `move_downloads.example.json`.
All filesystem examples use Linux paths. For a new server, copy the directory
and remote examples to the corresponding filenames without `.example`, then
edit the paths, hosts, URLs and groups. The external-sites list is optional.
The users example illustrates the format only; use `manage_user.py` below to
create a real password hash. Do not copy example values over existing settings
when upgrading. The download-moving example is for the optional CLI utility.

1. `directories.json`: use `directories.example.json` as a guide. Configure actual
   local storage paths and the corresponding file-serving URLs on this server.
   **Each `root` and `url` pair must map to the same underlying directory.**
   `root` is the filesystem path used by fileList; `url` is the browser-accessible
   web address serving those same files. For example, with
   `root: /srv/shared/comics` and `url: https://files.example.com/comics`, the file
   `/srv/shared/comics/issue-01.pdf` must open at
   `https://files.example.com/comics/issue-01.pdf`. This also applies to every
   subdirectory beneath the root.
   The app lists/manages files; your existing HTTP server supplies those URLs.
   Configure that server's document root or alias yourself—fileList does not set
   it up. A mismatch can leave listings working while file links and previews
   fail or display different files. Test a known file's URL in your browser.
   An empty directory configuration exposes no files. Keep application code,
   user configuration, session secrets, and job storage outside managed roots.
2. Create a login:

```bash
cd "$HOME/fileList"
.venv/bin/python manage_user.py paul --groups file-managers
```

3. `remote_destinations.json`: copy/edit the example entries to identify your SSH
   hosts and existing absolute remote directories. Group names must match the user.
4. Verify SSH as the same Linux account that will run the worker. Establish and
   verify each host key normally before using the app; batch transfers cannot prompt.
5. Start `./start`. Default address is `0.0.0.0:8081`; `HOST` and `PORT` override it.
   This launcher uses Flask's built-in server. Keep your existing reverse proxy,
   HTTPS and service deployment arrangement for production installations.

The full feature/configuration documentation is in the installed `README.md`.
Run checks after installation with:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tools/check_config.py
```

Use `.venv/bin/python tools/check_config.py --ssh` after configuring remote
destinations to verify SSH, remote write access, and rsync without transferring
files.

The same ZIP can be used on each Linux server. Configure each new server separately;
upgrades preserve its settings. `--skip-dependencies` is available only when you
already provide a Python environment containing the requirements.
