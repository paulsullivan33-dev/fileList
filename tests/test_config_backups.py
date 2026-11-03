"""Configuration replacements are backed up and can be restored exactly."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import config_backups
import manage_user
from tools import config_backup


class ConfigBackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "users.json"
        self.original = b'[{"username":"before","password":"oldhash","groups":"members"}]\n'
        self.path.write_bytes(self.original)

    def test_replacement_backs_up_original_bytes_and_lists_it(self):
        saved = config_backups.write_json(self.path, [{"username": "after"}])
        self.assertEqual((saved / "config.json").read_bytes(), self.original)
        self.assertEqual(json.loads(self.path.read_text()), [{"username": "after"}])
        backups = config_backups.list_backups(self.path)
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0]["backup"], str(saved))
        self.assertEqual(backups[0]["source"], str(self.path))

    def test_restore_backs_up_current_settings_and_preserves_exact_original(self):
        saved = config_backups.backup(self.path)
        current = b'{"changed": true}\n'
        self.path.write_bytes(current)
        target, previous = config_backups.restore(saved)
        self.assertEqual(target, self.path)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual((previous / "config.json").read_bytes(), current)
        self.assertEqual(len(config_backups.list_backups(self.path)), 2)

    def test_restore_to_another_target_does_not_change_original(self):
        saved = config_backups.backup(self.path)
        target = self.root / "restored.json"
        config_backups.restore(saved, target)
        self.assertEqual(target.read_bytes(), self.original)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_backup_failure_prevents_replacement(self):
        with patch.object(config_backups, "backup", side_effect=OSError("Backup disk unavailable")):
            with self.assertRaisesRegex(OSError, "Backup disk unavailable"):
                config_backups.write_json(self.path, {"changed": True})
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_corrupt_backup_cannot_replace_current_settings(self):
        saved = config_backups.backup(self.path)
        (saved / "config.json").write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "checksum"):
            config_backups.restore(saved)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_list_filters_backups_by_original_configuration(self):
        config_backups.backup(self.path)
        other = self.root / "directories.json"
        other.write_text("[]", encoding="utf-8")
        config_backups.backup(other)
        self.assertEqual(len(config_backups.list_backups(self.path)), 1)

    def test_manage_user_automatically_saves_previous_user_settings(self):
        with patch.object(manage_user, "USERS_FILE", self.path), \
                patch.object(manage_user.getpass, "getpass", side_effect=["new-password", "new-password"]), \
                patch.object(manage_user, "generate_password_hash", return_value="newhash"), \
                contextlib.redirect_stdout(io.StringIO()):
            manage_user.main(["after", "--groups", "members"])
        saved = config_backups.list_backups(self.path)
        self.assertEqual(len(saved), 1)
        self.assertEqual((Path(saved[0]["backup"]) / "config.json").read_bytes(), self.original)
        self.assertEqual([entry["username"] for entry in json.loads(self.path.read_text())], ["before", "after"])

    def test_backup_and_restore_commands_do_not_print_configuration_contents(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(config_backup.main(["backup", str(self.path)]), 0)
            self.assertEqual(config_backup.main(["list", str(self.path)]), 0)
        self.assertNotIn("oldhash", output.getvalue())
        saved = config_backups.list_backups(self.path)[0]["backup"]
        self.path.write_text("[]", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(config_backup.main(["restore", saved]), 0)
        self.assertEqual(self.path.read_bytes(), self.original)
