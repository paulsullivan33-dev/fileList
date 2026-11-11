"""run.sh and stop.sh must stop the launcher, the app and the worker together.

These tests run the real scripts against a stand-in launcher and Python, so no
Flask app, worker or configuration is involved. They cover the case that used
to leave the app and worker running: a launcher that is gone (or will not die)
when stop.sh runs.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

FAKE_PYTHON = """#!/usr/bin/env bash
case "$1" in
    tools/check_config.py) exit 0 ;;
    *) exec -a "fake-$1" sleep 300 ;;
esac
"""

STUBBORN_START = """#!/usr/bin/env bash
[[ "${1:-}" == "--check" ]] && exit 0
trap '' TERM
bash -c 'trap "" TERM; exec -a fake-app sleep 300' &
bash -c 'trap "" TERM; exec -a fake-worker sleep 300' &
wait
"""


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("setsid"),
                     "process-group scripts need Linux and setsid")
class RunScriptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        for name in ("run.sh", "stop.sh", "start"):
            shutil.copy2(REPO / name, self.folder / name)
        fake = self.folder / "fakepy"
        fake.write_text(FAKE_PYTHON, encoding="utf-8")
        fake.chmod(0o755)
        self.environment = {**os.environ, "PYTHON_BIN": str(fake)}
        self.addCleanup(self.kill_leftovers)

    def script(self, name):
        return subprocess.run(["bash", name], cwd=self.folder, env=self.environment,
                              capture_output=True, text=True, timeout=60)

    def pid(self):
        return int((self.folder / ".filelist-run.pid").read_text())

    def live_members(self, group):
        result = subprocess.run(["ps", "-o", "stat=", "-g", str(group)],
                                capture_output=True, text=True)
        return [line for line in result.stdout.split() if not line.startswith("Z")]

    def wait_for_members(self, group, count):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if len(self.live_members(group)) >= count:
                return
            time.sleep(0.1)
        self.fail(f"expected {count} live processes in group {group}")

    def kill_leftovers(self):
        pid_file = self.folder / ".filelist-run.pid"
        if pid_file.exists():
            subprocess.run(["kill", "-KILL", "--", f"-{pid_file.read_text().strip()}"],
                           capture_output=True)

    def test_launcher_app_and_worker_share_one_group_and_stop_together(self):
        self.assertEqual(self.script("run.sh").returncode, 0)
        group = self.pid()
        self.wait_for_members(group, 3)
        result = self.script("stop.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.live_members(group), [])
        self.assertFalse((self.folder / ".filelist-run.pid").exists())

    def test_app_and_worker_left_behind_by_a_dead_launcher_are_stopped(self):
        self.assertEqual(self.script("run.sh").returncode, 0)
        group = self.pid()
        self.wait_for_members(group, 3)
        os.kill(group, 9)
        time.sleep(0.3)
        self.assertEqual(len(self.live_members(group)), 2)
        # The orphaned app and worker still count as a running instance.
        self.assertIn("already running", self.script("run.sh").stdout)
        result = self.script("stop.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.live_members(group), [])

    def test_processes_that_ignore_term_are_killed_as_a_group(self):
        start = self.folder / "start"
        start.write_text(STUBBORN_START, encoding="utf-8")
        self.assertEqual(self.script("run.sh").returncode, 0)
        group = self.pid()
        self.wait_for_members(group, 3)
        result = self.script("stop.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SIGKILL", result.stdout)
        self.assertEqual(self.live_members(group), [])
        self.assertFalse((self.folder / ".filelist-run.pid").exists())


if __name__ == "__main__":
    unittest.main()
