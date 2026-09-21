"""Regression tests for file-listing recovery."""
import os
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

    def test_missing_directory_returns_to_root(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FLASK_SECRET_KEY": "test-key"}
        ):
            root = Path(temporary) / "files"
            root.mkdir()
            app = Flask(__name__)
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
