import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from nalar.apply import (MAX_RECORD_META, clear_last_apply, read_last_apply,
                         record_apply)
from nalar.coding import Repository

ROOT = Path(__file__).resolve().parents[1]


class ApplyLastTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "--template="],
                       check=True)
        (self.repo / "app.py").write_text("answer = 41\n")
        (self.repo / "other.py").write_text("value = 1\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=F", "-c",
                        "user.email=f@e.test", "commit", "-qm", "fixture"], check=True)
        repository = Repository(self.repo)
        repository.call("replace", {"path": "app.py", "before": "41", "after": "42"})
        self.patch = Path(self.enterContext(tempfile.TemporaryDirectory())) / "change.patch"
        self.patch.write_text(repository.patch(), encoding="utf-8")
        self.repository = repository

    def run_apply(self, *args, stdin=subprocess.DEVNULL):
        return subprocess.run(
            [sys.executable, "-m", "nalar", "apply", "--repo", str(self.repo),
             *args], cwd=ROOT, stdin=stdin, capture_output=True, text=True,
            timeout=30)

    def apply(self):
        result = self.run_apply(str(self.patch))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def record_paths(self):
        meta = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--git-path",
             "nalar-last-apply.json"], check=True, capture_output=True,
            text=True).stdout.strip()
        base = Path(meta)
        if not base.is_absolute():
            base = self.repo / base
        return base, base.with_name("nalar-last-apply.patch")

    def test_last_reverts_without_a_patch_file_and_clears_the_record(self):
        self.apply()
        meta, patch = self.record_paths()
        self.assertTrue(meta.is_file() and patch.is_file())
        reverted = self.run_apply("--last", "--yes")
        self.assertEqual(reverted.returncode, 0, reverted.stderr)
        payload = json.loads(reverted.stdout)
        self.assertTrue(payload["reversed"])
        self.assertTrue(payload["last"])
        self.assertEqual(payload["files"], ["app.py"])
        self.assertEqual((self.repo / "app.py").read_text(), "answer = 41\n")
        self.assertFalse(meta.exists() or patch.exists())
        again = self.run_apply("--last", "--yes")
        self.assertEqual(again.returncode, 2)
        self.assertIn("No recorded apply", again.stderr)

    def test_last_check_is_a_dry_run_and_keeps_the_record(self):
        self.apply()
        checked = self.run_apply("--last", "--check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        payload = json.loads(checked.stdout)
        self.assertTrue(payload["checked"])
        self.assertFalse(payload["reversed"])
        self.assertEqual((self.repo / "app.py").read_text(), "answer = 42\n")
        meta, patch = self.record_paths()
        self.assertTrue(meta.is_file() and patch.is_file())
        reverted = self.run_apply("--last", "--yes")
        self.assertEqual(reverted.returncode, 0, reverted.stderr)

    def test_last_requires_yes_when_stdin_is_not_a_terminal(self):
        self.apply()
        refused = self.run_apply("--last")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("--yes", refused.stderr)
        self.assertEqual((self.repo / "app.py").read_text(), "answer = 42\n")

    def test_last_refuses_when_head_moved(self):
        self.apply()
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=F", "-c",
                        "user.email=f@e.test", "commit", "-qm", "moved"], check=True)
        refused = self.run_apply("--last", "--yes")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("HEAD moved", refused.stderr)
        meta, patch = self.record_paths()
        self.assertTrue(meta.is_file() and patch.is_file())

    def test_last_refuses_unrelated_working_tree_changes(self):
        self.apply()
        (self.repo / "other.py").write_text("value = 2\n")
        refused = self.run_apply("--last", "--yes")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("other changes", refused.stderr)
        self.assertEqual((self.repo / "app.py").read_text(), "answer = 42\n")
        (self.repo / "other.py").write_text("value = 1\n")
        reverted = self.run_apply("--last", "--yes")
        self.assertEqual(reverted.returncode, 0, reverted.stderr)
        self.assertEqual((self.repo / "app.py").read_text(), "answer = 41\n")

    def test_failed_or_checked_apply_records_nothing(self):
        checked = self.run_apply("--check", str(self.patch))
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(self.run_apply("--last", "--yes").returncode, 2)
        (self.repo / "untracked.txt").write_text("local")
        failed = self.run_apply(str(self.patch))
        self.assertEqual(failed.returncode, 2)
        meta, patch = self.record_paths()
        self.assertFalse(meta.exists() or patch.exists())

    def test_last_rejects_conflicting_arguments(self):
        self.apply()
        for args in (("--last", str(self.patch)),
                     ("--last", "--include", "app.py"),
                     ("--last", "--exclude", "app.py"),
                     ("--last", "--base", "0" * 10),
                     ("--last", "--verify")):
            result = self.run_apply(*args)
            self.assertEqual(result.returncode, 2, args)

    def test_malformed_or_symlinked_record_fails_closed(self):
        self.apply()
        meta, patch = self.record_paths()
        meta.write_text("not json", encoding="utf-8")
        refused = self.run_apply("--last", "--yes")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("metadata is invalid", refused.stderr)
        meta.unlink()
        meta.symlink_to(patch)
        refused = self.run_apply("--last", "--yes")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("is a symlink", refused.stderr)

    def test_explicit_revert_clears_only_the_identical_record(self):
        self.apply()
        self.assertFalse(clear_last_apply(self.repository, "diff --git a/x b/x\n"))
        self.assertEqual(read_last_apply(self.repository)["files"], ["app.py"])
        clear_last_apply(self.repository, read_last_apply(self.repository)["patch"])
        with self.assertRaisesRegex(ValueError, "No recorded apply"):
            read_last_apply(self.repository)

    def test_record_accepts_sha256_bases_and_checks_the_file_list(self):
        self.apply()
        meta_path, _ = self.record_paths()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["base"] = "a" * 64
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        self.assertEqual(read_last_apply(self.repository)["base"], "a" * 64)
        meta["files"] = ["other.py"]
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not match"):
            read_last_apply(self.repository)

    def test_non_utf8_or_symlinked_patch_records_have_specific_messages(self):
        self.apply()
        _, patch = self.record_paths()
        patch.write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(ValueError, "not valid UTF-8"):
            read_last_apply(self.repository)
        patch.unlink()
        patch.symlink_to(self.patch)
        with self.assertRaisesRegex(ValueError, "is a symlink"):
            read_last_apply(self.repository)

    def test_last_reverts_on_a_sha256_repository(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        init = subprocess.run(
            ["git", "-C", str(root), "init", "-q", "--object-format=sha256",
             "--template="], capture_output=True, text=True)
        if init.returncode != 0:
            self.skipTest("git does not support sha256 repositories")
        (root / "app.py").write_text("answer = 41\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=F", "-c",
                        "user.email=f@e.test", "commit", "-qm", "sha256"], check=True)
        repository = Repository(root)
        self.assertEqual(len(repository.commit), 64)
        repository.call("replace", {"path": "app.py", "before": "41", "after": "42"})
        patch = Path(self.enterContext(tempfile.TemporaryDirectory())) / "change.patch"
        patch.write_text(repository.patch(), encoding="utf-8")
        applied = subprocess.run(
            [sys.executable, "-m", "nalar", "apply", "--repo", str(root),
             str(patch)], cwd=ROOT, stdin=subprocess.DEVNULL,
            capture_output=True, text=True)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        reverted = subprocess.run(
            [sys.executable, "-m", "nalar", "apply", "--repo", str(root),
             "--last", "--yes"], cwd=ROOT, stdin=subprocess.DEVNULL,
            capture_output=True, text=True)
        self.assertEqual(reverted.returncode, 0, reverted.stderr)
        self.assertTrue(json.loads(reverted.stdout)["reversed"])
        self.assertEqual((root / "app.py").read_text(encoding="utf-8"),
                         "answer = 41\n")

    def test_oversized_and_non_regular_records_are_named(self):
        self.apply()
        meta, _ = self.record_paths()
        meta.write_bytes(b"x" * (MAX_RECORD_META + 1))
        with self.assertRaisesRegex(ValueError, "metadata is too large"):
            read_last_apply(self.repository)
        meta.unlink()
        meta.mkdir()
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            read_last_apply(self.repository)

    def test_record_rejects_invalid_metadata(self):
        meta, patch = self.record_paths()
        meta.parent.mkdir(parents=True, exist_ok=True)
        patch.write_text("diff --git a/app.py b/app.py\n", encoding="utf-8")
        meta.write_text(json.dumps({"base": "0" * 40, "extra": 1}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid"):
            read_last_apply(self.repository)
        meta.write_text(json.dumps({"base": "0" * 40, "files": [1]}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid"):
            read_last_apply(self.repository)
