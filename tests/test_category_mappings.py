"""Use real observed category labels, not numeric feed catalog IDs."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from flask import Flask
import actions
import auth
import category_mappings
import move_downloads
from config_loader import Directory


class CategoryMappingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "superFetch"
        self.data.mkdir()
        (self.data / "readAndParse.py").write_text("# Contract source location\n", encoding="utf-8")
        (self.data / "feeds.py").write_text('FEEDS = (Feed("7020", "books.xml", ("ebooks",)), Feed("3030", "audio.xml", ("audio",)))\n', encoding="utf-8")
        self.history = self.data / "downloaded.txt"
        self.history.write_text("Title, with comma,Books > eBook\nAnother private title,Audio Books\n", encoding="utf-8")
        for filename, category in (("books.xml", "Books > eBook"), ("audio.xml", "Audio Books")):
            rss = ET.Element("rss")
            item = ET.SubElement(ET.SubElement(rss, "channel"), "item")
            ET.SubElement(item, "title").text = "Private sample title"
            ET.SubElement(item, "category").text = category
            ET.ElementTree(rss).write(self.data / filename, encoding="utf-8")
        self.incoming = self.root / "incoming"
        self.incoming.mkdir()
        self.config = self.root / "move_downloads.json"
        self.write_config()
        (self.root / "remotes.json").write_text("[]", encoding="utf-8")
        environment = patch.dict(os.environ, SUPERFETCH_SOURCE=str(self.data / "readAndParse.py"),
                                 SUPERFETCH_DATA_DIR=str(self.data), FILELIST_MOVE_CONFIG=str(self.config),
                                 FILELIST_REMOTE_CONFIG=str(self.root / "remotes.json"),
                                 JOB_DATA_DIR=str(self.root / "jobs"), FLASK_SECRET_KEY="mappings-key")
        environment.start()
        self.addCleanup(environment.stop)

    def write_config(self, destinations=None):
        self.config.write_text(json.dumps({"source_directory": str(self.incoming),
            "downloaded_file": str(self.history), "destinations": destinations or {"Books > eBook": str(self.root / "books")}}), encoding="utf-8")

    def test_missing_category_is_reported_from_history_and_feed_labels(self):
        report = category_mappings.check()
        self.assertEqual(report["status"], "missing")
        self.assertEqual(report["missing"], ["Audio Books"])
        categories = {item["category"]: item for item in report["categories"]}
        self.assertEqual(categories["Audio Books"]["history_count"], 1)
        self.assertEqual(categories["Audio Books"]["feed_count"], 1)
        self.assertNotIn("Private sample title", json.dumps(report))

    def test_all_mapped_checks_are_read_only(self):
        self.write_config({"Books > eBook": str(self.root / "books"), "Audio Books": str(self.root / "audio")})
        before = set(self.root.rglob("*"))
        report = category_mappings.check()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["missing"], [])
        self.assertEqual(set(self.root.rglob("*")), before)

    def test_numeric_feed_ids_are_not_treated_as_destination_labels(self):
        self.write_config({"7020": str(self.root / "books"), "3030": str(self.root / "audio")})
        self.assertEqual(category_mappings.check()["missing"], ["Audio Books", "Books > eBook"])

    def test_missing_or_malformed_cache_reports_incomplete_coverage(self):
        self.write_config({"Books > eBook": str(self.root / "books"), "Audio Books": str(self.root / "audio")})
        (self.data / "audio.xml").unlink()
        report = category_mappings.check()
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["missing_feed_files"], 1)
        (self.data / "audio.xml").write_text("{not xml", encoding="utf-8")
        report = category_mappings.check()
        self.assertEqual(report["status"], "incomplete")
        self.assertTrue(any("could not be read" in note for note in report["warnings"]))

    def test_different_history_path_does_not_claim_full_compatibility(self):
        value = json.loads(self.config.read_text())
        value["downloaded_file"] = str(self.root / "wrong-history.txt")
        self.config.write_text(json.dumps(value), encoding="utf-8")
        report = category_mappings.check()
        self.assertTrue(any("configured history differs" in note for note in report["warnings"]))
        self.assertIn("Audio Books", report["missing"])

    def test_absent_config_is_explained_and_not_created(self):
        self.config.unlink()
        report = category_mappings.check()
        self.assertEqual(report["status"], "not_configured")
        self.assertFalse(self.config.exists())

    def test_check_mappings_command_never_moves_files(self):
        with patch.object(move_downloads, "move_downloads") as move, contextlib.redirect_stdout(io.StringIO()) as output:
            result = move_downloads.main([str(self.config), "--check-mappings", "--superfetch-data", str(self.data)])
        move.assert_not_called()
        self.assertEqual(result, 1)
        self.assertIn("MISSING DESTINATION: Audio Books", output.getvalue())

    def test_configuration_checker_can_include_mapping_checks(self):
        from tools import check_config
        with patch.object(check_config, "validate_startup", return_value=([], [])), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = check_config.main(["--categories"])
        self.assertEqual(result, 1)
        self.assertIn("MISSING DESTINATION: Audio Books", output.getvalue())

    def test_configuration_page_checks_on_demand_and_endpoint_returns_missing_labels(self):
        (self.root / "users.json").write_text(json.dumps([
            {"username": "tester", "password": "unused", "groups": "members"}
        ]), encoding="utf-8")
        app = Flask(__name__, root_path=str(self.root))
        app.config["TESTING"] = True
        app.jinja_env.globals["csrf_token"] = lambda: "token"
        directories = [Directory(str(self.incoming), "/incoming", "Incoming", "members")]
        auth.register(app, directories)
        actions.register(app, directories)
        client = app.test_client()
        with client.session_transaction() as session:
            session["username"] = "tester"
            session["groups"] = ["members"]
        with patch.object(category_mappings, "check", wraps=category_mappings.check) as check:
            page = client.get("/configuration").get_data(as_text=True)
            check.assert_not_called()
            self.assertIn("Check category mappings", page)
            data = client.get("/category_mappings").get_json()
            self.assertEqual(data["missing"], ["Audio Books"])
            check.assert_called_once()

    @unittest.skipUnless(shutil.which("node"), "Requires Node.js for mapping display")
    def test_mapping_display_shows_missing_labels_and_recovers_from_errors(self):
        script = category_mappings.web_controls().split("<script>", 1)[1].rsplit("</script>", 1)[0]
        setup = r"""
const assert = require('node:assert/strict');
class Element { constructor(){this.children=[];} appendChild(child){this.children.push(child);} replaceChildren(){this.children=[];} }
const button = {}, output = new Element();
const document = {getElementById:id=>id==='checkCategoryMappings'?button:output,createElement:()=>new Element()};
let fail = false;
async function fetch() { if(fail) throw new Error('offline'); return {ok:true,json:async()=>({success:true,status:'missing',categories:[{category:'Audio Books',mapped:false}],missing:['Audio Books'],warnings:[]})}; }
"""
        checks = r"""
(async()=>{
    await button.onclick.call(button);
    assert.equal(button.disabled,false);
    assert.match(output.children[0].textContent,/1 missing destination/);
    assert.equal(output.children[1].children[0].textContent,'Audio Books — MISSING DESTINATION');
    fail = true; await button.onclick.call(button);
    assert.equal(button.disabled,false);
    assert.equal(output.textContent,'offline');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which("node"), "-e", setup + script + checks], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
