import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from nalar.apply import ApplyError, read_patch_source, read_patch_stream

ROOT = Path(__file__).resolve().parents[1]
PATCH = ("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
         "@@ -1 +1 @@\n-answer = 41\n+answer = 42\n")


class CIFlowTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(self.root), "init", "-q", "--template="],
                       check=True)
        (self.root / "app.py").write_text("answer = 41\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm", "init"],
                       check=True)

    def cli(self, *args, input_text=None):
        env = {**os.environ,
               "NALAR_GLOBAL_CONFIG": str(self.root / "none.json")}
        return subprocess.run(
            [sys.executable, "-m", "nalar", *args], cwd=ROOT, env=env,
            input=input_text, capture_output=True, text=True, timeout=60)

    def test_apply_reads_a_patch_from_stdin_without_writing(self):
        result = self.cli("apply", "-", "--check", "--repo", str(self.root),
                          input_text=PATCH)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIs(payload["checked"], True)
        self.assertIs(payload["applied"], False)
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"),
                         "answer = 41\n")

    def test_scan_reads_a_patch_from_stdin_without_echoing_values(self):
        patch = PATCH.replace('+answer = 42', '+AWS_KEY = "AKIAABCDEFGHIJKLMNOP"')
        result = self.cli("scan", "--file", "-", "--repo", str(self.root),
                          input_text=patch)
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["scope"], "file")
        self.assertEqual(payload["findings"],
                         [{"file": "app.py", "line": 1,
                           "kind": "aws_access_key"}])
        self.assertNotIn("AKIA", result.stdout)

    def test_review_file_dash_uses_the_shared_reader(self):
        self.assertEqual(read_patch_source(Path("-"), io.StringIO(PATCH)), PATCH)
        self.assertEqual(read_patch_source(Path("-"), io.BytesIO(PATCH.encode())),
                         PATCH)

    def test_patch_stream_is_bounded_and_validated(self):
        for stream in (io.StringIO(""), io.StringIO("plain text"),
                       io.BytesIO(b"\xff")):
            with self.assertRaises(ApplyError):
                read_patch_stream(stream)
        with self.assertRaises(ApplyError):
            read_patch_stream(io.StringIO(PATCH + "x" * 20), limit=10)
        with self.assertRaises(ApplyError):
            read_patch_source(Path("-"), io.StringIO("not a diff"))

    def test_missing_stdin_patch_does_not_create_files(self):
        result = self.cli("apply", "-", "--check", "--repo", str(self.root),
                          input_text="")
        self.assertEqual(result.returncode, 2)
        self.assertIn("empty", result.stderr)
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"),
                         "answer = 41\n")

    def test_apply_rejects_malformed_hunks_without_a_traceback(self):
        patch = PATCH.replace(
            "@@ -1 +1 @@",
            "@@ -0,0 +" + "9" * 5000 + " @@")
        result = self.cli("apply", "-", "--check", "--repo", str(self.root),
                          input_text=patch)
        self.assertEqual(result.returncode, 2)
        self.assertIn("malformed", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"),
                         "answer = 41\n")
