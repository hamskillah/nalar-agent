import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nalar.coding import Repository
from nalar.preflight import PreflightError, verify_patch


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.git("init", "-q", "--template=")
        (self.root / "app.py").write_text("answer = 41\n")
        (self.root / "data.bin").write_bytes(b"\x00\x01")
        self.git("add", ".")
        self.git("-c", "user.name=F", "-c", "user.email=f@example.test", "commit", "-qm", "fixture")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True).stdout

    def patch(self):
        repo = Repository(self.root)
        repo.call("replace", {"path": "app.py", "before": "41", "after": "42"})
        return repo, repo.patch()

    def test_passing_checks_do_not_touch_the_checkout(self):
        repo, diff = self.patch()
        result = verify_patch(repo, diff, [[sys.executable, "-c", "import app; assert app.answer == 42"]])
        self.assertTrue(result["ok"])
        self.assertEqual(result["commands"][0]["exit"], 0)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")

    def test_failing_checks_are_reported_with_output(self):
        repo, diff = self.patch()
        result = verify_patch(repo, diff, [[sys.executable, "-c",
                                           "import app; assert app.answer == 99, 'wrong answer'"]])
        self.assertFalse(result["ok"])
        self.assertIn("wrong answer", result["commands"][0]["output"])

    def test_rejected_patch_is_reported(self):
        repo, diff = self.patch()
        broken = diff.replace("-answer = 41", "-missing = 1")
        result = verify_patch(repo, broken, [[sys.executable, "-c", "print('should not run')"]])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "patch_rejected")
        self.assertIn("does not apply", result["detail"])
        self.assertEqual(result["commands"], [])

    def test_binary_and_symlinks_are_not_extracted(self):
        (self.root / "link").symlink_to("app.py")
        self.git("add", "link")
        self.git("-c", "user.name=F", "-c", "user.email=f@example.test", "commit", "-qm", "link")
        repo, diff = self.patch()
        result = verify_patch(repo, diff, [[sys.executable, "-c",
                                           "import pathlib,sys; "
                                           "sys.exit(1 if pathlib.Path('link').is_symlink() else 0)"]])
        self.assertTrue(result["ok"])

    def test_executable_bits_are_preserved(self):
        (self.root / "script.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.git("add", "script.sh")
        self.git("update-index", "--chmod=+x", "script.sh")
        self.git("-c", "user.name=F", "-c", "user.email=f@example.test", "commit", "-qm", "script")
        repo, diff = self.patch()
        result = verify_patch(repo, diff, [[sys.executable, "-c",
                                           "import os,sys; sys.exit(0 if os.access('script.sh', os.X_OK) else 1)"]])
        self.assertTrue(result["ok"])

    def test_rejected_patch_includes_git_diagnostics(self):
        repo, diff = self.patch()
        result = verify_patch(repo, diff.replace("-answer = 41", "-missing = 1"),
                              [[sys.executable, "-c", "print('unused')"]])
        self.assertFalse(result["ok"])
        self.assertIn("does not apply", result["detail"])

    def test_git_based_checks_run_inside_the_export(self):
        repo, diff = self.patch()
        result = verify_patch(repo, diff, [[sys.executable, "-c",
                                           "import subprocess; "
                                           "subprocess.run(['git','status','--porcelain'], check=True)"]])
        self.assertTrue(result["ok"], result)

    def test_commands_must_be_configured_and_temporary_files_are_clean(self):
        repo, diff = self.patch()
        with self.assertRaises(PreflightError):
            verify_patch(repo, diff, [])
        private = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with patch("tempfile.tempdir", str(private)):
            verify_patch(repo, diff, [[sys.executable, "-c", "print('ok')"]])
            leftovers = list(private.glob("nalar-preflight-*"))
        self.assertEqual(leftovers, [])
