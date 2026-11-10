"""Create-text-file tests: the helper's no-overwrite contract and the
route's validation (name, path jail, extension allowlist, size cap)."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import uploads


class CreateTextHelperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_creates_file_with_exact_content(self):
        data = "first line\nsecond line, no trailing newline"
        result = uploads._create_text_file(str(self.root), "notes.txt", data.encode("utf-8"))
        self.assertEqual(result, {"name": "notes.txt", "size": len(data.encode("utf-8"))})
        self.assertEqual((self.root / "notes.txt").read_bytes(), data.encode("utf-8"))
        self.assertFalse(list(self.root.glob("*.partial")))

    def test_created_file_is_world_readable(self):
        # Regression (Paul, 2026-11-09): the mkstemp 0600 mode survived
        # publication, so a file created here could not be read later
        # from another machine where Paul browses as a different user.
        uploads._create_text_file(str(self.root), "notes.txt", b"hi")
        mode = stat.S_IMODE(os.stat(self.root / "notes.txt").st_mode)
        self.assertEqual(mode, 0o644)

    def test_existing_file_is_a_collision_and_untouched(self):
        (self.root / "notes.txt").write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            uploads._create_text_file(str(self.root), "notes.txt", b"new")
        self.assertEqual((self.root / "notes.txt").read_bytes(), b"original")
        self.assertFalse(list(self.root.glob("*.partial")))

    def test_existing_directory_is_a_collision(self):
        (self.root / "notes.txt").mkdir()
        with self.assertRaises(FileExistsError):
            uploads._create_text_file(str(self.root), "notes.txt", b"new")
        self.assertTrue((self.root / "notes.txt").is_dir())

    def test_empty_content_creates_empty_file(self):
        result = uploads._create_text_file(str(self.root), "empty.md", b"")
        self.assertEqual(result["size"], 0)
        self.assertEqual((self.root / "empty.md").read_bytes(), b"")


class CreateTextRouteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.files = self.root / "files"
        self.files.mkdir()
        self.environment = patch.dict(os.environ, {
            "FILELIST_OPERATION_LOG": str(self.root / "operations.sqlite3"),
            "JOB_DATA_DIR": str(self.root / "jobs"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        app = Flask(__name__)
        app.secret_key = "test-key"
        uploads.register(app, [(str(self.files), "")])
        self.client = app.test_client()

    def create(self, name="notes.txt", content="hello", path="", dir_index="0"):
        return self.client.post("/create_text", data={
            "dir": dir_index, "path": path, "name": name, "content": content,
        }).get_json()

    def test_route_creates_in_current_directory(self):
        response = self.create()
        self.assertTrue(response["success"])
        self.assertEqual(response["name"], "notes.txt")
        self.assertEqual(response["size"], 5)
        self.assertEqual((self.files / "notes.txt").read_text(encoding="utf-8"), "hello")

    def test_route_creates_in_subdirectory(self):
        (self.files / "sub").mkdir()
        response = self.create(name="todo.md", content="- item", path="sub")
        self.assertTrue(response["success"])
        self.assertEqual((self.files / "sub" / "todo.md").read_text(encoding="utf-8"), "- item")

    def test_route_refuses_existing_name(self):
        self.assertTrue(self.create()["success"])
        response = self.create(content="overwrite attempt")
        self.assertFalse(response["success"])
        self.assertIn("already exists", response["error"])
        self.assertEqual((self.files / "notes.txt").read_text(encoding="utf-8"), "hello")

    def test_route_rejects_traversal_names(self):
        for name in ("../evil.txt", "a/b.txt", "..\\evil.txt"):
            response = self.create(name=name)
            self.assertFalse(response["success"], name)
        self.assertEqual(list(self.files.iterdir()), [])
        self.assertFalse((self.root / "evil.txt").exists())

    def test_route_rejects_escaping_path(self):
        response = self.create(path="../")
        self.assertFalse(response["success"])
        self.assertEqual(list(self.files.iterdir()), [])

    def test_route_rejects_disallowed_extension(self):
        response = self.create(name="evil.exe")
        self.assertFalse(response["success"])
        self.assertEqual(list(self.files.iterdir()), [])

    def test_route_rejects_reserved_name(self):
        response = self.create(name=".htaccess")
        self.assertFalse(response["success"])
        self.assertEqual(list(self.files.iterdir()), [])

    def test_route_rejects_oversize_text(self):
        with patch.object(uploads, "MAX_TEXT_SIZE", 4):
            response = self.create(content="12345")
        self.assertFalse(response["success"])
        self.assertEqual(list(self.files.iterdir()), [])

    def test_route_rejects_invalid_dir_index(self):
        response = self.create(dir_index="9")
        self.assertFalse(response["success"])


if __name__ == "__main__":
    unittest.main()
