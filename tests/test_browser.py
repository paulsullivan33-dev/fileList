"""Regression tests for file-listing recovery."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import auth
import browser
from config_loader import Directory


class BrowserRecoveryTests(unittest.TestCase):
    def test_missing_directory_returns_to_root(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FLASK_SECRET_KEY": "test-key"}
        ):
            root = Path(temporary) / "files"
            root.mkdir()
            app = Flask(__name__)
            app.config.update(TESTING=True)
            directories = [Directory(str(root), "/files", "Files", "members")]
            auth.register(app, directories)
            browser.register(app, directories)
            client = app.test_client()
            with client.session_transaction() as session:
                session["username"] = "tester"
                session["groups"] = ["members"]

            response = client.get("/?dir=0&path=missing-folder")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Cannot list directory", response.get_data(as_text=True))
            self.assertIn("window.location='/?dir=0&path='", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
