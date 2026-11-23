"""End-to-end workflow: propose, save, apply, verify and revert."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nalar.__main__ import main
from nalar.core import Call, Reply


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(self.root), "init", "-q", "--template="], check=True)
        (self.root / "app.py").write_text("answer = 41\n", encoding="utf-8")
        (self.root / "nalar.json").write_text(json.dumps({
            "model": "fixture",
            "verify": [[sys.executable, "-c", "import app; assert app.answer == 42"]],
        }), encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture"],
                       check=True)
        self.patch_file = self.outside / "change.patch"

    def chat(self, messages, tools):
        if len(messages) == 2:
            return Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                                 "after": "42"}),))
        return Reply(text="done")

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        env = {"NALAR_API_KEY": "fixture-key",
               "NALAR_GLOBAL_CONFIG": str(self.root / "missing.json")}
        with patch.dict(os.environ, env), \
                patch("nalar.__main__.ChatCompletions", return_value=self.chat), \
                patch("sys.argv", argv), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                code = main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_propose_apply_verify_and_revert(self):
        code, out, err = self.run_cli([
            "nalar", "code", "--repo", str(self.root), "--model", "fixture",
            "--verify", "--yes", "--patch-out", str(self.patch_file), "Fix the answer"])
        self.assertEqual(code, 0, err)
        self.assertIn("+answer = 42", out)
        self.assertTrue(self.patch_file.exists())
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")
        metadata = json.loads(err.strip().splitlines()[-1])
        self.assertTrue(metadata["verification"]["ok"])
        self.assertEqual(metadata["patch_path"], str(self.patch_file))

        code, out, err = self.run_cli([
            "nalar", "apply", "--repo", str(self.root), "--check", str(self.patch_file)])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["files"], ["app.py"])
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")

        code, out, err = self.run_cli([
            "nalar", "apply", "--repo", str(self.root), "--verify", "--yes",
            str(self.patch_file)])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["verification"]["ok"])
        self.assertEqual((self.root / "app.py").read_text(), "answer = 42\n")

        code, out, err = self.run_cli([
            "nalar", "apply", "--repo", str(self.root), "--revert", "--yes",
            str(self.patch_file)])
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["reversed"])
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")

    def test_a_failing_verification_stops_before_saving(self):
        (self.root / "nalar.json").write_text(json.dumps({
            "model": "fixture",
            "verify": [[sys.executable, "-c", "import app; assert app.answer == 99"]],
        }), encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "nalar.json"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.test", "commit", "-qm", "verify"],
                       check=True)
        code, out, err = self.run_cli([
            "nalar", "code", "--repo", str(self.root), "--model", "fixture",
            "--verify", "--yes", "--patch-out", str(self.patch_file), "Fix the answer"])
        self.assertEqual(code, 1)
        self.assertIn("+answer = 42", out)
        metadata = json.loads(err.strip().splitlines()[-1])
        self.assertFalse(metadata["verification"]["ok"])
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")
