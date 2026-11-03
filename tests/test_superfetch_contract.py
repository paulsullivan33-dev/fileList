"""Compare naming and history routing with real producer code, not a copy."""
from pathlib import Path
import os
import tempfile
import unittest

from tools import check_superfetch


class SuperFetchContractTests(unittest.TestCase):
    def test_real_superfetch_contract(self):
        path = check_superfetch.producer_path()
        if not path.exists():
            if "SUPERFETCH_SOURCE" in os.environ:
                self.fail(f"Configured superFetch source is missing: {path}")
            self.skipTest("Set SUPERFETCH_SOURCE to superFetch/readAndParse.py to check the producer")
        self.assertEqual(check_superfetch.check(path), [])

    def test_changed_producer_naming_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "readAndParse.py"
            path.write_text('def safe_nzb_filename(title, category=""):\n    return title + ".nzb"\n'
                            'def load_downloaded(path):\n    return {tuple(line.rstrip("\\n").rsplit(",",1)) for line in path.read_text().splitlines()}\n',
                            encoding="utf-8")
            errors = check_superfetch.check(path)
            self.assertTrue(any("Filename contract mismatch" in error for error in errors))
            self.assertTrue(any("Routing contract mismatch" in error for error in errors))

    def test_missing_producer_contract_function_is_explained(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "readAndParse.py"
            path.write_text("# Producer was reorganized\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "functions changed or are missing"):
                check_superfetch.load_contract(path)
