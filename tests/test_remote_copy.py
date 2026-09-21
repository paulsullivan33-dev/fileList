import io
import json
import os
from pathlib import Path
import tempfile
import sys
import subprocess
import unittest
from unittest.mock import patch, Mock

from flask import Flask
from flask_wtf.csrf import CSRFProtect

import actions
import auth
import job_store
import job_worker
import remote_copy
from config_loader import Directory


class RemoteCopyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "remotes.json"
        self.destination = dict(id="backup", name="Backup <server>", host="backup-alias",
                                path="/srv/Paul's files", group="allowed")
        self.config.write_text(json.dumps([self.destination]), encoding="utf-8")
        self.environment = patch.dict(os.environ, FILELIST_REMOTE_CONFIG=str(self.config),
                                      JOB_DATA_DIR=str(self.root / "jobs"), FLASK_SECRET_KEY="test-key")
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "a file.txt").write_text("content", encoding="utf-8")
        (self.source / "folder").mkdir()
        (self.source / "folder" / "nested.txt").write_text("nested", encoding="utf-8")
        self.directories = [Directory(str(self.source), "/files", "Source", "allowed")]
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        auth.register(self.app, self.directories)
        CSRFProtect(self.app)
        actions.register(self.app, self.directories)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["username"] = "tester"
            session["groups"] = ["allowed"]

    def submit(self, **updates):
        data = dict(src_dir="0", src_path="", name="a file.txt", remote_id="backup")
        data.update(updates)
        return self.client.post("/remote_copy", data=data)

    def job(self):
        self.assertEqual(self.submit().status_code, 200)
        job, path = job_store.claim_next()
        return job, path

    def run_job(self, job, path, process):
        users = json.dumps([dict(username="tester", groups="allowed")])
        with patch.object(Path, "open", return_value=io.StringIO(users)), \
                patch.object(remote_copy, "load_destinations", return_value=[self.destination]), \
                patch.object(remote_copy, "run_process", side_effect=process), \
                patch.object(remote_copy, "run_rsync", side_effect=lambda command, check, report: process(command, check)):
            # job_store uses builtin open, so only the users file read is mocked.
            remote_copy.run_job(job, path, self.directories, job_worker.Progress, job_worker._check_cancel)

    def test_configuration_validation(self):
        for change in (dict(host="-oProxyCommand=evil"), dict(path="relative"), dict(port=0),
                       dict(user="bad user"), dict(group=""), dict(id="../bad")):
            with self.subTest(change=change):
                self.config.write_text(json.dumps([{**self.destination, **change}]), encoding="utf-8")
                with self.assertRaises(ValueError):
                    remote_copy.load_destinations()

    def test_duplicate_ids_rejected(self):
        self.config.write_text(json.dumps([self.destination, self.destination]), encoding="utf-8")
        with self.assertRaises(ValueError):
            remote_copy.load_destinations()

    def test_all_pickers_show_remote_host_path_and_copy_button(self):
        for route, selection in (("/copy", {"src_name": "a file.txt"}),
                                 ("/move", {"src_name": "folder"}),
                                 ("/bulk_move", {"name": ["a file.txt", "folder"]})):
            with self.subTest(route=route):
                response = self.client.get(route, query_string=dict(src_dir="0", src_path="", **selection))
                self.assertEqual(response.status_code, 200)
                page = response.get_data(as_text=True)
                self.assertIn("Remote servers", page)
                self.assertIn("Backup &lt;server&gt;", page)
                self.assertIn("backup-alias", page)
                self.assertIn("/srv/Paul&#x27;s files", page)
                self.assertIn("action='/remote_copy'", page)
                self.assertIn("Copy here", page)

    def test_queues_file_and_directory_without_modifying_sources(self):
        response = self.submit(name=["a file.txt", "folder"])
        self.assertEqual(response.status_code, 200)
        job, _ = job_store.claim_next()
        self.assertEqual(job["operation"], "remote_copy")
        self.assertEqual(job["source"]["names"], ["a file.txt", "folder"])
        self.assertEqual(job["destination"]["configuration"], self.destination)
        self.assertTrue((self.source / "a file.txt").exists())

    def test_remote_browser_lists_only_safe_immediate_directories(self):
        result = type("Result", (), {"returncode": 0, "stdout": "incoming\n..\nname/with/slash\n"})()
        with patch.object(remote_copy.subprocess, "run", return_value=result) as run:
            listed = remote_copy.list_directories(self.destination)
        self.assertEqual(listed, {"path": "", "directories": ["incoming"]})
        self.assertIn("find . -mindepth 1 -maxdepth 1 -type d", run.call_args.args[0][-1])

    def test_browsed_remote_subpath_is_queued_and_preserved(self):
        response = self.submit(remote_subpath="incoming/films")
        self.assertEqual(response.status_code, 200)
        job, _ = job_store.claim_next()
        self.assertEqual(job["destination"]["configuration"]["path"], "/srv/Paul's files/incoming/films")
        self.assertEqual(job["destination"]["configuration"]["base_path"], "/srv/Paul's files")

    def test_remote_escape_paths_are_rejected_before_ssh_or_queueing(self):
        for subpath in ("/tmp/outside", "//tmp/outside", "../outside", "incoming/../../outside",
                        "\\outside", "incoming\\outside", "incoming\nfolder"):
            with self.subTest(subpath=subpath), patch.object(remote_copy.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    remote_copy.list_directories(self.destination, subpath)
                self.assertEqual(self.submit(remote_subpath=subpath).status_code, 400)
                run.assert_not_called()
                self.assertEqual(job_store.pending_count("tester"), 0)

    def test_worker_rejects_previously_queued_absolute_subpath(self):
        job, path = self.job()
        job["destination"]["configuration"] = {
            **self.destination, "path": "/tmp/outside", "subpath": "/tmp/outside",
            "base_path": self.destination["path"],
        }
        process = Mock()
        with self.assertRaises(ValueError):
            self.run_job(job, path, process)
        process.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "Requires POSIX paths and symlinks")
    def test_remote_containment_rejects_symlinks_outside_root(self):
        root = self.root / "remote root"
        child = root / "child"
        outside = self.root / "remote root sibling"
        child.mkdir(parents=True)
        outside.mkdir()
        (root / "escape").symlink_to(outside, target_is_directory=True)
        destination = {**self.destination, "path": str(root)}
        for subpath, allowed in (("", True), ("child", True), ("escape", False)):
            with self.subTest(subpath=subpath):
                selected = remote_copy.with_subpath(destination, subpath)
                result = subprocess.run(["/bin/sh", "-c", remote_copy._containment_check(selected)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, allowed)

    def test_retry_checks_remote_destination_permission(self):
        job, path = self.job()
        job_store.finish(path, "failed", "temporary error")
        self.destination["group"] = "other-group"
        self.config.write_text(json.dumps([self.destination]), encoding="utf-8")
        response = self.client.post("/job_retry", data={"job_id": job["job_id"]})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(job_store.pending_count("tester"), 0)

    def test_missing_source_directory_is_forbidden(self):
        self.assertEqual(self.submit(src_dir="").status_code, 403)

    def test_source_permissions_checked(self):
        with self.client.session_transaction() as session:
            session["groups"] = ["different"]
        self.assertEqual(self.submit().status_code, 403)

    def test_remote_permissions_checked_and_hidden(self):
        self.destination["group"] = "different"
        self.config.write_text(json.dumps([self.destination]), encoding="utf-8")
        self.assertEqual(self.submit().status_code, 403)
        response = self.client.get("/copy?src_dir=0&src_name=folder")
        self.assertNotIn("backup-alias", response.get_data(as_text=True))

    def test_unknown_destination_rejected(self):
        self.assertEqual(self.submit(remote_id="other").status_code, 403)

    def test_invalid_names_rejected(self):
        for name in ("../outside", ".", "folder/nested.txt", "missing.txt"):
            with self.subTest(name=name):
                self.assertEqual(self.submit(name=name).status_code, 400)

    def test_csrf_enforced(self):
        self.app.config["WTF_CSRF_ENABLED"] = True
        self.assertEqual(self.submit().status_code, 400)

    def test_stages_rsync_and_publishes_without_overwrite(self):
        job, path = self.job()
        commands = []
        self.run_job(job, path, lambda command, check: commands.append(command))
        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[0][0], "ssh")
        self.assertIn("-oBatchMode=yes", commands[0])
        self.assertIn("-oStrictHostKeyChecking=yes", commands[0])
        self.assertIn("df -Pk", commands[0][-1])
        self.assertIn("mkdir --", commands[0][-1])
        self.assertEqual(commands[1][0], "rsync")
        self.assertIn("--info=progress2", commands[1])
        self.assertIn("--protect-args", commands[1])
        self.assertEqual(commands[1][-2], str(self.source / "a file.txt"))
        self.assertIn("mv -T -n --", commands[2][-1])
        record = job_store.read_path(path)
        self.assertEqual(record["progress_unit"], "bytes")
        self.assertEqual(record["bytes_total"], len("content"))
        self.assertEqual(record["bytes_completed"], len("content"))
        self.assertIsNone(record["remote_partial_path"])
        self.assertTrue((self.source / "a file.txt").exists())

    def test_rsync_failure_never_publishes(self):
        job, path = self.job()
        commands = []
        def process(command, check):
            commands.append(command)
            if command[0] == "rsync":
                raise RuntimeError("connection failed")
        with self.assertRaisesRegex(RuntimeError, "connection failed"):
            self.run_job(job, path, process)
        self.assertEqual(len(commands), 2)
        self.assertIn(".partial", job_store.read_path(path)["remote_partial_path"])
        self.assertTrue((self.source / "a file.txt").exists())

    def test_partial_remote_copy_retry_keeps_only_unfinished_items(self):
        self.assertEqual(self.submit(name=["a file.txt", "folder"]).status_code, 200)
        job, path = job_store.claim_next()
        transfers = []
        def process(command, check):
            if command[0] == "rsync":
                transfers.append(command[-2])
                if len(transfers) == 2:
                    raise RuntimeError("Second transfer failed")
        with self.assertRaisesRegex(RuntimeError, "Second transfer"):
            self.run_job(job, path, process)
        failed = job_store.finish(path, "failed", "Second transfer failed")
        self.assertEqual(failed["completed_names"], ["a file.txt"])
        source, destination = job_store.retry_selection(failed)
        self.assertEqual(source["names"], ["folder"])
        retry = job_store.create("remote_copy", "tester", source, destination)
        retry, path = job_store.claim_next()
        commands = []
        self.run_job(retry, path, lambda command, check: commands.append(command))
        copies = [command for command in commands if command[0] == "rsync"]
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0][-2], str(self.source / "folder"))

    def test_existing_remote_target_stops_before_transfer(self):
        job, path = self.job()
        commands = []
        def process(command, check):
            commands.append(command)
            raise RuntimeError("Destination already exists")
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            self.run_job(job, path, process)
        self.assertEqual(len(commands), 1)

    def test_cancellation_before_transfer(self):
        job, path = self.job()
        job_store.request_cancel(job["job_id"], "tester")
        process = Mock()
        with self.assertRaises(job_worker.JobCancelled):
            self.run_job(job, path, process)
        process.assert_not_called()

    def test_changed_destination_fails(self):
        job, path = self.job()
        self.destination["host"] = "different-host"
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            self.run_job(job, path, Mock())

    def test_worker_dispatches_and_completes_remote_job(self):
        self.assertEqual(self.submit().status_code, 200)
        with patch.object(job_worker.config_loader, "load_directories", return_value=self.directories), \
                patch.object(remote_copy, "run_job") as run:
            self.assertTrue(job_worker.run_once())
        run.assert_called_once()
        self.assertEqual(len(list((self.root / "jobs" / "complete").glob("*.json"))), 1)

    def test_process_error_is_reported(self):
        with self.assertRaisesRegex(RuntimeError, "SSH failed"):
            remote_copy.run_process([sys.executable, "-c", "import sys; print('SSH failed'); sys.exit(1)"], lambda: None)

    def test_rsync_progress_is_reported(self):
        progress = []
        remote_copy.run_rsync(
            [sys.executable, "-c", "import sys; sys.stdout.write('  1,024  50%\\r  2,048 100%\\r'); sys.stdout.flush()"],
            lambda: None,
            progress.append,
        )
        self.assertEqual(progress, [1024, 2048])

    def test_running_process_is_stopped_on_cancellation(self):
        def cancel():
            raise job_worker.JobCancelled("cancelled")
        with self.assertRaises(job_worker.JobCancelled):
            remote_copy.run_process([sys.executable, "-c", "import time; time.sleep(30)"], cancel)

    def test_revoked_user_permissions_stop_worker(self):
        job, path = self.job()
        users = json.dumps([dict(username="tester", groups="revoked")])
        with patch.object(Path, "open", return_value=io.StringIO(users)), \
                patch.object(remote_copy, "run_process") as process:
            with self.assertRaises(PermissionError):
                remote_copy.run_job(job, path, self.directories, job_worker.Progress, job_worker._check_cancel)
        process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
