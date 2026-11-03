"""Real Linux workers, filesystems, cancellation, and crash recovery."""
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

from config_loader import Directory
import job_store
import worker_health


ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(sys.platform.startswith("linux"), "Real worker integration requires Linux")
class LinuxIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="filelist-integration-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "application"
        self.project.mkdir()
        (self.project / "tools").mkdir()
        # Run the actual worker code with isolated users/settings, never the
        # installed application's configuration or its real job queue.
        for name in ("job_worker.py", "job_store.py", "config_loader.py", "utils.py",
                     "remote_copy.py", "operation_log.py", "worker_health.py", "tools/check_config.py"):
            shutil.copy2(ROOT / name, self.project / name)
        (self.project / "users.json").write_text(json.dumps([
            {"username": "integration", "password": "unused", "groups": "testers"}
        ]), encoding="utf-8")
        (self.project / "remotes.json").write_text("[]", encoding="utf-8")
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()
        self.config = self.project / "directories.json"
        environment = patch.dict(os.environ, {
            "JOB_DATA_DIR": str(self.root / "jobs"), "FILELIST_CONFIG": str(self.config),
            "FILELIST_REMOTE_CONFIG": str(self.project / "remotes.json"),
            "FILELIST_OPERATION_LOG": str(self.root / "operations.sqlite3"),
            "FILELIST_MIN_FREE_BYTES": "0", "JOB_PROGRESS_INTERVAL": "0.1",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.processes = []
        self.addCleanup(self.stop_workers)

    def stop_workers(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def directories(self, target):
        entries = [dict(id="source", root=str(self.source), url="/source", group="testers"),
                   dict(id="target", root=str(target), url="/target", group="testers")]
        self.config.write_text(json.dumps(entries), encoding="utf-8")
        return [Directory(entry["root"], entry["url"], entry["id"], entry["group"], entry["id"])
                for entry in entries]

    def job(self, operation="copy", name="large..bin", target=None):
        target = target or self.target
        return job_store.create(operation, "integration",
            {"directory_index": 0, "path": "", "names": [name]},
            {"directory_index": 1, "path": "", "names": [name]}, directories=self.directories(target))

    def start_worker(self, slow=False):
        if slow:
            # Timing-only instrumentation makes interruption deterministic; I/O,
            # authorization, locking, and recovery still use production code.
            program = ("import time,job_worker; job_worker.CHUNK_SIZE=65536; "
                       "original=job_worker.ensure_free_space; "
                       "job_worker.ensure_free_space=lambda *a,**k:(time.sleep(0.002),original(*a,**k))[1]; "
                       "raise SystemExit(job_worker.main(['--once']))")
            arguments = [sys.executable, "-B", "-c", program]
        else:
            arguments = [sys.executable, "-B", str(self.project / "job_worker.py"), "--once"]
        process = subprocess.Popen(arguments, cwd=str(self.project), env=dict(os.environ),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        return process

    def wait_worker(self, process, code=0):
        output, error = process.communicate(timeout=30)
        self.assertEqual(process.returncode, code, output + error)
        return output + error

    def record(self, job):
        return job_store.find(job["job_id"])[0]

    def make_large_file(self):
        source = self.source / "large..bin"
        with source.open("wb") as stream:
            stream.truncate(64 * 1024 * 1024)
        return source

    def await_staging(self, process, job):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            record = self.record(job) or {}
            temporary = record.get("temporary_path")
            if temporary:
                path = Path(temporary)
                try:
                    if path.is_file() and path.stat().st_size:
                        return path
                except FileNotFoundError:
                    pass
            if process.poll() is not None:
                output, error = process.communicate()
                self.fail("Worker exited before interruption: " + output + error)
            time.sleep(0.01)
        self.fail("Worker did not create a staging file within 20 seconds")

    @staticmethod
    def digest(path):
        result = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                result.update(block)
        return result.hexdigest()

    def different_filesystem(self):
        path = Path("/dev/shm")
        reason = "A writable /dev/shm on a second filesystem with 4 MB free is required"
        if (not path.is_dir() or not os.access(path, os.W_OK | os.X_OK)
                or path.stat().st_dev == self.source.stat().st_dev or shutil.disk_usage(path).free < 4 * 1024 * 1024):
            if os.environ.get("FILELIST_REQUIRE_LINUX_INTEGRATION") == "1":
                self.fail(reason)
            self.skipTest(reason)
        temporary = tempfile.TemporaryDirectory(prefix="filelist-integration-", dir=str(path))
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def test_real_cross_filesystem_move(self):
        target = self.different_filesystem()
        source = self.source / "cross..bin"
        source.write_bytes(b"cross-filesystem content\n" * 50000)
        expected = self.digest(source)
        job = self.job("move", source.name, target)
        self.wait_worker(self.start_worker())
        self.assertEqual(self.record(job)["status"], "complete")
        self.assertFalse(source.exists())
        self.assertEqual(self.digest(target / source.name), expected)
        self.assertFalse(list(target.glob("*.partial")))

    def test_real_cross_filesystem_low_space_refusal(self):
        target = self.different_filesystem()
        source = self.source / "cross..bin"
        source.write_bytes(b"contents")
        job = self.job("move", source.name, target)
        with patch.dict(os.environ, FILELIST_MIN_FREE_BYTES=str(1 << 60)):
            self.wait_worker(self.start_worker())
        self.assertEqual(self.record(job)["status"], "failed")
        self.assertIn("free disk space", self.record(job)["error"])
        self.assertEqual(source.read_bytes(), b"contents")
        self.assertEqual(list(target.iterdir()), [])

    def test_real_cancellation_cleans_partial_and_preserves_source(self):
        source = self.make_large_file()
        job = self.job()
        worker = self.start_worker(slow=True)
        temporary = self.await_staging(worker, job)
        job_store.request_cancel(job["job_id"], "integration")
        self.wait_worker(worker)
        self.assertEqual(self.record(job)["status"], "cancelled")
        self.assertTrue(source.exists())
        self.assertFalse(temporary.exists())
        self.assertEqual(list(self.target.iterdir()), [])

    def test_real_worker_crash_restart_and_retry(self):
        source = self.make_large_file()
        expected = self.digest(source)
        job = self.job()
        worker = self.start_worker(slow=True)
        temporary = self.await_staging(worker, job)
        worker.kill()
        worker.communicate(timeout=10)
        self.assertTrue(temporary.exists())
        heartbeat = job_store.read_path(job_store.job_root() / "worker_health.json")
        self.assertEqual(worker_health.status(now=heartbeat["heartbeat"] + 16)["state"], "unresponsive")
        self.wait_worker(self.start_worker())
        failed = self.record(job)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("Worker stopped", failed["error"])
        self.assertFalse(temporary.exists())
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertEqual(worker_health.status()["state"], "stopped")
        selection, destination = job_store.retry_selection(failed)
        retry = job_store.create("copy", "integration", selection, destination, directories=self.directories(self.target))
        self.wait_worker(self.start_worker())
        self.assertEqual(self.record(retry)["status"], "complete")
        self.assertEqual(self.digest(self.target / source.name), expected)
        self.assertEqual(self.digest(source), expected)

    def test_second_worker_cannot_recover_an_active_workers_job(self):
        self.make_large_file()
        job = self.job()
        first = self.start_worker(slow=True)
        temporary = self.await_staging(first, job)
        error = self.wait_worker(self.start_worker(), code=1)
        self.assertIn("Another fileList job worker", error)
        heartbeat = job_store.read_path(job_store.job_root() / "worker_health.json")
        self.assertEqual(heartbeat["pid"], first.pid)
        self.assertEqual(self.record(job)["status"], "running")
        self.assertTrue(temporary.exists())
        job_store.request_cancel(job["job_id"], "integration")
        self.wait_worker(first)
        self.assertEqual(self.record(job)["status"], "cancelled")
