"""Configuration-checker regression tests."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("check_config", ROOT / "tools" / "check_config.py")
check_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_config)


class ConfigCheckTests(unittest.TestCase):
    def test_directories_require_existing_writable_absolute_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "directories.json"
            config.write_text(json.dumps([{
                "root": str(root), "url": "https://files.example.test/root", "group": "members",
            }]), encoding="utf-8")
            messages, errors = check_config.check_directories(config)
            self.assertTrue(messages)
            self.assertFalse(errors)
            config.write_text(json.dumps([{
                "root": "relative", "url": "https://files.example.test/root", "group": "members",
            }]), encoding="utf-8")
            _, errors = check_config.check_directories(config)
            self.assertTrue(errors)

    def test_users_require_unique_complete_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "users.json"
            config.write_text(json.dumps([
                {"username": "sam", "password": "hash", "groups": "members"},
                {"username": "sam", "password": "hash", "groups": "members"},
            ]), encoding="utf-8")
            _, errors = check_config.check_users(config)
            self.assertIn("Duplicate username: sam", errors)


if __name__ == "__main__":
    unittest.main()
