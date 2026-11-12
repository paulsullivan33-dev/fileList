"""Local transfer capacity checks and accurate item recovery summaries."""
import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config_loader import Directory
import job_store
import job_worker


class TransferTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()
        self.directories = [Directory(str(self.source), "/source", "Source", "members"),
                            Directory(str(self.target), "/target", "Target", "members")]
        self.environment = patch.dict(os.environ, JOB_DATA_DIR=str(self.root / "jobs"))
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def job(self, operation="copy", names=None):
        names = names or ["file.txt"]
        for name in names:
            (self.source / name).write_bytes(b"contents")
        return job_store.create(operation, "tester",
            {"directory_index": 0, "path": "", "names": names},
            {"directory_index": 1, "path": "", "names": names}, directories=self.directories)

    def run_worker(self, job):
        with patch.object(job_worker.config_loader, "load_directories", return_value=self.directories), \
                patch.object(job_worker, "current_user_groups", return_value=["members"]):
            self.assertTrue(job_worker.run_once())
        return job_store.find(job["job_id"])[0]

    def test_copy_checks_space_for_whole_batch_before_writing(self):
        job = self.job(names=["first.txt", "second.txt"])
        with patch.object(job_worker, "ensure_free_space", side_effect=OSError("Not enough free disk space")) as space, \
                patch.object(job_worker, "_copy_file") as copy:
            stored = self.run_worker(job)
        space.assert_called_once_with(str(self.target), 2 * len(b"contents"))
        copy.assert_not_called()
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertEqual(job_store.item_summary(stored)["remaining_names"], ["first.txt", "second.txt"])

    def test_disk_space_lost_during_copy_cleans_staging(self):
        job = self.job()
        with patch.object(job_worker, "ensure_free_space", side_effect=[None, None, OSError("Disk filled")]):
            stored = self.run_worker(job)
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["failed_names"], ["file.txt"])
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertTrue((self.source / "file.txt").exists())

    def test_cross_drive_move_checks_space_before_staging_and_keeps_source(self):
        job = self.job("move")
        with patch.object(job_worker, "_rename_without_replace", side_effect=OSError(errno.EXDEV, "Cross-device")), \
                patch.object(job_worker, "ensure_free_space", side_effect=OSError("Not enough space")) as space, \
                patch.object(job_worker, "_copy_file") as copy:
            stored = self.run_worker(job)
        space.assert_called_once_with(str(self.target), len(b"contents"))
        copy.assert_not_called()
        self.assertEqual(stored["status"], "failed")
        self.assertTrue((self.source / "file.txt").exists())
        self.assertEqual(list(self.target.iterdir()), [])

    def test_same_drive_move_does_not_require_room_for_a_copy(self):
        job = self.job("move")
        with patch.object(job_worker, "ensure_free_space", side_effect=OSError("Low space")) as space:
            stored = self.run_worker(job)
        space.assert_not_called()
        self.assertEqual(stored["status"], "complete")
        self.assertFalse((self.source / "file.txt").exists())
        self.assertEqual((self.target / "file.txt").read_bytes(), b"contents")

    def test_cross_drive_move_succeeds_when_space_is_available(self):
        job = self.job("move")
        original = job_worker._rename_without_replace
        def cross_drive(source, destination):
            if source == str(self.source / "file.txt"):
                raise OSError(errno.EXDEV, "Cross-device")
            return original(source, destination)
        with patch.object(job_worker, "_rename_without_replace", side_effect=cross_drive), \
                patch.object(job_worker, "ensure_free_space") as space:
            stored = self.run_worker(job)
        self.assertGreaterEqual(space.call_count, 2)
        self.assertEqual(stored["status"], "complete")
        self.assertFalse((self.source / "file.txt").exists())
        self.assertEqual((self.target / "file.txt").read_bytes(), b"contents")

    def test_cancelled_current_item_is_remaining_not_failed(self):
        job = self.job(names=["first.txt", "second.txt"])
        _, path = job_store.claim_next()
        job_store.complete_item(path, "first.txt")
        job_store.start_item(path, "second.txt")
        cancelled = job_store.finish(path, "cancelled", "Cancelled by user")
        summary = job_store.item_summary(cancelled)
        self.assertEqual(summary["completed_names"], ["first.txt"])
        self.assertEqual(summary["failed_names"], [])
        self.assertEqual(summary["remaining_names"], ["second.txt"])
        self.assertEqual(summary["retry_count"], 1)
        self.assertTrue(summary["can_retry"])

    def test_fully_completed_late_cancellation_has_no_retry_button(self):
        job = self.job()
        _, path = job_store.claim_next()
        job_store.complete_item(path, "file.txt")
        cancelled = job_store.finish(path, "cancelled", "Late cancellation")
        self.assertFalse(job_store.item_summary(cancelled)["can_retry"])
        with self.assertRaisesRegex(ValueError, "nothing to retry"):
            job_store.retry_selection(cancelled)

    def test_completed_conversion_cannot_be_retried_after_late_cancellation(self):
        job = self.job("convert")
        _, path = job_store.claim_next()
        job_store.complete_item(path, "file.txt")
        cancelled = job_store.finish(path, "cancelled", "Late cancellation")
        with self.assertRaisesRegex(ValueError, "nothing to retry"):
            job_store.retry_selection(cancelled)

    def test_running_summary_distinguishes_current_and_unstarted(self):
        job = self.job(names=["first.txt", "second.txt"])
        _, path = job_store.claim_next()
        job_store.start_item(path, "first.txt")
        summary = job_store.item_summary(job_store.read_path(path))
        self.assertEqual(summary["current_name"], "first.txt")
        self.assertEqual(summary["remaining_names"], ["second.txt"])
        self.assertFalse(summary["can_retry"])


