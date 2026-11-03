"""Regression tests for file-listing recovery."""
import os
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import auth
import browser
from config_loader import Directory


class BrowserRecoveryTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Requires Node.js for browser JavaScript")
    def test_upload_errors_remain_visible_and_retry_only_failed_file(self):
        scripts = browser._inline_scripts()
        uploads = scripts[scripts.index("async function uploadFiles("):scripts.index("let previewModal")]
        harness = r"""
const assert = require('node:assert/strict');
class Element {
    constructor() { this.children = []; this.fields = {}; this.disabled = false; }
    set innerHTML(value) {
        for (const key of ['.upload-name', 'progress', '.upload-percent', '.upload-result']) this.fields[key] = {};
    }
    querySelector(key) { return this.fields[key] || this.children.find(child => '.' + child.className === key); }
    appendChild(child) { this.children.push(child); }
}
const queue = new Element();
const elements = {currentDir: {value: '2'}, currentPath: {value: 'incoming'}, uploadQueue: queue};
const document = {createElement: () => new Element(), getElementById: id => elements[id]};
const csrfToken = 'token';
let reloads = 0;
const location = {reload: () => reloads++};
function setTimeout() { throw new Error('Uploads must not schedule automatic refresh'); }
const messages = [];
function flash(text) { messages.push(text); }
class FormData { constructor() { this.fields = {}; } append(key, value) { this.fields[key] = value; } }
const results = [];
const sent = [];
class XMLHttpRequest {
    constructor() { this.listeners = {}; this.upload = {addEventListener() {}}; }
    open(method, url) { assert.equal(method, 'POST'); assert.equal(url, '/upload'); }
    setRequestHeader(name, value) { assert.equal(value, csrfToken); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    send(form) {
        sent.push(form.fields);
        const next = results.shift();
        if (next === 'throw') throw new Error('send failed');
        this.status = next.status || 200;
        this.responseText = next.body;
        queueMicrotask(() => this.listeners[next.event || 'load']());
    }
}
const success = {body: JSON.stringify({success: true})};
(async () => {
    const good = {name: 'good.txt'}, bad = {name: 'bad.txt'};
    results.push(success, {body: JSON.stringify({success: false, errors: [{error: 'Disk full'}]})});
    await uploadFiles([good, bad]);
    const goodRow = queue.children[0], badRow = queue.children[1];
    assert.equal(goodRow.querySelector('.upload-result').textContent, 'Uploaded');
    assert.equal(badRow.querySelector('.upload-result').textContent, 'Disk full');
    const retry = badRow.children[0];
    assert.equal(retry.hidden, false);
    assert.equal(reloads, 0);
    assert.match(messages[0], /1 uploaded, 1 failed/);
    results.push(success);
    await uploadFiles([{name: 'later.txt'}]);
    assert.equal(badRow.querySelector('.upload-result').textContent, 'Disk full');
    assert.equal(reloads, 0);
    results.push(success);
    await retry.onclick();
    assert.equal(sent.length, 4);
    assert.equal(sent[3].files, bad);
    assert.equal(sent[3].dir, '2');
    assert.equal(sent[3].path, 'incoming');
    assert.equal(retry.hidden, true);
    assert.equal(retry.disabled, false);
    assert.equal(badRow.querySelector('.upload-result').textContent, 'Uploaded');
    for (const failure of [{event: 'error'}, {event: 'abort'}, {event: 'timeout'}, {body: 'invalid JSON'}, 'throw']) {
        const row = new Element(); row.innerHTML = '';
        results.push(failure);
        assert.equal(await uploadOne(bad, row, '2', 'incoming'), false);
        assert.equal(row.querySelector('.upload-result').className, 'upload-result error');
    }
    assert.equal(reloads, 0);
    queue.querySelector('.upload-refresh').onclick();
    assert.equal(reloads, 1);
    results.push({body: JSON.stringify({success: true, saved: [{name: 'file (1).txt'}]})});
    await uploadFiles([{name: 'file.txt'}]);
    assert.equal(queue.children.at(-1).querySelector('.upload-result').textContent, 'Uploaded as file (1).txt');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run([shutil.which("node"), "-e", uploads + harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Requires Node.js for browser JavaScript")
    def test_retry_tracks_new_job_and_recovers_from_errors(self):
        scripts = browser._inline_scripts()
        retry = scripts[scripts.index("async function retryJob("):scripts.index("async function cancelJob(")]
        harness = r"""
