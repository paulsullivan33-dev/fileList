"""Large-folder pages retain global search/sort without reading every size."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
import auth
import browser
import folder_listing
from config_loader import Directory


class ListingCostTests(unittest.TestCase):
    def entries(self):
        reads = []
        class Entry:
            def __init__(self, number):
                self.name = f"{number:05}.txt"
                self.number = number
            def is_dir(self):
                return False
            def stat(self):
                reads.append(self.name)
                return SimpleNamespace(st_size=self.number, st_mtime=self.number)
        @contextmanager
        def scan(path):
            yield iter([Entry(number) for number in range(1000)])
        return scan, reads

    def test_name_browsing_reads_metadata_only_for_the_visible_page(self):
        scan, reads = self.entries()
        with patch.object(folder_listing.os, "scandir", side_effect=scan):
            result = folder_listing.page("unused", page_number=2, page_size=25)
        self.assertEqual(len(reads), 25)
        self.assertEqual(result["entries"][0]["name"], "00025.txt")
        self.assertEqual(result["total"], 1000)

    def test_search_finds_names_beyond_the_first_page_without_statting_nonmatches(self):
        scan, reads = self.entries()
        with patch.object(folder_listing.os, "scandir", side_effect=scan):
            result = folder_listing.page("unused", query="00999")
        self.assertEqual([record["name"] for record in result["entries"]], ["00999.txt"])
        self.assertEqual(reads, ["00999.txt"])

    def test_size_and_date_sorting_apply_to_the_whole_folder(self):
        for column in (3, 4):
            scan, reads = self.entries()
            with patch.object(folder_listing.os, "scandir", side_effect=scan):
                result = folder_listing.page("unused", page_size=25, sort=column, order="desc")
            self.assertEqual(result["entries"][0]["name"], "00999.txt")
            self.assertEqual(len(reads), 1000)


class PaginationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.files = self.root / "files"
        self.files.mkdir()
        for number in range(60):
            (self.files / f"entry-{number:03}.txt").write_bytes(b"x" * (number + 1))
        (self.root / "users.json").write_text(json.dumps([
            {"username": "tester", "password": "unused", "groups": "members"}
        ]), encoding="utf-8")
        environment = patch.dict(os.environ, FLASK_SECRET_KEY="pagination-key")
        environment.start()
        self.addCleanup(environment.stop)
        self.app = Flask(__name__, root_path=str(self.root))
        self.app.config["TESTING"] = True
        self.directories = [Directory(str(self.files), "/files", "Files", "members")]
        auth.register(self.app, self.directories)
        browser.register(self.app, self.directories)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["username"] = "tester"
            session["groups"] = ["members"]

    def page(self, **changes):
        return self.client.get("/", query_string={"dir": 0, "per_page": 25, **changes}).get_data(as_text=True)

    def names(self, text):
        return re.findall(r"<tr[^>]*data-name='([^']*)'", text)

    def test_pagination_renders_only_current_rows_and_preserves_query_sort(self):
        first = self.page(q="entry", sort=1, order="asc")
        self.assertEqual(self.names(first), [f"entry-{number:03}.txt" for number in range(25)])
        self.assertIn("Showing 1–25 of 60 matches", first)
        self.assertIn("Selection is limited to this page", first)
        self.assertIn("page=2", first)
        self.assertIn("per_page=25", first)
        self.assertIn("q=entry", first)
        second = self.page(page=2)
        self.assertEqual(self.names(second)[0], "entry-025.txt")
        self.assertEqual(len(self.names(second)), 25)

    def test_search_and_sort_are_global(self):
        self.assertEqual(self.names(self.page(q="entry-059")), ["entry-059.txt"])
        self.assertEqual(self.names(self.page(sort=3, order="desc"))[0], "entry-059.txt")

    def test_page_bounds_and_page_size_are_bounded(self):
        self.assertEqual(len(self.names(self.page(page=9999))), 10)
        self.assertEqual(self.names(self.page(page=-10))[0], "entry-000.txt")
        self.assertEqual(len(self.names(self.page(per_page=1000000))), 60)
        self.assertIn("Page 1 of 1", self.page(q="absent"))
        self.assertEqual(self.names(self.page(q="absent")), [])

    def test_totals_are_only_calculated_when_requested(self):
        with patch.object(folder_listing, "totals", wraps=folder_listing.totals) as totals:
            page = self.page()
            totals.assert_not_called()
            self.assertNotIn("class='stats'", page)
            self.assertIn("in displayed files", page)
            self.assertIn("Calculate folder totals", page)
            response = self.client.get("/folder_stats", query_string={"dir": 0})
            totals.assert_called_once()
        data = response.get_json()
        self.assertEqual(data["files"], 60)
        self.assertEqual(data["bytes"], sum(range(1, 61)))

    def test_fresh_page_reflects_changes_and_parent_row_does_not_consume_page_size(self):
        (self.files / "entry-999.txt").write_bytes(b"new")
        self.assertIn("of 61 items", self.page())
        child = self.files / "folder..name"
        child.mkdir()
        for number in range(26):
            (child / f"{number:03}.txt").touch()
        names = self.names(self.page(path="folder..name"))
        self.assertEqual(names[0], "..")
        self.assertEqual(len(names), 26)

    @unittest.skipUnless(shutil.which("node"), "Requires Node.js for listing navigation")
    def test_browser_search_and_sort_navigate_globally_without_reload_on_init(self):
        scripts = browser._inline_scripts()
        functions = scripts[scripts.index("function sortTable("):scripts.index("function toggleAll(")]
        harness = r"""
const assert = require('node:assert/strict');
const moves = [];
const current = 'http://local/?dir=2&path=folder&page=3&per_page=25&q=old&sort=1&order=asc';
const window = {location:{href:current,origin:'http://local',assign:url=>moves.push(new URL(url))}};
const state = {sortCol:1,sortDir:'asc'};
function saveState() {}
const output = {};
const fields = {searchInput:{value:'new match'},folderTotals:output,currentDir:{value:'2'},currentPath:{value:'folder'}};
const document = {getElementById:id=>fields[id],querySelectorAll:()=>[]};
let requests = 0;
async function fetch(url) { requests++; return {ok:true,json:async()=>({success:true,folders:2,files:60,size:'1 KB',unavailable:1})}; }
(async () => {
    applySort(1,'asc');
    assert.equal(moves.length,0);
    sortTable(3);
    assert.equal(moves[0].searchParams.get('sort'),'3');
    assert.equal(moves[0].searchParams.get('page'),'1');
    assert.equal(moves[0].searchParams.get('q'),'old');
    assert.equal(moves[0].searchParams.get('per_page'),'25');
    let prevented = false;
    filterTable({preventDefault(){prevented=true;}});
    assert.equal(prevented,true);
    assert.equal(moves[1].searchParams.get('q'),'new match');
    assert.equal(moves[1].searchParams.get('page'),'1');
    assert.equal(requests,0);
    const button = {disabled:false};
    await loadFolderTotals(button);
    assert.equal(requests,1);
    assert.equal(button.disabled,false);
    assert.match(output.textContent,/totals are partial/);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which("node"), "-e", functions + harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
