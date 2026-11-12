"""Durable jobs retain folder identities across configuration changes."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config_loader
import job_store
import job_worker
from config_loader import Directory
from utils import authorize_job, bind_job_selection, resolve_job_directory


class JobIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.other = self.root / "other"
        for path in (self.source, self.target, self.other):
            path.mkdir()
        self.directories = [Directory(str(self.source), "/source", "Source", "members", "source"),
                            Directory(str(self.target), "/target", "Target", "members", "target")]
        (self.source / "item.txt").write_text("original", encoding="utf-8")
        (self.other / "item.txt").write_text("unrelated", encoding="utf-8")
        environment = patch.dict(os.environ, JOB_DATA_DIR=str(self.root / "jobs"))
        environment.start()
        self.addCleanup(environment.stop)

    def job(self, operation="copy"):
        return job_store.create(operation, "tester",
            {"directory_index": 0, "path": "", "names": ["item.txt"]},
            {"directory_index": 1, "path": "", "names": ["item.txt"]},
            directories=self.directories)

    def run_worker(self, directories):
        with patch.object(job_worker.config_loader, "load_directories", return_value=directories), \
                patch.object(job_worker, "current_user_groups", return_value=["members"]):
            self.assertTrue(job_worker.run_once())

    def test_reordering_keeps_source_and_destination_for_copy_and_move(self):
        for operation in ("copy", "move"):
            with self.subTest(operation=operation):
                job = self.job(operation)
                self.run_worker(list(reversed(self.directories)))
                stored, _ = job_store.find(job["job_id"])
                self.assertEqual(stored["status"], "complete", stored["error"])
                self.assertEqual((self.target / "item.txt").read_text(), "original")
                self.assertEqual((self.source / "item.txt").exists(), operation == "copy")
                (self.target / "item.txt").unlink()

    def test_repointed_explicit_id_fails_before_any_copy_or_move(self):
        for operation in ("copy", "move", "convert"):
            for replaced_index in (0, 1) if operation != "convert" else (0,):
                job = self.job(operation)
                changed = list(self.directories)
                old = changed[replaced_index]
                changed[replaced_index] = Directory(str(self.other), old.url, old.name, old.group, old.id)
                self.run_worker(changed)
                stored, _ = job_store.find(job["job_id"])
                self.assertEqual(stored["status"], "failed")
                self.assertIn("root changed", stored["error"])
                self.assertTrue((self.source / "item.txt").exists())
                self.assertFalse((self.target / "item.txt").exists())
                self.assertEqual((self.other / "item.txt").read_text(), "unrelated")

    def test_removed_identity_cannot_fall_back_to_old_index(self):
        job = self.job()
        changed = [Directory(str(self.other), "/other", "Other", "members", "other"), self.directories[1]]
        self.run_worker(changed)
        stored, _ = job_store.find(job["job_id"])
        self.assertEqual(stored["status"], "failed")
        self.assertIn("identity changed", stored["error"])

    def test_legacy_jobs_fail_closed(self):
        job = job_store.create("move", "tester",
            {"directory_index": 0, "path": "", "names": ["item.txt"]},
            {"directory_index": 1, "path": "", "names": ["item.txt"]})
        self.run_worker(list(reversed(self.directories)))
        stored, _ = job_store.find(job["job_id"])
        self.assertEqual(stored["status"], "failed")
        self.assertIn("submit the operation again", stored["error"])
        self.assertTrue((self.source / "item.txt").exists())

    def test_retry_rebinds_current_positions_without_changing_identity(self):
        job = self.job()
        _, path = job_store.claim_next()
        failed = job_store.finish(path, "failed", "temporary failure")
        source, destination = job_store.retry_selection(failed)
        reordered = list(reversed(self.directories))
        retry = job_store.create("copy", "tester", source, destination, directories=reordered)
        self.assertEqual(retry["source"]["directory_index"], 1)
        self.assertEqual(retry["source"]["directory_id"], job["source"]["directory_id"])
        self.run_worker(reordered)
        self.assertEqual((self.target / "item.txt").read_text(), "original")

    def test_archive_and_extract_jobs_created_with_directories_run(self):
        # job_store.create used to bind only copy/move/convert destinations,
        # so these jobs were queued unbound and always failed in the worker.
        archive = job_store.create("archive", "tester",
            {"directory_index": 0, "path": "", "names": ["item.txt"]},
            {"directory_index": 0, "path": "", "names": ["bundle.zip"], "archive_type": "zip"},
            directories=self.directories)
        self.assertIn("directory_id", archive["destination"])
        self.run_worker(list(reversed(self.directories)))
        stored, _ = job_store.find(archive["job_id"])
        self.assertEqual(stored["status"], "complete", stored["error"])
        self.assertTrue((self.source / "bundle.zip").is_file())

        extract = job_store.create("extract", "tester",
            {"directory_index": 0, "path": "", "names": ["bundle.zip"]},
            {"directory_index": 0, "path": "", "names": ["bundle"]},
            directories=self.directories)
        self.run_worker(list(reversed(self.directories)))
        stored, _ = job_store.find(extract["job_id"])
        self.assertEqual(stored["status"], "complete", stored["error"])
        self.assertEqual((self.source / "bundle" / "item.txt").read_text(), "original")

    def test_routes_and_job_store_share_one_binding_rule(self):
        import inspect
        import actions
        import utils
        self.assertNotIn("bind_job_selection(", inspect.getsource(actions._enqueue_job))
        selection = {"directory_index": 0, "path": "", "names": ["item.txt"]}
        local_destination = {"directory_index": 1, "path": "", "names": ["item.txt"]}
        remote_destination = {"remote_id": "backup", "path": "", "names": ["item.txt"]}
        for operation in ("copy", "move", "convert", "archive", "extract", "remote_copy", "remote_move"):
            with self.subTest(operation=operation):
                remote = operation.startswith("remote_")
                destination = remote_destination if remote else local_destination
                expected = utils.bind_job_selections(operation, selection, destination, self.directories)
                created = job_store.create(operation, "tester", selection, destination,
                                           directories=self.directories)
                self.assertEqual((created["source"], created["destination"]), expected)
                self.assertEqual("directory_id" in created["destination"], not remote)
                self.assertEqual(operation in utils.LOCAL_DESTINATION_OPERATIONS, not remote)

    def test_permissions_follow_identity_after_reordering(self):
        job = self.job()
        reordered = [self.directories[1], Directory(str(self.source), "/source", "Source", "private", "source")]
        with self.assertRaises(PermissionError):
            authorize_job(job, reordered, ["members"])

    def test_active_destination_survives_reordering(self):
        job = self.job()
        rebound = bind_job_selection(job["destination"], list(reversed(self.directories)))
        self.assertTrue(job_store.has_active_destination(rebound))

    def test_auto_ids_require_no_config_rewrite_and_survive_reordering(self):
        config = self.root / "directories.json"
        entries = [{"root": str(path), "url": "/files", "group": "members"}
                   for path in (self.source, self.target)]
        with patch.object(config_loader, "CONFIG_FILE", config):
            config.write_text(json.dumps(entries), encoding="utf-8")
            before = config_loader.load_directories()
            reference = bind_job_selection({"directory_index": 0}, before)
            config.write_text(json.dumps(list(reversed(entries))), encoding="utf-8")
            after = config_loader.load_directories()
        self.assertEqual(resolve_job_directory(reference, after), 1)

    def test_duplicate_config_ids_are_rejected(self):
        config = self.root / "directories.json"
        entries = [{"id": "duplicate", "root": str(path), "url": "/files", "group": "members"}
                   for path in (self.source, self.target)]
        config.write_text(json.dumps(entries), encoding="utf-8")
        with patch.object(config_loader, "CONFIG_FILE", config):
            with self.assertRaisesRegex(ValueError, "Duplicate directory ID"):
                config_loader.load_directories()
