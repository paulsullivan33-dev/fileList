"""Archives cannot escape their sources or overwrite user data."""
import io
import os
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import actions
import archive_ops


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "files"
        self.root.mkdir()
        self.folder = self.root / "folder..name"
        self.folder.mkdir()
        (self.folder / "empty").mkdir()
        (self.folder / "file..txt").write_text("contents", encoding="utf-8")

    def create(self, kind, name="archive"):
        target = self.root / f"{name}.{kind}"
        archive_ops.create_archive(str(self.root), str(self.root), [self.folder.name], str(target), kind)
        return target

    def extract(self, source, target):
        archive_ops.extract_archive(str(source), str(target), actions._validate_archive_size)

    def assert_no_staging(self):
        self.assertFalse(list(self.root.glob(".filelist-*")))

    def symlink(self, path, target, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except OSError as error:
            self.skipTest(f"Symlink creation unavailable: {error}")

    def test_round_trip_preserves_contents_empty_folders_and_double_periods(self):
        for kind in ("zip", "tar", "tgz"):
            with self.subTest(kind=kind):
                source = self.create(kind)
                target = self.root / f"extracted-{kind}"
                self.extract(source, target)
                self.assertEqual((target / self.folder.name / "file..txt").read_text(), "contents")
                self.assertTrue((target / self.folder.name / "empty").is_dir())
                self.assert_no_staging()

    def test_existing_archive_is_never_truncated(self):
        for kind in ("zip", "tar", "tgz"):
            target = self.root / f"archive.{kind}"
            target.write_bytes(b"existing archive")
            with self.assertRaises(FileExistsError):
                self.create(kind)
            self.assertEqual(target.read_bytes(), b"existing archive")

    def test_extracted_tree_gets_standard_readable_permissions(self):
        # Even under a restrictive service umask, the extracted folder and
        # its contents must come out readable (the staging directory starts
        # life at 0700 and must not pass that on to the published folder).
        previous = os.umask(0o077)
        try:
            for kind in ("zip", "tar", "tgz"):
                with self.subTest(kind=kind):
                    target = self.root / f"readable-{kind}"
                    self.extract(self.create(kind, f"readable-{kind}"), target)
                    self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o755)
                    extracted_file = target / self.folder.name / "file..txt"
                    self.assertEqual(stat.S_IMODE(os.stat(extracted_file).st_mode), 0o644)
                    extracted_dir = target / self.folder.name / "empty"
                    self.assertEqual(stat.S_IMODE(os.stat(extracted_dir).st_mode), 0o755)
        finally:
            os.umask(previous)

    def test_same_name_wrapper_folder_is_not_nested_twice(self):
        source = self.root / "wrap.zip"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("wrap/file.txt", "contents")
            archive.writestr("wrap/sub/deep.txt", "deep")
        target = self.root / "wrap"
        self.extract(source, target)
        self.assertEqual((target / "file.txt").read_text(encoding="utf-8"), "contents")
        self.assertEqual((target / "sub" / "deep.txt").read_text(encoding="utf-8"), "deep")
        self.assertFalse((target / "wrap").exists())

    def test_differently_named_wrapper_folder_is_kept(self):
        source = self.root / "bundle.zip"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("inner/file.txt", "contents")
        target = self.root / "bundle"
        self.extract(source, target)
        self.assertEqual((target / "inner" / "file.txt").read_text(encoding="utf-8"), "contents")

    def test_existing_extraction_folder_and_file_are_preserved(self):
        source = self.create("zip")
        for is_directory in (True, False):
            target = self.root / f"existing-{is_directory}"
            if is_directory:
                target.mkdir()
                sentinel = target / "keep.txt"
            else:
                sentinel = target
            sentinel.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                self.extract(source, target)
            self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assert_no_staging()

    def test_nested_external_file_symlink_is_rejected_for_every_format(self):
        secret = self.base / "secret.txt"
        secret.write_text("private", encoding="utf-8")
        self.symlink(self.folder / "link.txt", secret)
        for kind in ("zip", "tar", "tgz"):
            with self.assertRaisesRegex(ValueError, "link.txt"):
                self.create(kind)
            self.assertFalse((self.root / f"archive.{kind}").exists())
        self.assert_no_staging()

    def test_external_directory_symlink_is_rejected(self):
        self.symlink(self.folder / "link-dir", self.base, directory=True)
        with self.assertRaisesRegex(ValueError, "link-dir"):
            self.create("zip")
        self.assert_no_staging()

    def test_direct_source_symlink_is_rejected(self):
        secret = self.base / "secret.txt"
        secret.write_text("private", encoding="utf-8")
        link = self.root / "link.txt"
        self.symlink(link, secret)
        with self.assertRaisesRegex(ValueError, "link.txt"):
            archive_ops.create_archive(str(self.root), str(self.root), [link.name], str(self.root / "bad.zip"), "zip")

    def test_link_validation_runs_even_when_host_cannot_create_symlinks(self):
        original = os.lstat
        blocked = self.folder / "file..txt"
        def linked_info(path, *args, **kwargs):
            if os.fspath(path) == str(blocked):
                return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
            return original(path, *args, **kwargs)
        with patch.object(archive_ops.os, "lstat", side_effect=linked_info):
            for kind in ("zip", "tar", "tgz"):
                with self.assertRaisesRegex(ValueError, "file..txt"):
                    self.create(kind)
                self.assertFalse((self.root / f"archive.{kind}").exists())

    def test_replaced_source_inode_is_rejected_before_reading(self):
        source = self.folder / "file..txt"
        expected = source.stat()
        replacement = self.folder / "replacement"
        replacement.write_text("replacement", encoding="utf-8")
        source.unlink()
        replacement.rename(source)
        with self.assertRaisesRegex(ValueError, "changed while being read"):
            archive_ops._open_source(str(self.root), str(source), expected)

    def test_zip_traversal_and_duplicate_members_leave_no_output(self):
        for entries in (("good.txt", "../outside.txt"), ("same.txt", "./same.txt")):
            source = self.root / "bad.zip"
            with zipfile.ZipFile(source, "w") as archive:
                for name in entries:
                    archive.writestr(name, b"payload")
            target = self.root / "output"
            with self.assertRaises((ValueError, FileExistsError)):
                self.extract(source, target)
            self.assertFalse(target.exists())
            self.assertFalse((self.root / "outside.txt").exists())
            self.assert_no_staging()

    def test_tar_links_and_traversal_are_rejected(self):
        for name, kind in (("../outside", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)):
            source = self.root / "bad.tar"
            with tarfile.open(source, "w") as archive:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = "../outside"
                member.size = 0
                archive.addfile(member, io.BytesIO())
            with self.assertRaises(ValueError):
                self.extract(source, self.root / "output")
            self.assertFalse((self.root / "output").exists())
            self.assert_no_staging()

    def test_creation_failure_cleans_staging_without_publishing(self):
        with patch.object(archive_ops, "_open_source", side_effect=OSError("Read failed")):
            with self.assertRaisesRegex(OSError, "Read failed"):
                self.create("zip")
        self.assertFalse((self.root / "archive.zip").exists())
        self.assert_no_staging()

    def test_size_limit_failure_leaves_no_extracted_output(self):
        source = self.create("zip")
        with patch.dict(os.environ, ARCHIVE_MAX_UNCOMPRESSED_BYTES="1"):
            with self.assertRaises(ValueError):
                self.extract(source, self.root / "output")
        self.assertFalse((self.root / "output").exists())
        self.assert_no_staging()

    def test_late_archive_destination_collision_does_not_overwrite(self):
        original = archive_ops._rename_without_replace
        def collision(source, target):
            Path(target).write_bytes(b"competitor")
            original(source, target)
        with patch.object(archive_ops, "_rename_without_replace", side_effect=collision):
            with self.assertRaises(FileExistsError):
                self.create("zip")
        self.assertEqual((self.root / "archive.zip").read_bytes(), b"competitor")
        self.assert_no_staging()

    def test_late_extraction_destination_collision_does_not_overwrite(self):
        source = self.create("zip")
        target = self.root / "output"
        original = archive_ops._rename_without_replace
        def collision(staging, destination):
            Path(destination).mkdir()
            (Path(destination) / "keep").write_bytes(b"competitor")
            original(staging, destination)
        with patch.object(archive_ops, "_rename_without_replace", side_effect=collision):
            with self.assertRaises(OSError):
                self.extract(source, target)
        self.assertEqual((target / "keep").read_bytes(), b"competitor")
        self.assertFalse((target / self.folder.name).exists())
        self.assert_no_staging()
