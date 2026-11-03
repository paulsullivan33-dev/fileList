"""Download routing uses superFetch's complete filename identity."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from move_downloads import download_folder_name, load_rules, move_downloads


class DownloadRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "incoming"
        self.source.mkdir()
        self.destinations = {key: self.root / key for key in ("Books", "Comics")}

    def rules(self, text):
        history = self.root / "downloaded.txt"
        history.write_text(text, encoding="utf-8")
        return load_rules(history)

    def run_move(self, rules, dry_run=False):
        with contextlib.redirect_stdout(io.StringIO()):
            return move_downloads(self.source, rules, self.destinations, dry_run=dry_run)

    def test_sanitized_names_move_with_contents_and_keep_real_names(self):
        # Fixed names produced by superFetch.safe_nzb_filename.
        names = ["Example-Release-bd7be7a9c691e649", "Example-Release-9a7ac1059226f38d"]
        rules = self.rules("Example Release,Books\nExample, Release,Comics\n")
        for name in names:
            folder = self.source / name
            folder.mkdir()
            (folder / "content.txt").write_text(name, encoding="utf-8")
        self.assertEqual(self.run_move(rules), (2, 0))
        for name, category in zip(names, ("Books", "Comics")):
            self.assertFalse((self.source / name).exists())
            self.assertEqual((self.destinations[category] / name / "content.txt").read_text(), name)

    def test_same_title_in_different_categories_keeps_each_destination(self):
        rules = self.rules("Example.Release,Books\nExample.Release,Comics\nExample.Release,Books\n")
        names = {"Books": "Example.Release-66d47339f95d963b",
                 "Comics": "Example.Release-842f0d88ff5f2803"}
        for name in names.values():
            (self.source / name).write_text("download", encoding="utf-8")
        self.assertEqual(self.run_move(rules), (2, 0))
        for category, name in names.items():
            self.assertTrue((self.destinations[category] / name).is_file())

    def test_long_unicode_and_empty_sanitized_titles_are_routable(self):
        titles = ["\u6f22" * 60, "?" * 8]
        rules = self.rules("".join(f"{title},Books\n" for title in titles))
        for title in titles:
            name = download_folder_name(title, "Books")
            self.assertLessEqual(len(name.rsplit("-", 1)[0].encode("utf-8")), 150)
            (self.source / name).mkdir()
        self.assertTrue(download_folder_name(titles[1], "Books").startswith("download-"))
        self.assertEqual(self.run_move(rules), (2, 0))

    def test_dry_run_preserves_sources_and_creates_no_destinations(self):
        rules = self.rules("Example Release,Books\n")
        source = self.source / "Example-Release-bd7be7a9c691e649"
        source.mkdir()
        self.assertEqual(self.run_move(rules, dry_run=True), (1, 0))
        self.assertTrue(source.is_dir())
        self.assertFalse(self.destinations["Books"].exists())

    def test_unknown_hash_is_not_matched_by_stripping_it(self):
        rules = self.rules("Example.Release,Books\n")
        source = self.source / "Example.Release-0000000000000000"
        source.mkdir()
        self.assertEqual(self.run_move(rules), (0, 0))
        self.assertTrue(source.is_dir())

    def test_legacy_names_keep_first_category_without_shadowing_generated_names(self):
        generated = "Example.Release-66d47339f95d963b"
        rules = self.rules(f"{generated},Comics\nExample.Release,Books\nExample.Release,Comics\n")
        self.assertEqual(rules[generated], "Books")
        self.assertEqual(rules["Example.Release"], "Books")
        (self.source / "Example.Release").mkdir()
        self.assertEqual(self.run_move(rules), (1, 0))
        self.assertTrue((self.destinations["Books"] / "Example.Release").is_dir())