class TransferCostTests(unittest.TestCase):
    """Each item is walked once, and free space is not re-checked per chunk."""
    setUp = TransferTests.setUp
    run_worker = TransferTests.run_worker

    def folder_job(self, operation, files=40):
        folder = self.source / "album"
        folder.mkdir()
        for number in range(files):
            (folder / f"{number:03}.txt").write_bytes(b"x" * 10)
        return job_store.create(operation, "tester",
            {"directory_index": 0, "path": "", "names": ["album"]},
            {"directory_index": 1, "path": "", "names": ["album"]}, directories=self.directories)

    def test_same_filesystem_move_does_not_walk_the_moved_folder_again(self):
        job = self.folder_job("move")
        with patch.object(job_worker, "_measure", wraps=job_worker._measure) as measure:
            stored = self.run_worker(job)
        self.assertEqual(stored["status"], "complete", stored["error"])
        self.assertEqual([call.args[0] for call in measure.call_args_list], [str(self.source / "album")])
        self.assertEqual(stored["bytes_completed"], 400)
        self.assertEqual(len(list((self.target / "album").iterdir())), 40)

    def test_copy_walks_each_item_once_and_checks_space_once_per_tree(self):
        job = self.folder_job("copy")
        with patch.object(job_worker, "_measure", wraps=job_worker._measure) as measure, \
                patch.object(job_worker, "ensure_free_space") as space:
            stored = self.run_worker(job)
        self.assertEqual(stored["status"], "complete", stored["error"])
        measure.assert_called_once_with(str(self.source / "album"))
        # Batch check, item check, then one throttled check for the 40 small
        # files (previously one per file: 42 in all).
        self.assertEqual(space.call_count, 3)
        self.assertEqual(len(list((self.target / "album").iterdir())), 40)

    def test_space_lost_part_way_through_a_large_copy_fails_cleanly(self):
        (self.source / "large.bin").write_bytes(b"x" * 64)
        job = job_store.create("copy", "tester",
            {"directory_index": 0, "path": "", "names": ["large.bin"]},
            {"directory_index": 1, "path": "", "names": ["large.bin"]}, directories=self.directories)
        calls = []

        def space(directory, required):
            calls.append(required)
            if len(calls) == 4:  # batch, item, first chunk, then a later re-check
                raise OSError("Not enough free disk space")

        with patch.object(job_worker, "CHUNK_SIZE", 8), \
                patch.object(job_worker.FreeSpaceGuard, "INTERVAL_BYTES", 16), \
                patch.object(job_worker, "ensure_free_space", side_effect=space):
            stored = self.run_worker(job)
        self.assertEqual(stored["status"], "failed")
        self.assertIn("Not enough free disk space", stored["error"])
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertTrue((self.source / "large.bin").exists())


class FreeSpaceGuardTests(unittest.TestCase):
    def test_checks_first_write_then_by_bytes_or_time(self):
        from utils import FreeSpaceGuard
        checks = []
        guard = FreeSpaceGuard("/target", check=lambda directory, size: checks.append(size))
        clock = [100.0]
        with patch("utils.time.monotonic", side_effect=lambda: clock[0]):
            guard.before_write(10)
            self.assertEqual(checks, [10])
            for _ in range(100):
                guard.before_write(1024)
            self.assertEqual(checks, [10])
            clock[0] += FreeSpaceGuard.INTERVAL_SECONDS
            guard.before_write(5)
            self.assertEqual(checks, [10, 5])
            guard.before_write(FreeSpaceGuard.INTERVAL_BYTES)
            guard.before_write(7)
            self.assertEqual(checks, [10, 5, 7])
