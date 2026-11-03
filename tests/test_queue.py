"""Queue positions and cancellations use the worker's real FIFO state."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
import actions
import auth
import browser
import job_store
from config_loader import Directory


class QueueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, JOB_DATA_DIR=str(self.root / "jobs"), FLASK_SECRET_KEY="queue-key")
        environment.start()
        self.addCleanup(environment.stop)
        (self.root / "users.json").write_text(json.dumps([
            {"username": "alice", "password": "unused", "groups": "members"}
        ]), encoding="utf-8")
        self.directories = [Directory(str(self.root), "/files", "Files", "members")]
        self.app = Flask(__name__, root_path=str(self.root))
        self.app.config["TESTING"] = True
        self.app.jinja_env.globals["csrf_token"] = lambda: "token"
        auth.register(self.app, self.directories)
        actions.register(self.app, self.directories)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["username"] = "alice"
            session["groups"] = ["members"]

    def job(self, owner="alice", name="file.txt"):
        return job_store.create("copy", owner, {"directory_index": 0, "path": "", "names": [name]},
                               {"directory_index": 0, "path": "target", "names": [name]}, directories=self.directories)

    def test_positions_match_worker_order_even_when_pending_file_mtime_changes(self):
        first = self.job(name="first.txt")
        other = self.job("bob", "private.txt")
        third = self.job(name="third.txt")
        for number, job in enumerate((first, other, third), 1):
            _, path = job_store.find(job["job_id"])
            job_store.update(path, created_at=f"2026-01-01T00:00:0{number}+00:00")
        _, first_path = job_store.find(first["job_id"])
        job_store.update(first_path, note="Metadata update must not move the job to the back")
        snapshot = job_store.queue_snapshot("alice")
        self.assertEqual(snapshot[first["job_id"]]["queue_position"], 1)
        self.assertEqual(snapshot[third["job_id"]]["queue_position"], 3)
        self.assertNotIn(other["job_id"], snapshot)
        claimed, _ = job_store.claim_next()
        self.assertEqual(claimed["job_id"], first["job_id"])
        snapshot = job_store.queue_snapshot("alice")
        self.assertEqual(snapshot[third["job_id"]]["queue_position"], 2)
        self.assertEqual(snapshot[third["job_id"]]["jobs_ahead"], 2)

    def test_pending_cancel_from_history_removes_job_and_updates_positions(self):
        first = self.job(name="first.txt")
        second = self.job(name="second.txt")
        page = self.client.get("/jobs").get_data(as_text=True)
        self.assertIn("Queued #1", page)
        self.assertIn("Cancel pending", page)
        self.assertIn("action='/job_cancel'", page)
        response = self.client.post("/job_cancel", data={"job_id": first["job_id"]})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/jobs"))
        self.assertEqual(job_store.find(first["job_id"])[0]["status"], "cancelled")
        status = self.client.get("/job_status", query_string={"job_id": second["job_id"]}).get_json()
        self.assertEqual(status["queue_position"], 1)
        self.assertEqual(status["jobs_ahead"], 0)
        self.assertEqual(job_store.claim_next()[0]["job_id"], second["job_id"])

    def test_cancel_during_claim_does_not_move_an_active_job_to_cancelled(self):
        job = self.job()
        _, pending = job_store.find(job["job_id"])
        running = job_store.job_root() / "running" / pending.name
        os.replace(pending, running)  # JSON still says pending until the claim finishes.
        result = job_store.request_cancel(job["job_id"], "alice")
        self.assertEqual(result["status"], "running")
        self.assertTrue(running.exists())
        self.assertTrue(job_store.cancellation_requested(job["job_id"]))
        self.assertFalse((job_store.job_root() / "cancelled" / pending.name).exists())
        self.assertEqual(job_store.history("alice")[0]["status"], "running")

    def test_claim_winning_the_atomic_move_still_gets_a_cancellation_marker(self):
        job = self.job()
        _, pending = job_store.find(job["job_id"])
        running = job_store.job_root() / "running" / pending.name
        original = os.replace
        def claimed_first(source, target):
            if Path(source) == pending:
                original(source, running)
                raise FileNotFoundError(source)
            return original(source, target)
        with patch.object(job_store.os, "replace", side_effect=claimed_first):
            result = job_store.request_cancel(job["job_id"], "alice")
        self.assertEqual(result["status"], "running")
        self.assertTrue(running.exists())
        self.assertTrue(job_store.cancellation_requested(job["job_id"]))

    def test_running_cancel_is_visible_and_wrong_owner_cannot_cancel(self):
        job = self.job()
        job_store.claim_next()
        self.assertIsNone(job_store.request_cancel(job["job_id"], "bob"))
        self.assertFalse(job_store.cancellation_requested(job["job_id"]))
        response = self.client.post("/job_cancel", json={"job_id": job["job_id"]})
        self.assertTrue(response.get_json()["cancellation_requested"])
        status = self.client.get("/job_status", query_string={"job_id": job["job_id"]}).get_json()
        self.assertTrue(status["cancellation_requested"])
        self.assertIn("Cancellation requested", self.client.get("/jobs").get_data(as_text=True))

    def test_finishing_while_cancellation_is_requested_does_not_leave_a_stale_marker(self):
        job = self.job()
        _, path = job_store.claim_next()
        original = Path.touch
        def finish_then_touch(marker, *args, **kwargs):
            job_store.finish(path, "complete")
            return original(marker, *args, **kwargs)
        with patch.object(Path, "touch", new=finish_then_touch):
            result = job_store.request_cancel(job["job_id"], "alice")
        self.assertEqual(result["status"], "complete")
        self.assertFalse(job_store.cancellation_requested(job["job_id"]))

    def test_active_jobs_are_not_hidden_by_newer_finished_history(self):
        active = self.job(name="waiting.txt")
        for number in range(3):
            finished = self.job(name=f"done-{number}.txt")
            _, path = job_store.find(finished["job_id"])
            job_store.finish(path, "complete")
        history = job_store.history("alice", limit=2)
        self.assertEqual(history[0]["job_id"], active["job_id"])
        self.assertEqual(len(history), 2)

    @unittest.skipUnless(shutil.which("node"), "Requires Node.js for queue controls")
    def test_browser_cancel_feedback_handles_success_and_network_failure(self):
        script = browser._inline_scripts()
        cancel = script[script.index("async function cancelJob("):script.index("async function pollJob(")]
        harness = r"""
const assert = require('node:assert/strict');
let result, fail = false;
async function csrfFetch() { if(fail) throw new Error('offline'); return {ok:true,json:async()=>result}; }
const messages = [];
function alert(message) { messages.push(message); }
(async () => {
    const button = {disabled:false,textContent:'Cancel'};
    result = {success:true,status:'cancelled',cancellation_requested:false};
    await cancelJob('pending',button);
    assert.equal(button.disabled,true); assert.equal(button.textContent,'Cancelled');
    result = {success:true,status:'running',cancellation_requested:true};
    await cancelJob('running',button);
    assert.equal(button.textContent,'Cancellation requested');
    const retry = {disabled:false,textContent:'Cancel'};
    fail = true; await cancelJob('unknown',retry);
    assert.equal(retry.disabled,false); assert.equal(retry.textContent,'Cancel');
    assert.match(messages[0],/could not be confirmed/);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which("node"), "-e", cancel + harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
