"""MKV-aware conversion: argument ladder, stream probing, route gate, worker.

These tests pin the smart conversion behaviour ported from the deployed
Vostro worker: an H.264 MKV is remuxed rather than re-encoded, other MKVs
take a mapped transcode, and AVI/MPG keep the historical single transcode.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import actions
import auth
import job_store
import job_worker
from config_loader import Directory


class ConversionArgSetsTests(unittest.TestCase):
    def arg_sets(self, source, video=None, audio=None):
        with patch.object(job_worker, "_probe_stream_codecs",
                          return_value=(video, audio)):
            return job_worker._conversion_arg_sets(source)

    def test_avi_keeps_legacy_transcode(self):
        self.assertEqual(self.arg_sets("/data/movie.avi"),
                         [["-c:v", "libx264", "-c:a", "aac"]])

    def test_mpg_keeps_legacy_transcode(self):
        self.assertEqual(self.arg_sets("/data/movie.mpg"),
                         [["-c:v", "libx264", "-c:a", "aac"]])

    def test_mkv_h264_aac_remuxes_with_transcode_fallback(self):
        sets = self.arg_sets("/data/movie.mkv", ("h264", 0), ("aac", 1))
        self.assertEqual(sets, [
            ["-map", "0:0", "-map", "0:1", "-c", "copy"],
            ["-map", "0:0", "-map", "0:1",
             "-c:v", "libx264", "-c:a", "aac"],
        ])

    def test_mkv_h264_other_audio_copies_video_transcodes_audio(self):
        sets = self.arg_sets("/data/movie.mkv", ("h264", 0), ("dts", 2))
        self.assertEqual(sets[0], ["-map", "0:0", "-map", "0:2",
                                   "-c:v", "copy", "-c:a", "aac"])
        self.assertEqual(len(sets), 2)

    def test_mkv_hevc_goes_straight_to_mapped_transcode(self):
        sets = self.arg_sets("/data/movie.mkv", ("hevc", 0), ("aac", 1))
        self.assertEqual(sets, [["-map", "0:0", "-map", "0:1",
                                 "-c:v", "libx264", "-c:a", "aac"]])

    def test_mkv_without_audio_remuxes_video_only(self):
        sets = self.arg_sets("/data/silent.mkv", ("h264", 1), None)
        self.assertEqual(sets[0], ["-map", "0:1", "-c", "copy"])

    def test_mkv_probe_failure_uses_legacy_transcode(self):
        self.assertEqual(self.arg_sets("/data/movie.mkv", None, None),
                         [["-c:v", "libx264", "-c:a", "aac"]])


class ProbeStreamCodecsTests(unittest.TestCase):
    def probe(self, streams):
        payload = json.dumps({"streams": streams})
        completed = subprocess.CompletedProcess(
            ["ffprobe"], 0, stdout=payload, stderr="")
        with patch.object(job_worker.shutil, "which",
                          return_value="/usr/bin/ffprobe"), \
                patch.object(job_worker.subprocess, "run",
                             return_value=completed):
            return job_worker._probe_stream_codecs("/data/movie.mkv")

    def test_skips_attached_picture(self):
        video, audio = self.probe([
            {"index": 0, "codec_name": "mjpeg", "codec_type": "video",
             "disposition": {"attached_pic": 1}},
            {"index": 1, "codec_name": "h264", "codec_type": "video",
             "disposition": {"attached_pic": 0}},
            {"index": 2, "codec_name": "dts", "codec_type": "audio"},
        ])
        self.assertEqual(video, ("h264", 1))
        self.assertEqual(audio, ("dts", 2))

    def test_missing_ffprobe_returns_none(self):
        with patch.object(job_worker.shutil, "which", return_value=None):
            self.assertEqual(
                job_worker._probe_stream_codecs("/data/movie.mkv"),
                (None, None))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "Requires ffmpeg and ffprobe")
    def test_probe_real_generated_mkv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sample.mkv")
            subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
                 "-i", "testsrc=duration=1:size=128x128:rate=5",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                 "-c:v", "libx264", "-c:a", "aac", "-shortest", path],
                check=True)
            video, audio = job_worker._probe_stream_codecs(path)
        self.assertEqual(video[0], "h264")
        self.assertEqual(audio[0], "aac")


class ConvertRouteGateTests(unittest.TestCase):
    """The /convert route accepts .mkv alongside the legacy formats."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, JOB_DATA_DIR=str(self.root / "jobs"),
                                 FLASK_SECRET_KEY="convert-key")
        environment.start()
        self.addCleanup(environment.stop)
        (self.root / "users.json").write_text(json.dumps([
            {"username": "alice", "password": "unused", "groups": "members"}
        ]), encoding="utf-8")
        (self.root / "clip.mkv").write_bytes(b"placeholder")
        (self.root / "clip.avi").write_bytes(b"placeholder")
        (self.root / "notes.txt").write_text("nope", encoding="utf-8")
        self.directories = [Directory(str(self.root), "/files", "Files", "members")]
        self.app = Flask(__name__, root_path=str(self.root))
        self.app.config["TESTING"] = True
        self.app.jinja_env.globals["csrf_token"] = lambda: "token"
        auth.register(self.app, self.directories)
        actions.register(self.app, self.directories)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["username"] = "alice"
            session["groups"] = ["members"]

    def post_convert(self, name):
        return self.client.post("/convert", json={
            "dir": 0, "path": "", "name": name})

    @unittest.skipUnless(os.sys.platform.startswith("linux"),
                         "Conversion route is Linux-only")
    def test_mkv_is_accepted(self):
        response = self.post_convert("clip.mkv")
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["success"])

    @unittest.skipUnless(os.sys.platform.startswith("linux"),
                         "Conversion route is Linux-only")
    def test_avi_still_accepted(self):
        response = self.post_convert("clip.avi")
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["success"])

    @unittest.skipUnless(os.sys.platform.startswith("linux"),
                         "Conversion route is Linux-only")
    def test_other_extensions_refused_with_mkv_in_message(self):
        response = self.post_convert("notes.txt")
        self.assertEqual(response.status_code, 400)
        self.assertIn("MKV", response.get_json()["error"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Requires ffmpeg and ffprobe")
class WorkerConversionTests(unittest.TestCase):
    """End to end: a queued convert job produces a valid MP4."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.directories = [Directory(str(self.media), "/media", "Media", "members", "media")]
        environment = patch.dict(os.environ, JOB_DATA_DIR=str(self.root / "jobs"))
        environment.start()
        self.addCleanup(environment.stop)

    def generate(self, name, video_codec, audio_codec):
        path = self.media / name
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
             "-i", "testsrc=duration=1:size=128x128:rate=5",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
             "-c:v", video_codec, "-c:a", audio_codec, "-shortest", str(path)],
            check=True)
        return path

    def run_conversion(self, name):
        job_store.create("convert", "tester",
                         {"directory_index": 0, "path": "", "names": [name]},
                         {"directory_index": 0, "path": "", "names": []},
                         directories=self.directories)
        with patch.object(job_worker.config_loader, "load_directories",
                          return_value=self.directories), \
                patch.object(job_worker, "current_user_groups",
                             return_value=["members"]):
            self.assertTrue(job_worker.run_once())

    def assert_mp4_codecs(self, path, video, audio):
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, check=True)
        codecs = {s["codec_type"]: s["codec_name"]
                  for s in json.loads(result.stdout)["streams"]}
        self.assertEqual(codecs.get("video"), video)
        self.assertEqual(codecs.get("audio"), audio)

    def test_h264_mkv_converts_and_completes(self):
        self.generate("movie.mkv", "libx264", "aac")
        self.run_conversion("movie.mkv")
        output = self.media / "movie.mp4"
        self.assertTrue(output.exists())
        self.assert_mp4_codecs(output, "h264", "aac")
        self.assertTrue((self.media / "movie.mkv").exists())
        self.assertEqual(list(self.media.glob("*.partial.mp4")), [])

    def test_mpeg4_mkv_transcodes_to_h264(self):
        self.generate("old.mkv", "mpeg4", "libmp3lame")
        self.run_conversion("old.mkv")
        output = self.media / "old.mp4"
        self.assertTrue(output.exists())
        self.assert_mp4_codecs(output, "h264", "aac")


if __name__ == "__main__":
    unittest.main()