const assert = require('node:assert/strict');
const calls = [];
let response;
let networkError = false;
const button = {disabled: false};
class FormData { append() {} }
async function csrfFetch(url, options) {
    assert.equal(button.disabled, true);
    assert.equal(url, '/job_retry');
    assert.equal(options.headers.Accept, 'application/json');
    if (networkError) throw new Error('offline');
    return response;
}
function rememberJob(job) { calls.push(['remember', job.job_id]); }
function forgetJob(id) { calls.push(['forget', id]); }
function showJob(job) { calls.push(['show', job.job_id]); }
function pollJob(id) { calls.push(['poll', id]); }
function alert(message) { calls.push(['alert', message]); }
(async () => {
    response = {ok: true, json: async () => ({success: true, job_id: 'new', status: 'pending'})};
    await retryJob('old', button);
    assert.deepEqual(calls, [['remember', 'new'], ['forget', 'old'], ['show', 'new'], ['poll', 'new']]);
    assert.equal(button.disabled, false);
    calls.length = 0;
    response = {ok: false, json: async () => ({success: false, error: 'Permission denied'})};
    await retryJob('old', button);
    assert.deepEqual(calls, [['alert', 'Permission denied']]);
    assert.equal(button.disabled, false);
    calls.length = 0;
    networkError = true;
    await retryJob('old', button);
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], 'alert');
    assert.equal(button.disabled, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run([shutil.which("node"), "-e", retry + harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Requires Node.js for browser JavaScript")
    def test_transfer_results_dismissable_success_auto_dismisses(self):
        scripts = browser._inline_scripts()
        storage = scripts[scripts.index("function activeJobs("):scripts.index("function showJob(")]
        display = scripts[scripts.index("function showJob("):scripts.index("function formatBytes(")]
        polling = scripts[scripts.index("async function pollJob("):scripts.index("function resumeJobs(")]
        harness = r"""
const assert = require('node:assert/strict');
class Element {
    constructor(tag) { this.tag = tag; this.children = []; this._text = ''; }
    remove() { const i = panel.children.indexOf(this); if (i >= 0) panel.children.splice(i, 1); }
    set textContent(value) { this._text = value; this.children = []; }
    get textContent() { return this._text; }
    set innerHTML(value) { throw new Error('Filenames must be text, never HTML'); }
    appendChild(child) { this.children.push(child); }
}
const panel = new Element('div');
const document = {
    getElementById(id) { return id === 'jobsPanel' ? panel : panel.children.find(row => row.id === id); },
    createElement: tag => new Element(tag),
    createTextNode: text => { const node = new Element('text'); node.textContent = text; return node; }
};
const JOB_KEY = 'jobs';
let stored = '[]';
const localStorage = {getItem: () => stored, setItem: (key, value) => stored = value};
let refreshed = 0;
const location = {reload: () => refreshed++};
const timers = [];
function setTimeout(fn, delay) { timers.push([fn, delay]); }
function flash() {}
function formatBytes(value) { return value + ' B'; }
const retries = [];
function retryJob(id) { retries.push(id); }
const badName = '<img src=x onerror=alert(1)>.txt';
const failed = {success: true, job_id: 'failed', operation: 'copy', status: 'failed',
    completed_names: ['completed.txt'], failed_names: [badName], remaining_names: ['unstarted.txt'],
    retry_count: 2, can_retry: true, error: 'Permission denied'};
const complete = {success: true, job_id: 'complete', operation: 'move', status: 'complete',
    completed_names: ['done.txt'], failed_names: [], remaining_names: [], retry_count: 0, can_retry: false};
let response;
async function fetch() { return {ok: true, json: async () => response}; }
(async () => {
    response = failed;
    await pollJob('failed');
    const row = document.getElementById('job_failed');
    assert.ok(row);
    const groups = row.children.filter(child => child.tag === 'details');
    assert.deepEqual(groups.map(group => group.children[0].textContent), ['Completed (1)', 'Failed (1)', 'Remaining (1)']);
    assert.equal(groups[1].children[1].children[0].textContent, badName);
    const retry = row.children.find(child => child.textContent === 'Retry remaining (2)');
    assert.ok(retry);
    retry.onclick();
    assert.deepEqual(retries, ['failed']);
    assert.equal(refreshed, 0);
    // A failure never auto-dismisses: no timer is scheduled for it, but a
    // dismiss button is offered.
    assert.equal(timers.length, 0);
    assert.ok(row.children.some(child => child.textContent === '×'));
    // Only owner-checked fetches provide names; storage keeps no file details.
    assert.equal(stored.includes(badName), false);
    assert.deepEqual(JSON.parse(stored), [{job_id:'failed', operation:'copy', status:'failed'}]);
    response = complete;
    await pollJob('complete');
    const done = document.getElementById('job_complete');
    assert.ok(done);
    // A one-item job shows no Completed/Failed/Remaining sections at all.
    assert.equal(done.children.some(child => child.tag === 'details'), false);
    // Neither does a one-item job still in progress.
    showJob({job_id: 'solo', operation: 'convert', status: 'running',
        completed_names: [], failed_names: [], remaining_names: ['movie.mkv'],
        current_name: 'movie.mkv'});
    assert.equal(document.getElementById('job_solo').children.some(child => child.tag === 'details'), false);
    assert.equal(done.children.some(child => child.textContent.startsWith('Retry remaining')), false);
    assert.equal(refreshed, 0);
    done.children.find(child => child.textContent === 'Refresh file list').onclick();
    assert.equal(refreshed, 1);
    // A success schedules its own dismissal after 5 seconds; running the
    // timer removes the row and forgets the job.
    assert.equal(timers.length, 1);
    assert.equal(timers[0][1], 5000);
    assert.ok(done.children.some(child => child.textContent === '×'));
    timers[0][0]();
    assert.equal(document.getElementById('job_complete'), undefined);
    assert.equal(JSON.parse(stored).some(job => job.job_id === 'complete'), false);
    // All-completed cancellation cannot offer an ineffective retry.
    showJob({...complete, job_id: 'cancelled', status: 'cancelled'});
    assert.equal(document.getElementById('job_cancelled').children.some(child => child.textContent.startsWith('Retry remaining')), false);
    // The dismiss button removes a terminal row immediately.
    document.getElementById('job_cancelled').children.find(child => child.textContent === '×').onclick();
    assert.equal(document.getElementById('job_cancelled'), undefined);
    saveJobs(Array.from({length: 20}, (_, i) => ({job_id: String(i), status: 'complete'})));
    assert.equal(JSON.parse(stored).length, 10);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run([shutil.which("node"), "-e", storage + display + polling + harness],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_directory_returns_to_root(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FLASK_SECRET_KEY": "test-key"}
        ):
            root = Path(temporary) / "files"
            root.mkdir()
            (Path(temporary) / 'users.json').write_text(json.dumps([
                dict(username='tester', password='unused', groups='members')
            ]), encoding='utf-8')
            app = Flask(__name__, root_path=temporary)
            app.config.update(TESTING=True)
            directories = [Directory(str(root), "/files", "Files", "members")]
            auth.register(app, directories)
            browser.register(app, directories)
            client = app.test_client()
            with client.session_transaction() as session:
                session["username"] = "tester"
                session["groups"] = ["members"]

            response = client.get("/?dir=0&path=missing-folder")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Cannot list directory", response.get_data(as_text=True))
            self.assertIn("window.location='/?dir=0&path='", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
