"""Concurrent uploads publish distinct complete files and clean up failures."""
import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from werkzeug.datastructures import FileStorage

import uploads


class UploadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def save(self, content, name="file.txt", limit=1024):
        file = FileStorage(stream=io.BytesIO(content), filename=name)
        return uploads._save_stream(file, str(self.root), limit)

    def test_simultaneous_same_name_uploads_do_not_overwrite(self):
        (self.root / "file.txt").write_bytes(b"original")
        original = uploads._rename_without_replace
        barrier = threading.Barrier(2)
        seen = set()
        lock = threading.Lock()
        def simultaneous_publish(source, destination):
            with lock:
                first = source not in seen
                seen.add(source)
            if first:
                barrier.wait(timeout=10)
            return original(source, destination)
        with patch.object(uploads, "_rename_without_replace", side_effect=simultaneous_publish):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(self.save, (b"first upload", b"second upload")))
        self.assertEqual(len({result["name"] for result in results}), 2)
        self.assertEqual((self.root / "file.txt").read_bytes(), b"original")
        self.assertEqual({(self.root / result["name"]).read_bytes() for result in results},
                         {b"first upload", b"second upload"})
        self.assertFalse(list(self.root.glob("*.partial")))

    def test_existing_directory_with_same_name_is_a_collision(self):
        (self.root / "file.txt").mkdir()
        result = self.save(b"new")
        self.assertEqual(result["name"], "file (1).txt")
        self.assertTrue((self.root / "file.txt").is_dir())

    def test_oversize_upload_leaves_no_partial_or_final_file(self):
        with self.assertRaisesRegex(ValueError, "too large"):
            self.save(b"oversize", limit=2)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_read_error_cleans_up_without_touching_existing_file(self):
        (self.root / "file.txt").write_bytes(b"original")
        class BrokenStream:
            def __init__(self):
                self.calls = 0
            def read(self, size):
                self.calls += 1
                if self.calls == 1:
                    return b"partial"
                raise OSError("Connection lost")
        file = FileStorage(stream=BrokenStream(), filename="file.txt")
        with self.assertRaisesRegex(OSError, "Connection lost"):
            uploads._save_stream(file, str(self.root), 1024)
        self.assertEqual([path.name for path in self.root.iterdir()], ["file.txt"])
        self.assertEqual((self.root / "file.txt").read_bytes(), b"original")

    def test_disk_or_flush_errors_clean_up(self):
        for owner, setting in ((uploads, "ensure_free_space"), (uploads.os, "fsync")):
            with self.subTest(setting=setting):
                with patch.object(owner, setting, side_effect=OSError("Disk full")):
                    with self.assertRaisesRegex(OSError, "Disk full"):
                        self.save(b"data")
                self.assertEqual(list(self.root.iterdir()), [])

    def test_publish_error_cleans_up(self):
        with patch.object(uploads, "_rename_without_replace", side_effect=OSError("Permission denied")):
            with self.assertRaisesRegex(OSError, "Permission denied"):
                self.save(b"data")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_empty_upload_is_valid(self):
        result = self.save(b"")
        self.assertEqual(result["size"], 0)
        self.assertEqual((self.root / result["name"]).read_bytes(), b"")
