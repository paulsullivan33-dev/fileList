"""Safety and history regression tests."""
import json
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from werkzeug.security import generate_password_hash

import actions
import auth
import job_store
import job_worker
import operation_log
import uploads
import utils
from config_loader import Directory


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.files = self.root / "files"
        self.files.mkdir()
        (self.files / "a file.txt").write_text("content", encoding="utf-8")
        (self.root / "users.json").write_text(json.dumps([{
            "username": "tester",
            "password": generate_password_hash("correct-password"),
            "groups": "members",
        }]), encoding="utf-8")
        self.environment = patch.dict(os.environ, {
            "FLASK_SECRET_KEY": "test-key",
            "JOB_DATA_DIR": str(self.root / "jobs"),
            "LOGIN_MAX_ATTEMPTS": "2",
            "LOGIN_WINDOW_SECONDS": "60",
            "LOGIN_LOCK_SECONDS": "60",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = Flask(__name__, root_path=str(self.root))
        self.app.config.update(TESTING=True)
        self.app.jinja_env.globals["csrf_token"] = lambda: "test-token"
        self.directories = [Directory(str(self.files), "/files", "Files", "members")]
        auth.register(self.app, self.directories)
        actions.register(self.app, self.directories)
        uploads.register(self.app, self.directories)
        self.client = self.app.test_client()

    def create_job(self, operation, owner, source, destination):
        return job_store.create(operation, owner, source, destination,
                                directories=self.directories if "directory_index" in source else None)

    def sign_in(self):
        with self.client.session_transaction() as session:
            session["username"] = "tester"
            session["groups"] = ["members"]

    def test_login_is_temporarily_throttled_after_failed_attempts(self):
        self.assertEqual(self.client.post("/login", data={"username": "tester", "password": "wrong"}).status_code, 401)
        self.assertEqual(self.client.post("/login", data={"username": "tester", "password": "wrong"}).status_code, 401)
        response = self.client.post("/login", data={"username": "tester", "password": "correct-password"})
        self.assertEqual(response.status_code, 429)
        self.assertIn("Too many sign-in attempts", response.get_data(as_text=True))

    def test_archive_limits_reject_large_expansion_before_extraction(self):
        with patch.dict(os.environ, {"ARCHIVE_MAX_MEMBERS": "1"}):
            with self.assertRaisesRegex(ValueError, "too many entries"):
                actions._validate_archive_size([1, 1])
        with patch.dict(os.environ, {"ARCHIVE_MAX_UNCOMPRESSED_BYTES": "2"}):
            with self.assertRaisesRegex(ValueError, "above the"):
                actions._validate_archive_size([3])

    def test_disk_reserve_blocks_upload_before_writing(self):
        self.sign_in()
        with patch.object(uploads, "ensure_free_space", side_effect=OSError("Not enough free disk space")):
            response = self.client.post("/upload", data={"dir": "0", "path": ""})
        self.assertEqual(response.status_code, 507)
        self.assertIn("Not enough free disk space", response.get_json()["error"])

    def test_disk_reserve_helper_reports_available_space(self):
        usage = type("Usage", (), {"free": 10})()
        with patch.object(utils.shutil, "disk_usage", return_value=usage):
            with self.assertRaisesRegex(OSError, "Not enough free disk space"):
                utils.ensure_free_space(self.files, 10, reserve_bytes=1)

    def test_recent_transfers_shows_only_the_signed_in_user(self):
        self.create_job("remote_copy", "tester", {"names": ["a file.txt"]}, {})
        self.create_job("remote_copy", "someone-else", {"names": ["private.txt"]}, {})
        self.sign_in()
        response = self.client.get("/jobs")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("a file.txt", page)
        self.assertNotIn("private.txt", page)

    def test_failed_job_can_be_retried_only_by_its_owner(self):
        job = self.create_job("copy", "tester", {"directory_index": 0, "path": "", "names": ["a file.txt"]},
                               {"directory_index": 0, "path": "", "names": ["a file.txt"]})
        claimed, path = job_store.claim_next()
        job_store.finish(path, "failed", "temporary error")
        self.sign_in()
        response = self.client.post("/job_retry", data={"job_id": job["job_id"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(job_store.pending_count("tester"), 1)

    def test_configuration_status_reports_loaded_directories(self):
        self.sign_in()
        response = self.client.get("/config_status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["directories"][0]["name"], "Files")

    def test_retry_json_response_identifies_new_trackable_job(self):
        job = self.create_job("copy", "tester", {"directory_index": 0, "path": "", "names": ["a file.txt"]},
                               {"directory_index": 0, "path": "", "names": ["a file.txt"]})
        _, path = job_store.claim_next()
        job_store.finish(path, "failed", "temporary error")
        self.sign_in()
        response = self.client.post("/job_retry", data={"job_id": job["job_id"]},
                                    headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 202)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertNotEqual(data["job_id"], job["job_id"])
        self.assertEqual(data["status"], "pending")
        status = self.client.get("/job_status", query_string={"job_id": data["job_id"]})
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()["operation"], "copy")

    def test_configuration_views_hide_other_groups_and_revoked_grants(self):
        self.directories.append(Directory("/private-root", "/private", "Private files", "private"))
        remotes = [
            {"id": "allowed", "name": "Allowed remote", "path": "/shared-remote", "group": "members"},
            {"id": "private", "name": "Private remote", "path": "/private-remote", "group": "private"},
        ]
        self.sign_in()
        with patch.object(actions.remote_copy, "load_destinations", return_value=remotes):
            for route in ("/config_status", "/configuration"):
                page = self.client.get(route).get_data(as_text=True)
                self.assertIn("Files", page)
                self.assertIn("/shared-remote", page)
                self.assertNotIn("/private-root", page)
                self.assertNotIn("/private-remote", page)
                self.assertNotIn("Private files", page)
                self.assertNotIn("Private remote", page)
            (self.root / "users.json").write_text("[]", encoding="utf-8")
            # Revocation now denies the whole request and invalidates the cookie.
            self.assertEqual(self.client.get("/config_status").status_code, 403)
            page = self.client.get("/configuration").get_data(as_text=True)
            self.assertNotIn("/shared-remote", page)
            self.assertNotIn("<strong>Files</strong>", page)

    def test_configuration_errors_do_not_disclose_private_paths(self):
        self.sign_in()
        with patch.object(actions.remote_copy, "load_destinations", side_effect=OSError("/private-config.json")):
            for route in ("/config_status", "/configuration"):
                page = self.client.get(route).get_data(as_text=True)
                self.assertIn("Remote configuration is unavailable", page)
                self.assertNotIn("/private-config.json", page)

    def make_transfer(self, operation="copy"):
        target = self.root / "target"
        target.mkdir(exist_ok=True)
        self.directories[:] = [
            Directory(str(self.files), "/files", "Files", "members"),
            Directory(str(target), "/target", "Target", "writers"),
        ]
        return self.create_job(operation, "tester",
            {"directory_index": 0, "path": "", "names": ["a file.txt"]},
            {"directory_index": 1, "path": "", "names": ["a file.txt"]})

    def test_failed_remote_reload_keeps_active_directories(self):
        self.sign_in()
        before = list(self.directories)
        with patch.object(actions.remote_copy, "load_destinations", side_effect=ValueError("invalid remote")), \
                patch.object(actions.config_loader, "load_directories") as load:
            response = self.client.post("/config_reload")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.directories, before)
        load.assert_not_called()

    def test_failed_directory_reload_keeps_active_directories(self):
        self.sign_in()
        before = list(self.directories)
        with patch.object(actions.remote_copy, "load_destinations", return_value=[]), \
                patch.object(actions.config_loader, "load_directories", side_effect=ValueError("invalid directory")):
            response = self.client.post("/config_reload")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.directories, before)

    def test_successful_reload_updates_shared_list_after_validation(self):
        self.sign_in()
        shared = self.directories
        before = list(shared)
        replacement = [Directory(str(self.files), "/new", "New files", "members")]
        def load():
            self.assertEqual(shared, before)
            return replacement
        with patch.object(actions.remote_copy, "load_destinations", return_value=[]) as remotes, \
                patch.object(actions.config_loader, "load_directories", side_effect=load):
            response = self.client.post("/config_reload")
        remotes.assert_called_once()
        self.assertEqual(response.status_code, 200)
        self.assertIs(self.directories, shared)
        self.assertEqual(shared, replacement)
        self.assertEqual(self.client.get("/config_status").get_json()["directories"][0]["name"], "New files")

    def test_retry_rechecks_source_and_destination_grants(self):
        job = self.make_transfer()
        _, path = job_store.claim_next()
        job_store.finish(path, "failed", "temporary error")
        with self.client.session_transaction() as session:
            session["username"] = "tester"
            session["groups"] = ["members", "writers"]
        for groups in ("members", "writers", ""):
            with self.subTest(groups=groups):
                (self.root / "users.json").write_text(json.dumps([
                    {"username": "tester", "groups": groups}]), encoding="utf-8")
                response = self.client.post("/job_retry", data={"job_id": job["job_id"]})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(job_store.pending_count("tester"), 0)

    def test_retry_rejects_deleted_user_with_existing_session(self):
        job = self.make_transfer()
        _, path = job_store.claim_next()
        job_store.finish(path, "failed", "temporary error")
        with self.client.session_transaction() as session:
            session["username"] = "tester"
            session["groups"] = ["members", "writers"]
        (self.root / "users.json").write_text("[]", encoding="utf-8")
        self.assertEqual(self.client.post("/job_retry", data={"job_id": job["job_id"]}).status_code, 403)
        self.assertEqual(job_store.pending_count("tester"), 0)

    def test_worker_blocks_revoked_grants_before_copy_move_or_conversion(self):
        for operation in ("copy", "move", "convert"):
            for groups in ("writers", "members" if operation != "convert" else ""):
                with self.subTest(operation=operation, groups=groups):
                    job = self.make_transfer(operation)
                    users_path = self.root / "users.json"
                    users_path.write_text(json.dumps([
                        {"username": "tester", "groups": groups}]), encoding="utf-8")
                    with patch.object(job_worker.config_loader, "load_directories", return_value=self.directories), \
                            patch.object(job_worker, "current_user_groups", side_effect=lambda owner: utils.current_user_groups(owner, users_path)), \
                            patch.object(job_worker, "_run_transfer") as transfer, \
                            patch.object(job_worker, "_run_conversion") as conversion:
                        self.assertTrue(job_worker.run_once())
                    transfer.assert_not_called()
                    conversion.assert_not_called()
                    stored, _ = job_store.find(job["job_id"])
                    self.assertEqual(stored["status"], "failed")
                    self.assertIn("cannot access", stored["error"])
                    self.assertTrue((self.files / "a file.txt").exists())
                    self.assertFalse((self.root / "target" / "a file.txt").exists())

    def test_authorized_retry_still_copies_file(self):
        job = self.make_transfer()
        _, path = job_store.claim_next()
        job_store.finish(path, "failed", "temporary error")
        users_path = self.root / "users.json"
        users_path.write_text(json.dumps([
            {"username": "tester", "groups": "members,writers"}]), encoding="utf-8")
        with self.client.session_transaction() as session:
            session["username"] = "tester"
            session["groups"] = ["members", "writers"]
        self.assertEqual(self.client.post("/job_retry", data={"job_id": job["job_id"]}).status_code, 200)
        with patch.object(job_worker.config_loader, "load_directories", return_value=self.directories), \
                patch.object(job_worker, "current_user_groups", side_effect=lambda owner: utils.current_user_groups(owner, users_path)):
            self.assertTrue(job_worker.run_once())
        self.assertEqual((self.root / "target" / "a file.txt").read_text(encoding="utf-8"), "content")

    def test_bulk_move_identifies_the_invalid_selected_item(self):
        self.sign_in()
        response = self.client.get("/bulk_move", query_string={
            "src_dir": 0, "src_path": "", "dest_dir": 0, "dest_path": "", "name": "../bad",
        })
        self.assertIn("Invalid selected item", response.get_data(as_text=True))
        self.assertIn("../bad", response.get_data(as_text=True))

    def test_partial_transfer_retry_skips_completed_items(self):
        for operation in ("copy", "move"):
            with self.subTest(operation=operation):
                first = f"{operation}-first.txt"
                second = f"{operation}-second.txt"
                for name in (first, second):
                    (self.files / name).write_text(name, encoding="utf-8")
                target = self.root / "target"
                target.mkdir(exist_ok=True)
                self.directories[:] = [Directory(str(self.files), "/files", "Files", "members"),
                                       Directory(str(target), "/target", "Target", "members")]
                job = self.create_job(operation, "tester",
                    {"directory_index": 0, "path": "", "names": [first, second]},
                    {"directory_index": 1, "path": "", "names": [first, second]})
                original_check = job_worker._check_cancel
                def cancel_after_first(job_id):
                    if (target / first).exists():
                        raise job_worker.JobCancelled("Interrupted after first item")
                    original_check(job_id)
                with patch.object(job_worker.config_loader, "load_directories", return_value=self.directories), \
                        patch.object(job_worker, "current_user_groups", return_value=["members"]), \
                        patch.object(job_worker, "_check_cancel", side_effect=cancel_after_first):
                    self.assertTrue(job_worker.run_once())
                old, _ = job_store.find(job["job_id"])
                self.assertEqual(old["status"], "cancelled")
                self.assertEqual(old["completed_names"], [first])
                self.sign_in()
                response = self.client.post("/job_retry", data={"job_id": job["job_id"]}, headers={"Accept": "application/json"})
                self.assertEqual(response.status_code, 202)
                retry, _ = job_store.find(response.get_json()["job_id"])
                self.assertEqual(retry["source"]["names"], [second])
                self.assertEqual(retry["destination"]["names"], [second])
                with patch.object(job_worker.config_loader, "load_directories", return_value=self.directories), \
                        patch.object(job_worker, "current_user_groups", return_value=["members"]):
                    self.assertTrue(job_worker.run_once())
                retried, _ = job_store.find(retry["job_id"])
                self.assertEqual(retried["status"], "complete")
                self.assertEqual((target / first).read_text(), first)
                self.assertEqual((target / second).read_text(), second)

    def test_retry_never_skips_unrecorded_destination_collisions(self):
        job = self.make_transfer()
        target = self.root / "target" / "a file.txt"
        target.write_text("unrelated existing file", encoding="utf-8")
        with patch.object(job_worker.config_loader, "load_directories", return_value=self.directories), \
                patch.object(job_worker, "current_user_groups", return_value=["members", "writers"]):
            self.assertTrue(job_worker.run_once())
        old, _ = job_store.find(job["job_id"])
        self.assertEqual(old["completed_names"], [])
        source, _ = job_store.retry_selection(old)
        self.assertEqual(source["names"], ["a file.txt"])
        self.assertEqual(target.read_text(), "unrelated existing file")

    def test_retry_with_every_item_completed_creates_no_new_job(self):
        job = self.make_transfer()
        _, path = job_store.claim_next()
        job_store.complete_item(path, "a file.txt")
        job_store.finish(path, "cancelled", "Late cancellation")
        old, _ = job_store.find(job["job_id"])
        with self.assertRaisesRegex(ValueError, "nothing to retry"):
            job_store.retry_selection(old)
        self.assertEqual(job_store.pending_count("tester"), 0)


    def test_consecutive_periods_are_names_not_traversal(self):
        for name in ("report..pdf", "release...mkv", "..hidden.txt"):
            self.assertTrue(utils.is_safe_segment(name), name)
            self.assertTrue(utils.is_safe_path(f"folder..name/{name}"), name)
        for name in (".", "..", "../outside", "folder/name", "folder\\name", None):
            self.assertFalse(utils.is_safe_segment(name), repr(name))
        for path in ("../outside", "folder/../outside", "folder/./file"):
            self.assertFalse(utils.is_safe_path(path), path)

    def test_copy_and_move_double_period_names_through_routes_and_worker(self):
        target = self.root / "target"
        target.mkdir()
        self.directories.append(Directory(str(target), "/target", "Target", "members"))
        for operation, route in (("copy", "/copy"), ("move", "/move_do")):
            name = f"{operation}..txt"
            (self.files / name).write_text("contents", encoding="utf-8")
            self.sign_in()
            response = self.client.post(route, data={
                "src_dir": 0, "src_path": "", "src_name": name,
                "dest_dir": 1, "dest_path": "",
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(job_store.pending_count("tester"), 1)
            with patch.object(job_worker.config_loader, "load_directories", return_value=self.directories), \
                    patch.object(job_worker, "current_user_groups", return_value=["members"]):
                self.assertTrue(job_worker.run_once())
            self.assertEqual((target / name).read_text(), "contents")
            self.assertEqual((self.files / name).exists(), operation == "copy")

    def test_worker_failure_names_item_and_preserves_completed_items(self):
        target = self.root / "target"
        target.mkdir()
        self.directories.append(Directory(str(target), "/target", "Target", "members"))
        names = ["first..txt", "failed..txt", "unstarted..txt"]
        for name in names:
            (self.files / name).write_text(name, encoding="utf-8")
        job = self.create_job("copy", "tester",
            {"directory_index": 0, "path": "", "names": names},
            {"directory_index": 1, "path": "", "names": names})
        original = job_worker._copy_staged
        def fail_second(source, destination, progress, *arguments):
            if os.path.basename(source) == names[1]:
                raise OSError("Disk full")
            return original(source, destination, progress, *arguments)
        with patch.object(job_worker.config_loader, "load_directories", return_value=self.directories), \
                patch.object(job_worker, "current_user_groups", return_value=["members"]), \
                patch.object(job_worker, "_copy_staged", side_effect=fail_second):
            self.assertTrue(job_worker.run_once())
        stored, _ = job_store.find(job["job_id"])
        self.assertEqual(stored["status"], "failed")
        self.assertIn(names[1], stored["error"])
        self.assertIn("Disk full", stored["error"])
        self.assertEqual(stored["completed_names"], [names[0]])
        self.sign_in()
        response = self.client.get("/job_status", query_string={"job_id": job["job_id"]})
        data = response.get_json()
        self.assertIn(names[1], data["error"])
        self.assertEqual(data["completed_names"], [names[0]])
        self.assertEqual(data["failed_names"], [names[1]])
        self.assertEqual(data["remaining_names"], [names[2]])
        self.assertEqual(data["retry_count"], 2)
        self.assertTrue(data["can_retry"])
        page = self.client.get("/jobs").get_data(as_text=True)
        self.assertIn("Completed (1)", page)
        self.assertIn("Failed (1)", page)
        self.assertIn("Remaining (1)", page)
        self.assertIn("Retry remaining (2)", page)
        (target / names[0]).write_text("keep completed", encoding="utf-8")
        response = self.client.post("/job_retry", data={"job_id": job["job_id"]},
                                    headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 202)
        retry, _ = job_store.find(response.get_json()["job_id"])
        self.assertEqual(retry["source"]["names"], names[1:])
        with patch.object(job_worker.config_loader, "load_directories", return_value=self.directories), \
                patch.object(job_worker, "current_user_groups", return_value=["members"]):
            self.assertTrue(job_worker.run_once())
        self.assertEqual(job_store.find(retry["job_id"])[0]["status"], "complete")
        self.assertEqual((target / names[0]).read_text(), "keep completed")
        for name in names[1:]:
            self.assertEqual((target / name).read_text(), name)

    def test_filename_error_cannot_break_out_of_script(self):
        self.sign_in()
        response = self.client.get("/bulk_move", query_string={
            "src_dir": 0, "name": "</script><script>alert(1)</script>",
        })
        self.assertNotIn("<script>alert(1)</script>", response.get_data(as_text=True))

    def run_worker(self):
        with patch.object(job_worker.config_loader, "load_directories", return_value=self.directories), \
                patch.object(job_worker, "current_user_groups", return_value=["members"]):
            return job_worker.run_once()

    def test_archive_routes_report_conflicts_without_overwriting(self):
        self.sign_in()
        response = self.client.post("/archive", json={
            "dir": 0, "names": ["a file.txt"], "name": "backup", "type": "zip",
        })
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["success"])
        self.assertTrue(self.run_worker())
        original = (self.files / "backup.zip").read_bytes()
        response = self.client.post("/archive", json={
            "dir": 0, "names": ["a file.txt"], "name": "backup", "type": "zip",
        })
        self.assertEqual(response.status_code, 409)
        self.assertIn("already exists", response.get_json()["error"])
        self.assertEqual((self.files / "backup.zip").read_bytes(), original)
        response = self.client.post("/extract", data={"dir": 0, "name": "backup.zip"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.run_worker())
        extracted = self.files / "backup" / "a file.txt"
        extracted.write_text("keep this version", encoding="utf-8")
        response = self.client.post("/extract", data={"dir": 0, "name": "backup.zip"})
        self.assertIn("already exists", response.get_data(as_text=True))
        self.assertEqual(extracted.read_text(), "keep this version")

    def test_archive_and_extract_run_as_queued_jobs_with_progress(self):
        self.sign_in()
        (self.files / "folder").mkdir()
        (self.files / "folder" / "inner.txt").write_text("inner contents", encoding="utf-8")
        response = self.client.post("/archive", json={
            "dir": 0, "names": ["a file.txt", "folder"], "name": "bundle", "type": "tgz",
        })
        data = response.get_json()
        self.assertEqual(data["operation"], "archive")
        self.assertEqual(data["status"], "pending")
        # Nothing is built inside the request.
        self.assertFalse((self.files / "bundle.tgz").exists())
        self.assertTrue(self.run_worker())
        job, _ = job_store.find(data["job_id"])
        self.assertEqual(job["status"], "complete")
        self.assertEqual(job["bytes_total"], len("content") + len("inner contents"))
        self.assertEqual(job["bytes_completed"], job["bytes_total"])
        self.assertEqual(sorted(job["completed_names"]), ["a file.txt", "folder"])
        self.assertTrue((self.files / "bundle.tgz").is_file())

        response = self.client.post("/extract", data={"dir": 0, "name": "bundle.tgz"})
        self.assertIn("activeFileJobs", response.get_data(as_text=True))
        self.assertFalse((self.files / "bundle").exists())
        self.assertTrue(self.run_worker())
        self.assertEqual((self.files / "bundle" / "folder" / "inner.txt").read_text(), "inner contents")
        records = operation_log.search("tester", operation="extract", outcome="complete")
        self.assertTrue(records)
        self.assertFalse(list(self.files.glob(".filelist-*")))

    def test_cancelled_archive_job_leaves_no_output_or_staging(self):
        self.sign_in()
        data = self.client.post("/archive", json={
            "dir": 0, "names": ["a file.txt"], "name": "never", "type": "zip",
        }).get_json()
        job_store.initialize()
        job_store.cancel_path(data["job_id"]).touch()
        # A cancel request for a pending job is honoured once it is running.
        self.assertTrue(self.run_worker())
        self.assertEqual(job_store.find(data["job_id"])[0]["status"], "cancelled")
        self.assertFalse((self.files / "never.zip").exists())
        self.assertFalse(list(self.files.glob(".filelist-*")))

    def test_interrupted_archive_staging_is_removed_on_worker_restart(self):
        self.sign_in()
        data = self.client.post("/archive", json={
            "dir": 0, "names": ["a file.txt"], "name": "crashed", "type": "zip",
        }).get_json()
        job, path = job_store.claim_next()
        staging = self.files / ".filelist-archive-abc123.partial"
        staging.mkdir()
        (staging / "archive").write_bytes(b"half written")
        job_store.update(path, temporary_path=str(staging))
        self.assertEqual(job_store.recover_interrupted(), 1)
        self.assertFalse(staging.exists())
        self.assertEqual(job_store.find(data["job_id"])[0]["status"], "failed")

    def test_failed_archive_job_can_be_retried(self):
        self.sign_in()
        data = self.client.post("/archive", json={
            "dir": 0, "names": ["a file.txt"], "name": "retry", "type": "zip",
        }).get_json()
        (self.files / "a file.txt").rename(self.files / "moved away.txt")
        self.assertTrue(self.run_worker())
        failed, _ = job_store.find(data["job_id"])
        self.assertEqual(failed["status"], "failed")
        (self.files / "moved away.txt").rename(self.files / "a file.txt")
        response = self.client.post("/job_retry", data={"job_id": data["job_id"]},
                                    headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 202)
        retry, _ = job_store.find(response.get_json()["job_id"])
        self.assertEqual(retry["destination"]["names"], ["retry.zip"])
        self.assertTrue(self.run_worker())
        self.assertTrue((self.files / "retry.zip").is_file())

    def test_failed_archive_job_reports_every_item_failed_and_names_the_cause(self):
        # The archive is built from all items and published only if all
        # succeed, so a failure on the last item must not be blamed on the
        # first, nor leave the others listed as not started.
        self.sign_in()
        (self.files / "b.txt").write_text("b", encoding="utf-8")
        (self.files / "c.txt").write_text("c", encoding="utf-8")
        data = self.client.post("/archive", json={
            "dir": 0, "names": ["a file.txt", "b.txt", "c.txt"], "name": "abc", "type": "zip",
        }).get_json()
        (self.files / "c.txt").unlink()
        self.assertTrue(self.run_worker())
        job, _ = job_store.find(data["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("c.txt", job["error"])
        summary = job_store.item_summary(job)
        self.assertEqual(summary["failed_names"], ["a file.txt", "b.txt", "c.txt"])
        self.assertEqual(summary["remaining_names"], [])
        self.assertEqual(summary["completed_names"], [])
        self.assertEqual(summary["retry_count"], 3)
        self.assertFalse((self.files / "abc.zip").exists())
        records = operation_log.search("tester", operation="archive", outcome="failed")
        self.assertTrue(records)
        self.assertNotIn(records[0].get("item"), {"a file.txt", "b.txt"})

    def test_archive_route_rejects_unknown_format_before_queueing(self):
        self.sign_in()
        response = self.client.post("/archive", json={
            "dir": 0, "names": ["a file.txt"], "name": "odd", "type": "rar",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(job_store.pending_count(), 0)

    def test_operation_log_search_shows_upload_rename_and_delete_paths(self):
        self.sign_in()
        response = self.client.post("/upload", data={"dir": "0", "files": (io.BytesIO(b"data"), "new.txt")})
        self.assertTrue(response.get_json()["success"])
        self.assertTrue(self.client.post("/rename", json={"dir": 0, "old": "new.txt", "new": "renamed.txt"}).get_json()["success"])
        self.assertEqual(self.client.post("/delete", data={"dir": 0, "name": "renamed.txt"}).status_code, 302)
        page = self.client.get("/operations", query_string={"q": "renamed.txt"}).get_data(as_text=True)
        self.assertIn("rename", page)
        self.assertIn("delete", page)
        self.assertIn("renamed.txt", page)
        records = operation_log.search("tester")
        self.assertEqual([record["operation"] for record in records], ["delete", "rename", "upload"])
        self.assertTrue(records[1]["source"].endswith("new.txt"))
        self.assertTrue(records[1]["destination"].endswith("renamed.txt"))

    def test_operation_log_escapes_details_and_hides_other_users(self):
        self.sign_in()
        operation_log.record("tester", "copy", "failed", item="<script>alert(1)</script>", detail="Disk full")
        operation_log.record("another-user", "copy", "failed", item="private.txt")
        page = self.client.get("/operations").get_data(as_text=True)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertNotIn("private.txt", page)


if __name__ == "__main__":
    unittest.main()
