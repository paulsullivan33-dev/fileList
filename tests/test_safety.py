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


if __name__ == "__main__":
    unittest.main()
