"""Publishing never replaces an existing entry, even without RENAME_NOREPLACE.

NFS and many FUSE filesystems reject renameat2(RENAME_NOREPLACE) with EINVAL.
These tests simulate that (and filesystems without hard links) and check that
files still publish, existing names are never overwritten, and EXDEV still
reaches movers so they fall back to copying.
"""
import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import job_worker


class FakeLibc:
    """A libc whose renameat2 fails with the given errno."""

    def __init__(self, error_number):
        self.error_number = error_number
        self.calls = 0

    def renameat2(self, *_arguments):
        self.calls += 1
        return -1


@unittest.skipIf(os.name == "nt", "POSIX rename semantics")
class RenameFallbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / ".upload.partial"
        self.source.write_text("new", encoding="utf-8")
        self.destination = self.root / "report.txt"

    def without_noreplace(self, error_number=errno.EINVAL):
        libc = FakeLibc(error_number)
        return libc, [
            patch.object(job_worker.sys, "platform", "linux"),
            patch.object(job_worker.ctypes, "CDLL", return_value=libc),
            patch.object(job_worker.ctypes, "get_errno", return_value=error_number),
        ]

    def test_unsupported_noreplace_publishes_file_by_hard_link(self):
        for error_number in (errno.EINVAL, errno.ENOSYS, errno.ENOTSUP):
            with self.subTest(errno=errno.errorcode[error_number]):
                self.source.write_text("new", encoding="utf-8")
                self.destination.unlink(missing_ok=True)
                libc, patches = self.without_noreplace(error_number)
                with patches[0], patches[1], patches[2], \
                        patch.object(job_worker.os, "rename", side_effect=AssertionError("unsafe rename")):
                    job_worker._rename_without_replace(str(self.source), str(self.destination))
                self.assertEqual(libc.calls, 1)
                self.assertEqual(self.destination.read_text(encoding="utf-8"), "new")
                self.assertFalse(self.source.exists())

    def test_hard_link_fallback_never_overwrites_an_existing_file(self):
        self.destination.write_text("existing", encoding="utf-8")
        _, patches = self.without_noreplace()
        with patches[0], patches[1], patches[2]:
            with self.assertRaises(FileExistsError):
                job_worker._rename_without_replace(str(self.source), str(self.destination))
        self.assertEqual(self.destination.read_text(encoding="utf-8"), "existing")
        self.assertEqual(self.source.read_text(encoding="utf-8"), "new")

    def test_without_hard_links_falls_back_to_check_then_rename(self):
        _, patches = self.without_noreplace()
        with patches[0], patches[1], patches[2], \
                patch.object(job_worker.os, "link", side_effect=OSError(errno.EPERM, "Operation not permitted")):
            job_worker._rename_without_replace(str(self.source), str(self.destination))
            self.assertEqual(self.destination.read_text(encoding="utf-8"), "new")
            self.assertFalse(self.source.exists())
            self.source.write_text("second", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                job_worker._rename_without_replace(str(self.source), str(self.destination))
        self.assertEqual(self.destination.read_text(encoding="utf-8"), "new")

    def test_folders_fall_back_to_check_then_rename(self):
        staging = self.root / ".extract.partial"
        (staging / "inner").mkdir(parents=True)
        (staging / "inner" / "file.txt").write_text("x", encoding="utf-8")
        target = self.root / "extracted"
        _, patches = self.without_noreplace()
        with patches[0], patches[1], patches[2], \
                patch.object(job_worker.os, "link", side_effect=AssertionError("folders cannot be linked")):
            job_worker._rename_without_replace(str(staging), str(target))
            self.assertEqual((target / "inner" / "file.txt").read_text(encoding="utf-8"), "x")
            staging.mkdir()
            with self.assertRaises(FileExistsError):
                job_worker._rename_without_replace(str(staging), str(target))
        self.assertTrue(staging.is_dir())

    def test_symlink_is_published_as_a_link_not_its_target(self):
        target_file = self.root / "elsewhere.txt"
        target_file.write_text("target", encoding="utf-8")
        link = self.root / ".link.partial"
        try:
            link.symlink_to(target_file)
        except OSError as error:
            self.skipTest(f"Symlink creation unavailable: {error}")
        _, patches = self.without_noreplace()
        with patches[0], patches[1], patches[2]:
            job_worker._rename_without_replace(str(link), str(self.destination))
        self.assertTrue(self.destination.is_symlink())
        self.assertFalse(os.path.lexists(link))

    def test_cross_device_still_reaches_the_mover(self):
        _, patches = self.without_noreplace()
        with patches[0], patches[1], patches[2], \
                patch.object(job_worker.os, "link", side_effect=OSError(errno.EXDEV, "Cross-device link")):
            with self.assertRaises(OSError) as raised:
                job_worker._rename_without_replace(str(self.source), str(self.destination))
        self.assertEqual(raised.exception.errno, errno.EXDEV)
        self.assertTrue(self.source.exists())

    def test_other_renameat2_errors_are_not_masked(self):
        _, patches = self.without_noreplace(errno.EACCES)
        with patches[0], patches[1], patches[2], \
                patch.object(job_worker.os, "link", side_effect=AssertionError("must not fall back")):
            with self.assertRaises(PermissionError):
                job_worker._rename_without_replace(str(self.source), str(self.destination))
        self.assertTrue(self.source.exists())

    def test_failed_source_removal_withdraws_the_new_name(self):
        _, patches = self.without_noreplace()
        real_unlink = os.unlink

        def refuse_source(path, *arguments, **keywords):
            if str(path) == str(self.source):
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return real_unlink(path, *arguments, **keywords)

        with patches[0], patches[1], patches[2], patch.object(job_worker.os, "unlink", side_effect=refuse_source):
            with self.assertRaises(PermissionError):
                job_worker._rename_without_replace(str(self.source), str(self.destination))
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.source.read_text(encoding="utf-8"), "new")

    @unittest.skipUnless(job_worker.sys.platform.startswith("linux"), "real renameat2")
    def test_real_renameat2_still_refuses_existing_destination(self):
        self.destination.write_text("existing", encoding="utf-8")
        with patch.object(job_worker.os, "link", side_effect=AssertionError("renameat2 handles this")):
            with self.assertRaises(FileExistsError):
                job_worker._rename_without_replace(str(self.source), str(self.destination))
        self.assertEqual(self.destination.read_text(encoding="utf-8"), "existing")


if __name__ == "__main__":
    unittest.main()
