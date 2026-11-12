"""Configuration-checker regression tests."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config_loader


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

    def test_directory_validation_reports_all_entries_without_creating_folders(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "directories.json"
            config.write_text(json.dumps([
                {"root": "missing", "url": "/files", "group": "members"},
                {"root": str(root), "url": "/files", "group": ""},
                "not an object",
            ]), encoding="utf-8")
            _, errors = check_config.check_directories(config)
            self.assertTrue(any("Directory 1" in error and "missing" in error for error in errors))
            self.assertTrue(any("Directory 2" in error for error in errors))
            self.assertTrue(any("Directory 3" in error for error in errors))
            self.assertFalse((root / "missing").exists())

    def test_missing_config_is_not_replaced_with_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "missing.json"
            with patch.object(config_loader, "CONFIG_FILE", config):
                with self.assertRaisesRegex(ValueError, "Cannot read directory configuration"):
                    config_loader.load_directories()
            self.assertFalse(config.exists())

    def test_relative_roots_have_same_meaning_in_checker_and_loader(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "files").mkdir()
            config = root / "directories.json"
            config.write_text(json.dumps([{"root": "files", "url": "/files", "group": "members"}]), encoding="utf-8")
            _, errors = check_config.check_directories(config)
            self.assertEqual(errors, [])
            with patch.object(config_loader, "CONFIG_FILE", config):
                self.assertEqual(config_loader.load_directories()[0].root, str(root / "files"))

    def test_local_only_runtime_does_not_require_ssh_and_allows_new_job_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, JOB_DATA_DIR=str(root / "new" / "jobs"),
                            FILELIST_OPERATION_LOG=str(root / "new" / "operations.sqlite3")), \
                    patch.object(check_config.shutil, "which", return_value=None):
                _, errors = check_config.check_runtime(False)
                self.assertEqual(errors, [])
                _, errors = check_config.check_runtime(True)
                self.assertTrue(any("ssh" in error for error in errors))
                self.assertTrue(any("rsync" in error for error in errors))
            self.assertFalse((root / "new").exists())

    def test_invalid_runtime_settings_have_named_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, JOB_DATA_DIR=temporary, FILELIST_OPERATION_LOG=str(Path(temporary) / "ops.sqlite3"),
                            PORT="oops", MAX_FILE_SIZE="-1", JOB_PROGRESS_INTERVAL="nan"):
                _, errors = check_config.check_runtime()
            for setting in ("PORT", "MAX_FILE_SIZE", "JOB_PROGRESS_INTERVAL"):
                self.assertTrue(any(setting in error for error in errors))

    def test_remote_availability_is_only_probed_when_requested(self):
        remote = {"name": "Backup", "host": "backup", "path": "/missing", "group": "members"}
        with patch.object(check_config.remote_copy, "load_destinations", return_value=[remote]), \
                patch.object(check_config.subprocess, "run") as run:
            _, errors, warnings = check_config.check_remotes(False)
            self.assertEqual((errors, warnings), ([], []))
            run.assert_not_called()
            run.return_value = type("Result", (), {"returncode": 1, "stdout": "Folder unavailable"})()
            _, errors, warnings = check_config.check_remotes(True)
            self.assertEqual(errors, [])
            self.assertTrue(any("Backup" in warning and "Folder unavailable" in warning for warning in warnings))
            _, errors, warnings = check_config.check_remotes(True, strict=True)
            self.assertEqual(warnings, [])
            self.assertTrue(any("Backup" in error and "Folder unavailable" in error for error in errors))

    def test_unreachable_remote_warns_without_failing_unless_strict(self):
        remote = {"name": "Backup", "host": "backup", "path": "/srv", "group": "members"}
        timeout = subprocess.TimeoutExpired(cmd="ssh", timeout=20)
        with patch.object(check_config.remote_copy, "load_destinations", return_value=[remote]),                 patch.object(check_config.subprocess, "run", side_effect=timeout),                 patch.object(check_config, "check_directories", return_value=([], [])),                 patch.object(check_config, "check_users", return_value=([], [])),                 patch.object(check_config, "check_runtime", return_value=([], [])),                 patch("sys.stdout") as stdout:
            self.assertEqual(check_config.main(["--ssh"]), 0)
            output = "".join(call.args[0] for call in stdout.write.call_args_list)
            self.assertIn("WARNING: Remote destination timed out: Backup", output)
            self.assertEqual(check_config.main(["--ssh", "--strict"]), 1)

    @unittest.skipUnless(os.name == "posix", "POSIX permission modes")
    def test_readable_users_file_is_a_warning_with_the_fix(self):
        with tempfile.TemporaryDirectory() as temporary:
            users = Path(temporary) / "users.json"
            users.write_text("[]", encoding="utf-8")
            users.chmod(0o644)
            warnings = check_config.check_private_file(users, "users.json")
            self.assertEqual(len(warnings), 1)
            self.assertIn("0644", warnings[0])
            self.assertIn("chmod 600", warnings[0])
            users.chmod(0o600)
            self.assertEqual(check_config.check_private_file(users, "users.json"), [])

    def test_unreadable_remote_destinations_file_is_still_an_error(self):
        with patch.object(check_config.remote_copy, "load_destinations", side_effect=ValueError("bad JSON")):
            _, errors, warnings = check_config.check_remotes(True)
        self.assertEqual(warnings, [])
        self.assertTrue(any("bad JSON" in error for error in errors))

    @unittest.skipUnless(os.name == "posix", "Bash launcher test requires Linux/POSIX")
    def test_launcher_stops_before_starting_processes_when_preflight_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            start = root / "start"
            start.write_bytes((ROOT / "start").read_bytes())
            (root / "tools").mkdir()
            (root / "tools" / "check_config.py").write_text("print('Configuration is invalid')\nraise SystemExit(1)\n")
            for name in ("job_worker.py", "app.py"):
                (root / name).write_text("from pathlib import Path\nPath('unexpected-start').touch()\n")
            result = subprocess.run(["bash", str(start)], env={**os.environ, "PYTHON_BIN": sys.executable},
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Configuration is invalid", result.stdout)
            self.assertFalse((root / "unexpected-start").exists())

    @unittest.skipUnless(os.name == "posix", "Bash launcher test requires Linux/POSIX")
    def test_launcher_skips_repeat_check_when_run_sh_already_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            start = root / "start"
            start.write_bytes((ROOT / "start").read_bytes())
            (root / "tools").mkdir()
            (root / "tools" / "check_config.py").write_text("from pathlib import Path\nPath('checked').touch()\n")
            for name in ("job_worker.py", "app.py"):
                (root / name).write_text("")
            environment = {**os.environ, "PYTHON_BIN": sys.executable}
            subprocess.run(["bash", str(start)], env={**environment, "FILELIST_PRECHECKED": "1"},
                           capture_output=True, text=True, timeout=10)
            self.assertFalse((root / "checked").exists())
            subprocess.run(["bash", str(start)], env=environment, capture_output=True, text=True, timeout=10)
            self.assertTrue((root / "checked").exists())


if __name__ == "__main__":
    unittest.main()
