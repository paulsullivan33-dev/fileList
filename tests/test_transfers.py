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
