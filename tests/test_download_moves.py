"""Standalone movers share worker staging, capacity checks, and recovery."""
import contextlib
import errno
import io
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import download_moves
import job_store
import job_worker
from move_downloads import move_downloads


class DownloadMoveSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()
        self.item = self.source / "release..txt"
        self.item.write_bytes(b"download contents")
        self.environment = patch.dict(os.environ, JOB_DATA_DIR=str(self.root / "web-jobs"))
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def run_move(self, dry_run=False):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = move_downloads(self.source, {self.item.name: "Books"}, {"Books": self.target}, dry_run)
        return result, output.getvalue()

    def cross_drive(self, source, destination):
        if Path(source) == self.item:
            raise OSError(errno.EXDEV, "Cross-device")
        return self.original_rename(source, destination)

    def journal(self):
        root = self.source / download_moves.STATE_DIRECTORY
        return download_moves.journal_path(root, self.item, self.target / self.item.name)

    def test_same_drive_move_does_not_need_space_for_a_second_copy_or_use_web_queue(self):
        with patch.object(job_worker, "ensure_free_space", side_effect=OSError("Low space")) as space:
            result, _ = self.run_move()
        space.assert_not_called()
        self.assertEqual(result, (1, 0))
        self.assertEqual((self.target / self.item.name).read_bytes(), b"download contents")
        self.assertFalse(self.item.exists())
        self.assertFalse((self.root / "web-jobs").exists())
        self.assertEqual(job_store.read_path(self.journal())["state"], "complete")

    def test_cross_drive_move_uses_worker_space_checks_and_keeps_source_on_failure(self):
        with patch.object(job_worker, "_rename_without_replace", side_effect=OSError(errno.EXDEV, "Cross-device")), \
                patch.object(job_worker, "ensure_free_space", side_effect=OSError("Not enough free disk space")):
            result, message = self.run_move()
        self.assertEqual(result, (0, 1))
        self.assertIn(self.item.name, message)
        self.assertIn("free disk space", message)
        self.assertEqual(self.item.read_bytes(), b"download contents")
        self.assertEqual(list(self.target.iterdir()), [])

    def test_late_destination_collision_never_overwrites(self):
        self.original_rename = job_worker._rename_without_replace
        def collision(source, destination):
            if Path(source) == self.item:
                raise OSError(errno.EXDEV, "Cross-device")
            Path(destination).write_bytes(b"existing owner")
            return self.original_rename(source, destination)
        with patch.object(job_worker, "_rename_without_replace", side_effect=collision):
            result, _ = self.run_move()
        self.assertEqual(result, (0, 1))
        self.assertTrue(self.item.exists())
        self.assertEqual((self.target / self.item.name).read_bytes(), b"existing owner")
        self.assertFalse(list(self.target.glob("*.partial")))

    def test_failed_copy_cleans_staging_and_next_run_retries(self):
        self.original_rename = job_worker._rename_without_replace
        with patch.object(job_worker, "_rename_without_replace", side_effect=self.cross_drive), \
                patch.object(job_worker, "_copy_file", side_effect=OSError("Read failed")):
            self.assertEqual(self.run_move()[0], (0, 1))
        self.assertTrue(self.item.exists())
        self.assertEqual(list(self.target.iterdir()), [])
        with patch.object(job_worker, "_rename_without_replace", side_effect=self.cross_drive):
            self.assertEqual(self.run_move()[0], (1, 0))
        self.assertFalse(self.item.exists())

    def test_interrupted_partial_is_removed_before_retry(self):
        self.original_rename = job_worker._rename_without_replace
        def interrupt(source, destination, progress):
            Path(destination).write_bytes(b"partial bytes")
            raise KeyboardInterrupt()
        with patch.object(job_worker, "_rename_without_replace", side_effect=self.cross_drive), \
                patch.object(job_worker, "_copy_file", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_move()
        self.assertTrue(list(self.target.glob("*.partial")))
        self.assertTrue(self.item.exists())
        with patch.object(job_worker, "_rename_without_replace", side_effect=self.cross_drive):
            self.assertEqual(self.run_move()[0], (1, 0))
        self.assertFalse(list(self.target.glob("*.partial")))
        self.assertEqual((self.target / self.item.name).read_bytes(), b"download contents")

    def test_source_removal_failure_preserves_both_and_requires_review_on_retry(self):
        self.original_rename = job_worker._rename_without_replace
        with patch.object(job_worker, "_rename_without_replace", side_effect=self.cross_drive), \
                patch.object(job_worker, "_remove", side_effect=OSError("Source busy")):
            self.assertEqual(self.run_move()[0], (0, 1))
        self.assertTrue(self.item.exists())
        self.assertEqual((self.target / self.item.name).read_bytes(), b"download contents")
        result, message = self.run_move()
        self.assertEqual(result, (0, 1))
        self.assertIn("Inspect both copies", message)
        self.assertTrue(self.item.exists())

    def test_dry_run_creates_no_recovery_state(self):
        self.assertEqual(self.run_move(dry_run=True)[0], (1, 0))
        self.assertFalse((self.source / download_moves.STATE_DIRECTORY).exists())
        self.assertEqual(list(self.target.iterdir()), [])

    def test_second_mover_for_same_source_is_rejected(self):
        with download_moves.source_lock(self.source):
            with self.assertRaisesRegex(RuntimeError, "Another download mover"):
                with download_moves.source_lock(self.source):
                    self.fail("Second mover acquired source lock")

    def test_moving_folder_inside_itself_is_rejected_without_creating_destination(self):
        folder = self.source / "release..folder"
        folder.mkdir()
        destination = folder / "nested"
        with contextlib.redirect_stdout(io.StringIO()):
            result = move_downloads(self.source, {folder.name: "Books"}, {"Books": destination})
        self.assertEqual(result, (0, 1))
        self.assertFalse(destination.exists())

    @unittest.skipUnless(os.name == "posix", "Symlink move check requires POSIX")
    def test_cross_drive_move_preserves_a_symlink(self):
        self.item.unlink()
        self.item.symlink_to("elsewhere.txt")
        self.original_rename = job_worker._rename_without_replace
        with patch.object(job_worker, "_rename_without_replace", side_effect=self.cross_drive):
            self.assertEqual(self.run_move()[0], (1, 0))
        target = self.target / self.item.name
        self.assertTrue(target.is_symlink())
        self.assertEqual(os.readlink(target), "elsewhere.txt")

    def shared_memory_target(self):
        parent = Path("/dev/shm")
        if (not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK)
                or parent.stat().st_dev == self.source.stat().st_dev or shutil.disk_usage(parent).free < 12 * 1024 * 1024):
            message = "A writable second filesystem with 12 MB free at /dev/shm is required"
            if os.environ.get("FILELIST_REQUIRE_LINUX_INTEGRATION") == "1":
                self.fail(message)
            self.skipTest(message)
        temporary = tempfile.TemporaryDirectory(prefix="filelist-download-test-", dir=str(parent))
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    @unittest.skipUnless(os.name == "posix", "Directory symlink move requires POSIX")
    def test_directory_link_is_moved_as_a_link_not_as_its_referent(self):
        self.item.unlink()
        self.item.symlink_to(self.target, target_is_directory=True)
        self.assertEqual(self.run_move()[0], (1, 0))
        target = self.target / self.item.name
        self.assertTrue(target.is_symlink())
        self.assertEqual(os.readlink(target), str(self.target))
        self.assertFalse(self.item.is_symlink())

    def command_config(self, target):
        history = self.root / "downloaded.txt"
        history.write_text(f"{self.item.name},Books\n", encoding="utf-8")
        config = self.root / "mover.json"
        config.write_text(json.dumps({"source_directory": str(self.source), "downloaded_file": str(history),
                                      "destinations": {"Books": str(target)}}), encoding="utf-8")
        return config

    @unittest.skipUnless(sys.platform.startswith("linux"), "Actual cross-filesystem mover requires Linux")
    def test_real_cross_filesystem_command_moves_without_using_the_web_queue(self):
        target = self.shared_memory_target()
        config = self.command_config(target)
        command = Path(__file__).resolve().parent.parent / "move_downloads.py"
        result = subprocess.run([sys.executable, "-B", str(command), str(config)],
                                env={**os.environ, "FILELIST_MIN_FREE_BYTES": "0"}, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.item.exists())
        self.assertEqual((target / self.item.name).read_bytes(), b"download contents")
        self.assertFalse((self.root / "web-jobs").exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Actual interrupted mover requires Linux")
    def test_killed_command_cleans_partial_and_retries_on_next_invocation(self):
        target = self.shared_memory_target()
        with self.item.open("wb") as output:
            output.truncate(8 * 1024 * 1024)
        expected = hashlib.sha256(self.item.read_bytes()).hexdigest()
        config = self.command_config(target)
        project = Path(__file__).resolve().parent.parent
        program = ("import sys,time; sys.path.insert(0," + repr(str(project)) + "); "
                   "import job_worker,move_downloads; job_worker.CHUNK_SIZE=65536; original=job_worker.ensure_free_space; "
                   "job_worker.ensure_free_space=lambda *a,**k:(time.sleep(0.02),original(*a,**k))[1]; "
                   "raise SystemExit(move_downloads.main([" + repr(str(config)) + "]))")
        environment = {**os.environ, "FILELIST_MIN_FREE_BYTES": "0"}
        process = subprocess.Popen([sys.executable, "-B", "-c", program], env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def stop():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
        self.addCleanup(stop)
        deadline = time.monotonic() + 20
        partial = None
        while time.monotonic() < deadline:
            partial = next((path for path in target.glob("*.partial") if path.stat().st_size), None)
            if partial is not None:
                break
            if process.poll() is not None:
                output, error = process.communicate()
                self.fail("Mover exited before interruption: " + output + error)
            time.sleep(0.01)
        self.assertIsNotNone(partial)
        stop()
        self.assertTrue(self.item.exists())
        self.assertTrue(partial.exists())
        result = subprocess.run([sys.executable, "-B", str(project / "move_downloads.py"), str(config)],
                                env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.item.exists())
        self.assertFalse(list(target.glob("*.partial")))
        self.assertEqual(hashlib.sha256((target / self.item.name).read_bytes()).hexdigest(), expected)
