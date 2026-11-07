"""Destination previews describe the exact transfer without changing files."""
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
import job_store
import job_worker
import remote_copy
import transfer_preview
from config_loader import Directory


class TransferPreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()
        (self.source / "report..txt").write_bytes(b"12345")
        folder = self.source / "folder..name"
        folder.mkdir()
        (folder / "data.txt").write_bytes(b"123")
        (folder / "empty").mkdir()
        remotes = self.root / "remotes.json"
        remotes.write_text("[]", encoding="utf-8")
        environment = patch.dict(os.environ, FLASK_SECRET_KEY="preview-test-key",
                                 JOB_DATA_DIR=str(self.root / "jobs"), FILELIST_REMOTE_CONFIG=str(remotes))
        environment.start()
        self.addCleanup(environment.stop)
        self.directories = [Directory(str(self.source), "/source", "Source", "members", "source"),
                            Directory(str(self.target), "/target", "Target", "members", "target")]
        (self.root / "users.json").write_text(json.dumps([
            {"username": "tester", "password": "unused", "groups": "members"}
        ]), encoding="utf-8")
        self.app = Flask(__name__, root_path=str(self.root))
        self.app.config["TESTING"] = True
        self.app.jinja_env.globals["csrf_token"] = lambda: "test"
        auth.register(self.app, self.directories)
        actions.register(self.app, self.directories)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["username"] = "tester"
            session["groups"] = ["members"]

    def preview(self, **changes):
        payload = {"operation": "copy", "src_dir": 0, "src_path": "", "names": ["report..txt"],
                   "dest_dir": 1, "dest_path": ""}
        payload.update(changes)
        return self.client.post("/transfer_preview", json=payload)

    def test_preview_counts_nested_files_and_empty_folders_without_creating_a_job(self):
        data = self.preview(names=["report..txt", "folder..name"]).get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["selected_count"], 2)
        self.assertEqual(data["file_count"], 2)
        self.assertEqual(data["folder_count"], 2)
        self.assertEqual(data["total_bytes"], 8)
        self.assertEqual(data["destination"], str(self.target))
        self.assertEqual(data["items"][0]["target"], str(self.target / "report..txt"))
        self.assertTrue(data["can_transfer"])
        self.assertTrue((self.source / "report..txt").exists())
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertFalse((self.root / "jobs").exists())

    def test_conflicts_identify_the_selected_file_and_keep_existing_data(self):
        target = self.target / "report..txt"
        target.write_bytes(b"keep")
        data = self.preview().get_json()
        self.assertFalse(data["can_transfer"])
        self.assertIn("already exists", data["items"][0]["conflict"])
        self.assertEqual(data["items"][0]["name"], "report..txt")
        self.assertEqual(target.read_bytes(), b"keep")

    def test_same_folder_and_folder_inside_itself_are_reported(self):
        data = self.preview(dest_dir=0).get_json()
        self.assertIn("Already in this folder", data["items"][0]["conflict"])
        data = self.preview(names=["folder..name"], dest_dir=0, dest_path="folder..name/empty").get_json()
        self.assertIn("inside itself", data["items"][0]["conflict"])

    def test_missing_source_or_unreadable_tree_provides_a_named_error(self):
        response = self.preview(names=["missing..txt"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("missing..txt", response.get_json()["error"])
        with patch.object(transfer_preview, "_measure", side_effect=PermissionError("Cannot read folder")):
            response = self.preview(names=["folder..name"])
        self.assertIn("folder..name", response.get_json()["error"])

    def test_private_destination_is_not_previewed(self):
        self.directories[1] = Directory(str(self.target), "/target", "Target", "private", "target")
        self.assertEqual(self.preview().status_code, 403)

    def test_all_local_pickers_use_preview_forms(self):
        for route, selection in (("/copy", {"src_name": "report..txt"}),
                                 ("/move", {"src_name": "report..txt"}),
                                 ("/bulk_move", {"name": ["report..txt", "folder..name"]})):
            page = self.client.get(route, query_string={"src_dir": 0, "dest_dir": 1, **selection}).get_data(as_text=True)
            self.assertIn("data-preview-operation=", page)
            self.assertIn("refreshTransferPreview", page)
            self.assertIn("type='submit' disabled", page)

    def test_preview_signature_matches_submitted_job_and_literal_percent_filename(self):
        name = "literal%2Fname..txt"
        (self.source / name).write_bytes(b"literal filename")
        data = self.preview(names=[name]).get_json()
        response = self.client.post("/copy", data={"src_dir": 0, "src_path": "", "src_name": name,
                                    "dest_dir": 1, "dest_path": "", "preview_signature": data["preview_signature"]})
        self.assertEqual(response.status_code, 200)
        job = job_store.history("tester")[0]
        self.assertEqual(job["source"]["names"], [name])
        with patch.object(job_worker.config_loader, "load_directories", return_value=self.directories), \
                patch.object(job_worker, "current_user_groups", return_value=["members"]):
            self.assertTrue(job_worker.run_once())
        self.assertEqual((self.target / name).read_bytes(), b"literal filename")

    def test_repointed_folder_after_preview_cannot_queue_a_different_transfer(self):
        data = self.preview().get_json()
        replacement = self.root / "replacement"
        replacement.mkdir()
        self.directories[1] = Directory(str(replacement), "/target", "Target", "members", "target")
        response = self.client.post("/copy", data={"src_dir": 0, "src_name": "report..txt", "dest_dir": 1,
                                    "preview_signature": data["preview_signature"]})
        self.assertIn("configuration changed", response.get_data(as_text=True))
        self.assertEqual(job_store.pending_count("tester"), 0)
        self.assertEqual(list(replacement.iterdir()), [])

    def test_move_pages_list_destination_folders_before_the_move_form(self):
        # At the root the default destination is the source folder itself,
        # so the preview reports conflicts; the way out -- the folders the
        # user can move into -- must be listed ahead of the move form and
        # its error, not buried underneath it.
        for url, params in (
            ("/bulk_move", {"src_dir": 0, "src_path": "", "name": "report..txt"}),
            ("/move", {"src_dir": 0, "src_path": "", "src_name": "report..txt"}),
        ):
            with self.subTest(url=url):
                page = self.client.get(url, query_string=params).get_data(as_text=True)
                self.assertIn("Folders in this destination:", page)
                self.assertLess(page.index("folder..name"), page.index("<div class='move-here-top'>"))

    def test_bulk_move_uses_the_previewed_selection_and_target(self):
        names = ["report..txt", "folder..name"]
        data = self.preview(operation="move", names=names).get_json()
        response = self.client.post("/bulk_move_do", data={"src_dir": 0, "src_path": "", "name": names,
                                    "dest_dir": 1, "dest_path": "", "preview_signature": data["preview_signature"]})
        self.assertEqual(response.status_code, 200)
        with patch.object(job_worker.config_loader, "load_directories", return_value=self.directories), \
                patch.object(job_worker, "current_user_groups", return_value=["members"]):
            self.assertTrue(job_worker.run_once())
        self.assertEqual((self.target / "report..txt").read_bytes(), b"12345")
        self.assertEqual((self.target / "folder..name" / "data.txt").read_bytes(), b"123")
        self.assertFalse((self.source / "folder..name").exists())
        self.assertFalse((self.source / "report..txt").exists())

    def test_remote_preview_shows_selected_subfolder_and_conflicts(self):
        destination = dict(id="backup", name="Backup", host="backup", path="/srv/backup", group="members")
        with patch.object(remote_copy, "load_destinations", return_value=[destination]), \
                patch.object(remote_copy, "preview_destination", return_value={"report..txt"}) as check:
            data = self.preview(remote_id="backup", remote_subpath="incoming").get_json()
        self.assertEqual(data["destination"], "backup:/srv/backup/incoming")
        self.assertEqual(data["items"][0]["target"], "backup:/srv/backup/incoming/report..txt")
        self.assertFalse(data["can_transfer"])
        self.assertEqual(check.call_args.args[0]["path"], "/srv/backup/incoming")

    def test_remote_timeout_has_clear_error(self):
        destination = dict(host="backup", path="/srv/backup")
        with patch.object(remote_copy.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 20)):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                remote_copy.preview_destination(destination, ["report..txt"])

    def test_remote_copy_queues_the_previewed_subfolder(self):
        destination = dict(id="backup", name="Backup", host="backup", path="/srv/backup", group="members")
        with patch.object(remote_copy, "load_destinations", return_value=[destination]), \
                patch.object(remote_copy, "preview_destination", return_value=set()):
            data = self.preview(remote_id="backup", remote_subpath="incoming").get_json()
            self.assertTrue(data["can_transfer"])
            response = self.client.post("/remote_copy", data={"src_dir": 0, "src_path": "", "name": "report..txt",
                                        "remote_id": "backup", "remote_subpath": "incoming",
                                        "preview_signature": data["preview_signature"]})
        self.assertEqual(response.status_code, 200)
        job = job_store.history("tester")[0]
        self.assertEqual(job["operation"], "remote_copy")
        self.assertEqual(job["destination"]["configuration"]["path"], "/srv/backup/incoming")
        self.assertEqual((self.source / "report..txt").read_bytes(), b"12345")

    def test_remote_move_queues_the_previewed_subfolder(self):
        destination = dict(id="backup", name="Backup", host="backup", path="/srv/backup", group="members")
        with patch.object(remote_copy, "load_destinations", return_value=[destination]), \
                patch.object(remote_copy, "preview_destination", return_value=set()):
            data = self.preview(operation="move", remote_id="backup", remote_subpath="incoming").get_json()
            self.assertEqual(data["operation"], "move")
            self.assertTrue(data["can_transfer"])
            response = self.client.post("/remote_move", data={"src_dir": 0, "src_path": "", "name": "report..txt",
                                        "remote_id": "backup", "remote_subpath": "incoming",
                                        "preview_signature": data["preview_signature"]})
        self.assertEqual(response.status_code, 200)
        job = job_store.history("tester")[0]
        self.assertEqual(job["operation"], "remote_move")
        self.assertEqual(job["destination"]["configuration"]["path"], "/srv/backup/incoming")
        # Queueing a move removes nothing; only a completed job may.
        self.assertEqual((self.source / "report..txt").read_bytes(), b"12345")

    @unittest.skipUnless(os.name == "posix", "Remote shell command check requires POSIX")
    def test_remote_conflict_command_handles_spaces_and_quotes(self):
        name = "report 'quoted'..txt"
        (self.target / name).write_bytes(b"keep")
        original = subprocess.run
        def local_command(arguments, **kwargs):
            return original(["sh", "-c", arguments[-1]], **kwargs)
        with patch.object(remote_copy.subprocess, "run", side_effect=local_command):
            conflicts = remote_copy.preview_destination({"host": "unused", "path": str(self.target)}, [name, "absent.txt"])
        self.assertEqual(conflicts, {name})
        self.assertEqual((self.target / name).read_bytes(), b"keep")

    @unittest.skipUnless(shutil.which("node"), "Requires Node.js for preview JavaScript")
    def test_browser_preview_disables_conflicts_ignores_stale_responses_and_renders_names_as_text(self):
        script = transfer_preview.scripts()[len("<script>"):-len("</script>")]
        harness = r"""
const assert = require('node:assert/strict');
class Element {
    constructor(tag) { this.tag = tag; this.children = []; }
    set textContent(value) { this.text = value; this.children = []; }
    set innerHTML(value) { throw new Error('Preview values must be text'); }
    appendChild(child) { this.children.push(child); }
    replaceChildren() { this.children = []; }
}
const box = new Element('section'), submit = {disabled: false};
const form = {dataset: {previewOperation:'copy'}, previewVersion:0,
    querySelector(selector) { return selector === '.transfer-preview' ? box : selector === 'button[type=submit]' ? submit : this.signature; },
    appendChild(input) { this.signature = input; }};
const document = {createElement: tag => new Element(tag), addEventListener() {}};
class FormData { set() {} }
const requests = [];
function fetch() { return new Promise(resolve => requests.push(resolve)); }
const name = '<img src=x onerror=alert(1)>.txt';
const ready = {success:true, destination:'/target', selected_count:1, file_count:1, folder_count:0,
    total_size:'5 B', items:[{name,target:'/target/'+name,size:'5 B',conflict:''}], can_transfer:true,preview_signature:'new'};
const result = data => ({ok:true,json:async()=>data});
(async () => {
    const old = refreshTransferPreview(form);
    assert.equal(submit.disabled, true);
    const latest = refreshTransferPreview(form);
    requests[1](result(ready)); await latest;
    assert.equal(submit.disabled, false);
    assert.equal(form.signature.value, 'new');
    const list = box.children.find(child => child.tag === 'ul');
    assert.ok(list.children[0].text.includes(name));
    requests[0](result({...ready,can_transfer:false,preview_signature:'old'})); await old;
    assert.equal(submit.disabled, false);
    assert.equal(form.signature.value, 'new');
    const conflict = refreshTransferPreview(form);
    requests[2](result({...ready,can_transfer:false,items:[{...ready.items[0],conflict:'Already exists'}]}));
    await conflict;
    assert.equal(submit.disabled, true);
    assert.equal(form.previewReady, false);
    const error = refreshTransferPreview(form);
    requests[3]({ok:false,json:async()=>({success:false,error:'Folder unavailable'})}); await error;
    assert.equal(submit.disabled, true);
    assert.equal(box.text, 'Folder unavailable');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run([shutil.which("node"), "-e", harness + script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
