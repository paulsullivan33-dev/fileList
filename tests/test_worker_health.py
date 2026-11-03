"""Heartbeat freshness and worker responsiveness are separate signals."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from flask import Flask
import actions
import auth
import job_store
import worker_health
from config_loader import Directory


class WorkerHealthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, JOB_DATA_DIR=str(self.root / "jobs"), FLASK_SECRET_KEY="health-key",
                                 FILELIST_WORKER_STALE_SECONDS="15", FILELIST_WORKER_STALL_SECONDS="120")
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, **values):
        record = {"heartbeat": 1000, "last_activity": 1000, "state": "idle", "pid": 123, **values}
        job_store._atomic_write(job_store.job_root() / "worker_health.json", record)

    def test_missing_or_corrupt_heartbeat_is_unknown(self):
        self.assertEqual(worker_health.status()["state"], "unknown")
        path = job_store.job_root() / "worker_health.json"
        path.parent.mkdir(parents=True)
        path.write_text("{bad", encoding="utf-8")
        self.assertEqual(worker_health.status()["state"], "unknown")
        self.write(heartbeat=float("nan"))
        self.assertEqual(worker_health.status(now=1000)["state"], "unknown")

    def test_idle_busy_and_explicit_stop_have_distinct_status(self):
        for stored, expected in (("idle", "idle"), ("busy", "busy"), ("recovering", "starting"), ("stopped", "stopped")):
            self.write(state=stored)
            self.assertEqual(worker_health.status(now=1000)["state"], expected)

    def test_stale_heartbeat_is_not_reported_as_a_running_worker(self):
        self.write(state="busy", heartbeat=984)
        status = worker_health.status(now=1000)
        self.assertEqual(status["state"], "unresponsive")
        self.assertIn("may have stopped", status["message"])

    def test_fresh_heartbeat_does_not_mask_a_stalled_main_loop(self):
        self.write(state="busy", last_activity=879)
        status = worker_health.status(now=1000)
        self.assertEqual(status["state"], "stalled")
        self.assertIn("may", status["message"])
        self.write(state="idle", last_activity=1)
        self.assertEqual(worker_health.status(now=1000)["state"], "idle")

    def test_background_heartbeat_advances_without_main_loop_activity(self):
        with patch.object(worker_health, "HEARTBEAT_INTERVAL", 0.02):
            monitor = worker_health.Monitor(job_store.job_root()).start()
            try:
                with monitor.lock:
                    monitor.data.update(state="busy", last_activity=time.time() - 121)
                initial = worker_health._read_record()["heartbeat"]
                deadline = time.monotonic() + 2
                while worker_health._read_record()["heartbeat"] <= initial:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                self.assertEqual(worker_health.status()["state"], "stalled")
                worker_health.checkpoint()
                monitor.write()
                self.assertEqual(worker_health.status()["state"], "busy")
            finally:
                monitor.stop()
        self.assertEqual(worker_health.status()["state"], "stopped")

    def test_heartbeat_write_failure_does_not_raise_into_file_operations(self):
        monitor = worker_health.Monitor(job_store.job_root())
        with patch.object(job_store, "_atomic_write", side_effect=OSError("Disk unavailable")):
            with self.assertLogs("worker_health", level="WARNING"):
                monitor.write()

    def test_transient_file_sharing_conflict_is_retried_before_reporting_unknown(self):
        record = {"heartbeat": 1000, "last_activity": 1000, "state": "idle"}
        with patch.object(job_store, "read_path", side_effect=[PermissionError("File being replaced"), record]), \
                patch.object(worker_health.time, "sleep"):
            self.assertEqual(worker_health.status(now=1000)["state"], "idle")

    def test_status_endpoint_and_history_explain_why_queue_is_waiting(self):
        (self.root / "users.json").write_text(json.dumps([
            {"username": "tester", "password": "unused", "groups": "members"}
        ]), encoding="utf-8")
        app = Flask(__name__, root_path=str(self.root))
        app.config["TESTING"] = True
        app.jinja_env.globals["csrf_token"] = lambda: "token"
        directories = [Directory(str(self.root), "/files", "Files", "members")]
        auth.register(app, directories)
        actions.register(app, directories)
        client = app.test_client()
        with client.session_transaction() as session:
            session["username"] = "tester"
            session["groups"] = ["members"]
        self.write(state="stopped")
        self.assertEqual(client.get("/worker_status").get_json()["state"], "stopped")
        page = client.get("/jobs").get_data(as_text=True)
        self.assertIn("Worker stopped", page)
        self.assertIn("Queued transfers will wait", page)

    def test_banner_hidden_unless_worker_has_a_problem(self):
        now = time.time()
        self.write(heartbeat=now, last_activity=now, state="idle")
        self.assertIn(" hidden", worker_health.banner())
        self.write(heartbeat=now, last_activity=now, state="busy")
        self.assertIn(" hidden", worker_health.banner())
        self.write(heartbeat=now, last_activity=now, state="stopped")
        self.assertNotIn(" hidden", worker_health.banner())

    @unittest.skipUnless(shutil.which("node"), "Requires Node.js for worker indicator")
    def test_browser_refreshes_health_and_handles_connection_failure(self):
        script = worker_health.banner().split("<script>", 1)[1].rsplit("</script>", 1)[0]
        setup = r"""
const assert = require('node:assert/strict');
const title = {}, message = {};
const box = {dataset:{},querySelector:tag=>tag==='strong'?title:message};
const document = {getElementById:()=>box};
const timers = [];
function setTimeout(callback, delay) { assert.equal(delay,10000); timers.push(callback); }
let fail = false;
let payload = {success:true,state:'stalled',label:'Worker may be stalled',message:'Check its log'};
async function fetch() { if(fail) throw new Error('offline'); return {ok:true,json:async()=>payload}; }
"""
        checks = r"""
(async () => {
    await timers.shift()();
    assert.equal(box.dataset.state,'stalled');
    assert.equal(title.textContent,'Worker may be stalled');
    assert.equal(box.hidden,false);
    payload = {success:true,state:'idle',label:'Worker ready',message:'Waiting'};
    await timers.shift()();
    assert.equal(box.dataset.state,'idle');
    assert.equal(box.hidden,true);
    fail = true; await timers.shift()();
    assert.equal(box.dataset.state,'unknown');
    assert.equal(box.hidden,false);
    assert.equal(title.textContent,'Worker status unavailable');
    assert.equal(timers.length,1);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which("node"), "-e", setup + script + checks], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
