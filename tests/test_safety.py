"""Safety and history regression tests."""
import json
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
        job_store.create("remote_copy", "tester", {"names": ["a file.txt"]}, {})
        job_store.create("remote_copy", "someone-else", {"names": ["private.txt"]}, {})
        self.sign_in()
        response = self.client.get("/jobs")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("a file.txt", page)
        self.assertNotIn("private.txt", page)

    def test_failed_job_can_be_retried_only_by_its_owner(self):
        job = job_store.create("copy", "tester", {"directory_index": 0, "path": "", "names": ["a file.txt"]},
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
        job = job_store.create("copy", "tester", {"directory_index": 0, "path": "", "names": ["a file.txt"]},
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
        return job_store.create(operation, "tester",
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
                job = job_store.create(operation, "tester",
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


if __name__ == "__main__":
    unittest.main()
