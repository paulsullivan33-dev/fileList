"""Operation records retain useful paths, survive jobs, and can be searched."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from config_loader import Directory
import job_store
import operation_log


class OperationLogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, JOB_DATA_DIR=str(self.root / "jobs"),
                                 FILELIST_OPERATION_LOG=str(self.root / "operations.sqlite3"))
        environment.start()
        self.addCleanup(environment.stop)

    def test_search_matches_file_destination_error_and_job_id(self):
        operation_log.record("alice", "move", "failed", "/source/file..txt", "/target/file..txt",
                             "file..txt", "job-123", "Disk full")
        for query in ("file..txt", "/target", "Disk full", "job-123"):
            self.assertEqual(len(operation_log.search("alice", query)), 1)
        self.assertEqual(operation_log.search("bob", "file..txt"), [])
        self.assertEqual(operation_log.search("alice", operation="copy"), [])
        self.assertEqual(len(operation_log.search("alice", operation="move", outcome="failed")), 1)
        self.assertEqual(operation_log.search("alice", "' OR 1=1 --"), [])

    def test_search_treats_percent_and_underscore_as_literal_characters(self):
        operation_log.record("alice", "upload", "complete", item="100%_done.txt")
        operation_log.record("alice", "upload", "complete", item="other.txt")
        self.assertEqual(len(operation_log.search("alice", "%_")), 1)

    def test_worker_events_include_file_paths_and_failure_reason(self):
        directories = [Directory(str(self.root / "source"), "/source", "Source", "members"),
                       Directory(str(self.root / "target"), "/target", "Target", "members")]
        job = job_store.create("move", "alice", {"directory_index": 0, "path": "", "names": ["first.txt", "bad.txt"]},
                               {"directory_index": 1, "path": "", "names": ["first.txt", "bad.txt"]}, directories=directories)
        _, path = job_store.claim_next()
        job_store.start_item(path, "first.txt")
        job_store.complete_item(path, "first.txt")
        job_store.complete_item(path, "first.txt")  # No duplicate completion record.
        job_store.start_item(path, "bad.txt")
        job_store.finish(path, "failed", "Cannot read file")
        records = operation_log.search("alice", job["job_id"])
        self.assertEqual([record["outcome"] for record in records], ["failed", "complete", "running", "queued"])
        self.assertEqual(records[0]["item"], "bad.txt")
        self.assertTrue(records[0]["source"].endswith(os.path.join("source", "bad.txt")))
        self.assertTrue(records[0]["destination"].endswith(os.path.join("target", "bad.txt")))
        self.assertEqual(records[0]["detail"], "Cannot read file")

    def test_concurrent_writers_keep_all_records_and_pagination(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda number: operation_log.record("alice", "copy", "complete", item=str(number)), range(20)))
        self.assertTrue(all(results))
        first = operation_log.search("alice", limit=10)
        second = operation_log.search("alice", limit=10, offset=10)
        self.assertEqual(len(first), 10)
        self.assertEqual(len(second), 10)
        self.assertFalse({row["id"] for row in first} & {row["id"] for row in second})

    def test_retention_prunes_old_records_only(self):
        operation_log.record("alice", "copy", "complete", item="old")
        operation_log.record("alice", "copy", "complete", item="new")
        with closing(sqlite3.connect(str(operation_log.database_path()))) as connection:
            with connection:
                connection.execute("UPDATE operations SET timestamp='2000-01-01T00:00:00+00:00' WHERE item='old'")
        operation_log.prune(30)
        self.assertEqual([row["item"] for row in operation_log.search("alice")], ["new"])

    def test_log_failure_does_not_break_job_completion(self):
        job = job_store.create("copy", "alice", {"names": ["file.txt"]}, {})
        _, path = job_store.claim_next()
        with patch.object(operation_log, "_connect", side_effect=sqlite3.OperationalError("Busy")):
            with self.assertLogs("operation_log", level="WARNING"):
                job_store.complete_item(path, "file.txt")
                stored = job_store.finish(path, "complete")
        self.assertEqual(stored["status"], "complete")
        self.assertEqual(stored["completed_names"], ["file.txt"])
